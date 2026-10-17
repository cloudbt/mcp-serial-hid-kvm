"""MCP server for KVM control — thin client that delegates to KVM server.

All hardware operations (serial, capture) are delegated to the KVM server
via TCP. Image state detection uses OpenCV; visual interpretation belongs
to the calling AI model.
"""

import asyncio
import base64
import datetime
import hashlib
import io
import json
import logging
import math
import os
import re
import time
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import ImageContent, TextContent, Tool
from PIL import Image, ImageDraw, ImageFont
from serial_hid_kvm.client import KvmClient, KvmClientError
from serial_hid_kvm.hid_keycodes import (
    char_to_hid, modifier_name_to_bit, special_key_to_hid, validate_chars,
)

from .config import config
from .vision import StateEngine
from .vision.tools import state_tools
from .runtime_config import RuntimeConfig
from .file_copy import CopyManager, file_copy_tools
from .after import ACTION_TOOLS, after_schema, validate_options
from .vision.state import _roi

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global instances
_client: KvmClient | None = None
_runtime: RuntimeConfig | None = None

# A successful target/capture switch invalidates this engine and active waits.
_state = StateEngine()

# Cached target screen size (for capture/HID coordinate metadata).
_screen_size: tuple[int, int] | None = None

# Best-effort last-known target cursor position (x, y) in screen pixels.
# This stack cannot query the real OS cursor; we track coordinates we issued.
_cursor_pos: tuple[int, int] | None = None

# Best-effort hint of the last shell open_shell focused ("powershell"/"wsl").
_focused_shell_hint: str | None = None

# Active KVM target (select_target switches this; get_client uses it).
_current_target: dict = {"name": "default",
                         "host": config.kvm_host, "port": config.kvm_port}

# Input interlock (set_input_lock). When set, all HID-generating tools are
# refused — for observing production screens (e.g. PRD/F56) without any risk
# of stray keystrokes. In-memory only: an MCP server restart clears it.
_input_lock: dict | None = None
_copies = CopyManager()
_copies.interlocked = lambda: _input_lock is not None


def get_config() -> RuntimeConfig:
    global _runtime
    if _runtime is None:
        _runtime = RuntimeConfig()
        logger.info(f"Runtime config loaded from {_runtime.config_path}")
    return _runtime


def _hard_max_wait() -> float:
    """Absolute ceiling for any wrapper loop/wait, from config max_wait_seconds."""
    return float(get_config().get("max_wait_seconds"))


# ---------------------------------------------------------------------------
# Wrapper-tool helpers (pure logic, unit-testable without hardware)
# ---------------------------------------------------------------------------


def get_client() -> KvmClient:
    global _client
    if _client is None:
        _client = KvmClient(_current_target["host"], _current_target["port"])
        _client.connect()
        logger.info(f"Connected to KVM server ({_current_target['name']})")
        # Push effective HID timing so the original + wrapper tools honor config.
        _apply_hardware_timing(_client)
    return _client


def _apply_hardware_timing(client: KvmClient) -> dict | None:
    """Send config-derived HID timing (seconds) to the KVM server.

    Best-effort: older servers without set_timing simply return an error which
    we swallow. Returns the effective timing dict on success, else None.
    """
    try:
        return client.set_timing(get_config().hardware_timing_seconds())
    except KvmClientError as e:
        logger.warning(f"set_timing not applied (server may be older): {e}")
        return None


def _save_capture_log(image: Image.Image, suffix: str = "") -> str | None:
    """Save a capture image to the log directory if configured."""
    log_dir = config.capture_log_dir
    if log_dir is None:
        return None

    try:
        os.makedirs(log_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        tag = f"_{suffix}" if suffix else ""
        filename = f"{ts}{tag}.jpg"
        filepath = os.path.join(log_dir, filename)
        image.save(filepath, format="JPEG", quality=85)
        logger.info(f"Capture log saved: {filepath}")
        return filepath
    except Exception as e:
        logger.warning(f"Failed to save capture log: {e}")
        return None


def _capture_frame(quality: int = 85, *, client=None) -> tuple[bytes, Image.Image]:
    """Fetch a frame from KVM server: the original JPEG bytes and a PIL Image."""
    jpeg_bytes, w, h = (client if client is not None else get_client()).capture_frame_jpeg(quality)
    image = Image.open(io.BytesIO(jpeg_bytes))
    image.load()
    return jpeg_bytes, image


def _capture_image(quality: int = 85) -> Image.Image:
    """Fetch a frame from KVM server and return as PIL Image."""
    return _capture_frame(quality)[1]


# capture_screen view options: zoom limit and "nice" grid steps (HID pixels).
_ZOOM_MAX = 4
_GRID_STEPS = (5, 10, 20, 25, 50, 100, 200, 250, 500, 1000)
_GRID_MIN_SPACING = 80  # image pixels between grid lines


def _num(value: float) -> float | int:
    """Round metadata numbers; integral values print without a decimal point."""
    value = round(value, 4)
    return int(value) if value == int(value) else value


def _grid_font():
    try:
        return ImageFont.load_default(size=14)
    except (TypeError, OSError):  # Pillow without FreeType / old Pillow
        return ImageFont.load_default()


def _draw_grid(view: Image.Image, box: list, capture_size: tuple[int, int],
               hid_size: tuple[int, int]) -> tuple[Image.Image, int]:
    """Overlay a labelled grid on *view*; labels are HID coordinates."""
    (cw, ch), (hw, hh) = capture_size, hid_size
    sx, sy = view.width / box[2], view.height / box[3]  # image px per capture px
    px_per_hid = min(sx * cw / hw, sy * ch / hh)
    step = next((s for s in _GRID_STEPS if s * px_per_hid >= _GRID_MIN_SPACING),
                _GRID_STEPS[-1])
    overlay = Image.new("RGBA", view.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font = _grid_font()

    def label(x, y, text):
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        w, h = right - left + 4, bottom - top + 4
        x, y = min(max(0, x), view.width - w), min(max(0, y), view.height - h)
        draw.rectangle([x, y, x + w, y + h], fill=(0, 0, 0, 170))
        draw.text((x + 2 - left, y + 2 - top), text, font=font, fill=(255, 255, 0, 255))

    lines = []
    for axis, start, length, cap, hid, scale in (
            ("x", box[0], box[2], cw, hw, sx), ("y", box[1], box[3], ch, hh, sy)):
        first = math.ceil(start * hid / cap / step) * step
        last = math.floor((start + length) * hid / cap)
        for v in range(first, last + 1, step):
            lines.append((axis, v, round((v * cap / hid - start) * scale)))
    for axis, v, pos in lines:
        if axis == "x":
            draw.line([(pos, 0), (pos, view.height)], fill=(255, 0, 255, 160))
        else:
            draw.line([(0, pos), (view.width, pos)], fill=(255, 0, 255, 160))
    # Labels last so no line crosses them.
    for axis, v, pos in lines:
        if axis == "x":
            label(pos + 2, 0, str(v))
        else:
            label(0, pos + 2, str(v))
    view = Image.alpha_composite(view.convert("RGBA"), overlay).convert("RGB")
    return view, step


def _capture_view(image: Image.Image, hid_size: tuple[int, int], *, region=None,
                  zoom=None, max_width=None, grid=False, _validate_only=False):
    """Apply capture_screen's optional region/zoom/max_width/grid to a frame.

    Returns ``(view, box, grid_step)``. view is None when no option changes the
    frame, so the caller can return the KVM's JPEG without re-encoding. box is
    the clamped [x, y, w, h] in capture pixels (the full frame without region).
    """
    cw, ch = image.size
    box = [0, 0, cw, ch]
    if region is not None:
        if (not isinstance(region, (list, tuple)) or len(region) != 4
                or any(type(v) is not int for v in region)):
            raise ValueError("region must be [x, y, w, h] integers in capture pixels")
        x, y, w, h = region
        left, top = max(0, x), max(0, y)
        right, bottom = min(cw, x + w), min(ch, y + h)
        if right <= left or bottom <= top:
            raise ValueError(f"region {list(region)} is outside the {cw}x{ch} capture")
        box = [left, top, right - left, bottom - top]
    zoom = 1 if zoom is None else zoom
    if (isinstance(zoom, bool) or not isinstance(zoom, (int, float))
            or not 1 <= zoom <= _ZOOM_MAX):
        raise ValueError(f"zoom must be between 1 and {_ZOOM_MAX}")
    if zoom != 1 and region is None:
        raise ValueError("zoom requires region")
    if max_width is not None and (type(max_width) is not int or max_width < 64):
        raise ValueError("max_width must be an integer >= 64")
    if _validate_only:
        return None, box, None

    view = image
    if box != [0, 0, cw, ch]:
        view = view.crop((box[0], box[1], box[0] + box[2], box[1] + box[3]))
    size = (max(1, round(box[2] * zoom)), max(1, round(box[3] * zoom)))
    if max_width is not None and size[0] > max_width:
        size = (max_width, max(1, round(size[1] * max_width / size[0])))
    if size != view.size:
        view = view.resize(size, Image.Resampling.LANCZOS)
    step = None
    if grid:
        view, step = _draw_grid(view, box, (cw, ch), hid_size)
    return (None if view is image else view), box, step


def _capture_meta(capture_size: tuple[int, int], hid_size: tuple[int, int],
                  box: list, image_size: tuple[int, int], grid_step=None) -> dict:
    """Short capture_screen metadata: sizes, region, scale and image->HID mapping."""
    (cw, ch), (hw, hh) = capture_size, hid_size
    scale = image_size[0] / box[2]
    kx = hw / cw * box[2] / image_size[0]
    ky = hh / ch * box[3] / image_size[1]
    ox, oy = box[0] * hw / cw, box[1] * hh / ch
    meta = {
        "capture": f"{cw}x{ch}", "hid": f"{hw}x{hh}",
        "image": f"{image_size[0]}x{image_size[1]}", "region": box,
        "scale": _num(scale),
        "to_hid": (f"hid_x = {_num(ox)} + image_x * {_num(kx)}; "
                   f"hid_y = {_num(oy)} + image_y * {_num(ky)}"),
    }
    if grid_step:
        meta["grid_step_hid"] = grid_step
    return meta


def _encode_capture_jpeg(image: Image.Image) -> tuple[bytes, Image.Image]:
    """JPEG-encode a capture under the 10 MB budget; returns (bytes, final image)."""
    # Use JPEG to keep size under 20MB (base64 limit)
    quality = 85
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    # If still too large, reduce quality then resize
    while buffer.tell() > 10_000_000 and quality > 20:
        quality -= 15
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=quality)
    if buffer.tell() > 10_000_000:
        image = image.resize((image.width // 2, image.height // 2))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=60)
    return buffer.getvalue(), image


def _get_screen_size() -> tuple[int, int]:
    """Target screen size (cached) for capture/HID coordinate metadata."""
    global _screen_size
    if _screen_size is None:
        try:
            cfg = get_client().get_device_info().get("config", {})
            _screen_size = (
                int(cfg.get("screen_width", 1920)),
                int(cfg.get("screen_height", 1080)),
            )
        except Exception:
            _screen_size = (1920, 1080)
    return _screen_size


def _set_cursor(x: int, y: int) -> None:
    """Record a best-effort absolute target cursor position."""
    global _cursor_pos
    _cursor_pos = (int(x), int(y))


def _bump_cursor(dx: int, dy: int) -> None:
    """Update the best-effort cursor by a relative offset, if known."""
    global _cursor_pos
    if _cursor_pos is not None:
        _cursor_pos = (_cursor_pos[0] + int(dx), _cursor_pos[1] + int(dy))


def _serial_vid_pid(info: dict) -> str | None:
    """VID:PID (e.g. 1A86:55D3) of the active serial port from get_device_info."""
    port = str(info.get("serial", {}).get("port") or "")
    for p in info.get("ch340_ports") or []:
        if not port or str(p.get("device", "")).upper() != port.upper():
            continue
        vid, pid = str(p.get("vid", "")), str(p.get("pid", ""))
        if vid and pid:
            return f"{int(vid, 16):04X}:{int(pid, 16):04X}"
        m = re.search(r"VID:PID=([0-9A-F]{4}):([0-9A-F]{4})", str(p.get("hwid", "")), re.I)
        if m:
            return f"{m.group(1)}:{m.group(2)}".upper()
    return None


def _do_health(client: KvmClient) -> dict:
    """Compact readiness snapshot for API / serial / video plus device identity."""
    global _screen_size
    errors: list[str] = []
    api_ok = serial_ok = video_ok = False
    capture_device = None
    resolution = None
    serial_port = serial_vid_pid = hid_size = None

    try:
        client.ping()
        api_ok = True
    except Exception as e:
        errors.append(f"api: {e}")

    if api_ok:
        try:
            info = client.get_device_info()
            serial = info.get("serial", {})
            serial_ok = bool(serial.get("connected"))
            if not serial_ok and serial.get("error"):
                errors.append(f"serial: {serial['error']}")
            serial_port = serial.get("port")
            try:
                serial_vid_pid = _serial_vid_pid(info)
            except ValueError:
                pass
            cfg = info.get("config", {})
            if cfg.get("screen_width") and cfg.get("screen_height"):
                _screen_size = (int(cfg["screen_width"]), int(cfg["screen_height"]))
                hid_size = f"{_screen_size[0]}x{_screen_size[1]}"
            cap = info.get("capture", {})
            if cap and not cap.get("error"):
                capture_device = (
                    str(cap.get("device")) if cap.get("device") is not None else None
                )
                w, h = cap.get("width"), cap.get("height")
                if w and h:
                    resolution = f"{w}x{h}"
                # Config alone is not enough: actually fetch a frame so a stalled
                # capture (e.g. capture thread not streaming) is reported as a
                # failure instead of a false-positive video:true.
                try:
                    client.capture_frame_jpeg(40)
                    video_ok = True
                except Exception as e:
                    errors.append(f"video: {e}")
            elif cap.get("error"):
                errors.append(f"video: {cap['error']}")
        except Exception as e:
            errors.append(f"device_info: {e}")

    return {
        "ok": api_ok and serial_ok and video_ok,
        "api": api_ok,
        "serial": serial_ok,
        "video": video_ok,
        "capture_device": capture_device,
        "resolution": resolution,
        "target": _current_target["name"],
        "input_locked": _input_lock is not None,
        "errors": errors,
        # Device identity, so a task can confirm its binding in one call.
        "api_host": _current_target["host"],
        "api_port": int(_current_target["port"]),
        "serial_port": serial_port,
        "serial_vid_pid": serial_vid_pid,
        "hid_size": hid_size,
        "capture_size": resolution,
        "targets": {n: f"{s['host']}:{s['port']}" for n, s in config.targets.items()},
    }


# Pause between the presses of a double/triple click. With the press hold
# (click_hold_ms, capped at 100 ms) every click stays well inside the Windows
# default double-click time of 500 ms.
_MULTI_CLICK_GAP = 0.08


async def _do_multi_click(client, button: str, x, y, count: int, guard=lambda: None) -> None:
    """Press/release *count* times with short fixed gaps (double/triple click)."""
    cfg = get_config()
    hold = min(cfg.get("click_hold_ms"), 100) / 1000
    for i in range(count):
        if i:
            await asyncio.sleep(_MULTI_CLICK_GAP)
        guard()
        client.mouse_down(button, x, y)
        await asyncio.sleep(hold)
        client.mouse_up(button, x, y)
    await asyncio.sleep(min(cfg.get("click_after_ms") / 1000, _hard_max_wait()))


def _clear_input_line(client: KvmClient) -> None:
    """Clear the target's current input line before typing a fresh command.

    Uses Esc (PSReadLine RevertLine) so leftover text from a prior step does not
    concatenate with the new command. Safe at an empty prompt; does not interrupt
    a running foreground process. No-op when clear_input_before_command is false.
    Tuned for a PowerShell/PSReadLine prompt (the shell our command tools type
    into); inside raw bash, Esc is a meta prefix rather than a line clear.
    """
    if not get_config().get("clear_input_before_command"):
        return
    try:
        client.send_key("escape")
    except KvmClientError:
        pass


# ---------------------------------------------------------------------------
# V2 wrapper helpers
# ---------------------------------------------------------------------------


async def _do_open_shell(client, *, shell="powershell", distro=None, method="win_r",
                         wait_seconds=None) -> dict:
    global _focused_shell_hint
    cfg = get_config()
    distro = distro or cfg.get("default_wsl_distro")
    wait_seconds = min(float(wait_seconds if wait_seconds is not None
                             else cfg.get("open_shell_wait_seconds")), _hard_max_wait())
    if method == "win_r":
        client.send_key("r", ["win"])
        await asyncio.sleep(0.6)
        client.send_key("0x91")  # Japanese IME Off: half-width alphanumeric.
        await asyncio.sleep(0.15)
        client.type_text("powershell", raw=True)
        await asyncio.sleep(0.2)
        client.send_key("enter")
        detail = "Win+R -> powershell"
    else:  # type_command
        client.send_key("0x91")
        await asyncio.sleep(0.15)
        client.type_text("powershell", raw=True)
        await asyncio.sleep(0.1)
        client.send_key("enter")
        detail = "typed 'powershell' into current focus"
    await asyncio.sleep(wait_seconds)

    if shell == "wsl":
        client.type_text(f"wsl.exe -d {distro}", raw=True)
        await asyncio.sleep(0.1)
        client.send_key("enter")
        detail += f"; wsl.exe -d {distro}"
        await asyncio.sleep(wait_seconds)

    _focused_shell_hint = shell
    return {"ok": True, "shell": shell,
            "distro": distro if shell == "wsl" else None,
            "method": method, "verified": False, "detail": detail,
            "verification": "calling_model_required"}


def _do_configure(client, *, values=None, reset=False, persist=False) -> dict:
    cfg = get_config()
    if reset:
        cfg.reset()
    changed: dict = {}
    if values:
        try:
            changed = cfg.update(values)
        except ValueError as e:
            return {"ok": False, "error": "invalid_config", "detail": str(e)}
    _apply_hardware_timing(client)
    persisted = False
    path = cfg.config_path
    if persist:
        try:
            path = cfg.save()
            persisted = True
        except OSError as e:
            return {"ok": False, "error": "persist_failed", "detail": str(e),
                    "changed": changed}
    try:
        timing = client.get_timing()
    except KvmClientError:
        timing = cfg.hardware_timing_seconds()
    return {"ok": True, "changed": changed, "config_path": path,
            "persisted": persisted, "timing": timing}


def _do_get_timing(client, include_source: bool = True) -> dict:
    cfg = get_config()
    result: dict = {
        "timing": cfg.as_dict(),
        "config_path": cfg.config_path,
        "loaded_at": cfg.loaded_at,
        "updated_at": cfg.updated_at,
    }
    if include_source:
        result["source"] = cfg.source()
    if _screen_size is not None:
        result["screen_size"] = {"width": _screen_size[0], "height": _screen_size[1]}
    try:
        result["hardware_timing"] = client.get_timing()
    except KvmClientError:
        pass
    return result


def _do_cursor_crop(*, x=None, y=None, radius=None, draw_crosshair=True,
                    quality=85) -> tuple[bytes | None, dict]:
    cfg = get_config()
    radius = int(radius if radius is not None else cfg.get("cursor_crop_radius"))
    if x is None or y is None:
        if _cursor_pos is None:
            return None, {"ok": False, "error": "cursor_unknown",
                          "detail": "No x/y given and no tracked cursor position."}
        cx, cy = _cursor_pos
        source = "tracked_cursor"
    else:
        cx, cy = int(x), int(y)
        _set_cursor(cx, cy)
        source = "explicit"
    image = _capture_image()
    iw, ih = image.size
    left = max(0, cx - radius)
    top = max(0, cy - radius)
    right = min(iw, cx + radius)
    bottom = min(ih, cy + radius)
    if right <= left or bottom <= top:
        return None, {"ok": False, "error": "out_of_bounds",
                      "detail": f"center ({cx},{cy}) outside frame {iw}x{ih}."}
    crop = image.crop((left, top, right, bottom)).convert("RGB")
    if draw_crosshair:
        d = ImageDraw.Draw(crop)
        ccx, ccy = cx - left, cy - top
        d.line([(ccx - 10, ccy), (ccx + 10, ccy)], fill=(255, 0, 0), width=2)
        d.line([(ccx, ccy - 10), (ccx, ccy + 10)], fill=(255, 0, 0), width=2)
    buf = io.BytesIO()
    crop.save(buf, format="JPEG", quality=max(1, min(100, int(quality))))
    meta = {"ok": True, "center": {"x": cx, "y": cy}, "radius": radius,
            "box": {"left": left, "top": top, "right": right, "bottom": bottom},
            "source": source}
    return buf.getvalue(), meta


# ---------------------------------------------------------------------------
# paste_unicode_text helpers
# ---------------------------------------------------------------------------

_PASTE_CMD_MAX = 7500


def _paste_unicode_build_command(text: str) -> tuple[str, str, bytes]:
    """Build a PowerShell one-liner that decodes Base64URL text and sets the clipboard.

    Returns (command, payload, utf8_bytes).  command is pure ASCII printable.
    payload is the Base64URL string with padding stripped.
    """
    utf8_bytes = text.encode("utf-8")
    payload = base64.urlsafe_b64encode(utf8_bytes).decode("ascii").rstrip("=")
    command = (
        f"$b='{payload}';"
        "$b=$b.Replace('-','+').Replace('_','/');"
        "while($b.Length%4){$b+='='};"
        "$s=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b));"
        "Set-Clipboard -Value $s;"
        "Write-Output ('PASTE_UNICODE_OK chars='+$s.Length)"
    )
    return command, payload, utf8_bytes


def _paste_unicode_estimate_seconds(
    n_chars: int, type_key_ms: int, type_inter_key_ms: int
) -> float:
    """Estimate HID typing time in seconds for n_chars at given per-key timing."""
    return round(n_chars * (type_key_ms + type_inter_key_ms) / 1000.0, 1)


async def _do_paste_unicode_text(
    client: KvmClient,
    *,
    text: str,
    focus_shell: bool = True,
    paste_after_set: bool = False,
    restore_focus_with_alt_tab: bool = False,
    wait_seconds: float = 1.0,
    fast_timing: bool = True,
    type_key_ms: int = 5,
    type_inter_key_ms: int = 5,
    type_shift_ms: int = 0,
    max_text_chars: int = 1200,
    dry_run: bool = False,
) -> dict:
    """Encode text as Base64URL, type the PowerShell decode+Set-Clipboard command
    into the target. The caller verifies the screen and pastes separately.
    """
    if paste_after_set:
        return {"ok": False, "error": "vision_verification_required",
                "detail": "Set clipboard, inspect with capture_screen, then send Ctrl+V separately."}
    if not text:
        return {"ok": False, "error": "empty_text"}
    if len(text) > max_text_chars:
        return {"ok": False, "error": "text_too_long",
                "text_chars": len(text), "max_text_chars": max_text_chars}

    command, payload, utf8_bytes = _paste_unicode_build_command(text)

    if len(command) > _PASTE_CMD_MAX:
        return {
            "ok": False,
            "error": "command_too_long",
            "command_chars": len(command),
            "max_command_chars": _PASTE_CMD_MAX,
            "detail": "Use transfer_unicode_file for large texts.",
        }

    sha256 = hashlib.sha256(utf8_bytes).hexdigest()
    cfg = get_config()
    eff_key_ms = type_key_ms if fast_timing else cfg.get("type_key_ms")
    eff_inter_ms = type_inter_key_ms if fast_timing else cfg.get("type_inter_key_ms")
    estimated_s = _paste_unicode_estimate_seconds(len(command), eff_key_ms, eff_inter_ms)

    meta: dict = {
        "text_chars": len(text),
        "utf8_bytes": len(utf8_bytes),
        "payload_chars": len(payload),
        "command_chars": len(command),
        "sha256": sha256,
        "estimated_type_seconds": estimated_s,
        "timing_used": {
            "type_key_ms": type_key_ms,
            "type_inter_key_ms": type_inter_key_ms,
            "type_shift_ms": type_shift_ms,
        } if fast_timing else None,
        "focus_shell": focus_shell,
    }

    if dry_run:
        return {"ok": True, "dry_run": True, "set_clipboard": False, "pasted": False,
                **meta, "verified": False, "warning": None}

    if focus_shell:
        await _do_open_shell(client, shell="powershell")

    old_timing = None
    timing_warning = None
    if fast_timing:
        try:
            old_timing = client.get_timing()
            client.set_timing({
                "char_delay": type_inter_key_ms / 1000.0,
                "type_key_hold": type_key_ms / 1000.0,
                "type_shift": type_shift_ms / 1000.0,
            })
        except KvmClientError as e:
            timing_warning = f"fast_timing not applied: {e}"
            old_timing = None

    verified = False
    pasted = False
    warnings: list[str] = []
    if timing_warning:
        warnings.append(timing_warning)

    try:
        client.send_key("0x91")  # The shell may have its own IME state.
        await asyncio.sleep(0.15)
        _clear_input_line(client)
        await asyncio.sleep(0.05)
        client.type_text(command, raw=True)
        await asyncio.sleep(0.1)
        client.send_key("enter")
        await asyncio.sleep(max(0.0, min(wait_seconds, _hard_max_wait())))

    finally:
        if old_timing is not None:
            try:
                client.set_timing(old_timing)
            except KvmClientError as e:
                warnings.append(f"timing restore failed: {e}")

    return {
        "ok": True,
        "set_clipboard": None,
        "clipboard_requested": True,
        "verification": "calling_model_required",
        "pasted": pasted,
        **meta,
        "verified": verified,
        "warning": "; ".join(warnings) if warnings else None,
    }


# ---------------------------------------------------------------------------
# Input interlock (set_input_lock)
# ---------------------------------------------------------------------------

# Tools that generate HID input on the target. Everything else (capture, image state,
# health, config, target selection) stays available while locked.
INPUT_TOOLS = {
    "type_text", "send_key",
    "mouse_move", "mouse_click", "mouse_drag", "mouse_scroll",
    "open_shell", "paste_unicode_text", "transfer_unicode_file",
}


_UNLOCK_CONFIRM = "UNLOCK"


def _check_input_lock(name: str, arguments: dict) -> dict | None:
    """Return a refusal payload if *name* is an input tool and the lock is set."""
    dry = name in ("paste_unicode_text", "transfer_unicode_file") and arguments.get("dry_run")
    if _input_lock is not None and name in INPUT_TOOLS and not dry:
        return {"ok": False, "error": "input_locked", "reason": _input_lock.get("reason"),
                "since": _input_lock.get("since"), "detail": "Input interlock is enabled."}
    busy = _copies.busy(_current_target) if name in INPUT_TOOLS | {"select_target", "set_capture_device", "set_capture_resolution", "configure"} else None
    if busy:
        if name in ("paste_unicode_text", "transfer_unicode_file") and arguments.get("dry_run"):
            return None
        return {"ok": False, "error": "file_copy_owns_input", "job_id": busy,
                "detail": "Cancel, observe helper cleanup, then release the copy job."}
    if name == "advance_file_copy" and _input_lock is not None:
        return {"ok": False, "error": "input_locked"}
    if _input_lock is None or name not in INPUT_TOOLS:
        return None
    # dry_run variants produce no HID input.
    if name in ("paste_unicode_text", "transfer_unicode_file") \
            and arguments.get("dry_run"):
        return None
    return {
        "ok": False,
        "error": "input_locked",
        "reason": _input_lock.get("reason"),
        "since": _input_lock.get("since"),
        "detail": ("Input tools are locked (read-only mode). "
                   f"Call set_input_lock with locked=false and confirm='{_UNLOCK_CONFIRM}' to release."),
    }


def _do_set_input_lock(*, locked: bool, reason=None, confirm=None) -> dict:
    global _input_lock
    if locked:
        _input_lock = {
            "reason": reason or "manual lock",
            "since": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        return {"ok": True, "locked": True, **_input_lock}
    if _input_lock is not None and confirm != _UNLOCK_CONFIRM:
        return {"ok": False, "error": "confirm_required",
                "detail": f"Pass confirm='{_UNLOCK_CONFIRM}' to release the input lock."}
    _input_lock = None
    return {"ok": True, "locked": False}


# ---------------------------------------------------------------------------
# Multi-target support (select_target / list_targets)
# ---------------------------------------------------------------------------

def _do_select_target(*, name=None, host=None, port=None) -> dict:
    """Switch the active KVM server. Resets per-target caches (baseline etc.)."""
    global _client, _screen_size, _cursor_pos, _focused_shell_hint
    global _current_target
    if name:
        spec = config.targets.get(name)
        if spec is None:
            return {"ok": False, "error": "unknown_target",
                    "known": sorted(config.targets)}
        host, port = spec["host"], spec["port"]
    elif not host or not port:
        return {"ok": False, "error": "missing_target",
                "detail": "Pass name (see list_targets), or host and port."}
    else:
        name = f"{host}:{port}"
    candidate = KvmClient(host, int(port))
    try:
        candidate.ping()
    except Exception as e:
        candidate.close()
        return {"ok": False, "target": name, "host": host, "port": int(port),
                "ping": False, "error": "ping_failed", "detail": str(e),
                "current": dict(_current_target)}
    if _client is not None:
        try:
            _client.close()
        except Exception:
            pass
    _client = candidate
    # Baselines, screen size and cursor tracking belong to the old target.
    _state.reset()
    _screen_size = None
    _cursor_pos = None
    _focused_shell_hint = None
    _current_target = {"name": name, "host": host, "port": int(port)}
    result = {"ok": True, "target": name, "host": host, "port": int(port)}
    _apply_hardware_timing(candidate)
    result["ping"] = True
    return result


def _do_list_targets() -> dict:
    return {
        "targets": {n: f"{s['host']}:{s['port']}" for n, s in config.targets.items()},
        "current": dict(_current_target),
        "connected": _client is not None,
        "input_locked": _input_lock is not None,
    }


# ---------------------------------------------------------------------------
# transfer_unicode_file helpers
# ---------------------------------------------------------------------------

_TRANSFER_TMP_EXPR = "(Join-Path $env:TEMP 'kvm_xfer.b64')"
_TRANSFER_DEFAULT_CHUNK = 2500


def _ps_quote_single(s: str) -> str:
    """Escape a string for a PowerShell single-quoted literal."""
    return s.replace("'", "''")


def _transfer_build_commands(text: str, target_path: str,
                             chunk_chars: int = _TRANSFER_DEFAULT_CHUNK):
    """Build the PowerShell command sequence that writes *text* (UTF-8, no BOM)
    to *target_path* via chunked Base64URL typed into a temp file.

    Returns ``(init_cmd, chunk_cmds, final_cmd, sha256_12, payload_chars,
    utf8_bytes)``. All commands are pure ASCII. sha256_12 is the uppercase
    12-hex-char prefix that the final command echoes for verification.
    """
    utf8 = text.encode("utf-8")
    payload = base64.urlsafe_b64encode(utf8).decode("ascii").rstrip("=")
    chunk_chars = max(100, int(chunk_chars))
    chunks = [payload[i:i + chunk_chars] for i in range(0, len(payload), chunk_chars)]
    dest = _ps_quote_single(target_path)
    init_cmd = f"Set-Content -LiteralPath {_TRANSFER_TMP_EXPR} -Value '' -NoNewline"
    chunk_cmds = [
        f"Add-Content -LiteralPath {_TRANSFER_TMP_EXPR} -Value '{c}' -NoNewline"
        for c in chunks
    ]
    sha256_12 = hashlib.sha256(utf8).hexdigest()[:12].upper()
    final_cmd = (
        f"$p={_TRANSFER_TMP_EXPR};"
        "$b=(Get-Content -LiteralPath $p -Raw);"
        "$b=$b.Replace('-','+').Replace('_','/');"
        "while($b.Length%4){$b+='='};"
        "$y=[Convert]::FromBase64String($b);"
        f"[IO.File]::WriteAllBytes('{dest}',$y);"
        "Remove-Item -LiteralPath $p;"
        f"$h=(Get-FileHash -LiteralPath '{dest}' -Algorithm SHA256).Hash.Substring(0,12);"
        "Write-Output ('XF'+'ER_OK '+$h+' bytes='+$y.Length)"
    )
    return init_cmd, chunk_cmds, final_cmd, sha256_12, len(payload), len(utf8)


async def _do_transfer_unicode_file(
    client: KvmClient,
    *,
    text: str,
    target_path: str,
    chunk_chars: int = _TRANSFER_DEFAULT_CHUNK,
    focus_shell: bool = True,
    wait_seconds: float = 2.0,
    fast_timing: bool = True,
    type_key_ms: int = 5,
    type_inter_key_ms: int = 5,
    type_shift_ms: int = 0,
    max_text_chars: int = 8000,
    dry_run: bool = False,
) -> dict:
    """Write Unicode text to a file on the target via chunked Base64 typing."""
    if not text:
        return {"ok": False, "error": "empty_text"}
    if len(text) > max_text_chars:
        return {"ok": False, "error": "text_too_long",
                "text_chars": len(text), "max_text_chars": max_text_chars}
    try:
        validate_chars(target_path)
    except Exception as e:
        return {"ok": False, "error": "bad_target_path", "detail": str(e)}

    init_cmd, chunk_cmds, final_cmd, sha256_12, payload_chars, utf8_bytes = \
        _transfer_build_commands(text, target_path, chunk_chars)
    all_cmds = [init_cmd, *chunk_cmds, final_cmd]
    for cmd in all_cmds:
        validate_chars(cmd)

    cfg = get_config()
    eff_key_ms = type_key_ms if fast_timing else cfg.get("type_key_ms")
    eff_inter_ms = type_inter_key_ms if fast_timing else cfg.get("type_inter_key_ms")
    total_chars = sum(len(c) for c in all_cmds)
    estimated_s = _paste_unicode_estimate_seconds(total_chars, eff_key_ms, eff_inter_ms)

    meta: dict = {
        "text_chars": len(text),
        "utf8_bytes": utf8_bytes,
        "payload_chars": payload_chars,
        "chunks": len(chunk_cmds),
        "sha256_12": sha256_12,
        "target_path": target_path,
        "estimated_type_seconds": estimated_s,
    }

    if dry_run:
        return {"ok": True, "dry_run": True, "written": False, "verified": False,
                **meta}

    if focus_shell:
        await _do_open_shell(client, shell="powershell")

    old_timing = None
    warnings: list[str] = []
    if fast_timing:
        try:
            old_timing = client.get_timing()
            client.set_timing({
                "char_delay": type_inter_key_ms / 1000.0,
                "type_key_hold": type_key_ms / 1000.0,
                "type_shift": type_shift_ms / 1000.0,
            })
        except KvmClientError as e:
            warnings.append(f"fast_timing not applied: {e}")
            old_timing = None

    verified_marker = False
    verified_hash = False
    try:
        client.send_key("0x91")
        await asyncio.sleep(0.15)
        for i, cmd in enumerate(all_cmds):
            _clear_input_line(client)
            await asyncio.sleep(0.05)
            client.type_text(cmd, raw=True)
            await asyncio.sleep(0.1)
            client.send_key("enter")
            # Final command decodes + hashes; give it the longer wait.
            await asyncio.sleep(
                max(0.0, min(wait_seconds, _hard_max_wait()))
                if i == len(all_cmds) - 1 else 0.15)

    finally:
        if old_timing is not None:
            try:
                client.set_timing(old_timing)
            except KvmClientError as e:
                warnings.append(f"timing restore failed: {e}")

    return {
        "ok": True,
        "written": None,
        "write_requested": True,
        "verification": "calling_model_required",
        "verified": verified_hash,
        "verified_marker": verified_marker,
        **meta,
        "warning": "; ".join(warnings) if warnings else None,
    }


# Create MCP server
app = Server("mcp-serial-hid-kvm")


def _validate_action(name, arguments):
    """Validate the entire input before the first HID request (including IME off)."""
    if name == "type_text":
        if not isinstance(arguments["text"], str):
            raise ValueError("text must be a string")
        validate_chars(arguments["text"])
        for key in ("raw", "ime_off"):
            if key in arguments and type(arguments[key]) is not bool:
                raise ValueError(f"{key} must be boolean")
        delay = arguments.get("char_delay_ms")
        if delay is not None and (type(delay) is not int or delay < 0):
            raise ValueError("char_delay_ms must be a nonnegative integer")
    elif name == "send_key":
        key = arguments["key"]
        if (not isinstance(key, str) or not key or
                (special_key_to_hid(key) is None and
                 (len(key) != 1 or char_to_hid(key) is None))):
            raise ValueError(f"Unknown key: {key}")
        modifiers = arguments.get("modifiers", [])
        if (not isinstance(modifiers, list) or any(
                not isinstance(m, str) or modifier_name_to_bit(m) is None for m in modifiers)):
            raise ValueError("modifiers must contain known modifier names")
    else:
        x, y = arguments.get("x"), arguments.get("y")
        if (x is None) != (y is None):
            raise ValueError(f"{name} needs both x and y, or neither")
        if x is not None and (type(x) is not int or type(y) is not int or x < 0 or y < 0):
            raise ValueError("x and y must be nonnegative HID coordinates")
        if name == "mouse_click":
            count = arguments.get("count", 1)
            if type(count) is not int or not 1 <= count <= 3:
                raise ValueError("count must be 1, 2 or 3")
            if arguments.get("button", "left") not in ("left", "right", "middle"):
                raise ValueError("button must be left, right or middle")
        elif type(arguments["amount"]) is not int or not -127 <= arguments["amount"] <= 127:
            raise ValueError("amount must be an integer between -127 and 127")


async def _send_action(client, name, arguments, guard=lambda: None):
    """Send once. A response acknowledges HID delivery, not business success."""
    guard()
    if name == "type_text":
        text = arguments["text"]
        if arguments.get("ime_off", False):
            client.send_key("0x91")
            await asyncio.sleep(0.15)
            guard()
        client.type_text(text, arguments.get("char_delay_ms"), raw=arguments.get("raw", False))
        return f"Typed {len(text)} characters"
    if name == "send_key":
        key, modifiers = arguments["key"], arguments.get("modifiers", [])
        client.send_key(key, modifiers)
        return f"Sent: {'+'.join(modifiers) + '+' if modifiers else ''}{key}"
    x, y = arguments.get("x"), arguments.get("y")
    pos = f" at ({x}, {y})" if x is not None else ""
    if name == "mouse_click":
        button, count = arguments.get("button", "left"), arguments.get("count", 1)
        if count == 1:
            client.mouse_click(button, x, y)
        else:
            await _do_multi_click(client, button, x, y, count, guard)
            guard()
        if x is not None:
            _set_cursor(x, y)
        verb = {1: "Clicked", 2: "Double-clicked", 3: "Triple-clicked"}[count]
        return f"{verb} {button}{pos}"
    if x is not None:
        client.mouse_move(x, y)
        _set_cursor(x, y)
        await asyncio.sleep(0.05)
        guard()
    amount = arguments["amount"]
    client.mouse_scroll(amount)
    return f"Scrolled {'up' if amount > 0 else 'down'} by {abs(amount)}{pos}"


def _stable_options(arguments):
    cfg = get_config()
    kwargs = {
        "timeout_seconds": arguments.get("timeout_seconds", cfg.get("wait_timeout_seconds")),
        "poll_ms": arguments.get("poll_ms", cfg.get("wait_poll_ms")),
        "threshold": arguments.get("threshold", cfg.get("screen_stable_threshold")),
        "stable_frames": arguments.get("stable_frames", cfg.get("screen_stable_frames")),
        "region": arguments.get("region"),
        "min_changed_area": arguments.get("min_changed_area"),
    }
    StateEngine._wait_args(kwargs["timeout_seconds"], kwargs["poll_ms"], kwargs["threshold"])
    value = kwargs["min_changed_area"]
    if value is not None and (isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 1):
        raise ValueError("min_changed_area must be between 0 and 1")
    if type(kwargs["stable_frames"]) is not int or not 1 <= kwargs["stable_frames"] <= 1000:
        raise ValueError("stable_frames must be an integer between 1 and 1000")
    kwargs["timeout_seconds"] = min(kwargs["timeout_seconds"], _hard_max_wait())
    return kwargs


async def _action_after(client, name, arguments, schema, start):
    after = arguments["after"]
    validate_options(after, schema)
    wait = after.get("wait", {})
    mode = wait.get("mode", "stable")
    stable = _stable_options(wait) if mode == "stable" else None
    seconds = wait.get("seconds", 0)
    if not math.isfinite(seconds):
        raise ValueError("after.wait.seconds must be finite")
    screenshot = after.get("screenshot", {})
    park = after.get("park_cursor")
    hid_size = _get_screen_size()
    for position in (park, arguments if arguments.get("x") is not None else None):
        if position and (not 0 <= position["x"] < hid_size[0] or not 0 <= position["y"] < hid_size[1]):
            raise ValueError("cursor position must be inside the HID screen")
    # Frame-dependent ROI validation is read-only and happens BEFORE input.
    probe = None
    if screenshot.get("region") is not None or (stable and stable["region"] is not None):
        probe = _capture_frame(client=client)[1]
    _capture_view(probe if probe is not None else Image.new("RGB", (1, 1)), hid_size,
                  _validate_only=True, **screenshot)
    if stable and stable["region"] is not None:
        _roi(stable["region"], probe.width, probe.height)

    generation = _state.generation
    target = dict(_current_target)
    result = {"ok": False, "tool": name, "target": target, "verified": False,
              "verification": "calling_model_required",
              "preparation_ms": round((time.monotonic() - start) * 1000, 3),
              "action": {"status": "not_sent", "sent": False, "elapsed_ms": 0},
              "park_cursor": {"status": "skipped", "elapsed_ms": 0},
              "wait": {"status": "skipped", "mode": mode, "elapsed_ms": 0},
              "screenshot": {"status": "skipped", "elapsed_ms": 0}}
    images = []

    def check_context():
        if generation != _state.generation or client is not _client:
            raise ValueError("context_changed")

    def check_input():
        check_context()
        refusal = _check_input_lock(name, arguments)
        if refusal:
            raise ValueError(refusal["error"])

    async def stage(key, operation):
        began = time.monotonic()
        try:
            value = await operation()
            result[key].update({"status": "ok", **(value or {})})
        except Exception as exc:
            # The action distinguishes a pre-send refusal from unknown delivery.
            if key != "action":
                result[key]["status"] = "failed"
            result[key]["error"] = str(exc)
        finally:
            result[key]["elapsed_ms"] = round((time.monotonic() - began) * 1000, 3)

    async def action():
        def attempting_input():
            check_input()
            # Once attempted, a transport error may mean partial delivery.
            result["action"].update(status="unknown", sent=None)

        message = await _send_action(client, name, arguments, attempting_input)
        return {"sent": True, "message": message}

    await stage("action", action)
    if result["action"]["status"] == "ok":
        if park:
            async def parking():
                check_input()
                client.mouse_move(park["x"], park["y"])
                _set_cursor(park["x"], park["y"])
                return {"x": park["x"], "y": park["y"]}
            await stage("park_cursor", parking)
        last_frame = None

        def capture():
            nonlocal last_frame
            check_context()
            last_frame = _capture_frame(client=client)
            return last_frame[1]

        async def waiting():
            check_context()
            if mode == "stable":
                observed = await _state.wait_for_stable(capture, **stable)
                check_context()
                return {**observed, "status": "failed" if observed.get("error") else
                        "timeout" if observed.get("timed_out") else "ok"}
            await asyncio.sleep(min(seconds, _hard_max_wait()))
            check_context()

        if mode != "none":
            await stage("wait", waiting)
            if result["wait"]["status"] == "failed":
                last_frame = None  # try a current capture after a polling failure

        async def snapshot():
            check_context()
            reused = last_frame is not None
            jpeg_bytes, image = last_frame if reused else _capture_frame(client=client)
            view, box, step = _capture_view(image, hid_size, **screenshot)
            if view is None and len(jpeg_bytes) <= 10_000_000:
                data, final = jpeg_bytes, image
            else:
                data, final = _encode_capture_jpeg(view if view is not None else image)
            # Baseline always keeps the untransformed full frame, with no disk save.
            _state.set_baseline(image)
            _copies.observe({"host": target["host"], "port": int(target["port"])})
            images.append(ImageContent(type="image", data=base64.standard_b64encode(data).decode("utf-8"), mimeType="image/jpeg"))
            return {"source": "wait_last_frame" if reused else "capture",
                    **_capture_meta(image.size, hid_size, box, final.size, step)}

        await stage("screenshot", snapshot)
    result["ok"] = all(result[key]["status"] in ("ok", "skipped")
                       for key in ("action", "park_cursor", "wait", "screenshot"))
    result["elapsed_ms"] = round((time.monotonic() - start) * 1000, 3)
    return [*images, TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


async def _all_tools() -> list[Tool]:
    """All callable definitions, including tools hidden from list_tools."""
    tools = [
        Tool(
            name="type_text",
            description=(
                "Type ASCII text on the target. Inline {tags} press keys: {enter}, {tab}, {esc}, "
                "{ctrl+c}, {alt+f4}, {0x87} (raw HID keycode 0x00-0xFF). Only known key names are tags; "
                "other braces (awk '{print $1}') are typed as-is. {{ and }} type a literal brace "
                "({{enter}} types \"{enter}\"). Line breaks press Enter (JSON \\n = Enter, \\\\n = "
                "backslash+n). Only ASCII printable, tab and newline; anything else is an error "
                "(use paste_unicode_text for Unicode)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text with optional {tags}. Keys: enter, space, tab, backspace, delete, insert, esc, up, down, left, right, home, end, pageup, pagedown, f1-f12, capslock, numlock, scrolllock, printscreen, pause, 0xNN. Modifiers ctrl/shift/alt/win joined with +: {ctrl+shift+esc}, {shift+0x87}.",
                    },
                    "raw": {
                        "type": "boolean",
                        "description": "Disable all {tag} parsing; only line breaks act as Enter. Default false.",
                    },
                    "ime_off": {
                        "type": "boolean",
                        "description": "First press HID 0x91 (Japanese IME off: half-width alphanumeric), as open_shell does. Default false.",
                    },
                    "char_delay_ms": {
                        "type": "integer",
                        "description": "Delay between characters in ms (default 20).",
                    },
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="send_key",
            description="Send a single key press with optional modifier keys (e.g., Ctrl+C, Alt+F4).",
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Key name: a-z, 0-9, enter, tab, escape, backspace, delete, up, down, left, right, home, end, pageup, pagedown, f1-f12, space, insert, printscreen",
                    },
                    "modifiers": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Modifier keys: ctrl, shift, alt, win (gui/super/meta)",
                    },
                },
                "required": ["key"],
            },
        ),
        Tool(
            name="mouse_move",
            description="Move the mouse cursor on the target PC.",
            inputSchema={
                "type": "object",
                "properties": {
                    "x": {
                        "type": "integer",
                        "description": "X coordinate (screen pixels for absolute, offset for relative)",
                    },
                    "y": {
                        "type": "integer",
                        "description": "Y coordinate (screen pixels for absolute, offset for relative)",
                    },
                    "relative": {
                        "type": "boolean",
                        "description": "If true, move relative to current position (default: false)",
                    },
                },
                "required": ["x", "y"],
            },
        ),
        Tool(
            name="mouse_click",
            description="Click a mouse button on the target PC, optionally at a specific position. count=2/3 double/triple-clicks within the Windows double-click time.",
            inputSchema={
                "type": "object",
                "properties": {
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                        "description": "Mouse button (default: left)",
                    },
                    "x": {
                        "type": "integer",
                        "description": "Optional X screen coordinate to click at",
                    },
                    "y": {
                        "type": "integer",
                        "description": "Optional Y screen coordinate to click at",
                    },
                    "count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 3,
                        "description": "Clicks: 1 (default), 2 = double-click, 3 = triple-click.",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="mouse_drag",
            description="Press at start, move to end, release (drag-and-drop, text selection, resizing).",
            inputSchema={
                "type": "object",
                "properties": {
                    "start_x": {
                        "type": "integer",
                        "description": "Starting X screen coordinate",
                    },
                    "start_y": {
                        "type": "integer",
                        "description": "Starting Y screen coordinate",
                    },
                    "end_x": {
                        "type": "integer",
                        "description": "Ending X screen coordinate",
                    },
                    "end_y": {
                        "type": "integer",
                        "description": "Ending Y screen coordinate",
                    },
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                        "description": "Mouse button (default: left)",
                    },
                    "steps": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "Moves from start to end (default 1). Use 5-20 for apps that need intermediate motion events.",
                    },
                },
                "required": ["start_x", "start_y", "end_x", "end_y"],
            },
        ),
        Tool(
            name="mouse_scroll",
            description="Scroll the mouse wheel on the target PC, optionally after moving to x/y.",
            inputSchema={
                "type": "object",
                "properties": {
                    "amount": {
                        "type": "integer",
                        "description": "Scroll amount: positive=up, negative=down (-127 to 127)",
                    },
                    "x": {
                        "type": "integer",
                        "description": "Optional X screen coordinate to scroll at (pass with y)",
                    },
                    "y": {
                        "type": "integer",
                        "description": "Optional Y screen coordinate to scroll at (pass with x)",
                    },
                },
                "required": ["amount"],
            },
        ),
        Tool(
            name="capture_screen",
            description=(
                "Capture the target screen as JPEG, followed by a short JSON text: sizes, region, "
                "scale and the image->HID formula. Always stores the full frame as the change baseline. "
                "Options: region to crop, zoom to enlarge it, max_width to shrink, grid for "
                "HID-coordinate labels usable by mouse_click."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "region": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "minItems": 4,
                        "maxItems": 4,
                        "description": "[x, y, w, h] in capture pixels; return only this area.",
                    },
                    "zoom": {
                        "type": "number",
                        "minimum": 1,
                        "maximum": _ZOOM_MAX,
                        "description": "Enlarge the region 1-4x (default 1). Requires region.",
                    },
                    "max_width": {
                        "type": "integer",
                        "minimum": 64,
                        "description": "Shrink the returned image to at most this width (saves tokens).",
                    },
                    "grid": {
                        "type": "boolean",
                        "description": "Overlay a labelled grid in HID coordinates (default false).",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="get_device_info",
            description="Show connection status and device information for the serial adapter and HDMI capture device.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="list_capture_devices",
            description="List all available video capture devices with their index and name. Use this to find the correct HDMI capture device.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="set_capture_resolution",
            description="Change the HDMI capture resolution. Common values: 1920x1080, 1280x720, 640x480. The actual resolution depends on what the capture device supports.",
            inputSchema={
                "type": "object",
                "properties": {
                    "width": {
                        "type": "integer",
                        "description": "Capture width in pixels (e.g. 1920)",
                    },
                    "height": {
                        "type": "integer",
                        "description": "Capture height in pixels (e.g. 1080)",
                    },
                },
                "required": ["width", "height"],
            },
        ),
        Tool(
            name="set_capture_device",
            description="Switch the active capture device by index or path. Use list_capture_devices first to see available options. Reopens the capture device.",
            inputSchema={
                "type": "object",
                "properties": {
                    "device": {
                        "type": "string",
                        "description": "Device index (e.g. '0', '1') or path (e.g. '/dev/video0')",
                    },
                },
                "required": ["device"],
            },
        ),
        # -------------------------------------------------------------------
        # Token-efficient wrapper tools (Route A MVP). These layer on top of
        # the same KVM client and return compact JSON (no images).
        # -------------------------------------------------------------------
        Tool(
            name="health",
            description=(
                "Compact readiness check (API, serial, video) plus device identity: target, API host/port, "
                "serial port and VID:PID, HID and capture size, configured targets. Small JSON, never an image."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        # -------------------------------------------------------------------
        # V2 wrapper tools: progressive verification, runtime config, shell
        # orchestration. All compact JSON except cursor_crop (image-oriented).
        # -------------------------------------------------------------------
        Tool(
            name="open_shell",
            description="Open a TARGET shell via HID. Launch only: verified=false; the calling model must inspect capture_screen before typing commands.",
            inputSchema={
                "type": "object",
                "properties": {
                    "shell": {
                        "type": "string",
                        "enum": ["powershell", "wsl"],
                        "description": "Which shell to open (default powershell).",
                    },
                    "distro": {
                        "type": "string",
                        "description": "WSL distro for shell=wsl (default from config default_wsl_distro).",
                    },
                    "method": {
                        "type": "string",
                        "enum": ["win_r", "type_command"],
                        "description": "win_r: Win+R run dialog. type_command: type launch command into current focus (default win_r).",
                    },
                    "wait_seconds": {
                        "type": "number",
                        "description": "Seconds to wait for the shell to appear (default from config open_shell_wait_seconds).",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="cursor_crop",
            description="Small image crop with a crosshair around x/y or the tracked cursor. Without x/y it needs a prior mouse_move/mouse_click (the real OS cursor cannot be read), else error cursor_unknown.",
            inputSchema={
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "Center X (screen px). Omit to use tracked cursor."},
                    "y": {"type": "integer", "description": "Center Y (screen px). Omit to use tracked cursor."},
                    "radius": {
                        "type": "integer",
                        "description": "Half-size of the crop box in px (default from config cursor_crop_radius).",
                    },
                    "draw_crosshair": {
                        "type": "boolean",
                        "description": "Draw a crosshair at the center (default true).",
                    },
                    "quality": {
                        "type": "integer",
                        "description": "JPEG quality 1-100 (default 85).",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="configure",
            description="Tune runtime timing/operational config without restarting. Applies HID timing to the KVM server live. Returns the effective timing.",
            inputSchema={
                "type": "object",
                "properties": {
                    "values": {
                        "type": "object",
                        "description": "Map of config keys to new values (see get_timing for keys).",
                    },
                    "reset": {
                        "type": "boolean",
                        "description": "Reset in-memory config to defaults before applying values (default false).",
                    },
                    "persist": {
                        "type": "boolean",
                        "description": "Write the effective config to the runtime config file (default false).",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="get_timing",
            description="Return the in-memory effective runtime config (timing + operational defaults) and its source. Does not capture the screen.",
            inputSchema={
                "type": "object",
                "properties": {
                    "include_source": {
                        "type": "boolean",
                        "description": "Include load-source breakdown (default true).",
                    },
                },
                "required": [],
            },
        ),
        # -------------------------------------------------------------------
        # Unicode / clipboard transfer
        # -------------------------------------------------------------------
        Tool(
            name="paste_unicode_text",
            description=(
                "Put Unicode text (Japanese, Chinese, ...) on the Target clipboard by typing a Base64 "
                "PowerShell command. Inspect the result, then send Ctrl+V separately. For files or "
                ">1000 chars use transfer_unicode_file."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Unicode text for the Target clipboard (newlines OK).",
                    },
                    "focus_shell": {
                        "type": "boolean",
                        "description": "Open a Target PowerShell via Win+R first (default true).",
                    },
                    "wait_seconds": {
                        "type": "number",
                        "description": "Wait after the clipboard command (default 1.0).",
                    },
                    "fast_timing": {
                        "type": "boolean",
                        "description": "Temporarily speed up HID typing, then restore (default true).",
                    },
                    "type_key_ms": {
                        "type": "integer",
                        "description": "Key hold ms with fast_timing (default 5).",
                    },
                    "type_inter_key_ms": {
                        "type": "integer",
                        "description": "Inter-key delay ms with fast_timing (default 5).",
                    },
                    "type_shift_ms": {
                        "type": "integer",
                        "description": "Modifier delay ms with fast_timing (default 0).",
                    },
                    "max_text_chars": {
                        "type": "integer",
                        "description": "Safety cap on characters (default 1200).",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "Only return sizes and time estimate; no input (default false).",
                    },
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="transfer_unicode_file",
            description=(
                "Write Unicode text to a FILE on the Target as exact UTF-8 (no BOM) via chunked Base64 "
                "typed into PowerShell; the Target echoes a SHA-256 prefix to check on screen. "
                "Call with dry_run=true first for the time estimate."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "File content (Unicode OK), written as UTF-8 without BOM.",
                    },
                    "target_path": {
                        "type": "string",
                        "description": "Absolute ASCII path on the target, e.g. C:\\Temp\\notes.md. Environment variables are NOT expanded.",
                    },
                    "chunk_chars": {
                        "type": "integer",
                        "description": "Base64 chars per chunk (default 2500, min 100).",
                    },
                    "focus_shell": {
                        "type": "boolean",
                        "description": "Open a Target PowerShell via Win+R first (default true).",
                    },
                    "wait_seconds": {
                        "type": "number",
                        "description": "Wait after the final decode command (default 2.0).",
                    },
                    "fast_timing": {
                        "type": "boolean",
                        "description": "Temporarily speed up HID typing, then restore (default true).",
                    },
                    "type_key_ms": {
                        "type": "integer",
                        "description": "Key hold ms with fast_timing (default 5).",
                    },
                    "type_inter_key_ms": {
                        "type": "integer",
                        "description": "Inter-key delay ms with fast_timing (default 5).",
                    },
                    "type_shift_ms": {
                        "type": "integer",
                        "description": "Modifier delay ms with fast_timing (default 0).",
                    },
                    "max_text_chars": {
                        "type": "integer",
                        "description": "Safety cap on characters (default 8000; takes minutes over HID).",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "Only return sizes, chunk count and time estimate; no input (default false).",
                    },
                },
                "required": ["text", "target_path"],
            },
        ),
        # -------------------------------------------------------------------
        # Ops tools: multi-target,
        # production interlock.
        # -------------------------------------------------------------------
        Tool(
            name="list_targets",
            description="List the configured KVM targets (from SHKVM_TARGETS) and which one is currently active. No hardware access.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="select_target",
            description=(
                "Switch the active KVM server (one serial-hid-kvm per target PC) by configured name "
                "(see list_targets) or host+port. Pings first; resets screen baseline and cursor tracking."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Configured target name from SHKVM_TARGETS (e.g. target1).",
                    },
                    "host": {
                        "type": "string",
                        "description": "KVM server host (alternative to name).",
                    },
                    "port": {
                        "type": "integer",
                        "description": "KVM server port (alternative to name).",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="set_input_lock",
            description=(
                "Production interlock: when locked, every HID input tool (keyboard, mouse, shell, paste/transfer) "
                "is refused with error=input_locked; capture, image-state and health stay available. "
                "Unlocking requires confirm='UNLOCK'. In-memory only (cleared on MCP restart)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "locked": {
                        "type": "boolean",
                        "description": "true to lock input tools, false to unlock (needs confirm).",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why the lock is set, e.g. 'observing production'. Echoed in refusals.",
                    },
                    "confirm": {
                        "type": "string",
                        "description": "Must be exactly 'UNLOCK' when unlocking.",
                    },
                },
                "required": ["locked"],
            },
        ),
    ]
    tools.extend(state_tools())
    tools.extend(file_copy_tools())
    definitions = {t.name: t for t in tools}
    schema = after_schema(definitions["capture_screen"].inputSchema,
                          definitions["wait_for_stable"].inputSchema)
    for name in ACTION_TOOLS:
        definitions[name].inputSchema["properties"]["after"] = schema
    return tools


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [t for t in await _all_tools() if t.name not in config.hidden_tools]


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent | ImageContent]:
    """Handle tool calls."""
    started = time.monotonic()
    try:
        # Production interlock: refuse HID-generating tools while locked.
        lock_error = _check_input_lock(name, arguments or {})
        if lock_error is not None:
            return [TextContent(type="text", text=json.dumps(lock_error, ensure_ascii=False))]

        # Tools that must not depend on a (possibly dead) connection to the
        # currently selected target.
        if name == "set_input_lock":
            result = _do_set_input_lock(
                locked=bool(arguments["locked"]),
                reason=arguments.get("reason"),
                confirm=arguments.get("confirm"))
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        elif name == "list_targets":
            result = _do_list_targets()
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        elif name == "get_file_copy_status":
            return [TextContent(type="text", text=json.dumps(_copies.status(arguments["job_id"]), ensure_ascii=False))]
        elif name == "cancel_file_copy":
            return [TextContent(type="text", text=json.dumps(_copies.cancel(arguments["job_id"]), ensure_ascii=False))]
        elif name == "copy_file_from_target":
            result = _copies.prepare(get_client(), _current_target, **arguments)
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
        elif name == "advance_file_copy":
            result = _copies.advance(get_client(), _current_target, **arguments)
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        elif name == "select_target":
            result = _do_select_target(
                name=arguments.get("name"),
                host=arguments.get("host"),
                port=arguments.get("port"))
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        # Reject removed/unknown tools before opening any hardware connection.
        definitions = {tool.name: tool for tool in await _all_tools()}
        if name not in definitions:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]
        client = get_client()

        if name in ACTION_TOOLS:
            _validate_action(name, arguments)
            if "after" in arguments:
                schema = definitions[name].inputSchema["properties"]["after"]
                return await _action_after(client, name, arguments, schema, started)
            message = await _send_action(client, name, arguments)
            return [TextContent(type="text", text=message)]

        elif name == "mouse_move":
            x = arguments["x"]
            y = arguments["y"]
            relative = arguments.get("relative", False)
            client.mouse_move(x, y, relative)
            if relative:
                _bump_cursor(x, y)
                return [TextContent(type="text", text=f"Moved mouse by ({x}, {y})")]
            else:
                _set_cursor(x, y)
                return [TextContent(type="text", text=f"Moved mouse to ({x}, {y})")]

        elif name == "mouse_drag":
            start_x = arguments["start_x"]
            start_y = arguments["start_y"]
            end_x = arguments["end_x"]
            end_y = arguments["end_y"]
            button = arguments.get("button", "left")
            steps = arguments.get("steps", 1)
            if type(steps) is not int or not 1 <= steps <= 50:
                raise ValueError("steps must be an integer between 1 and 50")
            client.mouse_down(button, start_x, start_y)
            await asyncio.sleep(0.05)
            for i in range(1, steps + 1):
                client.mouse_move(round(start_x + (end_x - start_x) * i / steps),
                                  round(start_y + (end_y - start_y) * i / steps))
                await asyncio.sleep(0.05)
            client.mouse_up(button, end_x, end_y)
            _set_cursor(end_x, end_y)
            return [TextContent(
                type="text",
                text=f"Dragged {button} from ({start_x}, {start_y}) to ({end_x}, {end_y})",
            )]

        elif name == "capture_screen":
            jpeg_bytes, image = _capture_frame()
            # Validate options before touching the baseline.
            view, box, grid_step = _capture_view(
                image, _get_screen_size(), region=arguments.get("region"),
                zoom=arguments.get("zoom"), max_width=arguments.get("max_width"),
                grid=bool(arguments.get("grid", False)))
            _copies.observe({"host": _current_target["host"], "port": int(_current_target["port"])})
            _state.set_baseline(image)  # always the full frame
            _save_capture_log(image, "capture")
            if view is None and len(jpeg_bytes) <= 10_000_000:
                # Untransformed: return the KVM's JPEG without a second compression.
                data, final = jpeg_bytes, image
            else:
                data, final = _encode_capture_jpeg(view if view is not None else image)
            meta = _capture_meta(image.size, _get_screen_size(), box, final.size, grid_step)
            return [
                ImageContent(
                    type="image",
                    data=base64.standard_b64encode(data).decode("utf-8"),
                    mimeType="image/jpeg",
                ),
                TextContent(type="text", text=json.dumps(meta)),
            ]


        elif name == "get_device_info":
            info = client.get_device_info()
            return [TextContent(
                type="text",
                text=json.dumps(info, indent=2, ensure_ascii=False),
            )]

        elif name == "set_capture_resolution":
            width = arguments["width"]
            height = arguments["height"]
            result = client.set_capture_resolution(width, height)
            _state.reset()
            cap_info = result.get("info", {})
            return [TextContent(
                type="text",
                text=f"Resolution set: {cap_info.get('width')}x{cap_info.get('height')} (requested {width}x{height})",
            )]

        elif name == "list_capture_devices":
            result = client.list_capture_devices()
            devices = result.get("devices", [])
            if not devices:
                return [TextContent(type="text", text="No capture devices found.")]
            return [TextContent(
                type="text",
                text=json.dumps(devices, indent=2, ensure_ascii=False),
            )]

        elif name == "set_capture_device":
            device = arguments["device"]
            result = client.set_capture_device(device)
            _state.reset()
            cap_info = result.get("info", {})
            return [TextContent(
                type="text",
                text=f"Switched to device {device}: {cap_info.get('width')}x{cap_info.get('height')} ({cap_info.get('backend')})",
            )]

        elif name == "health":
            result = _do_health(client)
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        elif name == "set_screen_baseline":
            result = _state.set_baseline(_capture_image(), arguments.get("region"))
            result["timestamp"] = datetime.datetime.now().isoformat(timespec="seconds")
            return [TextContent(type="text", text=json.dumps(result))]

        elif name == "get_changed_regions":
            result = _state.observe(
                _capture_image(), threshold=arguments.get("threshold", get_config().get("screen_change_threshold")),
                region=arguments.get("region"), min_changed_area=arguments.get("min_changed_area"),
                auto_baseline=arguments.get("auto_baseline", False))
            return [TextContent(type="text", text=json.dumps(result))]

        elif name in ("wait_for_change", "wait_for_stable"):
            if name == "wait_for_stable":
                result = await _state.wait_for_stable(_capture_image, **_stable_options(arguments))
            else:
                cfg = get_config()
                kwargs = {
                    "timeout_seconds": arguments.get("timeout_seconds", cfg.get("wait_timeout_seconds")),
                    "poll_ms": arguments.get("poll_ms", cfg.get("wait_poll_ms")),
                    "threshold": arguments.get("threshold", cfg.get("screen_change_threshold")),
                    "region": arguments.get("region"),
                    "min_changed_area": arguments.get("min_changed_area"),
                    "auto_baseline": arguments.get("auto_baseline", True),
                    "update_baseline_on_change": arguments.get("update_baseline_on_change", False),
                }
                StateEngine._wait_args(kwargs["timeout_seconds"], kwargs["poll_ms"], kwargs["threshold"])
                kwargs["timeout_seconds"] = min(kwargs["timeout_seconds"], _hard_max_wait())
                result = await _state.wait_for_change(_capture_image, **kwargs)
            return [TextContent(type="text", text=json.dumps(result))]

        elif name == "open_shell":
            result = await _do_open_shell(
                client, shell=arguments.get("shell", "powershell"),
                distro=arguments.get("distro"), method=arguments.get("method", "win_r"),
                wait_seconds=arguments.get("wait_seconds"))
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


        elif name == "cursor_crop":
            img_bytes, meta = _do_cursor_crop(
                x=arguments.get("x"), y=arguments.get("y"),
                radius=arguments.get("radius"),
                draw_crosshair=arguments.get("draw_crosshair", True),
                quality=arguments.get("quality", 85))
            if img_bytes is None:
                return [TextContent(type="text", text=json.dumps(meta, ensure_ascii=False))]
            b64 = base64.standard_b64encode(img_bytes).decode("utf-8")
            return [
                TextContent(type="text", text=json.dumps(meta, ensure_ascii=False)),
                ImageContent(type="image", data=b64, mimeType="image/jpeg"),
            ]


        elif name == "configure":
            args = dict(arguments)
            reset = bool(args.pop("reset", False))
            persist = bool(args.pop("persist", False))
            values = dict(args.pop("values", None) or {})
            # Accept flat keys for compatibility.
            for key in list(args.keys()):
                values[key] = args.pop(key)
            result = _do_configure(client, values=values, reset=reset, persist=persist)
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        elif name == "get_timing":
            result = _do_get_timing(client, include_source=arguments.get("include_source", True))
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        elif name == "paste_unicode_text":
            result = await _do_paste_unicode_text(
                client,
                text=arguments["text"],
                focus_shell=arguments.get("focus_shell", True),
                paste_after_set=arguments.get("paste_after_set", False),
                restore_focus_with_alt_tab=arguments.get("restore_focus_with_alt_tab", False),
                wait_seconds=float(arguments.get("wait_seconds", 1.0)),
                fast_timing=arguments.get("fast_timing", True),
                type_key_ms=int(arguments.get("type_key_ms", 5)),
                type_inter_key_ms=int(arguments.get("type_inter_key_ms", 5)),
                type_shift_ms=int(arguments.get("type_shift_ms", 0)),
                max_text_chars=int(arguments.get("max_text_chars", 1200)),
                dry_run=arguments.get("dry_run", False),
            )
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        elif name == "transfer_unicode_file":
            result = await _do_transfer_unicode_file(
                client,
                text=arguments["text"],
                target_path=arguments["target_path"],
                chunk_chars=int(arguments.get("chunk_chars", _TRANSFER_DEFAULT_CHUNK)),
                focus_shell=arguments.get("focus_shell", True),
                wait_seconds=float(arguments.get("wait_seconds", 2.0)),
                fast_timing=arguments.get("fast_timing", True),
                type_key_ms=int(arguments.get("type_key_ms", 5)),
                type_inter_key_ms=int(arguments.get("type_inter_key_ms", 5)),
                type_shift_ms=int(arguments.get("type_shift_ms", 0)),
                max_text_chars=int(arguments.get("max_text_chars", 8000)),
                dry_run=arguments.get("dry_run", False),
            )
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except KvmClientError as e:
        logger.error(f"KVM server error in tool {name}: {e}")
        return [TextContent(type="text", text=f"Error: {str(e)}")]
    except Exception as e:
        logger.exception(f"Error in tool {name}")
        return [TextContent(type="text", text=f"Error: {str(e)}")]


async def run():
    """Run the MCP server."""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def main():
    """Entry point."""
    asyncio.run(run())


if __name__ == "__main__":
    main()

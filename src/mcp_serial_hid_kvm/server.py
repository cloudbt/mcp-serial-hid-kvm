"""MCP server for KVM control — thin client that delegates to KVM server.

All hardware operations (serial, capture) are delegated to the KVM server
via TCP.  OCR is run locally using frames fetched from the KVM server.
"""

import asyncio
import base64
import datetime
import hashlib
import io
import json
import logging
import os
import re
import time
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import ImageContent, TextContent, Tool
from PIL import Image, ImageChops, ImageDraw, ImageStat
from serial_hid_kvm.client import KvmClient, KvmClientError
from serial_hid_kvm.hid_keycodes import validate_chars

from .config import config
from .ocr import TerminalOCR
from .runtime_config import RuntimeConfig

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global instances
_client: KvmClient | None = None
_ocr: TerminalOCR | None = None
_runtime: RuntimeConfig | None = None

# In-memory screen baseline for screen_changed (set by set_screen_baseline).
# Holds {"image": PIL.Image (grayscale), "width", "height", "timestamp", "region"}.
_baseline: dict | None = None

# Cached target screen size (for mapping OCR pixels -> click coordinates).
_screen_size: tuple[int, int] | None = None

# Best-effort last-known target cursor position (x, y) in screen pixels.
# This stack cannot query the real OS cursor; we track coordinates we issued.
_cursor_pos: tuple[int, int] | None = None

# Best-effort hint of the last shell open_shell focused ("powershell"/"wsl").
_focused_shell_hint: str | None = None


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

def _normalize_lines(text: str) -> list[str]:
    """Strip trailing whitespace, collapse blank runs, trim edge blanks."""
    lines = [line.rstrip() for line in text.splitlines()]
    cleaned: list[str] = []
    blank_run = 0
    for line in lines:
        if not line.strip():
            blank_run += 1
            if blank_run > 1:
                continue
        else:
            blank_run = 0
        cleaned.append(line)
    while cleaned and not cleaned[0].strip():
        cleaned.pop(0)
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    return cleaned


def _compact_text(
    text: str,
    max_lines: int,
    max_chars: int,
    tail: bool = False,
) -> tuple[str, int, bool]:
    """Bound OCR text by line and character count.

    Args:
        text: Raw OCR text.
        max_lines: Maximum number of lines to keep (<=0 disables the limit).
        max_chars: Maximum number of characters to keep (<=0 disables).
        tail: If True keep the last lines/chars (command output lives at the
            bottom of a terminal); otherwise keep from the top.

    Returns:
        ``(compact_text, line_count, truncated)``.
    """
    lines = _normalize_lines(text)
    truncated = False
    if max_lines and max_lines > 0 and len(lines) > max_lines:
        lines = lines[-max_lines:] if tail else lines[:max_lines]
        truncated = True
    out = "\n".join(lines)
    if max_chars and max_chars > 0 and len(out) > max_chars:
        out = out[-max_chars:] if tail else out[:max_chars]
        truncated = True
    return out, len(lines), truncated


def _crop_region(image: Image.Image, region: Any) -> Image.Image:
    """Crop ``image`` to ``region`` = [x, y, w, h]; return image if region is falsy."""
    if not region:
        return image
    try:
        x, y, w, h = (int(v) for v in region)
    except (TypeError, ValueError):
        return image
    x = max(0, min(x, image.width))
    y = max(0, min(y, image.height))
    right = max(x, min(x + w, image.width))
    bottom = max(y, min(y + h, image.height))
    return image.crop((x, y, right, bottom))


def _ocr_failed(text: str) -> bool:
    """True if extract_text returned its error sentinel."""
    return text.startswith("[OCR Error:")


def _compact_ocr_error(text: str, limit: int = 200) -> dict:
    """Build a compact error payload from an OCR error sentinel (no traceback dump)."""
    detail = text[len("[OCR Error:"):].rstrip("]").strip()
    detail = " ".join(detail.split())
    if len(detail) > limit:
        detail = detail[:limit] + "..."
    return {"ok": False, "error": "ocr_failed", "detail": detail}


def _diff_score(img_a: Image.Image, img_b: Image.Image,
                pixel_delta: int = 30) -> float:
    """Fraction (0..1) of pixels whose grayscale value changed by > pixel_delta.

    A changed-pixel fraction is far more sensitive than a mean difference for
    "dark screen, different text" transitions (e.g. VS Code -> a full page of
    terminal text), where only the text strokes differ and a mean is diluted by
    the unchanged dark background.
    """
    a = img_a.convert("L")
    b = img_b.convert("L")
    if a.size != b.size:
        b = b.resize(a.size)
    diff = ImageChops.difference(a, b)
    mask = diff.point(lambda v: 255 if v > pixel_delta else 0)
    return ImageStat.Stat(mask).mean[0] / 255.0


def _escape_ps_double_quotes(text: str) -> str:
    """Escape double quotes for a PowerShell double-quoted string (backtick)."""
    return text.replace('"', '`"')


def _build_wsl_command(command: str, distro: str) -> str:
    """Build a PowerShell line that runs *command* in WSL *distro*.

    Shape: ``wsl.exe -d <distro> -- bash -lc "<escaped command>"``.
    Embedded double quotes are backtick-escaped for PowerShell. Complex
    quoting is unreliable in the MVP; prefer simple commands or base64.
    """
    return f'wsl.exe -d {distro} -- bash -lc "{_escape_ps_double_quotes(command)}"'


def get_client() -> KvmClient:
    global _client
    if _client is None:
        _client = KvmClient(config.kvm_host, config.kvm_port)
        _client.connect()
        logger.info("Connected to KVM server")
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


def get_ocr() -> TerminalOCR:
    global _ocr
    if _ocr is None:
        _ocr = TerminalOCR(config.tesseract_cmd)
    return _ocr


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


def _capture_image(quality: int = 85) -> Image.Image:
    """Fetch a frame from KVM server and return as PIL Image."""
    jpeg_bytes, w, h = get_client().capture_frame_jpeg(quality)
    return Image.open(io.BytesIO(jpeg_bytes))


def _get_screen_size() -> tuple[int, int]:
    """Target screen size (cached) used to map OCR pixels to click coordinates."""
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


def _do_health(client: KvmClient) -> dict:
    """Compact readiness snapshot for API / serial / video / OCR."""
    errors: list[str] = []
    api_ok = serial_ok = video_ok = ocr_ok = False
    capture_device = None
    resolution = None

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

    try:
        import pytesseract
        get_ocr()  # ensures tesseract_cmd / PATH are configured
        pytesseract.get_tesseract_version()
        ocr_ok = True
    except Exception as e:
        errors.append(f"ocr: {e}")

    return {
        "ok": api_ok and serial_ok and video_ok and ocr_ok,
        "api": api_ok,
        "serial": serial_ok,
        "video": video_ok,
        "ocr": ocr_ok,
        "capture_device": capture_device,
        "resolution": resolution,
        "errors": errors,
    }


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


async def _run_target_command(
    client: KvmClient,
    command: str,
    wait_seconds: float,
    max_lines: int,
    max_chars: int,
    lang: str | None = None,
) -> tuple[bool, str, bool]:
    """Type *command* into the focused target shell, wait, OCR, return tail output.

    Returns ``(ok, output_or_error_detail, truncated)``. The current input line
    is cleared first (see _clear_input_line), then the command is sent in raw
    mode (no {tag} interpretation) followed by Enter. ``ok`` is False only when
    OCR itself failed (output holds a compact error detail).
    """
    validate_chars(command)
    _clear_input_line(client)
    await asyncio.sleep(0.05)
    client.type_text(command, raw=True)
    await asyncio.sleep(0.1)
    client.send_key("enter")
    await asyncio.sleep(max(0.0, min(wait_seconds, _hard_max_wait())))
    image = _capture_image()
    text = get_ocr().extract_text(image, lang=lang or get_config().get("ocr_fast_lang"))
    if _ocr_failed(text):
        err = _compact_ocr_error(text)
        return False, err["detail"], False
    output, _line_count, truncated = _compact_text(text, max_lines, max_chars, tail=True)
    return True, output, truncated


# ---------------------------------------------------------------------------
# V2 wrapper helpers
# ---------------------------------------------------------------------------

def _match_text(haystack: str, needle: str, mode: str) -> tuple[bool, int]:
    """Return (found, count) for needle in haystack using contains/exact/regex."""
    if not needle:
        return (False, 0)
    if mode == "regex":
        try:
            matches = re.findall(needle, haystack)
        except re.error:
            return (False, 0)
        return (len(matches) > 0, len(matches))
    if mode == "exact":
        lines = [line.strip() for line in haystack.splitlines()]
        count = sum(1 for line in lines if line == needle)
        return (count > 0, count)
    # contains (case-insensitive)
    count = haystack.lower().count(needle.lower())
    return (count > 0, count)


def _ocr_region_text(region, lang: str | None = None) -> str:
    """Capture, optionally crop to region, and OCR to text."""
    return get_ocr().extract_text(_crop_region(_capture_image(), region), lang=lang)


def _do_set_baseline(region=None) -> dict:
    """Capture and store the in-memory baseline. Returns compact metadata."""
    global _baseline
    cropped = _crop_region(_capture_image(), region)
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    _baseline = {
        "image": cropped.convert("L"),
        "width": cropped.width,
        "height": cropped.height,
        "timestamp": ts,
        "region": region,
    }
    return {"ok": True, "width": cropped.width, "height": cropped.height,
            "timestamp": ts}


def _do_screen_changed(threshold: float, region=None,
                       auto_baseline: bool = False) -> dict:
    """One-shot baseline diff. Mirrors the screen_changed tool."""
    global _baseline
    current = _crop_region(_capture_image(), region)
    if _baseline is None:
        if auto_baseline:
            _do_set_baseline(region)
            return {"changed": False, "score": 0.0, "threshold": threshold,
                    "baseline_created": True}
        return {"ok": False, "error": "no_baseline",
                "detail": "Call set_screen_baseline first or pass auto_baseline=true."}
    score = _diff_score(_baseline["image"], current,
                        get_config().get("screen_diff_pixel_delta"))
    return {"changed": score > threshold, "score": round(score, 4),
            "threshold": threshold}


def _do_get_terminal_output(*, region=None, max_lines=None, max_chars=None,
                            tail: bool = True, lang=None) -> dict:
    cfg = get_config()
    max_lines = int(max_lines if max_lines is not None else cfg.get("terminal_max_lines"))
    max_chars = int(max_chars if max_chars is not None else cfg.get("terminal_max_chars"))
    if region is None:
        region = cfg.get("terminal_region")  # optional default bottom region
    text = _ocr_region_text(region, lang=lang or cfg.get("ocr_fast_lang"))
    if _ocr_failed(text):
        return _compact_ocr_error(text)
    output, line_count, truncated = _compact_text(text, max_lines, max_chars, tail=tail)
    return {"ok": True, "output": output, "line_count": line_count,
            "truncated": truncated, "region": region}


async def _do_wait_for_text(client, *, text, match="contains", present=True,
                            timeout_seconds=None, poll_ms=None, region=None,
                            min_confidence=0.0, max_chars=1000, lang=None) -> dict:
    cfg = get_config()
    lang = lang or cfg.get("ocr_fast_lang")
    timeout = min(float(timeout_seconds if timeout_seconds is not None
                        else cfg.get("wait_timeout_seconds")), _hard_max_wait())
    poll = (poll_ms if poll_ms is not None else cfg.get("wait_poll_ms")) / 1000.0
    start = time.monotonic()
    attempts = 0
    haystack = ""
    found = False
    count = 0
    while True:
        attempts += 1
        if min_confidence and min_confidence > 0:
            els = get_ocr().extract_elements(
                _crop_region(_capture_image(), region),
                min_confidence=min_confidence, lang=lang)
            haystack = "\n".join(e["text"] for e in els)
        else:
            haystack = _ocr_region_text(region, lang=lang)
        found, count = _match_text(haystack, text, match)
        satisfied = found if present else (not found)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        if satisfied:
            excerpt, _lc, _tr = _compact_text(haystack, 0, max_chars, tail=True)
            return {"ok": True, "found": found, "elapsed_ms": elapsed_ms,
                    "attempts": attempts, "match_count": count,
                    "text_excerpt": excerpt}
        if time.monotonic() - start >= timeout:
            excerpt, _lc, _tr = _compact_text(haystack, 0, max_chars, tail=True)
            return {"ok": False, "found": found, "elapsed_ms": elapsed_ms,
                    "attempts": attempts, "match_count": count,
                    "text_excerpt": excerpt, "timed_out": True}
        await asyncio.sleep(poll)


async def _do_wait_for_screen_change(client, *, threshold=None, timeout_seconds=None,
                                     poll_ms=None, region=None, auto_baseline=True,
                                     update_baseline_on_change=False) -> dict:
    global _baseline
    cfg = get_config()
    threshold = float(threshold if threshold is not None
                      else cfg.get("screen_change_threshold"))
    timeout = min(float(timeout_seconds if timeout_seconds is not None
                        else cfg.get("wait_timeout_seconds")), _hard_max_wait())
    poll = (poll_ms if poll_ms is not None else cfg.get("wait_poll_ms")) / 1000.0
    if _baseline is None:
        if auto_baseline:
            _do_set_baseline(region)
        else:
            return {"ok": False, "error": "no_baseline",
                    "detail": "Call set_screen_baseline first or pass auto_baseline=true."}
    pixel_delta = cfg.get("screen_diff_pixel_delta")
    start = time.monotonic()
    attempts = 0
    score = 0.0
    while True:
        attempts += 1
        current = _crop_region(_capture_image(), region)
        score = _diff_score(_baseline["image"], current, pixel_delta)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        if score > threshold:
            if update_baseline_on_change:
                _baseline["image"] = current.convert("L")
            return {"changed": True, "score": round(score, 4),
                    "threshold": threshold, "elapsed_ms": elapsed_ms,
                    "attempts": attempts}
        if time.monotonic() - start >= timeout:
            return {"changed": False, "score": round(score, 4),
                    "threshold": threshold, "elapsed_ms": elapsed_ms,
                    "attempts": attempts, "timed_out": True}
        await asyncio.sleep(poll)


async def _do_open_shell(client, *, shell="powershell", distro=None, method="win_r",
                         wait_seconds=None, verify=True) -> dict:
    global _focused_shell_hint
    cfg = get_config()
    distro = distro or cfg.get("default_wsl_distro")
    wait_seconds = min(float(wait_seconds if wait_seconds is not None
                             else cfg.get("open_shell_wait_seconds")), _hard_max_wait())
    if method == "win_r":
        client.send_key("r", ["win"])
        await asyncio.sleep(0.6)
        client.type_text("powershell", raw=True)
        await asyncio.sleep(0.2)
        client.send_key("enter")
        detail = "Win+R -> powershell"
    else:  # type_command
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
    verified = False
    if verify:
        text = _ocr_region_text(None)
        if not _ocr_failed(text):
            low = text.lower()
            if shell == "powershell":
                verified = any(tok in low for tok in ("ps ", "ps>", "powershell")) or (">" in text)
            else:
                verified = ("$" in text) or ("@" in text) or (distro.split("-")[0].lower() in low)
    return {"ok": True, "shell": shell,
            "distro": distro if shell == "wsl" else None,
            "method": method, "verified": verified, "detail": detail}


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


ALLOWED_TASK_ACTIONS = {
    "open_shell", "run_powershell_and_read", "run_wsl_and_read", "wait_for_text",
    "wait_for_screen_change", "get_terminal_output", "screen_changed",
    "set_screen_baseline", "send_key", "type_text",
}


async def _dispatch_task_action(client, action: str, args: dict) -> dict:
    """Run a single allowed task step and return a compact result dict."""
    cfg = get_config()
    if action == "open_shell":
        return await _do_open_shell(
            client, shell=args.get("shell", "powershell"), distro=args.get("distro"),
            method=args.get("method", "win_r"), wait_seconds=args.get("wait_seconds"),
            verify=args.get("verify", True))
    if action in ("run_powershell_and_read", "run_wsl_and_read"):
        wait = min(float(args.get("wait_seconds", cfg.get("terminal_wait_seconds"))),
                   _hard_max_wait())
        max_lines = int(args.get("max_lines", cfg.get("terminal_max_lines")))
        max_chars = int(args.get("max_chars", cfg.get("terminal_max_chars")))
        lang = args.get("lang")
        if action == "run_wsl_and_read":
            distro = args.get("distro") or cfg.get("default_wsl_distro")
            line = _build_wsl_command(args["command"], distro)
            ok, out, tr = await _run_target_command(
                client, line, wait, max_lines, max_chars, lang=lang)
            base = {"shell": "wsl", "distro": distro}
        else:
            ok, out, tr = await _run_target_command(
                client, args["command"], wait, max_lines, max_chars, lang=lang)
            base = {"shell": "powershell"}
        if not ok:
            return {"ok": False, "error": "ocr_failed", "detail": out, **base}
        return {"ok": True, "output": out, "truncated": tr, **base}
    if action == "wait_for_text":
        return await _do_wait_for_text(
            client, text=args["text"], match=args.get("match", "contains"),
            present=args.get("present", True), timeout_seconds=args.get("timeout_seconds"),
            poll_ms=args.get("poll_ms"), region=args.get("region"),
            min_confidence=float(args.get("min_confidence", 0.0)),
            max_chars=int(args.get("max_chars", 1000)), lang=args.get("lang"))
    if action == "wait_for_screen_change":
        return await _do_wait_for_screen_change(
            client, threshold=args.get("threshold"),
            timeout_seconds=args.get("timeout_seconds"), poll_ms=args.get("poll_ms"),
            region=args.get("region"), auto_baseline=args.get("auto_baseline", True),
            update_baseline_on_change=args.get("update_baseline_on_change", False))
    if action == "get_terminal_output":
        return _do_get_terminal_output(
            region=args.get("region"), max_lines=args.get("max_lines"),
            max_chars=args.get("max_chars"), tail=args.get("tail", True),
            lang=args.get("lang"))
    if action == "screen_changed":
        return _do_screen_changed(
            float(args.get("threshold", cfg.get("screen_change_threshold"))),
            args.get("region"), args.get("auto_baseline", False))
    if action == "set_screen_baseline":
        return _do_set_baseline(args.get("region"))
    if action == "send_key":
        client.send_key(args["key"], args.get("modifiers", []))
        return {"ok": True, "sent": args["key"]}
    if action == "type_text":
        validate_chars(args["text"])
        client.type_text(args["text"], args.get("char_delay_ms"), raw=args.get("raw", False))
        return {"ok": True, "typed": len(args["text"])}
    return {"ok": False, "error": "unknown_action", "action": action}


def _brief_result(action: str, result: dict) -> str:
    """One-line summary of a step result for the task report."""
    if not isinstance(result, dict):
        return "done"
    if result.get("error"):
        return f"ERROR {result['error']}"
    for key in ("found", "changed", "verified", "output", "typed", "sent", "ok"):
        if key in result:
            val = result[key]
            if key == "output" and isinstance(val, str):
                val = val.replace("\n", " ")
                if len(val) > 80:
                    val = val[:80] + "..."
            return f"{key}={val}"
    return "ok"


async def _do_run_task_and_report(client, *, task="", steps, max_steps=10,
                                  timeout_seconds=None, stop_on_error=True,
                                  stop_on_unverified=True,
                                  max_report_chars=2000) -> dict:
    cfg = get_config()
    budget = min(float(timeout_seconds if timeout_seconds is not None
                       else cfg.get("max_wait_seconds")), _hard_max_wait())
    start = time.monotonic()
    steps_run = 0
    failed_step: int | None = None
    summary_parts: list[str] = []
    final_output = ""
    limit = min(int(max_steps), len(steps))
    for idx in range(limit):
        if time.monotonic() - start >= budget:
            summary_parts.append(f"[{idx}] aborted: time budget exceeded")
            failed_step = idx
            break
        step = steps[idx]
        if not isinstance(step, dict) or "action" not in step:
            failed_step = idx
            summary_parts.append(f"[{idx}] invalid step (no action)")
            if stop_on_error:
                break
            continue
        action = step["action"]
        if action not in ALLOWED_TASK_ACTIONS:
            failed_step = idx
            summary_parts.append(f"[{idx}] {action}: not allowed")
            if stop_on_error:
                break
            continue
        args = {k: v for k, v in step.items() if k != "action"}
        try:
            result = await _dispatch_task_action(client, action, args)
        except Exception as e:
            failed_step = idx
            summary_parts.append(f"[{idx}] {action}: error {e}")
            if stop_on_error:
                break
            continue
        steps_run += 1
        # Treat an unverified shell as a failure so later steps don't run in the
        # wrong window (avoids the focus/concatenation class of problems).
        if (stop_on_unverified and action == "open_shell"
                and isinstance(result, dict) and not result.get("verified")):
            result["error"] = "shell_unverified"
        if isinstance(result, dict) and result.get("output"):
            final_output = result["output"]
        summary_parts.append(f"[{idx}] {action}: {_brief_result(action, result)}")
        if isinstance(result, dict) and result.get("error"):
            failed_step = idx
            if stop_on_error:
                break
    summary = "\n".join(summary_parts)
    if len(summary) > max_report_chars:
        summary = summary[:max_report_chars] + "..."
    fo_excerpt, _lc, _tr = _compact_text(final_output, 0, 600, tail=True)
    return {"ok": failed_step is None, "task": task, "steps_run": steps_run,
            "failed_step": failed_step, "summary": summary,
            "final_output_excerpt": fo_excerpt}


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
        "Write-Output ('PASTE_UNICODE_OK chars='+$s.Length);"
        "exit"
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
    into the target, and optionally paste with Ctrl+V.
    """
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
        await _do_open_shell(client, shell="powershell", verify=False)

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
        _clear_input_line(client)
        await asyncio.sleep(0.05)
        client.type_text(command, raw=True)
        await asyncio.sleep(0.1)
        client.send_key("enter")
        await asyncio.sleep(max(0.0, min(wait_seconds, _hard_max_wait())))

        try:
            ocr_text = get_ocr().extract_text(
                _capture_image(), lang=cfg.get("ocr_fast_lang"))
            if not _ocr_failed(ocr_text) and "PASTE_UNICODE_OK" in ocr_text:
                verified = True
        except Exception:
            pass  # OCR failure is non-fatal

        if paste_after_set:
            if restore_focus_with_alt_tab:
                client.send_key("tab", ["alt"])
                await asyncio.sleep(0.3)
            client.send_key("v", ["ctrl"])
            pasted = True
            if not restore_focus_with_alt_tab:
                warnings.append(
                    "Ctrl+V sent; focus may still be PowerShell unless moved by caller.")

    finally:
        if old_timing is not None:
            try:
                client.set_timing(old_timing)
            except KvmClientError as e:
                warnings.append(f"timing restore failed: {e}")

    return {
        "ok": True,
        "set_clipboard": True,
        "pasted": pasted,
        **meta,
        "verified": verified,
        "warning": "; ".join(warnings) if warnings else None,
    }


# Create MCP server
app = Server("mcp-serial-hid-kvm")


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return [
        Tool(
            name="type_text",
            description=(
                'Type a string as keyboard input on the target PC. Supports inline tags: {enter}, {tab}, {ctrl+c}, {shift+0x87}, etc. '
                '{0xNN} sends any HID keycode by hex value (0x00-0xFF) for keys without a named tag, e.g. {0x87} = JIS ろ key (International1).\n'
                '\n'
                '**Whitelist-based tag parsing:** Only recognized special key names inside {braces} are interpreted as tags. '
                'Unknown {content} (e.g. {print $1}) is passed through as literal text including the braces. '
                'This means code with curly braces (awk, Python, shell) can be sent without escaping in most cases.\n'
                '\n'
                '**Escaping:** Use {{ and }} to force literal braces when they collide with a recognized tag name '
                '(e.g. {{enter}} to type the literal text "{enter}").\n'
                '\n'
                '**Raw mode (raw=true):** Disables ALL tag interpretation. '
                'Actual line breaks in the input (LF, CRLF, CR) are sent as Enter key presses. '
                'In JSON, \\n is decoded into an actual line break, so it becomes Enter. '
                'To type a literal backslash + n, use \\\\n in JSON.\n'
                '\n'
                'Examples:\n'
                '  "ls -la{enter}"                     → types "ls -la" then presses Enter\n'
                '  "awk \'{print $1}\' file.txt{enter}"  → types the awk command then Enter (braces preserved)\n'
                '  "echo {{enter}}"                    → types "echo {enter}" (escaped to avoid tag)\n'
                '  raw=true: "ls -la\\necho hi\\n"       → types "ls -la", Enter, "echo hi", Enter\n'
                '\n'
                '**Supported characters:** ASCII printable (space through ~), tab, and newline only. '
                'Unicode, CJK, accented characters, and control characters cause an error. '
                'For unsupported characters or binary data, use base64 encoding as a workaround: '
                'encode on the host and type a decode command (e.g. `echo <b64> | base64 -d`) on the target.'
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text with optional {tag} sequences. Tags: {enter}/{return}, {space}, {tab}, {backspace}, {delete}, {insert}, {escape}/{esc}, {up}, {down}, {left}, {right}, {home}, {end}, {pageup}, {pagedown}, {f1}-{f12}, {capslock}, {numlock}, {scrolllock}, {printscreen}, {pause}, {0xNN} for raw HID keycodes (0x00-0xFF). Modifiers: ctrl/lctrl/rctrl, shift/lshift/rshift, alt/lalt/ralt, win/lwin/rwin/gui/super/meta — combine with +: {ctrl+c}, {alt+f4}, {ctrl+shift+del}, {shift+0x87}. Only recognized key names are treated as tags; other braces pass through literally.",
                    },
                    "raw": {
                        "type": "boolean",
                        "description": "If true, disable all {tag} interpretation. Actual line breaks (LF, CRLF, CR) become Enter. In JSON, \\n is decoded into a line break and becomes Enter; \\\\n types literal backslash + n. Default: false.",
                    },
                    "char_delay_ms": {
                        "type": "integer",
                        "description": "Delay between characters in milliseconds (default: 20)",
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
            name="send_key_sequence",
            description="Send a sequence of key steps with optional per-step delays. Useful for complex keyboard operations.",
            inputSchema={
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "key": {"type": "string", "description": "Key name"},
                                "modifiers": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Modifier keys",
                                },
                                "delay_ms": {
                                    "type": "integer",
                                    "description": "Delay after this step in ms (default: 100)",
                                },
                            },
                            "required": ["key"],
                        },
                        "description": "List of key steps to execute",
                    },
                    "default_delay_ms": {
                        "type": "integer",
                        "description": "Default delay between steps in ms (default: 100)",
                    },
                },
                "required": ["steps"],
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
            description="Click a mouse button on the target PC, optionally at a specific position.",
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
                },
                "required": [],
            },
        ),
        Tool(
            name="mouse_drag",
            description="Drag from one position to another (press button at start, move to end, release). Useful for drag-and-drop, selecting text, resizing windows, etc.",
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
                },
                "required": ["start_x", "start_y", "end_x", "end_y"],
            },
        ),
        Tool(
            name="mouse_scroll",
            description="Scroll the mouse wheel on the target PC.",
            inputSchema={
                "type": "object",
                "properties": {
                    "amount": {
                        "type": "integer",
                        "description": "Scroll amount: positive=up, negative=down (-127 to 127)",
                    },
                },
                "required": ["amount"],
            },
        ),
        Tool(
            name="capture_screen",
            description="Capture the target PC screen via HDMI capture device. Returns the image. Use sparingly as images consume many tokens.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="get_screen_text",
            description="Capture the target PC screen and extract text using OCR. Prefer this over capture_screen for text content.",
            inputSchema={
                "type": "object",
                "properties": {
                    "lang": {
                        "type": "string",
                        "description": "OCR language override (e.g. 'eng', 'eng+jpn', 'eng+jpn+chi_sim'). Default from config ocr_lang (eng+jpn).",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="execute_and_read",
            description="Type a command, press Enter, wait for output, then capture screen and OCR. Convenient for running shell commands on the target PC.",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Command to type and execute",
                    },
                    "wait_seconds": {
                        "type": "number",
                        "description": "Seconds to wait for output (default: 1.0)",
                    },
                    "lang": {
                        "type": "string",
                        "description": "OCR language override. Default from config ocr_fast_lang (eng); pass 'eng+jpn' for Japanese output.",
                    },
                },
                "required": ["command"],
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
        # the same KVM client + local OCR and return compact JSON (no images).
        # -------------------------------------------------------------------
        Tool(
            name="health",
            description="Compact readiness check for the whole stack (API, serial, video, OCR). Returns small JSON, never an image.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        Tool(
            name="set_screen_baseline",
            description="Capture the current frame and store it in memory as the baseline for screen_changed. Returns ok/width/height/timestamp (no image).",
            inputSchema={
                "type": "object",
                "properties": {
                    "region": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Optional [x, y, w, h] region to baseline. Omit for full frame.",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="screen_changed",
            description="Compare the current frame to the baseline and return only {changed, score, threshold}. No image. Set auto_baseline=true to create a baseline if none exists.",
            inputSchema={
                "type": "object",
                "properties": {
                    "threshold": {
                        "type": "number",
                        "description": "Change threshold 0..1 (default 0.02). changed = score > threshold.",
                    },
                    "region": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Optional [x, y, w, h] region to compare.",
                    },
                    "auto_baseline": {
                        "type": "boolean",
                        "description": "If true and no baseline exists, set one and report changed=false (default false).",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="get_screen_text_compact",
            description="OCR the current screen and return whitespace-normalized, bounded text (no image). Use instead of get_screen_text for token efficiency.",
            inputSchema={
                "type": "object",
                "properties": {
                    "max_lines": {
                        "type": "integer",
                        "description": "Maximum lines to return (default 40).",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum characters to return (default 4000).",
                    },
                    "region": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Optional [x, y, w, h] region to OCR.",
                    },
                    "lang": {
                        "type": "string",
                        "description": "OCR language override (e.g. 'eng', 'eng+jpn', 'eng+jpn+chi_sim'). Default from config ocr_lang (eng+jpn).",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="detect_text_elements",
            description="OCR the screen with Tesseract TSV and return text elements with bounding boxes for local click targeting. Compact JSON, no image.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional case-insensitive substring filter on element text.",
                    },
                    "max_items": {
                        "type": "integer",
                        "description": "Maximum elements to return (default 100).",
                    },
                    "min_confidence": {
                        "type": "number",
                        "description": "Minimum OCR confidence 0..1 (default 0.0).",
                    },
                    "region": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Optional [x, y, w, h] region to OCR. Returned coordinates are full-frame.",
                    },
                    "lang": {
                        "type": "string",
                        "description": "OCR language override. Default from config ocr_lang (eng+jpn).",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="click_text",
            description="Find text on screen via OCR bounding boxes and click its center. Supports dry_run=true to return the coordinate without clicking.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text to find.",
                    },
                    "match": {
                        "type": "string",
                        "enum": ["contains", "exact"],
                        "description": "Match mode (default contains).",
                    },
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                        "description": "Mouse button (default left).",
                    },
                    "index": {
                        "type": "integer",
                        "description": "Which match to click when several match (default 0).",
                    },
                    "min_confidence": {
                        "type": "number",
                        "description": "Minimum OCR confidence 0..1 (default 0.0).",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "If true, return the chosen coordinate without clicking (default false).",
                    },
                    "lang": {
                        "type": "string",
                        "description": "OCR language override. Default from config ocr_lang (eng+jpn).",
                    },
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="run_powershell_and_read",
            description="Run a PowerShell command on the TARGET PC via KVM keyboard input, wait, then OCR the screen and return compact output. Assumes a PowerShell prompt is focused on the target. Does NOT execute anything on the host.",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "PowerShell command to type into the focused target shell.",
                    },
                    "lang": {
                        "type": "string",
                        "description": "OCR language override. Default from config ocr_fast_lang (eng); pass 'eng+jpn' for Japanese output.",
                    },
                    "wait_seconds": {
                        "type": "number",
                        "description": "Seconds to wait before OCR (default from config terminal_wait_seconds; capped by max_wait_seconds).",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "Maximum output lines to return (default from config terminal_max_lines).",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum output characters to return (default from config terminal_max_chars).",
                    },
                },
                "required": ["command"],
            },
        ),
        Tool(
            name="run_wsl_and_read",
            description='Run a Linux command on the TARGET via WSL by typing `wsl.exe -d <distro> -- bash -lc "<command>"` into the focused target PowerShell, then OCR the result. Embedded double quotes are backtick-escaped; complex quoting is unreliable. Does NOT execute anything on the host.',
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Linux command to run inside WSL bash -lc.",
                    },
                    "lang": {
                        "type": "string",
                        "description": "OCR language override. Default from config ocr_fast_lang (eng); pass 'eng+jpn' for Japanese output.",
                    },
                    "distro": {
                        "type": "string",
                        "description": "WSL distro name (default from config default_wsl_distro).",
                    },
                    "wait_seconds": {
                        "type": "number",
                        "description": "Seconds to wait before OCR (default from config terminal_wait_seconds; capped by max_wait_seconds).",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "Maximum output lines to return (default from config terminal_max_lines).",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum output characters to return (default from config terminal_max_chars).",
                    },
                },
                "required": ["command"],
            },
        ),
        # -------------------------------------------------------------------
        # V2 wrapper tools: progressive verification, runtime config, shell
        # orchestration. All compact JSON except cursor_crop (image-oriented).
        # -------------------------------------------------------------------
        Tool(
            name="open_shell",
            description="Focus/open a TARGET shell (PowerShell or WSL) via KVM HID so command tools do not depend on current focus. Never runs host commands.",
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
                    "verify": {
                        "type": "boolean",
                        "description": "If true, OCR after opening to look for a prompt (default true).",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="wait_for_text",
            description="Poll-OCR locally until text appears (or disappears). Avoids repeated model round-trips. Returns compact JSON, no image. NOTE: each poll is one capture+OCR, so the poll granularity equals OCR latency (slow with CJK); for fast gating use wait_for_screen_change first, then OCR. Defaults to ocr_fast_lang (eng); pass lang for CJK.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to wait for."},
                    "match": {
                        "type": "string",
                        "enum": ["contains", "exact", "regex"],
                        "description": "Match mode (default contains).",
                    },
                    "present": {
                        "type": "boolean",
                        "description": "Wait until present (true, default) or absent (false).",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": "Max seconds to poll (default from config wait_timeout_seconds; capped by max_wait_seconds).",
                    },
                    "poll_ms": {
                        "type": "integer",
                        "description": "Delay between polls in ms (default from config wait_poll_ms).",
                    },
                    "region": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Optional [x,y,w,h] region to OCR.",
                    },
                    "min_confidence": {
                        "type": "number",
                        "description": "Minimum OCR confidence 0..1 when using element matching (default 0.0).",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Max chars in returned text_excerpt (default 1000).",
                    },
                    "lang": {
                        "type": "string",
                        "description": "OCR language override. Default from config ocr_fast_lang (eng); pass 'eng+jpn' to wait for Japanese text.",
                    },
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="wait_for_screen_change",
            description="Poll a local image-diff (changed-pixel fraction) against set_screen_baseline until the screen changes (or timeout). Fast (no OCR) - use as a pre-gate before wait_for_text/OCR. Returns compact JSON, no image.",
            inputSchema={
                "type": "object",
                "properties": {
                    "threshold": {
                        "type": "number",
                        "description": "Change threshold 0..1 (default from config screen_change_threshold).",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": "Max seconds to poll (default from config wait_timeout_seconds; capped by max_wait_seconds).",
                    },
                    "poll_ms": {
                        "type": "integer",
                        "description": "Delay between polls in ms (default from config wait_poll_ms).",
                    },
                    "region": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Optional [x,y,w,h] region to compare.",
                    },
                    "auto_baseline": {
                        "type": "boolean",
                        "description": "Seed a baseline if none exists (default true).",
                    },
                    "update_baseline_on_change": {
                        "type": "boolean",
                        "description": "Replace the baseline with the changed frame when detected (default false).",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="cursor_crop",
            description="Return a small image crop around a coordinate (or the best-effort tracked cursor). Only V2 tool that returns an image. FIRST USE: pass x/y, or call mouse_move/mouse_click/click_text first to set the tracked cursor; otherwise returns {ok:false,error:'cursor_unknown'} (the stack cannot read the real OS cursor).",
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
            name="get_terminal_output",
            description="OCR only a region (default: full-screen tail) and return the latest lines. Compact JSON, no image.",
            inputSchema={
                "type": "object",
                "properties": {
                    "region": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Optional [x,y,w,h] region. Default is full screen (tail).",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "Maximum lines (default from config terminal_max_lines).",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum characters (default from config terminal_max_chars).",
                    },
                    "tail": {
                        "type": "boolean",
                        "description": "Keep the last lines/chars (default true).",
                    },
                    "lang": {
                        "type": "string",
                        "description": "OCR language override. Default from config ocr_fast_lang (eng); pass 'eng+jpn' for Japanese output.",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="run_task_and_report",
            description="Run a small bounded sequence of allowed wrapper steps locally and return only a summary (token-saving). Not for arbitrary autonomy; no host commands; text-only report.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "Short label for the task."},
                    "steps": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Ordered steps. Each: {action, ...action args}. Allowed actions: open_shell, run_powershell_and_read, run_wsl_and_read, wait_for_text, wait_for_screen_change, get_terminal_output, screen_changed, set_screen_baseline, send_key, type_text.",
                    },
                    "max_steps": {
                        "type": "integer",
                        "description": "Maximum steps to run (default 10).",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "description": "Overall budget in seconds (default from config max_wait_seconds; capped by it).",
                    },
                    "stop_on_error": {
                        "type": "boolean",
                        "description": "Stop at the first failing step (default true).",
                    },
                    "stop_on_unverified": {
                        "type": "boolean",
                        "description": "Treat an open_shell step with verified=false as a failure, so later steps don't run in the wrong window (default true).",
                    },
                    "max_report_chars": {
                        "type": "integer",
                        "description": "Maximum characters in the summary (default 2000).",
                    },
                },
                "required": ["steps"],
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
                "Transfer Unicode text (Japanese, Chinese, etc.) to the Target clipboard "
                "via UTF-8 Base64URL typed through PowerShell. Optionally paste with Ctrl+V. "
                "Good for short/medium text (1-500 CJK chars ≈ 2-22 s at fast_timing). "
                "Large text is slow over HID; use transfer_unicode_file for > 1000 chars."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Unicode text to set on the Target clipboard. Supports Japanese, Chinese, English, newlines, and symbols.",
                    },
                    "focus_shell": {
                        "type": "boolean",
                        "description": "Open/focus Target PowerShell before setting clipboard (default true).",
                    },
                    "paste_after_set": {
                        "type": "boolean",
                        "description": "After setting the clipboard, send Ctrl+V (default false; focus is still PowerShell after this tool unless you moved it).",
                    },
                    "restore_focus_with_alt_tab": {
                        "type": "boolean",
                        "description": "If paste_after_set=true, send Alt+Tab before Ctrl+V to return to the previous app (default false).",
                    },
                    "wait_seconds": {
                        "type": "number",
                        "description": "Seconds to wait after executing the PowerShell clipboard command (default 1.0).",
                    },
                    "fast_timing": {
                        "type": "boolean",
                        "description": "Temporarily speed up HID typing for the Base64 payload, then restore prior timing (default true).",
                    },
                    "type_key_ms": {
                        "type": "integer",
                        "description": "Per-key hold in ms when fast_timing=true (default 5).",
                    },
                    "type_inter_key_ms": {
                        "type": "integer",
                        "description": "Inter-key delay in ms when fast_timing=true (default 5).",
                    },
                    "type_shift_ms": {
                        "type": "integer",
                        "description": "Shift/modifier staging delay in ms when fast_timing=true (default 0).",
                    },
                    "max_text_chars": {
                        "type": "integer",
                        "description": "Safety cap for Unicode character count (default 1200).",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "Return size/estimate metadata without typing anything (default false).",
                    },
                },
                "required": ["text"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent | ImageContent]:
    """Handle tool calls."""
    try:
        client = get_client()

        if name == "type_text":
            text = arguments["text"]
            char_delay = arguments.get("char_delay_ms")
            raw = arguments.get("raw", False)
            validate_chars(text)
            result = client.type_text(text, char_delay, raw=raw)
            return [TextContent(type="text", text=f"Typed {len(text)} characters")]

        elif name == "send_key":
            key = arguments["key"]
            modifiers = arguments.get("modifiers", [])
            client.send_key(key, modifiers)
            mod_str = "+".join(modifiers) + "+" if modifiers else ""
            return [TextContent(type="text", text=f"Sent: {mod_str}{key}")]

        elif name == "send_key_sequence":
            steps = arguments["steps"]
            default_delay = arguments.get("default_delay_ms", 100)
            client.send_key_sequence(steps, default_delay)
            return [TextContent(type="text", text=f"Sent {len(steps)} key steps")]

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

        elif name == "mouse_click":
            button = arguments.get("button", "left")
            x = arguments.get("x")
            y = arguments.get("y")
            client.mouse_click(button, x, y)
            if x is not None and y is not None:
                _set_cursor(x, y)
            pos_str = f" at ({x}, {y})" if x is not None and y is not None else ""
            return [TextContent(type="text", text=f"Clicked {button}{pos_str}")]

        elif name == "mouse_drag":
            start_x = arguments["start_x"]
            start_y = arguments["start_y"]
            end_x = arguments["end_x"]
            end_y = arguments["end_y"]
            button = arguments.get("button", "left")
            client.mouse_down(button, start_x, start_y)
            await asyncio.sleep(0.05)
            client.mouse_move(end_x, end_y)
            await asyncio.sleep(0.05)
            client.mouse_up(button, end_x, end_y)
            _set_cursor(end_x, end_y)
            return [TextContent(
                type="text",
                text=f"Dragged {button} from ({start_x}, {start_y}) to ({end_x}, {end_y})",
            )]

        elif name == "mouse_scroll":
            amount = arguments["amount"]
            client.mouse_scroll(amount)
            direction = "up" if amount > 0 else "down"
            return [TextContent(type="text", text=f"Scrolled {direction} by {abs(amount)}")]

        elif name == "capture_screen":
            image = _capture_image()
            _save_capture_log(image, "capture")
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
            b64_image = base64.standard_b64encode(buffer.getvalue()).decode("utf-8")
            return [ImageContent(
                type="image",
                data=b64_image,
                mimeType="image/jpeg",
            )]

        elif name == "get_screen_text":
            image = _capture_image()
            _save_capture_log(image, "ocr")
            lang = arguments.get("lang") or get_config().get("ocr_lang")
            text = get_ocr().extract_text(image, lang=lang)
            return [TextContent(type="text", text=text)]

        elif name == "execute_and_read":
            command = arguments["command"]
            wait_seconds = arguments.get("wait_seconds", 1.0)
            lang = arguments.get("lang") or get_config().get("ocr_fast_lang")

            # Raw mode: no tag interpretation for command text
            validate_chars(command)
            _clear_input_line(client)
            await asyncio.sleep(0.05)
            client.type_text(command, raw=True)
            await asyncio.sleep(0.1)
            client.send_key("enter")
            await asyncio.sleep(wait_seconds)

            image = _capture_image()
            _save_capture_log(image, "exec")
            text = get_ocr().extract_text(image, lang=lang)
            return [TextContent(type="text", text=text)]

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
            cap_info = result.get("info", {})
            return [TextContent(
                type="text",
                text=f"Switched to device {device}: {cap_info.get('width')}x{cap_info.get('height')} ({cap_info.get('backend')})",
            )]

        elif name == "health":
            result = _do_health(client)
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        elif name == "set_screen_baseline":
            global _baseline
            region = arguments.get("region")
            image = _capture_image()
            cropped = _crop_region(image, region)
            ts = datetime.datetime.now().isoformat(timespec="seconds")
            _baseline = {
                "image": cropped.convert("L"),
                "width": cropped.width,
                "height": cropped.height,
                "timestamp": ts,
                "region": region,
            }
            return [TextContent(type="text", text=json.dumps({
                "ok": True,
                "width": cropped.width,
                "height": cropped.height,
                "timestamp": ts,
            }, ensure_ascii=False))]

        elif name == "screen_changed":
            threshold = float(arguments.get(
                "threshold", get_config().get("screen_change_threshold")))
            region = arguments.get("region")
            auto_baseline = arguments.get("auto_baseline", False)
            image = _capture_image()
            current = _crop_region(image, region)
            if _baseline is None:
                if auto_baseline:
                    ts = datetime.datetime.now().isoformat(timespec="seconds")
                    _baseline = {
                        "image": current.convert("L"),
                        "width": current.width,
                        "height": current.height,
                        "timestamp": ts,
                        "region": region,
                    }
                    return [TextContent(type="text", text=json.dumps({
                        "changed": False, "score": 0.0, "threshold": threshold,
                        "baseline_created": True,
                    }, ensure_ascii=False))]
                return [TextContent(type="text", text=json.dumps({
                    "ok": False, "error": "no_baseline",
                    "detail": "Call set_screen_baseline first or pass auto_baseline=true.",
                }, ensure_ascii=False))]
            score = _diff_score(_baseline["image"], current)
            return [TextContent(type="text", text=json.dumps({
                "changed": score > threshold,
                "score": round(score, 4),
                "threshold": threshold,
            }, ensure_ascii=False))]

        elif name == "get_screen_text_compact":
            max_lines = int(arguments.get("max_lines", 40))
            max_chars = int(arguments.get("max_chars", 4000))
            region = arguments.get("region")
            lang = arguments.get("lang") or get_config().get("ocr_lang")
            image = _crop_region(_capture_image(), region)
            text = get_ocr().extract_text(image, lang=lang)
            if _ocr_failed(text):
                return [TextContent(type="text", text=json.dumps(
                    _compact_ocr_error(text), ensure_ascii=False))]
            compact, line_count, truncated = _compact_text(text, max_lines, max_chars)
            return [TextContent(type="text", text=json.dumps({
                "text": compact,
                "line_count": line_count,
                "truncated": truncated,
            }, ensure_ascii=False))]

        elif name == "detect_text_elements":
            query = arguments.get("query")
            max_items = int(arguments.get("max_items", 100))
            min_conf = float(arguments.get("min_confidence", 0.0))
            region = arguments.get("region")
            lang = arguments.get("lang") or get_config().get("ocr_lang")
            image = _capture_image()
            full_w, full_h = image.size
            cropped = _crop_region(image, region)
            off_x, off_y = (int(region[0]), int(region[1])) if region else (0, 0)
            elements = get_ocr().extract_elements(cropped, min_confidence=min_conf, lang=lang)
            for el in elements:
                el["x"] += off_x
                el["y"] += off_y
            if query:
                q = query.lower()
                elements = [e for e in elements if q in e["text"].lower()]
            if max_items > 0:
                elements = elements[:max_items]
            return [TextContent(type="text", text=json.dumps({
                "elements": elements,
                "width": full_w,
                "height": full_h,
            }, ensure_ascii=False))]

        elif name == "click_text":
            target = arguments["text"]
            match = arguments.get("match", "contains")
            button = arguments.get("button", "left")
            index = int(arguments.get("index", 0))
            min_conf = float(arguments.get("min_confidence", 0.0))
            dry_run = arguments.get("dry_run", False)
            lang = arguments.get("lang") or get_config().get("ocr_lang")
            image = _capture_image()
            img_w, img_h = image.size
            elements = get_ocr().extract_elements(image, min_confidence=min_conf, lang=lang)
            q = target.lower()
            if match == "exact":
                matches = [e for e in elements if e["text"].lower() == q]
            else:
                matches = [e for e in elements if q in e["text"].lower()]
            if not matches:
                return [TextContent(type="text", text=json.dumps({
                    "clicked": False, "text": target, "match_count": 0,
                    "error": "text_not_found",
                }, ensure_ascii=False))]
            if index < 0 or index >= len(matches):
                return [TextContent(type="text", text=json.dumps({
                    "clicked": False, "text": target, "match_count": len(matches),
                    "error": "index_out_of_range",
                }, ensure_ascii=False))]
            el = matches[index]
            cx = el["x"] + el["w"] // 2
            cy = el["y"] + el["h"] // 2
            screen_w, screen_h = _get_screen_size()
            sx = int(round(cx * screen_w / img_w)) if img_w else cx
            sy = int(round(cy * screen_h / img_h)) if img_h else cy
            if dry_run:
                return [TextContent(type="text", text=json.dumps({
                    "clicked": False, "dry_run": True, "text": el["text"],
                    "x": sx, "y": sy, "match_count": len(matches),
                }, ensure_ascii=False))]
            client.mouse_click(button, sx, sy)
            _set_cursor(sx, sy)
            return [TextContent(type="text", text=json.dumps({
                "clicked": True, "text": el["text"],
                "x": sx, "y": sy, "match_count": len(matches),
            }, ensure_ascii=False))]

        elif name == "run_powershell_and_read":
            cfg = get_config()
            command = arguments["command"]
            wait_seconds = min(
                float(arguments.get("wait_seconds", cfg.get("terminal_wait_seconds"))),
                _hard_max_wait())
            max_lines = int(arguments.get("max_lines", cfg.get("terminal_max_lines")))
            max_chars = int(arguments.get("max_chars", cfg.get("terminal_max_chars")))
            ok, output, truncated = await _run_target_command(
                client, command, wait_seconds, max_lines, max_chars,
                lang=arguments.get("lang")
            )
            if not ok:
                return [TextContent(type="text", text=json.dumps({
                    "ok": False, "shell": "powershell",
                    "error": "ocr_failed", "detail": output,
                }, ensure_ascii=False))]
            return [TextContent(type="text", text=json.dumps({
                "ok": True, "shell": "powershell",
                "output": output, "truncated": truncated,
            }, ensure_ascii=False))]

        elif name == "run_wsl_and_read":
            cfg = get_config()
            command = arguments["command"]
            distro = arguments.get("distro") or cfg.get("default_wsl_distro")
            wait_seconds = min(
                float(arguments.get("wait_seconds", cfg.get("terminal_wait_seconds"))),
                _hard_max_wait())
            max_lines = int(arguments.get("max_lines", cfg.get("terminal_max_lines")))
            max_chars = int(arguments.get("max_chars", cfg.get("terminal_max_chars")))
            ps_line = _build_wsl_command(command, distro)
            ok, output, truncated = await _run_target_command(
                client, ps_line, wait_seconds, max_lines, max_chars,
                lang=arguments.get("lang")
            )
            if not ok:
                return [TextContent(type="text", text=json.dumps({
                    "ok": False, "shell": "wsl", "distro": distro,
                    "error": "ocr_failed", "detail": output,
                }, ensure_ascii=False))]
            return [TextContent(type="text", text=json.dumps({
                "ok": True, "shell": "wsl", "distro": distro,
                "output": output, "truncated": truncated,
            }, ensure_ascii=False))]

        elif name == "open_shell":
            result = await _do_open_shell(
                client, shell=arguments.get("shell", "powershell"),
                distro=arguments.get("distro"), method=arguments.get("method", "win_r"),
                wait_seconds=arguments.get("wait_seconds"),
                verify=arguments.get("verify", True))
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        elif name == "wait_for_text":
            result = await _do_wait_for_text(
                client, text=arguments["text"], match=arguments.get("match", "contains"),
                present=arguments.get("present", True),
                timeout_seconds=arguments.get("timeout_seconds"),
                poll_ms=arguments.get("poll_ms"), region=arguments.get("region"),
                min_confidence=float(arguments.get("min_confidence", 0.0)),
                max_chars=int(arguments.get("max_chars", 1000)),
                lang=arguments.get("lang"))
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        elif name == "wait_for_screen_change":
            result = await _do_wait_for_screen_change(
                client, threshold=arguments.get("threshold"),
                timeout_seconds=arguments.get("timeout_seconds"),
                poll_ms=arguments.get("poll_ms"), region=arguments.get("region"),
                auto_baseline=arguments.get("auto_baseline", True),
                update_baseline_on_change=arguments.get("update_baseline_on_change", False))
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        elif name == "get_terminal_output":
            result = _do_get_terminal_output(
                region=arguments.get("region"), max_lines=arguments.get("max_lines"),
                max_chars=arguments.get("max_chars"), tail=arguments.get("tail", True),
                lang=arguments.get("lang"))
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

        elif name == "run_task_and_report":
            result = await _do_run_task_and_report(
                client, task=arguments.get("task", ""), steps=arguments.get("steps", []),
                max_steps=int(arguments.get("max_steps", 10)),
                timeout_seconds=arguments.get("timeout_seconds"),
                stop_on_error=arguments.get("stop_on_error", True),
                stop_on_unverified=arguments.get("stop_on_unverified", True),
                max_report_chars=int(arguments.get("max_report_chars", 2000)))
            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

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

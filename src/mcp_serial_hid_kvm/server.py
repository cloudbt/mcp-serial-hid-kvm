"""MCP server for KVM control — thin client that delegates to KVM server.

All hardware operations (serial, capture) are delegated to the KVM server
via TCP.  OCR is run locally using frames fetched from the KVM server.
"""

import asyncio
import base64
import datetime
import io
import json
import logging
import os
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import ImageContent, TextContent, Tool
from PIL import Image, ImageChops, ImageStat
from serial_hid_kvm.client import KvmClient, KvmClientError
from serial_hid_kvm.hid_keycodes import validate_chars

from .config import config
from .ocr import TerminalOCR

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global instances
_client: KvmClient | None = None
_ocr: TerminalOCR | None = None

# In-memory screen baseline for screen_changed (set by set_screen_baseline).
# Holds {"image": PIL.Image (grayscale), "width", "height", "timestamp", "region"}.
_baseline: dict | None = None

# Cached target screen size (for mapping OCR pixels -> click coordinates).
_screen_size: tuple[int, int] | None = None

# Hard ceiling on any wait/timeout exposed by the wrapper command tools.
MAX_WAIT_SECONDS = 60.0


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


def _diff_score(img_a: Image.Image, img_b: Image.Image) -> float:
    """Mean absolute grayscale difference between two images, normalized 0..1."""
    a = img_a.convert("L")
    b = img_b.convert("L")
    if a.size != b.size:
        b = b.resize(a.size)
    diff = ImageChops.difference(a, b)
    return ImageStat.Stat(diff).mean[0] / 255.0


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
    return _client


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
                video_ok = True
                capture_device = (
                    str(cap.get("device")) if cap.get("device") is not None else None
                )
                w, h = cap.get("width"), cap.get("height")
                if w and h:
                    resolution = f"{w}x{h}"
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


async def _run_target_command(
    client: KvmClient,
    command: str,
    wait_seconds: float,
    max_lines: int,
    max_chars: int,
) -> tuple[bool, str, bool]:
    """Type *command* into the focused target shell, wait, OCR, return tail output.

    Returns ``(ok, output_or_error_detail, truncated)``. The command text is
    sent in raw mode (no {tag} interpretation) followed by Enter. ``ok`` is
    False only when OCR itself failed (output holds a compact error detail).
    """
    validate_chars(command)
    client.type_text(command, raw=True)
    await asyncio.sleep(0.1)
    client.send_key("enter")
    await asyncio.sleep(max(0.0, min(wait_seconds, MAX_WAIT_SECONDS)))
    image = _capture_image()
    text = get_ocr().extract_text(image)
    if _ocr_failed(text):
        err = _compact_ocr_error(text)
        return False, err["detail"], False
    output, _line_count, truncated = _compact_text(text, max_lines, max_chars, tail=True)
    return True, output, truncated


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
                "properties": {},
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
                    "wait_seconds": {
                        "type": "number",
                        "description": "Seconds to wait for output before OCR (default 2, max 60).",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "Maximum output lines to return (default 30).",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum output characters to return (default 3000).",
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
                    "distro": {
                        "type": "string",
                        "description": "WSL distro name (default Ubuntu-24.04).",
                    },
                    "wait_seconds": {
                        "type": "number",
                        "description": "Seconds to wait for output before OCR (default 2, max 60).",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "Maximum output lines to return (default 30).",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum output characters to return (default 3000).",
                    },
                },
                "required": ["command"],
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
                return [TextContent(type="text", text=f"Moved mouse by ({x}, {y})")]
            else:
                return [TextContent(type="text", text=f"Moved mouse to ({x}, {y})")]

        elif name == "mouse_click":
            button = arguments.get("button", "left")
            x = arguments.get("x")
            y = arguments.get("y")
            client.mouse_click(button, x, y)
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
            text = get_ocr().extract_text(image)
            return [TextContent(type="text", text=text)]

        elif name == "execute_and_read":
            command = arguments["command"]
            wait_seconds = arguments.get("wait_seconds", 1.0)

            # Raw mode: no tag interpretation for command text
            validate_chars(command)
            client.type_text(command, raw=True)
            await asyncio.sleep(0.1)
            client.send_key("enter")
            await asyncio.sleep(wait_seconds)

            image = _capture_image()
            _save_capture_log(image, "exec")
            text = get_ocr().extract_text(image)
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
            threshold = float(arguments.get("threshold", 0.02))
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
            image = _crop_region(_capture_image(), region)
            text = get_ocr().extract_text(image)
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
            image = _capture_image()
            full_w, full_h = image.size
            cropped = _crop_region(image, region)
            off_x, off_y = (int(region[0]), int(region[1])) if region else (0, 0)
            elements = get_ocr().extract_elements(cropped, min_confidence=min_conf)
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
            image = _capture_image()
            img_w, img_h = image.size
            elements = get_ocr().extract_elements(image, min_confidence=min_conf)
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
            return [TextContent(type="text", text=json.dumps({
                "clicked": True, "text": el["text"],
                "x": sx, "y": sy, "match_count": len(matches),
            }, ensure_ascii=False))]

        elif name == "run_powershell_and_read":
            command = arguments["command"]
            wait_seconds = min(float(arguments.get("wait_seconds", 2.0)), MAX_WAIT_SECONDS)
            max_lines = int(arguments.get("max_lines", 30))
            max_chars = int(arguments.get("max_chars", 3000))
            ok, output, truncated = await _run_target_command(
                client, command, wait_seconds, max_lines, max_chars
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
            command = arguments["command"]
            distro = arguments.get("distro", "Ubuntu-24.04")
            wait_seconds = min(float(arguments.get("wait_seconds", 2.0)), MAX_WAIT_SECONDS)
            max_lines = int(arguments.get("max_lines", 30))
            max_chars = int(arguments.get("max_chars", 3000))
            ps_line = _build_wsl_command(command, distro)
            ok, output, truncated = await _run_target_command(
                client, ps_line, wait_seconds, max_lines, max_chars
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

"""Configuration for the MCP server (thin client)."""

import json
import logging
import os
import platform

logger = logging.getLogger(__name__)


def _default_capture_log_dir() -> str:
    """Return the platform-appropriate default directory for capture logs."""
    if platform.system() == "Windows":
        base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~/AppData/Local"))
    else:
        base = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    return os.path.join(base, "mcp-serial-hid-kvm", "captures")


def _default_evidence_dir() -> str:
    """Default directory for save_evidence screenshots (user-visible, not cache)."""
    if platform.system() == "Windows":
        return os.path.join(os.path.expanduser("~"), "Documents", "kvm-evidence")
    return os.path.join(os.path.expanduser("~"), "kvm-evidence")


def parse_targets(raw: str | None, default_host: str, default_port: int) -> dict:
    """Parse SHKVM_TARGETS JSON into {name: {host, port}}.

    Accepts ``{"aws-pc": "127.0.0.1:9329", "sap-pc": {"host": "...", "port": 9331}}``.
    Always contains a "default" entry pointing at SHKVM_API_HOST/PORT. Invalid
    entries are skipped with a warning; invalid JSON leaves only "default".
    """
    targets: dict = {"default": {"host": default_host, "port": default_port}}
    if not raw:
        return targets
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(f"Ignoring SHKVM_TARGETS (invalid JSON): {e}")
        return targets
    if not isinstance(data, dict):
        logger.warning("Ignoring SHKVM_TARGETS (must be a JSON object)")
        return targets
    for name, spec in data.items():
        try:
            if isinstance(spec, str):
                host, port = spec.rsplit(":", 1)
                targets[str(name)] = {"host": host, "port": int(port)}
            elif isinstance(spec, dict):
                targets[str(name)] = {"host": str(spec["host"]),
                                      "port": int(spec["port"])}
            else:
                raise ValueError("must be 'host:port' or {host, port}")
        except (KeyError, ValueError) as e:
            logger.warning(f"Ignoring SHKVM_TARGETS entry {name!r}: {e}")
    return targets


# Tools hidden from list_tools by default: superseded legacy tools and
# setup-time device management. Calls to them still work (hidden != disabled);
# override with SHKVM_HIDDEN_TOOLS (comma-separated, or "none" to show all).
DEFAULT_HIDDEN_TOOLS = frozenset({
    "execute_and_read",        # superseded by run_powershell_and_read
    "get_screen_text",         # superseded by get_screen_text_compact
    "screen_changed",          # superseded by wait_for_screen_change
    "send_key_sequence",       # covered by type_text {tags} + send_key
    "list_capture_devices",    # setup-time: use examples/ scripts instead
    "set_capture_device",
    "set_capture_resolution",
})


def parse_hidden_tools(raw: str | None) -> set:
    """Parse SHKVM_HIDDEN_TOOLS. None -> defaults; ''/'none' -> hide nothing."""
    if raw is None:
        return set(DEFAULT_HIDDEN_TOOLS)
    if raw.strip().lower() in ("", "none"):
        return set()
    return {t.strip() for t in raw.split(",") if t.strip()}


class Config:
    """Minimal configuration for the MCP thin-client server."""

    def __init__(self):
        # KVM server connection
        self.kvm_host: str = os.environ.get("SHKVM_API_HOST", "127.0.0.1")
        self.kvm_port: int = int(os.environ.get("SHKVM_API_PORT", "9329"))

        # Named KVM targets for select_target (multi-PC setups)
        self.targets: dict = parse_targets(
            os.environ.get("SHKVM_TARGETS"), self.kvm_host, self.kvm_port)

        # Local OCR
        self.tesseract_cmd: str | None = os.environ.get("SHKVM_OCR_CMD")

        # Capture log directory
        raw = os.environ.get("SHKVM_CAPTURE_LOG_DIR")
        if raw is None:
            self.capture_log_dir: str | None = _default_capture_log_dir()
        elif raw == "":
            self.capture_log_dir = None
        else:
            self.capture_log_dir = raw

        # Tools hidden from list_tools (still callable)
        self.hidden_tools: set = parse_hidden_tools(
            os.environ.get("SHKVM_HIDDEN_TOOLS"))

        # Evidence directory for save_evidence (empty string disables)
        raw_ev = os.environ.get("SHKVM_EVIDENCE_DIR")
        if raw_ev is None:
            self.evidence_dir: str | None = _default_evidence_dir()
        elif raw_ev == "":
            self.evidence_dir = None
        else:
            self.evidence_dir = raw_ev


config = Config()

"""Runtime configuration for the wrapper tools.

Holds tunable timing and operational defaults that ``configure`` can change at
runtime and ``get_timing`` can inspect. Effective values are computed in this
order (later wins):

1. Hard-coded ``DEFAULTS``.
2. JSON config file (see ``resolve_config_path``).
3. Environment variable overrides (``SHKVM_RT_<KEY>``).
4. Runtime overrides applied via ``configure``.

Config path resolution:

1. ``SHKVM_RUNTIME_CONFIG`` if set.
2. ``MCP_SERIAL_HID_KVM_RUNTIME_CONFIG`` if set.
3. ``%LOCALAPPDATA%\\mcp-serial-hid-kvm\\runtime-config.json`` (Windows) or the
   XDG equivalent elsewhere.

The file is never written under ``.venv``.
"""

import datetime
import json
import logging
import os

logger = logging.getLogger(__name__)

# Effective default values. Units are encoded in the key suffix:
#   *_ms     -> milliseconds (int)
#   *_seconds-> seconds (float)
#   *_lines / *_chars / *_radius -> count (int)
DEFAULTS: dict = {
    "default_wsl_distro": "Ubuntu-24.04",
    # OCR language for content tools (get_screen_text, get_screen_text_compact,
    # detect_text_elements, click_text). Keep CJK here.
    "ocr_lang": "eng+jpn",
    # OCR language for command/poll tools (run_*_and_read, execute_and_read,
    # wait_for_text, get_terminal_output). ASCII output -> eng is fast + clean.
    "ocr_fast_lang": "eng",
    # Optional default [x, y, w, h] region for get_terminal_output. null = full
    # screen (tail). Set a bottom-screen region to read only the prompt area.
    "terminal_region": None,
    # screen_changed/wait_for_screen_change: a pixel counts as "changed" when
    # its grayscale delta exceeds this (0-255); score = fraction of such pixels.
    "screen_diff_pixel_delta": 30,
    # Clear the target input line (PSReadLine Esc/RevertLine) before typing a
    # command, so back-to-back run_* calls do not concatenate onto leftover text.
    "clear_input_before_command": True,
    "click_hold_ms": 50,
    "click_after_ms": 100,
    "key_hold_ms": 30,
    "combo_mod_ms": 10,
    "type_key_ms": 20,
    "type_inter_key_ms": 20,
    "type_shift_ms": 10,
    "terminal_wait_seconds": 2.0,
    "wait_timeout_seconds": 30.0,
    "wait_poll_ms": 500,
    "screen_change_threshold": 0.02,
    "cursor_crop_radius": 150,
    "terminal_max_lines": 30,
    "terminal_max_chars": 3000,
    "open_shell_wait_seconds": 2.0,
    "max_wait_seconds": 60.0,
}

# (python type, min, max) per key. None bound = unbounded on that side.
_INT = int
_FLOAT = float
_SPECS: dict = {
    "default_wsl_distro": (str, None, None),
    "ocr_lang": (str, None, None),
    "ocr_fast_lang": (str, None, None),
    "terminal_region": ("region", None, None),
    "screen_diff_pixel_delta": (_INT, 0, 255),
    "clear_input_before_command": (bool, None, None),
    "click_hold_ms": (_INT, 0, 10000),
    "click_after_ms": (_INT, 0, 10000),
    "key_hold_ms": (_INT, 0, 10000),
    "combo_mod_ms": (_INT, 0, 10000),
    "type_key_ms": (_INT, 0, 10000),
    "type_inter_key_ms": (_INT, 0, 10000),
    "type_shift_ms": (_INT, 0, 10000),
    "terminal_wait_seconds": (_FLOAT, 0.0, 600.0),
    "wait_timeout_seconds": (_FLOAT, 0.0, 600.0),
    "wait_poll_ms": (_INT, 10, 60000),
    "screen_change_threshold": (_FLOAT, 0.0, 1.0),
    "cursor_crop_radius": (_INT, 1, 4096),
    "terminal_max_lines": (_INT, 1, 1000),
    "terminal_max_chars": (_INT, 1, 200000),
    "open_shell_wait_seconds": (_FLOAT, 0.0, 600.0),
    "max_wait_seconds": (_FLOAT, 1.0, 600.0),
}

# Map config keys (ms) -> serial-hid-kvm timing keys (seconds).
_HW_TIMING_MAP = {
    "type_inter_key_ms": "char_delay",
    "type_key_ms": "type_key_hold",
    "key_hold_ms": "key_hold",
    "combo_mod_ms": "combo_mod",
    "type_shift_ms": "type_shift",
    "click_hold_ms": "click_hold",
    "click_after_ms": "click_after",
}

_ENV_PREFIX = "SHKVM_RT_"


def resolve_config_path() -> str:
    explicit = (
        os.environ.get("SHKVM_RUNTIME_CONFIG")
        or os.environ.get("MCP_SERIAL_HID_KVM_RUNTIME_CONFIG")
    )
    if explicit:
        return explicit
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~/AppData/Local"))
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(base, "mcp-serial-hid-kvm", "runtime-config.json")


def _coerce(key: str, value):
    """Coerce/validate a single value. Raises ValueError on bad key/value."""
    if key not in _SPECS:
        raise ValueError(f"unknown config key: {key}")
    typ, lo, hi = _SPECS[key]
    if typ == "region":
        if value is None:
            return None
        if (isinstance(value, (list, tuple)) and len(value) == 4
                and all(isinstance(v, (int, float)) for v in value)):
            return [int(v) for v in value]
        raise ValueError(f"{key} must be null or [x, y, w, h]")
    if typ is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("true", "false"):
            return value.strip().lower() == "true"
        if value in (0, 1):
            return bool(value)
        raise ValueError(f"{key} must be a boolean")
    if typ is str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string")
        return value
    try:
        coerced = typ(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be {typ.__name__}")
    if lo is not None and coerced < lo:
        raise ValueError(f"{key} must be >= {lo}")
    if hi is not None and coerced > hi:
        raise ValueError(f"{key} must be <= {hi}")
    return coerced


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


class RuntimeConfig:
    """Effective runtime config with layered loading and runtime overrides."""

    def __init__(self, path: str | None = None):
        self.config_path = path or resolve_config_path()
        self._values: dict = dict(DEFAULTS)
        self.file_loaded = False
        self.env_keys: list[str] = []
        self.runtime_keys: set[str] = set()
        self.loaded_at = _now()
        self.updated_at = self.loaded_at
        self._load_file()
        self._load_env()

    # -- loading -------------------------------------------------------------

    def _load_file(self):
        try:
            with open(self.config_path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Ignoring runtime config {self.config_path}: {e}")
            return
        if not isinstance(data, dict):
            logger.warning(f"Runtime config {self.config_path} is not an object")
            return
        for key, value in data.items():
            try:
                self._values[key] = _coerce(key, value)
            except ValueError as e:
                logger.warning(f"Ignoring config key from file: {e}")
        self.file_loaded = True

    def _load_env(self):
        for key in DEFAULTS:
            env_name = _ENV_PREFIX + key.upper()
            if env_name in os.environ:
                try:
                    self._values[key] = _coerce(key, os.environ[env_name])
                    self.env_keys.append(key)
                except ValueError as e:
                    logger.warning(f"Ignoring env override {env_name}: {e}")

    # -- accessors -----------------------------------------------------------

    def get(self, key: str):
        return self._values[key]

    def as_dict(self) -> dict:
        return dict(self._values)

    def hardware_timing_seconds(self) -> dict:
        """Return the serial-hid-kvm timing dict (seconds) derived from config."""
        return {
            hw_key: self._values[cfg_key] / 1000.0
            for cfg_key, hw_key in _HW_TIMING_MAP.items()
        }

    def source(self) -> dict:
        return {
            "defaults": True,
            "file": self.file_loaded,
            "file_path": self.config_path,
            "env_overrides": list(self.env_keys),
            "runtime_overrides": sorted(self.runtime_keys),
        }

    # -- mutation ------------------------------------------------------------

    def validate(self, values: dict) -> tuple[dict, list[str]]:
        clean: dict = {}
        errors: list[str] = []
        for key, value in values.items():
            try:
                clean[key] = _coerce(key, value)
            except ValueError as e:
                errors.append(str(e))
        return clean, errors

    def update(self, values: dict) -> dict:
        """Validate and apply *values* to memory. Returns the changed subset.

        Raises ValueError (with all messages) if any value is invalid; nothing
        is applied in that case.
        """
        clean, errors = self.validate(values)
        if errors:
            raise ValueError("; ".join(errors))
        changed = {}
        for key, value in clean.items():
            if self._values.get(key) != value:
                changed[key] = value
            self._values[key] = value
            self.runtime_keys.add(key)
        if changed:
            self.updated_at = _now()
        return changed

    def reset(self):
        self._values = dict(DEFAULTS)
        self.runtime_keys.clear()
        self.updated_at = _now()

    def save(self) -> str:
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self._values, f, indent=2, ensure_ascii=False)
        return self.config_path

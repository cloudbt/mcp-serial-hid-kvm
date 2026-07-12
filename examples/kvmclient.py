"""Minimal stdlib-only client for the serial-hid-kvm TCP JSON Lines API.

No third-party dependencies (works with any Python >= 3.10, including a bare
system install). Protocol: one JSON object per line over TCP, response is
``{"id", "ok", "result"|"error"}``.

Usage:
    from kvmclient import Kvm, connect

    with Kvm("127.0.0.1", 9329) as kvm:
        kvm.type_text("dir{enter}")

    with connect("aws-pc") as kvm:      # named target from SHKVM_TARGETS
        print(kvm.ping())
"""

import base64
import json
import os
import socket
import uuid


class KvmError(RuntimeError):
    """Server-side error or connection failure."""


class Kvm:
    def __init__(self, host: str = "127.0.0.1", port: int = 9329,
                 timeout: float = 30.0):
        self.host, self.port = host, port
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._rfile = self._sock.makefile("r", encoding="utf-8")

    # -- context manager ------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        try:
            self._rfile.close()
            self._sock.close()
        except OSError:
            pass

    # -- core RPC ---------------------------------------------------------
    def call(self, method: str, **params) -> dict:
        req = {"id": uuid.uuid4().hex[:8], "method": method, "params": params}
        self._sock.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        line = self._rfile.readline()
        if not line:
            raise KvmError("server closed connection")
        resp = json.loads(line)
        if not resp.get("ok"):
            raise KvmError(resp.get("error", "unknown error"))
        return resp.get("result", {})

    # -- convenience wrappers ----------------------------------------------
    def ping(self) -> dict:
        return self.call("ping")

    def type_text(self, text: str, raw: bool = False,
                  char_delay_ms: int | None = None) -> dict:
        params: dict = {"text": text}
        if raw:
            params["raw"] = True
        if char_delay_ms is not None:
            params["char_delay_ms"] = char_delay_ms
        return self.call("type_text", **params)

    def send_key(self, key: str, modifiers: list[str] | None = None) -> dict:
        return self.call("send_key", key=key, modifiers=modifiers or [])

    def mouse_click(self, button: str = "left",
                    x: int | None = None, y: int | None = None) -> dict:
        params: dict = {"button": button}
        if x is not None:
            params["x"] = x
        if y is not None:
            params["y"] = y
        return self.call("mouse_click", **params)

    def capture_jpeg(self, quality: int = 85) -> tuple[bytes, int, int]:
        """Return (jpeg_bytes, width, height)."""
        r = self.call("capture_frame", quality=quality)
        return base64.b64decode(r["jpeg_b64"]), r["width"], r["height"]

    def save_screenshot(self, path: str, quality: int = 90) -> str:
        data, _w, _h = self.capture_jpeg(quality)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def device_info(self) -> dict:
        return self.call("get_device_info")

    def set_timing(self, **timing) -> dict:
        """Keys (seconds): char_delay, type_key_hold, key_hold, combo_mod,
        type_shift, click_hold, click_after."""
        return self.call("set_timing", **timing)

    def get_timing(self) -> dict:
        return self.call("get_timing")


# ---------------------------------------------------------------------------
# Named targets — same convention as the MCP server's SHKVM_TARGETS:
#   SHKVM_TARGETS={"aws-pc": "127.0.0.1:9329", "sap-pc": "127.0.0.1:9331"}
# ---------------------------------------------------------------------------

def targets_from_env() -> dict[str, tuple[str, int]]:
    host = os.environ.get("SHKVM_API_HOST", "127.0.0.1")
    port = int(os.environ.get("SHKVM_API_PORT", "9329"))
    targets = {"default": (host, port)}
    raw = os.environ.get("SHKVM_TARGETS")
    if raw:
        try:
            for name, spec in json.loads(raw).items():
                if isinstance(spec, str):
                    h, p = spec.rsplit(":", 1)
                    targets[name] = (h, int(p))
                elif isinstance(spec, dict):
                    targets[name] = (str(spec["host"]), int(spec["port"]))
        except (ValueError, KeyError) as e:
            print(f"warning: bad SHKVM_TARGETS entry ignored: {e}")
    return targets


def connect(target: str = "default", timeout: float = 30.0) -> Kvm:
    """Connect to a named target ('default', 'aws-pc', ...) or 'host:port'."""
    targets = targets_from_env()
    if target in targets:
        host, port = targets[target]
    elif ":" in target:
        host, p = target.rsplit(":", 1)
        port = int(p)
    else:
        raise KvmError(f"unknown target {target!r}; known: {sorted(targets)}")
    return Kvm(host, port, timeout)

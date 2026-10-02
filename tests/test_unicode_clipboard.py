"""Clipboard regressions using simulated HID/OCR and isolated PowerShell mocks."""

import asyncio
import hashlib
import shutil
import subprocess
from unittest.mock import AsyncMock

import pytest

import mcp_serial_hid_kvm.server as srv
from mcp_serial_hid_kvm.runtime_config import DEFAULTS


CALL_ID = "acde23456789abcd"
PROMPT = r"PS C:\test>"
TEXT = "日本語\n中文 😀\r\n"


def records(text=TEXT, call_id=CALL_ID):
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    prefix = "KVMCLIP_" + call_id
    return [f"{prefix} OK chars={len(text.encode('utf-16-le')) // 2}",
            f"{prefix} HASH1={sha[:32]}", f"{prefix} HASH2={sha[32:]}",
            f"{prefix} DONE"]


def output(lines):
    return "\n".join([*lines, PROMPT])


class Client:
    def __init__(self):
        self.keys = []
        self.commands = []
        self.timings = []
        self.old_timing = {"char_delay": 0.02, "type_key_hold": 0.02, "type_shift": 0.01}

    def send_key(self, key, modifiers=None):
        self.keys.append((key, modifiers))

    def type_text(self, text, raw=False):
        assert raw is True
        self.commands.append(text)

    def get_timing(self):
        return self.old_timing

    def set_timing(self, timing):
        self.timings.append(timing)


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(srv.secrets, "token_hex", lambda _: CALL_ID)
    monkeypatch.setattr(srv, "get_config", lambda: type("Config", (), {"get": lambda _, k: DEFAULTS[k]})())
    monkeypatch.setattr(srv.asyncio, "sleep", AsyncMock())
    client = Client()
    frames = [PROMPT, output(["KVMASCII_" + CALL_ID]), output(records())]

    def ocr(*args, **kwargs):
        return frames.pop(0) if len(frames) > 1 else frames[0]

    monkeypatch.setattr(srv, "_ocr_region_text", ocr)
    return client, frames


def run(client, **kwargs):
    return asyncio.run(srv._do_paste_unicode_text(
        client, text=TEXT, focus_shell=False, wait_seconds=0,
        verify_timeout_seconds=0, **kwargs))


@pytest.mark.parametrize("timing", [{}, {"type_key_ms": 15, "type_inter_key_ms": 15},
                                  {"fast_timing": False}])
def test_verified_readback_before_paste_and_timing_restored(setup, timing):
    client, _ = setup
    result = run(client, paste_after_set=True, restore_focus_with_alt_tab=True, **timing)
    assert result["ok"] and result["verified"] and result["set_clipboard"] and result["pasted"]
    assert result["call_id"] == CALL_ID
    assert result["utf16_chars"] == len(TEXT) + 1
    assert client.keys[-2:] == [("tab", ["alt"]), ("v", ["ctrl"])]
    assert len(client.commands) == 2  # probe precedes clipboard mutation
    if timing.get("fast_timing") is False:
        assert not client.timings
    else:
        assert client.timings[-1] == client.old_timing
        assert client.timings[0]["char_delay"] == timing.get("type_inter_key_ms", 5) / 1000


def test_clipboard_only_success_does_not_paste_or_exit_shell(setup):
    client, _ = setup
    result = run(client)
    assert result["ok"] and result["set_clipboard"] and not result["pasted"]
    assert ("v", ["ctrl"]) not in client.keys
    assert "exit" not in client.commands[-1]


@pytest.mark.parametrize("bad_frame", [
    "Write-Output ('PASTE_UNICODE_OK chars='+$s.Length);exit",
    output(records(call_id="1111111111111111")),  # previous call
    output(records(text="old clipboard")),
    output([line.replace("HASH1=", "HASH1=f") for line in records()]),
    output([records()[0].replace("chars=", "chars=9"), *records()[1:]]),
    "\n".join(records()),  # command completion is unobserved
    output(records()[:-1]),  # missing DONE
    output(["PS C:\\test> " + records()[0], *records()[1:]]),
    "[OCR Error: unavailable]",
    output(["KVMCLIP_" + CALL_ID + " FAIL", "KVMCLIP_" + CALL_ID + " DONE"]),
])
def test_verification_failures_never_restore_focus_or_paste(setup, bad_frame):
    client, frames = setup
    frames[-1] = bad_frame
    result = run(client, paste_after_set=True, restore_focus_with_alt_tab=True)
    assert not any(result[k] for k in ("ok", "verified", "set_clipboard", "pasted"))
    assert result["failed_stage"] == "verify_clipboard"
    assert ("tab", ["alt"]) not in client.keys
    assert ("v", ["ctrl"]) not in client.keys
    assert client.timings[-1] == client.old_timing


@pytest.mark.parametrize("bad_probe", [
    "Ｗｒｉｔｅ－Ｏｕｔｐｕｔ　（かな）",  # full-width/kana IME
    "Write-Output (@KVM@+@ASCII_acde23456789abcd@)",  # keyboard layout mismatch
    "PS C:\\test> Write-Output ('KVM'+'ASCII_acde23456789abcd')",
    output(["KVMASCII_1111111111111111"]),
])
def test_ascii_probe_failure_blocks_clipboard_command(setup, bad_probe):
    client, frames = setup
    frames[:] = [PROMPT, bad_probe]
    result = run(client, paste_after_set=True)
    assert result["failed_stage"] == "prepare_input"
    assert result["error"] == "ascii_input_not_verified"
    assert not result["ok"] and not result["set_clipboard"] and not result["pasted"]
    assert len(client.commands) == 1
    assert "Set-Clipboard" not in client.commands[0]
    assert client.keys[:2] == [("escape", None), ("escape", None)]


def test_ocr_exception_during_verification_fails_closed(setup, monkeypatch):
    client, frames = setup
    def ocr(*args, **kwargs):
        if frames:
            return frames.pop(0)
        raise RuntimeError("capture failed")
    frames.pop()
    monkeypatch.setattr(srv, "_ocr_region_text", ocr)
    result = run(client, paste_after_set=True)
    assert result["detail"] == "ocr_failed"
    assert not result["ok"] and not result["pasted"]


def test_dry_run_has_no_observation_or_hid(setup, monkeypatch):
    client, _ = setup
    monkeypatch.setattr(srv, "_ocr_region_text", lambda *a, **kw: pytest.fail("dry-run captured"))
    result = run(client, dry_run=True)
    assert result["ok"] and not result["verified"] and not result["set_clipboard"]
    assert not client.keys and not client.commands and not client.timings


def test_missing_shell_stops_before_typing(setup):
    client, frames = setup
    frames[:] = ["Some other application"]
    result = run(client)
    assert result["failed_stage"] == "open_shell"
    assert not client.keys and not client.commands


def test_open_shell_must_be_verified(setup, monkeypatch):
    client, _ = setup
    open_shell = AsyncMock(return_value={"ok": True, "verified": False})
    monkeypatch.setattr(srv, "_do_open_shell", open_shell)
    result = asyncio.run(srv._do_paste_unicode_text(client, text=TEXT))
    assert result["error"] == "shell_not_verified"
    assert not client.commands
    assert open_shell.call_args.kwargs["verify"] is True


def test_echo_never_contains_verifiable_records():
    command, _, _ = srv._paste_unicode_build_command(TEXT, CALL_ID)
    assert "KVMCLIP_" + CALL_ID not in command
    assert "PASTE_UNICODE_OK" not in command
    assert not srv._unicode_output_complete(command + "\n" + PROMPT, records())
    assert all(len(line) < 80 for line in records())


@pytest.mark.parametrize("text", ["日本語", "中文 😀\r\n", "a\n\nb\n", "quotes ' \" $ ;", "x"])
@pytest.mark.parametrize("readback", ["correct", "stale", "set_failure", "get_failure"])
def test_generated_powershell_executes_with_mocked_clipboard(text, readback):
    # No real clipboard or KVM access: both cmdlets are overridden in a child
    # process, testing actual PowerShell syntax and UTF-8/UTF-16/hash semantics.
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell not available for command integration test")
    command, _, _ = srv._paste_unicode_build_command(text, CALL_ID)
    setter = "$script:clip=$Value" if readback != "set_failure" else "throw 'set failed'"
    getter = {"correct": "$script:clip", "stale": "'old clipboard'",
              "set_failure": "$script:clip", "get_failure": "throw 'get failed'"}[readback]
    script = (
        "function Set-Clipboard {param([string]$Value,[string]$ErrorAction);" + setter + "};"
        "function Get-Clipboard {param([switch]$Raw,[string]$ErrorAction);" + getter + "};"
        + command
    )
    proc = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0, proc.stderr
    expected = records(text) if readback == "correct" else [
        "KVMCLIP_" + CALL_ID + " FAIL", "KVMCLIP_" + CALL_ID + " DONE"]
    assert proc.stdout.splitlines() == expected


def test_polling_accepts_delayed_output(setup, monkeypatch):
    _, frames = setup
    frames[:] = ["still running", output(records())]
    assert asyncio.run(srv._wait_unicode_output(records(), 1)) == "verified"


def test_new_calls_have_distinct_ids():
    a, _, _ = srv._paste_unicode_build_command("a")
    b, _, _ = srv._paste_unicode_build_command("a")
    assert a != b


@pytest.mark.parametrize("layout", ["us104", "jp106"])
def test_probe_and_clipboard_commands_are_typeable_on_supported_layouts(layout):
    from serial_hid_kvm.hid_keycodes import build_char_map
    mapping = build_char_map(layout)
    command, _, _ = srv._paste_unicode_build_command(TEXT, CALL_ID)
    probe = f"Write-Output ('KVM'+'ASCII_{CALL_ID}')"
    assert all(char in mapping for char in command + probe)


def test_initial_ocr_error_prevents_input(setup):
    client, frames = setup
    frames[:] = ["[OCR Error: no video]"]
    result = run(client, paste_after_set=True)
    assert result["failed_stage"] == "observe_screen"
    assert not any(result[k] for k in ("ok", "verified", "set_clipboard", "pasted"))
    assert not client.commands and not client.keys
    assert client.timings[-1] == client.old_timing


def test_input_exception_returns_failure_and_restores_timing(setup, monkeypatch):
    client, _ = setup
    def fail(*args, **kwargs):
        raise srv.KvmClientError("serial disconnected")
    monkeypatch.setattr(client, "type_text", fail)
    result = run(client, paste_after_set=True)
    assert result["error"] == "operation_failed"
    assert result["failed_stage"] == "prepare_input"
    assert not result["set_clipboard"] and not result["pasted"]
    assert client.timings[-1] == client.old_timing


def test_public_schema_and_dispatch_forward_verification_timeout(setup, monkeypatch):
    client, _ = setup
    tool = next(tool for tool in asyncio.run(srv.list_tools()) if tool.name == "paste_unicode_text")
    assert "verify_timeout_seconds" in tool.inputSchema["properties"]
    operation = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(srv, "get_client", lambda: client)
    monkeypatch.setattr(srv, "_do_paste_unicode_text", operation)
    asyncio.run(srv.call_tool("paste_unicode_text", {"text": TEXT, "verify_timeout_seconds": 2.5}))
    assert operation.call_args.kwargs["verify_timeout_seconds"] == 2.5

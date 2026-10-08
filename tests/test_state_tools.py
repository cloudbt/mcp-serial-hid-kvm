"""MCP integration boundaries with mocked captures and TCP clients."""

import asyncio
import json
import subprocess
import sys
from unittest.mock import Mock

import pytest
from PIL import Image

import mcp_serial_hid_kvm.server as srv
from mcp_serial_hid_kvm.runtime_config import RuntimeConfig
from mcp_serial_hid_kvm.vision import StateEngine

REMOVED = {
    "get_screen_text", "get_screen_text_compact", "detect_text_elements", "click_text",
    "wait_for_text", "get_terminal_output", "run_powershell_and_read", "run_wsl_and_read",
    "execute_and_read", "run_powershell_until_done", "run_task_and_report",
}


@pytest.fixture
def context(monkeypatch, tmp_path):
    client = Mock()
    monkeypatch.setattr(srv, "_client", client)
    monkeypatch.setattr(srv, "_state", StateEngine())
    monkeypatch.setattr(srv, "_input_lock", None)
    monkeypatch.setattr(srv, "_current_target", dict(srv._current_target))
    monkeypatch.setattr(srv, "_screen_size", None)
    monkeypatch.setattr(srv, "_cursor_pos", None)
    monkeypatch.setattr(srv, "_focused_shell_hint", None)
    monkeypatch.setattr(srv, "_runtime", RuntimeConfig(str(tmp_path / "config.json")))
    monkeypatch.setattr(srv.config, "capture_log_dir", None)
    monkeypatch.setattr(srv, "_capture_image", lambda: Image.new("RGB", (640, 480)))
    return client


def call(name, **arguments):
    result = asyncio.run(srv.call_tool(name, arguments))
    return json.loads(result[0].text)


def test_removed_tools_cannot_be_listed_even_when_unhidden(monkeypatch):
    monkeypatch.setattr(srv.config, "hidden_tools", set())
    names = {t.name for t in asyncio.run(srv.list_tools())}
    assert names.isdisjoint(REMOVED)
    assert {"wait_for_change", "wait_for_stable", "get_changed_regions"} <= names


@pytest.mark.parametrize("name", sorted(REMOVED))
def test_removed_tools_have_no_dispatch_or_connection(name, monkeypatch):
    client = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(srv, "get_client", client)
    monkeypatch.setattr(srv.config, "hidden_tools", REMOVED)
    assert asyncio.run(srv.call_tool(name, {}))[0].text == f"Unknown tool: {name}"
    client.assert_not_called()


def test_capture_screen_seeds_pre_action_baseline(context):
    result = asyncio.run(srv.call_tool("capture_screen", {}))
    assert result[0].type == "image" and srv._state.baseline is not None


@pytest.mark.parametrize("name", ["wait_for_change", "wait_for_stable", "get_changed_regions"])
def test_state_tools_work_under_input_lock(name, context):
    srv._do_set_input_lock(locked=True, reason="read-only")
    srv._state.set_baseline(Image.new("RGB", (640, 480)))
    args = {"timeout_seconds": 0} if name.startswith("wait") else {}
    result = call(name, **args)
    assert "error" not in result
    context.send_key.assert_not_called()
    context.type_text.assert_not_called()


def test_stable_tool_actual_success_under_lock(context):
    srv._do_set_input_lock(locked=True)
    result = call("wait_for_stable", stable_frames=2, poll_ms=10, timeout_seconds=1)
    assert result["stable"] and result["attempts"] == 3


def test_compatibility_aliases_use_same_engine(context):
    call("set_screen_baseline")
    assert call("screen_changed") == call("get_changed_regions")
    old = call("wait_for_screen_change", timeout_seconds=0)
    new = call("wait_for_change", timeout_seconds=0)
    old.pop("elapsed_ms")
    new.pop("elapsed_ms")
    assert old == new


def test_successful_select_target_aborts_wait_without_comparing_new_target(context, monkeypatch):
    srv._state.set_baseline(Image.new("RGB", (640, 480)))
    captures = Mock(return_value=Image.new("RGB", (640, 480)))
    monkeypatch.setattr(srv, "_capture_image", captures)
    monkeypatch.setattr(srv, "KvmClient", Mock(return_value=Mock()))
    async def scenario():
        task = asyncio.create_task(srv.call_tool("wait_for_change", {"poll_ms": 20}))
        await asyncio.sleep(0)
        assert srv._do_select_target(host="127.0.0.1", port=9331)["ok"]
        return json.loads((await task)[0].text)
    result = asyncio.run(scenario())
    assert result["error"] == "context_changed" and captures.call_count == 1


def test_health_needs_only_api_serial_video(context):
    context.get_device_info.return_value = {"serial": {"connected": True},
                                           "capture": {"device": 0, "width": 640, "height": 480}}
    result = call("health")
    assert result["ok"] and "ocr" not in result


def test_unicode_auto_paste_refused_before_hid(context):
    result = call("paste_unicode_text", text="test", paste_after_set=True)
    assert result["error"] == "vision_verification_required"
    context.send_key.assert_not_called()
    context.type_text.assert_not_called()


def test_unicode_transfer_reports_unverified_delivery(context, monkeypatch):
    async def no_wait(seconds):
        pass
    monkeypatch.setattr(srv.asyncio, "sleep", no_wait)
    result = call("transfer_unicode_file", text="test", target_path="C:\\Temp\\x.txt", focus_shell=False)
    assert result["write_requested"] and result["written"] is None and not result["verified"]
    assert result["verification"] == "calling_model_required"
    context.set_timing.assert_called()


@pytest.mark.parametrize("tool,args", [
    ("paste_unicode_text", {"text": "test", "focus_shell": False}),
    ("transfer_unicode_file", {"text": "test", "target_path": "C:\\Temp\\x.txt", "focus_shell": False}),
])
def test_unicode_typing_failure_restores_timing(tool, args, context, monkeypatch):
    async def no_wait(seconds):
        pass
    monkeypatch.setattr(srv.asyncio, "sleep", no_wait)
    timing = {"char_delay": 0.02, "type_key_hold": 0.02}
    context.get_timing.return_value = timing
    context.type_text.side_effect = srv.KvmClientError("typing failed")
    result = asyncio.run(srv.call_tool(tool, args))
    assert "typing failed" in result[0].text
    assert context.set_timing.call_args.args == (timing,)


def test_unicode_clipboard_has_no_implicit_paste(context, monkeypatch):
    async def no_wait(seconds):
        pass
    monkeypatch.setattr(srv.asyncio, "sleep", no_wait)
    result = call("paste_unicode_text", text="test", focus_shell=False)
    assert result["clipboard_requested"] and result["set_clipboard"] is None and not result["verified"]
    assert not result["pasted"]
    assert all(c.args[0] != "v" for c in context.send_key.call_args_list)


def test_import_and_registration_work_with_all_ocr_imports_blocked():
    code = '''
import sys, importlib.abc, asyncio
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if 'pytesseract' in fullname or fullname.endswith('.ocr'):
            raise AssertionError('OCR import attempted: ' + fullname)
sys.meta_path.insert(0, Block())
from mcp_serial_hid_kvm.server import list_tools
assert asyncio.run(list_tools())
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

"""Action + observation integration with fake transport, frames and clock only."""

import asyncio
import base64
import io
import inspect
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PIL import Image

import mcp_serial_hid_kvm.server as srv
from mcp_serial_hid_kvm.config import Config
from mcp_serial_hid_kvm.runtime_config import RuntimeConfig
from mcp_serial_hid_kvm.vision import StateEngine


def jpeg(color=90):
    buffer = io.BytesIO()
    Image.new("RGB", (640, 480), (color,) * 3).save(buffer, format="JPEG")
    return buffer.getvalue(), 640, 480


@pytest.fixture
def context(monkeypatch, tmp_path):
    client = Mock()
    client.capture_frame_jpeg.return_value = jpeg()
    monkeypatch.setattr(srv, "_client", client)
    monkeypatch.setattr(srv, "_state", StateEngine())
    monkeypatch.setattr(srv, "_screen_size", (1280, 960))
    monkeypatch.setattr(srv, "_cursor_pos", None)
    monkeypatch.setattr(srv, "_input_lock", None)
    monkeypatch.setattr(srv, "_current_target", {"name": "fake", "host": "localhost", "port": 9329})
    monkeypatch.setattr(srv, "_runtime", RuntimeConfig(str(tmp_path / "runtime.json")))
    log = Mock(side_effect=AssertionError("after must never save a capture"))
    monkeypatch.setattr(srv, "_save_capture_log", log)
    clock = SimpleNamespace(now=0., on_sleep=None)

    async def sleep(seconds):
        clock.now += seconds
        if clock.on_sleep:
            pending = clock.on_sleep()
            if inspect.isawaitable(pending):
                await pending

    monkeypatch.setattr(srv, "time", SimpleNamespace(monotonic=lambda: clock.now))
    monkeypatch.setattr(srv.asyncio, "sleep", sleep)
    stable = srv._state.wait_for_stable

    async def wait(capture, **kwargs):
        return await stable(capture, clock=lambda: clock.now, sleep=sleep, **kwargs)

    monkeypatch.setattr(srv._state, "wait_for_stable", wait)
    return client, clock


def call(name="mouse_scroll", **arguments):
    if name == "mouse_scroll":
        arguments.setdefault("amount", -5)
    content = asyncio.run(srv.call_tool(name, arguments))
    return content, json.loads(content[-1].text)


@pytest.mark.parametrize("name,args,method,text", [
    ("mouse_scroll", {"amount": 5}, "mouse_scroll", "Scrolled up by 5"),
    ("mouse_click", {"x": 10, "y": 20}, "mouse_click", "Clicked left at (10, 20)"),
    ("send_key", {"key": "enter"}, "send_key", "Sent: enter"),
    ("type_text", {"text": "hello"}, "type_text", "Typed 5 characters"),
])
def test_legacy_and_after_for_all_actions(context, name, args, method, text):
    client, _ = context
    legacy = asyncio.run(srv.call_tool(name, args))
    assert len(legacy) == 1
    assert json.loads(legacy[0].text) == {"ok": True, "tool": name, "sent": True, "message": text}
    client.capture_frame_jpeg.assert_not_called()
    client.reset_mock()
    content, result = call(name, **args, after={"wait": {"mode": "none"}})
    assert [item.type for item in content] == ["image", "text"]
    assert result["ok"] and result["action"]["sent"] is True
    assert result["action"]["message"] == text
    assert not result["verified"]
    assert getattr(client, method).call_count == 1
    assert result["park_cursor"]["status"] == "skipped"
    assert result["wait"]["status"] == "skipped"
    client.capture_frame_jpeg.assert_called_once()


def test_stable_reuses_exact_last_jpeg_and_full_baseline(context):
    client, _ = context
    frames = [jpeg(0), jpeg(255), jpeg(255), jpeg(255)]
    client.capture_frame_jpeg.side_effect = frames
    content, result = call(after={"wait": {"stable_frames": 2}})
    assert result["ok"] and result["wait"]["attempts"] == 4
    assert client.capture_frame_jpeg.call_count == 4  # no fifth capture
    assert base64.b64decode(content[0].data) == frames[-1][0]
    assert result["screenshot"]["source"] == "wait_last_frame"
    assert srv._state.baseline.shape[:2] == (480, 640)
    assert srv._state.baseline.min() == 255


def test_default_after_uses_runtime_stability_options(context):
    client, _ = context
    srv.get_config().update({"screen_stable_frames": 2, "wait_poll_ms": 30})
    _, result = call(after={})
    assert result["wait"]["stable"] and result["wait"]["attempts"] == 3
    assert result["wait"]["elapsed_ms"] == pytest.approx(60)
    assert result["screenshot"]["elapsed_ms"] == 0
    assert result["elapsed_ms"] == pytest.approx(60)
    assert client.capture_frame_jpeg.call_count == 3


def test_park_then_wait_then_crop_with_hid_mapping(context):
    client, _ = context
    content, result = call(x=1000, y=500, after={
        "park_cursor": {"x": 98, "y": 50},
        "wait": {"stable_frames": 1},
        "screenshot": {"region": [100, 50, 200, 100], "zoom": 2, "max_width": 300},
    })
    names = [c[0] for c in client.method_calls]
    assert names == ["capture_frame_jpeg", "mouse_move", "mouse_scroll", "mouse_move",
                     "capture_frame_jpeg", "capture_frame_jpeg"]
    assert client.mouse_move.call_args_list[-1].args == (98, 50)
    assert srv._cursor_pos == (98, 50)
    assert result["screenshot"]["image"] == "300x150"
    assert result["screenshot"]["to_hid"] == "hid_x = 200 + image_x * 1.3333; hid_y = 100 + image_y * 1.3333"
    assert Image.open(io.BytesIO(base64.b64decode(content[0].data))).size == (300, 150)
    assert srv._state.baseline.shape[:2] == (480, 640)


@pytest.mark.parametrize("wait", [{"timeout_seconds": 0}, {"timeout_seconds": .3, "poll_ms": 100, "stable_frames": 5}])
def test_timeout_returns_last_frame_without_repeating_action(context, wait):
    client, _ = context
    content, result = call(after={"wait": wait})
    assert not result["ok"] and result["wait"]["status"] == "timeout"
    assert result["wait"]["timed_out"] and not result["wait"]["stable"]
    assert result["action"]["sent"] is True
    assert result["screenshot"]["status"] == "ok" and content[0].type == "image"
    assert result["screenshot"]["source"] == "wait_last_frame"
    assert client.capture_frame_jpeg.call_count == result["wait"]["attempts"]
    client.mouse_scroll.assert_called_once()


def test_delay_and_phase_timings(context):
    client, clock = context
    def action(amount):
        clock.now += .025
    def capture(quality):
        clock.now += .045
        return jpeg()
    client.mouse_scroll.side_effect = action
    client.capture_frame_jpeg.side_effect = capture
    _, result = call(after={"wait": {"mode": "delay", "seconds": .2}})
    assert result["action"]["elapsed_ms"] == pytest.approx(25)
    assert result["wait"]["elapsed_ms"] == pytest.approx(200)
    assert result["screenshot"]["elapsed_ms"] == pytest.approx(45)
    assert result["elapsed_ms"] == pytest.approx(270)


def test_capture_failure_preserves_action_and_does_not_retry(context):
    client, _ = context
    client.capture_frame_jpeg.side_effect = srv.KvmClientError("no video")
    _, result = call(after={"wait": {"mode": "none"}})
    assert result["action"]["status"] == "ok" and result["action"]["sent"] is True
    assert result["screenshot"]["status"] == "failed"
    assert "no video" in result["screenshot"]["error"] and not result["ok"]
    client.mouse_scroll.assert_called_once()
    client.capture_frame_jpeg.assert_called_once()


def test_wait_capture_failure_still_attempts_current_screenshot(context):
    client, _ = context
    client.capture_frame_jpeg.side_effect = [jpeg(), srv.KvmClientError("poll failed"), jpeg(255)]
    content, result = call(after={})
    assert result["wait"]["status"] == "failed"
    assert result["screenshot"]["status"] == "ok"
    assert result["screenshot"]["source"] == "capture"
    assert base64.b64decode(content[0].data) == jpeg(255)[0]
    assert result["action"]["sent"] is True
    client.mouse_scroll.assert_called_once()


def test_wait_and_screenshot_both_fail_without_losing_action(context):
    client, _ = context
    client.capture_frame_jpeg.side_effect = srv.KvmClientError("no video")
    _, result = call("send_key", key="enter", after={})
    assert result["action"]["sent"] is True
    assert result["wait"]["status"] == result["screenshot"]["status"] == "failed"
    assert result["wait"]["error"] == result["screenshot"]["error"] == "no video"
    assert client.capture_frame_jpeg.call_count == 2
    client.send_key.assert_called_once_with("enter", [])


def test_delay_is_capped_by_runtime_maximum(context):
    _, clock = context
    srv.get_config().update({"max_wait_seconds": 1})
    _, result = call(after={"wait": {"mode": "delay", "seconds": 600}})
    assert result["ok"] and clock.now == 1


def test_stable_time_includes_capture_but_screenshot_does_not(context):
    client, clock = context
    def capture(quality):
        clock.now += .02
        return jpeg()
    client.capture_frame_jpeg.side_effect = capture
    _, result = call(after={"wait": {"stable_frames": 1}})
    assert result["wait"]["elapsed_ms"] == pytest.approx(240)
    assert result["screenshot"]["elapsed_ms"] == 0
    assert result["elapsed_ms"] == pytest.approx(240)


def test_grid_uses_last_frame_without_extra_capture(context):
    client, _ = context
    _, result = call(after={"wait": {"stable_frames": 1}, "screenshot": {"grid": True}})
    assert result["screenshot"]["grid_step_hid"] > 0
    assert result["screenshot"]["source"] == "wait_last_frame"
    assert client.capture_frame_jpeg.call_count == 2


def test_encoding_failure_after_stable_preserves_wait_and_action(context, monkeypatch):
    client, _ = context
    monkeypatch.setattr(srv, "_encode_capture_jpeg", Mock(side_effect=OSError("encode failed")))
    _, result = call(after={"wait": {"stable_frames": 1}, "screenshot": {"max_width": 320}})
    assert result["action"]["sent"] is True and result["wait"]["stable"]
    assert result["screenshot"]["status"] == "failed"
    assert client.capture_frame_jpeg.call_count == 2


def test_park_failure_preserves_action_and_attempts_observation(context):
    client, _ = context
    client.mouse_move.side_effect = srv.KvmClientError("park failed")
    _, result = call(after={"park_cursor": {"x": 50, "y": 30}, "wait": {"mode": "none"}})
    assert not result["ok"] and result["park_cursor"]["status"] == "failed"
    assert result["action"]["sent"] is True and result["screenshot"]["status"] == "ok"
    client.mouse_scroll.assert_called_once()
    client.mouse_move.assert_called_once()


def test_action_transport_error_is_unknown_and_never_replayed(context):
    client, _ = context
    client.mouse_scroll.side_effect = srv.KvmClientError("response lost")
    _, result = call(after={})
    assert result["action"]["status"] == "unknown" and result["action"]["sent"] is None
    assert "response lost" in result["action"]["error"]
    assert result["wait"]["status"] == result["screenshot"]["status"] == "skipped"
    client.mouse_scroll.assert_called_once()
    client.capture_frame_jpeg.assert_not_called()


@pytest.mark.parametrize("after", [None, [], {"unknown": 1}, {"park_cursor": {"x": 20}},
    {"park_cursor": {"x": 1280, "y": 10}}, {"wait": {"mode": "change"}},
    {"wait": {"mode": "none", "threshold": 0}}, {"wait": {"mode": "delay"}},
    {"wait": {"mode": "delay", "seconds": float("nan")}},
    {"wait": {"threshold": float("nan")}}, {"wait": {"timeout_seconds": -1}},
    {"wait": {"stable_frames": 0}}, {"wait": {"region": [0, 0, 999, 999]}},
    {"screenshot": {"zoom": 2}}, {"screenshot": {"max_width": 10}},
    {"screenshot": {"region": [900, 0, 10, 10]}}, {"screenshot": {"grid": "yes"}},
    {"screenshot": {"save": True}},
])
def test_invalid_after_sends_no_input(context, after):
    client, _ = context
    content = asyncio.run(srv.call_tool("type_text", {"text": "hello", "ime_off": True, "after": after}))
    result = json.loads(content[0].text)
    assert result["error"] == "invalid_arguments" and result["sent"] is False
    client.send_key.assert_not_called()
    client.type_text.assert_not_called()
    client.mouse_move.assert_not_called()


@pytest.mark.parametrize("name,args", [("type_text", {"text": "日本", "ime_off": True}),
    ("send_key", {"key": "enter", "modifiers": ["bad"]}),
    ("mouse_click", {"count": 4}), ("mouse_scroll", {"amount": -5, "x": 20})])
def test_invalid_action_sends_no_input(context, name, args):
    client, _ = context
    content = asyncio.run(srv.call_tool(name, {**args, "after": {}}))
    result = json.loads(content[0].text)
    assert result["error"] == "invalid_arguments" and result["sent"] is False
    assert result["retry_safe"] is True and not client.method_calls


@pytest.mark.parametrize("refusal", ["input_locked", "file_copy_owns_input"])
def test_locks_refuse_action_and_parking(context, monkeypatch, refusal):
    client, _ = context
    if refusal == "input_locked":
        srv._do_set_input_lock(locked=True)
    else:
        monkeypatch.setattr(srv._copies, "busy", lambda _: "owned-job")
    _, result = call(after={"park_cursor": {"x": 10, "y": 20}})
    assert result["error"] == refusal and not client.method_calls


@pytest.mark.parametrize("mode", ["delay", "stable"])
def test_context_change_cannot_return_a_different_target_frame(context, mode):
    client, clock = context
    clock.on_sleep = srv._state.reset
    _, result = call(after={"wait": {"mode": mode, **({"seconds": .2} if mode == "delay" else {})}})
    assert result["action"]["sent"] is True
    assert result["wait"]["status"] == result["screenshot"]["status"] == "failed"
    assert result["screenshot"]["error"] == "context_changed"
    assert srv._state.baseline is None
    assert client.capture_frame_jpeg.call_count == (1 if mode == "stable" else 0)


def test_evidence_config_and_helpers_removed(monkeypatch):
    monkeypatch.setenv("SHKVM_EVIDENCE_DIR", "must-be-ignored")
    assert not hasattr(Config(), "evidence_dir")
    assert not hasattr(srv, "_do_save_evidence")
    assert not hasattr(srv, "_sanitize_label")


def test_screenshot_records_exact_endpoint_for_copy_focus(context):
    call(after={"wait": {"mode": "none"}})
    assert srv._copies.observed[0] == {"host": "localhost", "port": 9329}


@pytest.mark.parametrize("change", ["context", "lock", "ownership"])
def test_interrupted_multi_request_action_stops_before_typing(context, monkeypatch, change):
    client, clock = context
    def interrupt():
        if change == "context":
            srv._state.reset()
        elif change == "lock":
            srv._do_set_input_lock(locked=True)
        else:
            monkeypatch.setattr(srv._copies, "busy", lambda _: "copy-job")
    clock.on_sleep = interrupt
    _, result = call("type_text", text="hello", ime_off=True, after={})
    client.send_key.assert_called_once_with("0x91")
    client.type_text.assert_not_called()
    assert result["action"]["status"] == "unknown" and result["action"]["sent"] is None


@pytest.mark.parametrize("refusal", ["input_locked", "file_copy_owns_input"])
def test_refusal_during_preparation_reports_known_unsent(context, monkeypatch, refusal):
    client, _ = context

    def probe(quality):
        if refusal == "input_locked":
            srv._do_set_input_lock(locked=True)
        else:
            monkeypatch.setattr(srv._copies, "busy", lambda _: "owned-job")
        return jpeg()

    client.capture_frame_jpeg.side_effect = probe
    _, result = call(after={"screenshot": {"region": [0, 0, 100, 100]}})
    assert result["action"]["sent"] is False
    assert result["action"]["status"] == "not_sent"
    assert result["action"]["error"] == refusal
    assert result["wait"]["status"] == result["screenshot"]["status"] == "skipped"
    assert [c[0] for c in client.method_calls] == ["capture_frame_jpeg"]


@pytest.mark.parametrize("kind", ["target", "capture"])
@pytest.mark.parametrize("mode", ["delay", "stable"])
def test_real_context_switch_during_wait_is_invalidated(context, monkeypatch, kind, mode):
    client, clock = context
    replacement = Mock()

    async def switch():
        clock.on_sleep = None
        if kind == "target":
            monkeypatch.setattr(srv, "KvmClient", Mock(return_value=replacement))
            assert srv._do_select_target(host="fake-next", port=12345)["ok"]
        else:
            reply = await srv.call_tool("set_capture_resolution", {"width": 320, "height": 240})
            assert json.loads(reply[0].text)["ok"]

    clock.on_sleep = switch
    content, result = call(after={"wait": {"mode": mode, **({"seconds": .2} if mode == "delay" else {})}})
    assert result["target"]["host"] == "localhost"
    assert result["action"]["sent"] is True
    assert result["screenshot"]["error"] == "context_changed"
    assert len(content) == 1 and srv._state.baseline is None
    replacement.capture_frame_jpeg.assert_not_called()


@pytest.mark.parametrize("change", ["lock", "ownership"])
def test_observation_remains_read_only_when_input_is_claimed_during_wait(context, monkeypatch, change):
    client, clock = context

    def claim():
        if change == "lock":
            srv._do_set_input_lock(locked=True)
        else:
            monkeypatch.setattr(srv._copies, "busy", lambda _: "owned-job")

    clock.on_sleep = claim
    _, result = call(after={"wait": {"stable_frames": 2}})
    assert result["ok"] and result["action"]["sent"] is True
    assert client.mouse_scroll.call_count == 1
    client.mouse_move.assert_not_called()
    assert client.capture_frame_jpeg.call_count == 3


def test_timeout_reuses_exact_final_bytes_with_full_baseline(context):
    client, _ = context
    frames = [jpeg(0), jpeg(255), jpeg(0)]
    client.capture_frame_jpeg.side_effect = frames
    content, result = call(after={"wait": {"timeout_seconds": .2, "poll_ms": 100}})
    assert result["wait"]["status"] == "timeout"
    assert result["wait"]["attempts"] == 3
    assert base64.b64decode(content[0].data) == frames[-1][0]
    assert client.capture_frame_jpeg.call_count == 3
    assert srv._state.baseline.shape[:2] == (480, 640)
    assert srv._state.baseline.max() == 0


def test_resolution_change_after_delivery_keeps_action_and_attempts_fallback(context):
    client, _ = context
    data = io.BytesIO()
    Image.new("RGB", (320, 240)).save(data, format="JPEG")
    client.capture_frame_jpeg.side_effect = [jpeg(), (data.getvalue(), 320, 240), (data.getvalue(), 320, 240)]
    content, result = call(after={})
    assert result["action"]["sent"] is True
    assert result["wait"]["status"] == "failed"
    assert result["screenshot"]["source"] == "capture"
    assert result["screenshot"]["capture"] == "320x240"
    assert len(content) == 2 and client.capture_frame_jpeg.call_count == 3


@pytest.fixture
def tcp_peer():
    """An actual local TCP peer with no hardware or connection to port 9329."""
    import socketserver
    import threading

    requests = []
    failures = []

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            for line in self.rfile:
                request = json.loads(line)
                requests.append(request)
                if failures:
                    failure = failures.pop(0)
                    if failure == "malformed":
                        self.wfile.write(b"invalid JSON\n")
                    return
                data, width, height = jpeg()
                result = {"jpeg_b64": base64.b64encode(data).decode(), "width": width, "height": height}
                self.wfile.write((json.dumps({"id": request["id"], "ok": True, "result": result}) + "\n").encode())

    class Peer(socketserver.ThreadingTCPServer):
        daemon_threads = True

    peer = Peer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: peer.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    client = srv.KvmClient(*peer.server_address, timeout=2)
    try:
        yield client, requests, failures
    finally:
        client.close()
        peer.shutdown()
        peer.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("name,args", [("mouse_scroll", {"amount": -1}),
    ("mouse_click", {}), ("send_key", {"key": "enter"}), ("type_text", {"text": "abc", "ime_off": False})])
@pytest.mark.parametrize("failure", ["eof", "malformed"])
def test_tcp_response_loss_integration_never_replays_input(context, monkeypatch, tcp_peer, name, args, failure):
    client, requests, failures = tcp_peer
    monkeypatch.setattr(srv, "_client", client)
    failures.append(failure)
    content, result = call(name, **args, after={})
    assert result["action"]["status"] == "unknown" and result["action"]["sent"] is None
    assert [r["method"] for r in requests] == [name]
    assert len(content) == 1 and result["screenshot"]["status"] == "skipped"


def test_tcp_observation_retry_and_stable_reuse_integration(context, monkeypatch, tcp_peer):
    client, requests, failures = tcp_peer
    monkeypatch.setattr(srv, "_client", client)
    failures.append("eof")
    client.capture_frame_jpeg()  # observation may reconnect once
    assert [r["method"] for r in requests] == ["capture_frame", "capture_frame"]
    requests.clear()
    content, result = call(after={"wait": {"stable_frames": 2}})
    assert result["ok"] and len(content) == 2
    assert [r["method"] for r in requests] == ["mouse_scroll"] + ["capture_frame"] * 3
    assert result["screenshot"]["source"] == "wait_last_frame"

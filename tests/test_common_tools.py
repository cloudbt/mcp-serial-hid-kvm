"""Issue #4: health identity, capture_screen options, mouse/type_text options
and the cleaned-up tool list, against a fake KVM client."""

import asyncio
import base64
import io
import json
from unittest.mock import Mock

import pytest
from PIL import Image

import mcp_serial_hid_kvm.server as srv
from mcp_serial_hid_kvm.config import DEFAULT_HIDDEN_TOOLS, Config, parse_hidden_tools
from mcp_serial_hid_kvm.runtime_config import RuntimeConfig
from mcp_serial_hid_kvm.vision import StateEngine

DEVICE_INFO = {
    "ch340_ports": [{"device": "COM3", "description": "USB-SERIAL CH343",
                     "hwid": "USB VID:PID=1A86:55D3 SER=X LOCATION=1-2",
                     "vid": "0x1A86", "pid": "0x55D3"}],
    "serial": {"port": "COM3", "baudrate": 9600, "connected": True},
    "capture": {"device": "0", "width": 1920, "height": 1080},
    "config": {"screen_width": 1920, "screen_height": 1080},
}


def jpeg(size=(1920, 1080), color=(90, 90, 90)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG", quality=85)
    return buf.getvalue()


@pytest.fixture
def client(monkeypatch, tmp_path):
    fake = Mock()
    fake.get_device_info.return_value = DEVICE_INFO
    fake.capture_frame_jpeg.return_value = (jpeg(), 1920, 1080)
    monkeypatch.setattr(srv, "_client", fake)
    monkeypatch.setattr(srv, "_state", StateEngine())
    monkeypatch.setattr(srv, "_input_lock", None)
    monkeypatch.setattr(srv, "_current_target",
                        {"name": "target1", "host": "127.0.0.1", "port": 9329})
    monkeypatch.setattr(srv, "_screen_size", None)
    monkeypatch.setattr(srv, "_cursor_pos", None)
    monkeypatch.setattr(srv, "_runtime", RuntimeConfig(str(tmp_path / "config.json")))
    monkeypatch.setattr(srv.config, "capture_log_dir", None)
    monkeypatch.setattr(srv.config, "targets", {
        "default": {"host": "127.0.0.1", "port": 9329},
        "target1": {"host": "127.0.0.1", "port": 9329}})
    return fake


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []

    async def record(seconds):
        recorded.append(seconds)
    monkeypatch.setattr(srv.asyncio, "sleep", record)
    return recorded


def run(name, **arguments):
    return asyncio.run(srv.call_tool(name, arguments))


def reply(name, **arguments):
    return json.loads(run(name, **arguments)[0].text)


def text(name, **arguments):
    """An action's message, a failure's detail, else the raw JSON text."""
    raw = run(name, **arguments)[0].text
    result = json.loads(raw)
    if "message" in result:
        return result["message"]
    return result["detail"] if "error" in result and "detail" in result else raw


def capture(**arguments):
    """Return (PIL image, raw JPEG bytes, metadata dict) from capture_screen."""
    image_part, meta_part = run("capture_screen", **arguments)
    assert image_part.type == "image" and image_part.mimeType == "image/jpeg"
    data = base64.b64decode(image_part.data)
    return Image.open(io.BytesIO(data)), data, json.loads(meta_part.text)


# --- 1. health identity -------------------------------------------------------

def test_health_adds_identity_and_keeps_existing_fields(client):
    result = json.loads(text("health"))
    assert {"ok", "api", "serial", "video", "capture_device", "resolution",
            "target", "input_locked", "errors"} <= result.keys()
    assert result["ok"] and result["resolution"] == "1920x1080"
    assert result["target"] == "target1"
    assert (result["api_host"], result["api_port"]) == ("127.0.0.1", 9329)
    assert result["serial_port"] == "COM3"
    assert result["serial_vid_pid"] == "1A86:55D3"
    assert result["hid_size"] == result["capture_size"] == "1920x1080"
    assert result["targets"] == {"default": "127.0.0.1:9329", "target1": "127.0.0.1:9329"}


def test_health_vid_pid_falls_back_to_hwid_and_unknown_port(client):
    info = json.loads(json.dumps(DEVICE_INFO))
    info["ch340_ports"][0].pop("vid")
    info["ch340_ports"][0].pop("pid")
    client.get_device_info.return_value = info
    assert json.loads(text("health"))["serial_vid_pid"] == "1A86:55D3"
    info["serial"]["port"] = "COM9"  # not among detected adapters
    assert json.loads(text("health"))["serial_vid_pid"] is None


# --- 2. capture_screen ------------------------------------------------------------

def test_capture_untransformed_returns_kvm_jpeg_and_metadata(client):
    image, data, meta = capture()
    assert data == client.capture_frame_jpeg.return_value[0]  # no re-encoding
    assert image.size == (1920, 1080)
    assert meta == {"ok": True, "capture": "1920x1080", "hid": "1920x1080", "image": "1920x1080",
                    "region": [0, 0, 1920, 1080], "scale": 1,
                    "to_hid": "hid_x = 0 + image_x * 1; hid_y = 0 + image_y * 1"}
    assert srv._state.baseline.shape[:2] == (1080, 1920)


def test_capture_region_and_zoom_keep_full_frame_baseline(client):
    image, data, meta = capture(region=[100, 200, 300, 150], zoom=2)
    assert data != client.capture_frame_jpeg.return_value[0]
    assert image.size == (600, 300)
    assert meta["region"] == [100, 200, 300, 150] and meta["scale"] == 2
    assert meta["to_hid"] == "hid_x = 100 + image_x * 0.5; hid_y = 200 + image_y * 0.5"
    assert srv._state.baseline.shape[:2] == (1080, 1920)


def test_capture_region_is_clamped_to_frame(client):
    image, _, meta = capture(region=[1800, 1000, 500, 500])
    assert image.size == (120, 80) and meta["region"] == [1800, 1000, 120, 80]


def test_capture_max_width_shrinks(client):
    image, _, meta = capture(max_width=960)
    assert image.size == (960, 540)
    assert meta["scale"] == 0.5 and meta["image"] == "960x540"
    assert meta["to_hid"] == "hid_x = 0 + image_x * 2; hid_y = 0 + image_y * 2"
    # Never upscales.
    assert capture(max_width=4000)[0].size == (1920, 1080)


def test_capture_maps_to_hid_when_sizes_differ(client):
    client.capture_frame_jpeg.return_value = (jpeg((1280, 720)), 1280, 720)
    srv._screen_size = (2560, 1440)
    image, _, meta = capture(region=[100, 50, 200, 100], zoom=2)
    assert image.size == (400, 200)
    assert meta["capture"] == "1280x720" and meta["hid"] == "2560x1440"
    assert meta["to_hid"] == "hid_x = 200 + image_x * 1; hid_y = 100 + image_y * 1"


def test_capture_grid_draws_hid_labelled_lines(client):
    image, _, meta = capture(grid=True)
    assert meta["grid_step_hid"] == 100 and image.size == (1920, 1080)
    r, g, b = image.getpixel((100, 650))  # vertical line at HID x=100
    assert r > g + 40 and b > g + 40
    assert image.getpixel((150, 650)) == pytest.approx((90, 90, 90), abs=6)
    # Grid steps are HID units: half-size capture of a larger HID screen.
    client.capture_frame_jpeg.return_value = (jpeg((1280, 720)), 1280, 720)
    srv._screen_size = (2560, 1440)
    image, _, meta = capture(grid=True)
    assert meta["grid_step_hid"] == 200
    r, g, b = image.getpixel((100, 450))  # HID x=200 -> capture x=100
    assert r > g + 40 and b > g + 40


@pytest.mark.parametrize("arguments,message", [
    ({"zoom": 2}, "zoom requires region"),
    ({"region": [5000, 0, 10, 10]}, "outside"),
    ({"region": [0, 0, 10]}, "region must be"),
    ({"region": [0, 0, 10, 10], "zoom": 5}, "zoom must be"),
    ({"max_width": 10}, "max_width"),
])
def test_capture_invalid_options_do_not_touch_baseline(client, arguments, message):
    result = run("capture_screen", **arguments)
    assert len(result) == 1 and result[0].type == "text"
    error = json.loads(result[0].text)
    assert error["error"] == "invalid_arguments" and message in error["detail"]
    assert srv._state.baseline is None


# --- 3. mouse ---------------------------------------------------------------------

def test_mouse_click_default_is_single_server_click(client, sleeps):
    assert text("mouse_click", x=10, y=20) == "Clicked left at (10, 20)"
    client.mouse_click.assert_called_once_with("left", 10, 20)
    client.mouse_down.assert_not_called()


@pytest.mark.parametrize("count,verb", [(2, "Double-clicked"), (3, "Triple-clicked")])
def test_mouse_click_count_within_double_click_time(client, sleeps, count, verb):
    assert text("mouse_click", x=10, y=20, count=count) == f"{verb} left at (10, 20)"
    client.mouse_click.assert_not_called()
    assert client.mouse_down.call_count == client.mouse_up.call_count == count
    assert all(c.args == ("left", 10, 20) for c in client.mouse_down.call_args_list)
    # Every press follows the previous one well inside 500 ms.
    hold = sleeps[0]
    assert hold + srv._MULTI_CLICK_GAP < 0.25
    assert srv._cursor_pos == (10, 20)


def test_mouse_click_rejects_bad_count_without_input(client, sleeps):
    assert "count must be" in text("mouse_click", count=4)
    assert not client.method_calls


def test_mouse_scroll_moves_first_when_position_given(client, sleeps):
    assert text("mouse_scroll", amount=-3, x=500, y=400) == "Scrolled down by 3 at (500, 400)"
    assert [c[0] for c in client.method_calls] == ["mouse_move", "mouse_scroll"]
    client.mouse_move.assert_called_once_with(500, 400)
    client.mouse_scroll.assert_called_once_with(-3)
    assert srv._cursor_pos == (500, 400)


def test_mouse_scroll_without_position_is_unchanged(client, sleeps):
    assert text("mouse_scroll", amount=2) == "Scrolled up by 2"
    client.mouse_move.assert_not_called()
    assert "both x and y" in text("mouse_scroll", amount=2, x=5)
    client.mouse_scroll.assert_called_once()


def test_mouse_drag_default_single_move(client, sleeps):
    text("mouse_drag", start_x=10, start_y=20, end_x=110, end_y=60)
    client.mouse_down.assert_called_once_with("left", 10, 20)
    client.mouse_move.assert_called_once_with(110, 60)
    client.mouse_up.assert_called_once_with("left", 110, 60)
    assert sleeps == [0.05, 0.05]


def test_mouse_drag_steps_interpolate(client, sleeps):
    text("mouse_drag", start_x=0, start_y=0, end_x=100, end_y=40, steps=4)
    assert [c.args for c in client.mouse_move.call_args_list] == [
        (25, 10), (50, 20), (75, 30), (100, 40)]
    assert [c[0] for c in client.method_calls] == [
        "mouse_down", "mouse_move", "mouse_move", "mouse_move", "mouse_move", "mouse_up"]
    assert "steps must be" in text("mouse_drag", start_x=0, start_y=0, end_x=1, end_y=1, steps=0)


# --- 4. type_text ime_off ------------------------------------------------------------

def test_type_text_ime_off_sends_0x91_first(client, sleeps):
    assert text("type_text", text="abc", ime_off=True) == "Typed 3 characters"
    assert [c[0] for c in client.method_calls] == ["send_key", "type_text"]
    client.send_key.assert_called_once_with("0x91")


def test_type_text_default_turns_ime_off_first(client, sleeps):
    assert text("type_text", text="abc") == "Typed 3 characters"
    assert [c[0] for c in client.method_calls] == ["send_key", "type_text"]
    client.send_key.assert_called_once_with("0x91")


def test_type_text_ime_off_false_sends_no_ime_key(client, sleeps):
    text("type_text", text="abc", ime_off=False)
    client.send_key.assert_not_called()
    client.type_text.assert_called_once_with("abc", None, raw=False)


def test_type_text_ime_default_follows_runtime_config(client, sleeps):
    srv.get_config().update({"type_ime_off_default": False})
    text("type_text", text="abc")
    client.send_key.assert_not_called()
    assert text("type_text", text="abc", ime_off=True) == "Typed 3 characters"
    client.send_key.assert_called_once_with("0x91")


def test_type_text_invalid_chars_send_nothing(client, sleeps):
    result = reply("type_text", text="日本", ime_off=True)
    assert result["error"] == "invalid_arguments" and result["sent"] is False
    assert not client.method_calls


# --- 5. tool list ---------------------------------------------------------------------

def test_removed_tools_are_unknown(client):
    names = {t.name for t in asyncio.run(srv._all_tools())}
    for name in ("screen_changed", "wait_for_screen_change", "send_key_sequence"):
        assert name not in names
        assert reply(name) == {"ok": False, "tool": name, "error": "unknown_tool"}
    assert not client.method_calls


def test_default_hidden_tools_are_listed_out_but_callable(monkeypatch):
    assert DEFAULT_HIDDEN_TOOLS == {
        "configure", "set_screen_baseline", "get_changed_regions",
        "list_capture_devices", "set_capture_device", "set_capture_resolution"}
    monkeypatch.setattr(srv.config, "hidden_tools", set(DEFAULT_HIDDEN_TOOLS))
    listed = {t.name for t in asyncio.run(srv.list_tools())}
    callable_ = {t.name for t in asyncio.run(srv._all_tools())}
    assert listed.isdisjoint(DEFAULT_HIDDEN_TOOLS)
    assert DEFAULT_HIDDEN_TOOLS <= callable_
    assert {"health", "capture_screen", "mouse_click", "type_text", "send_key",
            "wait_for_change", "wait_for_stable", "get_timing"} <= listed


ONE = {"default": {"host": "127.0.0.1", "port": 9329},
       "target1": {"host": "127.0.0.1", "port": 9329}}
TWO = {**ONE, "target2": {"host": "127.0.0.1", "port": 9331}}


def test_select_target_hidden_only_with_one_endpoint():
    assert "select_target" in parse_hidden_tools(None, ONE)
    assert "select_target" in parse_hidden_tools(None, {"default": ONE["default"]})
    assert "select_target" not in parse_hidden_tools(None, TWO)
    # Explicit SHKVM_HIDDEN_TOOLS replaces the defaults entirely.
    assert parse_hidden_tools("none", ONE) == set()
    assert parse_hidden_tools("configure", ONE) == {"configure"}
    assert parse_hidden_tools("select_target", TWO) == {"select_target"}


@pytest.mark.parametrize("targets,hidden", [
    ('{"target1":"127.0.0.1:9329"}', True),
    ('{"target1":"127.0.0.1:9329","target2":"127.0.0.1:9331"}', False),
])
def test_config_select_target_visibility_from_env(monkeypatch, targets, hidden):
    monkeypatch.setenv("SHKVM_API_HOST", "127.0.0.1")
    monkeypatch.setenv("SHKVM_API_PORT", "9329")
    monkeypatch.setenv("SHKVM_TARGETS", targets)
    monkeypatch.delenv("SHKVM_HIDDEN_TOOLS", raising=False)
    assert ("select_target" in Config().hidden_tools) is hidden


def test_tool_definitions_are_compact():
    tools = {t.name: t for t in asyncio.run(srv._all_tools())}
    size = len(json.dumps(tools["type_text"].model_dump(exclude_none=True)))
    assert size < 4500  # includes shared after wait/capture options
    assert "ime_off" in tools["type_text"].inputSchema["properties"]
    assert {"region", "zoom", "max_width", "grid"} <= \
        tools["capture_screen"].inputSchema["properties"].keys()


# --- 6. uniform JSON results ------------------------------------------------------------

@pytest.mark.parametrize("name,arguments", [
    ("mouse_move", {"x": 5, "y": 6}),
    ("mouse_drag", {"start_x": 0, "start_y": 0, "end_x": 1, "end_y": 1}),
    ("list_capture_devices", {}),
])
def test_plain_tools_reply_with_ok_json(client, sleeps, name, arguments):
    client.list_capture_devices.return_value = {}
    result = reply(name, **arguments)
    assert result["ok"] is True and result["tool"] == name
    if name == "list_capture_devices":
        assert result["devices"] == []
    else:
        assert result["sent"] is True and result["message"]


def test_input_failure_after_sending_is_unknown_delivery(client, sleeps):
    client.type_text.side_effect = srv.KvmClientError("lost")
    result = reply("type_text", text="abc", ime_off=False)
    assert result == {"ok": False, "tool": "type_text", "error": "kvm_error",
                      "detail": "lost", "sent": None, "retry_safe": False}


def test_read_only_failure_is_retry_safe(client):
    client.get_device_info.side_effect = srv.KvmClientError("down")
    result = reply("get_device_info")
    assert result["error"] == "kvm_error" and result["retry_safe"] is True
    assert "sent" not in result


def test_unreachable_server_sends_nothing(monkeypatch):
    def refuse():
        raise ConnectionRefusedError("refused")
    monkeypatch.setattr(srv, "get_client", refuse)
    result = reply("mouse_click", x=1, y=2)
    assert result["error"] == "kvm_unreachable"
    assert result["sent"] is False and result["retry_safe"] is True


def test_server_down_stays_unreachable_on_later_calls(monkeypatch):
    dead = Mock()
    dead.connect.side_effect = ConnectionRefusedError("refused")
    monkeypatch.setattr(srv, "KvmClient", Mock(return_value=dead))
    monkeypatch.setattr(srv, "_client", None)
    for _ in range(2):
        result = reply("mouse_click", x=1, y=2)
        assert result["error"] == "kvm_unreachable" and result["sent"] is False
    assert srv._client is None
    dead.mouse_click.assert_not_called()


def test_dropped_connection_reconnects_before_input(client, sleeps):
    client.connected = False
    client.connect.side_effect = ConnectionRefusedError("refused")
    result = reply("type_text", text="abc")
    assert result["error"] == "kvm_unreachable" and result["sent"] is False
    client.type_text.assert_not_called()
    client.send_key.assert_not_called()


def test_recording_cc_and_meeting_tools_pass_through_to_the_target(client):
    client.call.return_value = {"recording": {"active": True, "started_by": "api"}}
    result = reply("recording_start", fps=30, source="hdmi", minutes="off", bogus=1)
    assert result["ok"] is True and result["recording"]["started_by"] == "api"
    client.call.assert_called_with("recording_start", {"fps": 30, "source": "hdmi", "minutes": "off"})
    client.call.return_value = {"on": True}
    assert reply("captions_set", on=True)["on"] is True
    client.call.assert_called_with("captions_set", {"on": True})
    for name in ("recording_stop", "recording_status", "captions_status", "meeting_points",
                 "meeting_summarize", "meeting_minutes"):
        reply(name)
        client.call.assert_called_with(name, {})
    listed = {t.name for t in asyncio.run(srv.list_tools())}
    assert set(srv._WEB_TOOLS) <= listed


def test_web_tool_errors_are_reported(client):
    client.call.side_effect = srv.KvmClientError("recording_status needs the web viewer (--web)")
    result = reply("recording_status")
    assert result["ok"] is False and result["error"] == "kvm_error" and "web viewer" in json.dumps(result)

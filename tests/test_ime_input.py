"""type_japanese / type_chinese: typing plans, key order and MCP wiring."""

import asyncio
import io
import json
from unittest.mock import Mock, call

import pytest
from PIL import Image

import mcp_serial_hid_kvm.server as srv
from mcp_serial_hid_kvm import ime_input
from mcp_serial_hid_kvm.runtime_config import RuntimeConfig
from mcp_serial_hid_kvm.vision import StateEngine


# --- plan ---------------------------------------------------------------------------

def test_japanese_plan_mixes_codes_kana_and_ascii():
    assert ime_input.plan("今日はABC会議ワークフローです。\n", "japanese") == [
        ("code", "4ECA"), ("code", "65E5"), ("hiragana", "ha"), ("ascii", "ABC"),
        ("code", "4F1A"), ("code", "8B70"), ("katakana", "wa-kufuro-"),
        ("hiragana", "desu"), ("code", "3002"), ("ascii", "\n"),
    ]


def test_chinese_plan_types_every_non_ascii_character_by_code():
    assert ime_input.plan("你好，ABC。です", "chinese") == [
        ("code", "4F60"), ("code", "597D"), ("code", "FF0C"), ("ascii", "ABC"),
        ("code", "3002"), ("code", "3067"), ("code", "3059"),
    ]


@pytest.mark.parametrize("text,romaji", [
    ("せんい", "senni"),         # ん before a vowel stays ん
    ("きょうは", "kixyouha"),     # small kana have their own spelling
    ("がっこう", "gaxtukou"),
    ("らーめん", "ra-menn"),
])
def test_hiragana_spellings_resolve_per_character(text, romaji):
    assert ime_input.plan(text, "japanese") == [("hiragana", romaji)]


def test_rare_kana_fall_back_to_code_conversion():
    assert ime_input.plan("ゐヵ・", "japanese") == [
        ("code", "3090"), ("code", "30F5"), ("code", "30FB")]


def test_long_kana_runs_are_chunked():
    segments = ime_input.plan("あ" * 45, "japanese")
    assert [len(p) for _, p in segments] == [20, 20, 5]


@pytest.mark.parametrize("text", ["", "𠮷", "a\x07b", "あ" * (ime_input.MAX_TEXT_CHARS + 1), 5])
def test_untypeable_text_is_rejected(text):
    with pytest.raises(ValueError):
        ime_input.plan(text, "japanese")


def test_summary_counts():
    segments = ime_input.plan("会議はAB", "japanese")
    assert ime_input.summary(segments) == {
        "ascii_chars": 2, "kana_runs": 1, "code_conversions": 2, "segments": 4}


# --- send --------------------------------------------------------------------------

def test_send_key_order_and_ime_state():
    client = Mock()
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    segments = [("ascii", "A"), ("code", "4ECA"), ("hiragana", "ha"),
                ("katakana", "ku"), ("ascii", "\n")]
    asyncio.run(ime_input.send(client, segments, convert_s=0.25, sleep=sleep))
    assert client.method_calls == [
        call.send_key("0x91"), call.type_text("A", raw=True),
        call.send_key("0x90"), call.type_text("4ECA", raw=True),
        call.send_key("f5"), call.send_key("enter"),
        call.type_text("ha", raw=True), call.send_key("enter"),
        call.type_text("ku", raw=True), call.send_key("f7"), call.send_key("enter"),
        call.send_key("0x91"), call.type_text("\n", raw=True),
    ]
    assert slept.count(0.25) == 2


def test_send_ends_with_ime_off():
    client = Mock()

    async def sleep(_):
        pass

    asyncio.run(ime_input.send(client, [("code", "738B")], convert_s=0, sleep=sleep))
    assert client.method_calls[-1] == call.send_key("0x91")


def test_guard_runs_before_every_request():
    client = Mock()
    guard = Mock(side_effect=[None, None, ValueError("input_locked")])

    async def sleep(_):
        pass

    with pytest.raises(ValueError):
        asyncio.run(ime_input.send(client, [("code", "738B")], convert_s=0,
                                   guard=guard, sleep=sleep))
    assert client.method_calls == [call.send_key("0x90"), call.type_text("738B", raw=True)]


# --- MCP wiring --------------------------------------------------------------------

def jpeg():
    buffer = io.BytesIO()
    Image.new("RGB", (640, 480), (90, 90, 90)).save(buffer, format="JPEG")
    return buffer.getvalue(), 640, 480


@pytest.fixture
def client(monkeypatch, tmp_path):
    fake = Mock()
    fake.capture_frame_jpeg.return_value = jpeg()
    monkeypatch.setattr(srv, "_client", fake)
    monkeypatch.setattr(srv, "_state", StateEngine())
    monkeypatch.setattr(srv, "_input_lock", None)
    monkeypatch.setattr(srv, "_screen_size", (1920, 1080))
    monkeypatch.setattr(srv, "_current_target", {"name": "t", "host": "127.0.0.1", "port": 9329})
    monkeypatch.setattr(srv, "_runtime", RuntimeConfig(str(tmp_path / "runtime.json")))

    async def no_sleep(_):
        pass
    monkeypatch.setattr(srv.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(ime_input.asyncio, "sleep", no_sleep)
    return fake


def run(name, **arguments):
    content = asyncio.run(srv.call_tool(name, arguments))
    return content, json.loads(content[-1].text)


@pytest.mark.parametrize("name", ["type_japanese", "type_chinese"])
def test_tools_are_advertised_with_after(name):
    tools = {t.name: t for t in asyncio.run(srv.list_tools())}
    assert "after" in tools[name].inputSchema["properties"]


def test_type_japanese_reports_sent_json(client):
    _, result = run("type_japanese", text="王さん")
    assert result == {"ok": True, "tool": "type_japanese", "sent": True,
                      "message": "Typed 3 characters via IME (1 code conversions)"}
    assert call.type_text("738B", raw=True) in client.method_calls
    assert call.type_text("sann", raw=True) in client.method_calls


def test_type_chinese_with_after_returns_screenshot(client):
    content, result = run("type_chinese", text="中文", after={"wait": {"mode": "none"}})
    assert [item.type for item in content] == ["image", "text"]
    assert result["ok"] and result["action"]["sent"] is True


def test_invalid_text_sends_nothing(client):
    _, result = run("type_chinese", text="𠮷")
    assert result["error"] == "invalid_arguments" and result["sent"] is False
    assert not client.method_calls


def test_dry_run_needs_no_connection_and_sends_nothing(monkeypatch):
    monkeypatch.setattr(srv, "get_client", Mock(side_effect=AssertionError("no connect")))
    _, result = run("type_japanese", text="会議です", dry_run=True)
    assert result == {"ok": True, "tool": "type_japanese", "dry_run": True, "sent": False,
                      "text_chars": 4, "ascii_chars": 0, "kana_runs": 1,
                      "code_conversions": 2, "segments": 3}


def test_input_lock_refuses_typing_but_not_dry_run(client):
    srv._do_set_input_lock(locked=True)
    _, refused = run("type_japanese", text="会議")
    assert refused["error"] == "input_locked" and not client.method_calls
    _, planned = run("type_japanese", text="会議", dry_run=True)
    assert planned["ok"] and planned["dry_run"]


def test_failure_mid_typing_is_unknown_delivery(client):
    client.send_key.side_effect = [None, srv.KvmClientError("lost")]
    _, result = run("type_chinese", text="中")
    assert result["error"] == "kvm_error" and result["sent"] is None


def test_convert_pause_follows_runtime_config(client, monkeypatch):
    slept = []

    async def record(seconds):
        slept.append(seconds)
    monkeypatch.setattr(ime_input.asyncio, "sleep", record)
    srv.get_config().update({"ime_convert_ms": 400})
    run("type_chinese", text="中")
    assert 0.4 in slept

"""Deterministic unit tests for the Route A wrapper-tool helpers.

These cover the pure logic (text bounding, image diff, WSL command shaping)
and structured OCR, without requiring KVM hardware or a running API server.
"""

import os

from PIL import Image, ImageDraw, ImageFont

import mcp_serial_hid_kvm.server as srv
from mcp_serial_hid_kvm.ocr import TerminalOCR
from mcp_serial_hid_kvm.runtime_config import DEFAULTS, RuntimeConfig


def test_normalize_lines_collapses_blanks_and_trims_edges():
    out = srv._normalize_lines("\n\n  a  \n\n\n b \n\n")
    assert out == ["  a", "", " b"]


def test_compact_text_head():
    out, count, truncated = srv._compact_text("l1\nl2\nl3\nl4", max_lines=2, max_chars=1000)
    assert out == "l1\nl2"
    assert count == 2
    assert truncated is True


def test_compact_text_tail():
    out, _count, truncated = srv._compact_text(
        "l1\nl2\nl3\nl4", max_lines=2, max_chars=1000, tail=True
    )
    assert out == "l3\nl4"
    assert truncated is True


def test_compact_text_char_limit():
    out, _count, truncated = srv._compact_text("abcdefgh", max_lines=0, max_chars=4)
    assert out == "abcd"
    assert truncated is True


def test_compact_text_no_truncation():
    out, count, truncated = srv._compact_text("a\nb", max_lines=10, max_chars=100)
    assert out == "a\nb"
    assert count == 2
    assert truncated is False


def test_diff_score_identical_is_zero():
    img = Image.new("L", (50, 50), 0)
    assert srv._diff_score(img, img.copy()) == 0.0


def test_diff_score_opposite_is_one():
    black = Image.new("L", (50, 50), 0)
    white = Image.new("L", (50, 50), 255)
    assert srv._diff_score(black, white) == 1.0


def test_diff_score_resizes_mismatched():
    a = Image.new("L", (40, 40), 0)
    b = Image.new("L", (80, 80), 0)
    assert srv._diff_score(a, b) == 0.0


def test_crop_region_clamps_bounds():
    img = Image.new("RGB", (100, 100), "white")
    cropped = srv._crop_region(img, [90, 90, 50, 50])
    assert cropped.size == (10, 10)


def test_crop_region_none_returns_same():
    img = Image.new("RGB", (100, 100), "white")
    assert srv._crop_region(img, None) is img


def test_build_wsl_command_basic():
    line = srv._build_wsl_command("uname -a", "Ubuntu-24.04")
    assert line == 'wsl.exe -d Ubuntu-24.04 -- bash -lc "uname -a"'


def test_build_wsl_command_escapes_double_quotes():
    line = srv._build_wsl_command('echo "hi"', "Ubuntu-24.04")
    assert line == 'wsl.exe -d Ubuntu-24.04 -- bash -lc "echo `"hi`""'


def test_ocr_failed_detects_sentinel():
    assert srv._ocr_failed("[OCR Error: boom]") is True
    assert srv._ocr_failed("normal text") is False


def test_compact_ocr_error_is_bounded_and_clean():
    huge = "[OCR Error: " + ("x " * 500) + "]"
    payload = srv._compact_ocr_error(huge, limit=50)
    assert payload["ok"] is False
    assert payload["error"] == "ocr_failed"
    assert len(payload["detail"]) <= 53  # 50 + "..."
    assert "\n" not in payload["detail"]


def test_extract_elements_returns_boxes():
    img = Image.new("RGB", (640, 200), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 48)
    except Exception:
        font = ImageFont.load_default()
    draw.text((40, 60), "OK CANCEL", fill="black", font=font)

    elements = TerminalOCR().extract_elements(img, min_confidence=0.0)
    texts = [e["text"] for e in elements]
    assert "OK" in texts
    for e in elements:
        assert set(("text", "x", "y", "w", "h", "confidence")) <= set(e)
        assert 0.0 <= e["confidence"] <= 1.0
        # coordinates mapped back into the original frame
        assert 0 <= e["x"] <= img.width
        assert 0 <= e["y"] <= img.height


# --- V2: text matching -----------------------------------------------------

def test_match_text_contains_is_case_insensitive():
    assert srv._match_text("Hello World", "world", "contains") == (True, 1)


def test_match_text_contains_counts_occurrences():
    assert srv._match_text("a a a", "a", "contains") == (True, 3)


def test_match_text_exact_matches_whole_line():
    assert srv._match_text("foo\nbar\nfoo", "foo", "exact") == (True, 2)
    assert srv._match_text("foobar", "foo", "exact") == (False, 0)


def test_match_text_regex():
    assert srv._match_text("err 2026 ok", r"\d{4}", "regex") == (True, 1)


def test_match_text_bad_regex_is_safe():
    assert srv._match_text("anything", "(", "regex") == (False, 0)


def test_match_text_empty_needle():
    assert srv._match_text("anything", "", "contains") == (False, 0)


def test_brief_result_summarizes():
    assert srv._brief_result("wait_for_text", {"found": True}) == "found=True"
    assert srv._brief_result("x", {"error": "ocr_failed"}) == "ERROR ocr_failed"


# --- V2: cursor tracking ---------------------------------------------------

def test_cursor_crop_unknown_when_no_position():
    srv._cursor_pos = None
    img, meta = srv._do_cursor_crop()
    assert img is None
    assert meta["error"] == "cursor_unknown"


def test_set_and_bump_cursor():
    srv._set_cursor(100, 200)
    assert srv._cursor_pos == (100, 200)
    srv._bump_cursor(5, -10)
    assert srv._cursor_pos == (105, 190)
    srv._cursor_pos = None  # reset shared state


# --- V2: runtime config ----------------------------------------------------

def _fresh_config(tmp_path, monkeypatch=None):
    path = os.path.join(str(tmp_path), "rt.json")
    os.environ["SHKVM_RUNTIME_CONFIG"] = path
    # clear any RT env overrides that might leak in
    for k in list(os.environ):
        if k.startswith("SHKVM_RT_"):
            del os.environ[k]
    return RuntimeConfig()


def test_runtime_config_defaults(tmp_path):
    cfg = _fresh_config(tmp_path)
    assert cfg.get("default_wsl_distro") == "Ubuntu-24.04"
    assert cfg.as_dict() == DEFAULTS
    assert cfg.file_loaded is False


def test_runtime_config_update_valid(tmp_path):
    cfg = _fresh_config(tmp_path)
    changed = cfg.update({"wait_poll_ms": 250})
    assert changed == {"wait_poll_ms": 250}
    assert cfg.get("wait_poll_ms") == 250
    assert "wait_poll_ms" in cfg.runtime_keys


def test_runtime_config_update_invalid_key(tmp_path):
    cfg = _fresh_config(tmp_path)
    try:
        cfg.update({"nope": 1})
        raised = False
    except ValueError:
        raised = True
    assert raised
    assert "nope" not in cfg.as_dict()


def test_runtime_config_update_out_of_range(tmp_path):
    cfg = _fresh_config(tmp_path)
    try:
        cfg.update({"screen_change_threshold": 5})  # max 1.0
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_runtime_config_hardware_timing_seconds(tmp_path):
    cfg = _fresh_config(tmp_path)
    cfg.update({"click_hold_ms": 80, "type_inter_key_ms": 20})
    hw = cfg.hardware_timing_seconds()
    assert hw["click_hold"] == 0.08
    assert hw["char_delay"] == 0.02
    assert set(hw) == {"char_delay", "type_key_hold", "key_hold", "combo_mod",
                       "type_shift", "click_hold", "click_after"}


def test_runtime_config_reset(tmp_path):
    cfg = _fresh_config(tmp_path)
    cfg.update({"wait_poll_ms": 999})
    cfg.reset()
    assert cfg.get("wait_poll_ms") == DEFAULTS["wait_poll_ms"]
    assert cfg.runtime_keys == set()


def test_runtime_config_persist_and_reload(tmp_path):
    cfg = _fresh_config(tmp_path)
    cfg.update({"cursor_crop_radius": 222})
    saved = cfg.save()
    assert os.path.exists(saved)
    cfg2 = RuntimeConfig()  # same env path
    assert cfg2.get("cursor_crop_radius") == 222
    assert cfg2.file_loaded is True


def test_runtime_config_env_override(tmp_path):
    _fresh_config(tmp_path)  # sets path, clears RT env
    os.environ["SHKVM_RT_WAIT_POLL_MS"] = "321"
    try:
        cfg = RuntimeConfig()
        assert cfg.get("wait_poll_ms") == 321
        assert "wait_poll_ms" in cfg.env_keys
    finally:
        del os.environ["SHKVM_RT_WAIT_POLL_MS"]


# --- V2.1: changed-pixel-fraction diff metric ------------------------------

def test_diff_score_fraction_identical_zero():
    img = Image.new("L", (50, 50), 0)
    assert srv._diff_score(img, img.copy()) == 0.0


def test_diff_score_fraction_full_change_one():
    assert srv._diff_score(Image.new("L", (50, 50), 0),
                           Image.new("L", (50, 50), 255)) == 1.0


def test_diff_score_respects_pixel_delta():
    a = Image.new("L", (50, 50), 100)
    b = Image.new("L", (50, 50), 110)  # uniform delta of 10
    assert srv._diff_score(a, b, pixel_delta=30) == 0.0   # 10 < 30 -> no change
    assert srv._diff_score(a, b, pixel_delta=5) == 1.0    # 10 > 5  -> all changed


def test_diff_score_sparse_text_is_small_but_nonzero():
    a = Image.new("L", (100, 100), 0)
    b = a.copy()
    for x in range(10):
        for y in range(10):
            b.putpixel((x, y), 255)  # 100 of 10000 px = 1%
    score = srv._diff_score(a, b, pixel_delta=30)
    assert 0.005 < score < 0.02


# --- V2.1: config region/bool coercion -------------------------------------

def test_config_region_accepts_null_and_quad(tmp_path):
    cfg = _fresh_config(tmp_path)
    cfg.update({"terminal_region": [0, 500, 1920, 580]})
    assert cfg.get("terminal_region") == [0, 500, 1920, 580]
    cfg.update({"terminal_region": None})
    assert cfg.get("terminal_region") is None


def test_config_region_rejects_bad_shape(tmp_path):
    cfg = _fresh_config(tmp_path)
    raised = False
    try:
        cfg.update({"terminal_region": [1, 2, 3]})
    except ValueError:
        raised = True
    assert raised


def test_config_bool_coercion(tmp_path):
    cfg = _fresh_config(tmp_path)
    cfg.update({"clear_input_before_command": "false"})
    assert cfg.get("clear_input_before_command") is False
    cfg.update({"clear_input_before_command": True})
    assert cfg.get("clear_input_before_command") is True


def test_config_ocr_lang_keys(tmp_path):
    cfg = _fresh_config(tmp_path)
    assert cfg.get("ocr_lang") == "eng+jpn"
    assert cfg.get("ocr_fast_lang") == "eng"
    cfg.update({"ocr_fast_lang": "eng+jpn"})
    assert cfg.get("ocr_fast_lang") == "eng+jpn"


# --- V2.1: OCR lang override plumbing --------------------------------------

def test_extract_text_accepts_lang_override():
    img = Image.new("RGB", (400, 120), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 40)
    except Exception:
        font = ImageFont.load_default()
    draw.text((20, 30), "hello", fill="black", font=font)
    # explicit eng override must not raise and should read ASCII text
    txt = TerminalOCR().extract_text(img, lang="eng")
    assert "hello" in txt.lower()


# --- paste_unicode_text helpers --------------------------------------------

def test_paste_unicode_command_is_pure_ascii():
    cmd, payload, _ = srv._paste_unicode_build_command("日本語テスト")
    assert cmd.isascii(), "command must contain only ASCII"
    assert payload.isascii(), "payload must contain only ASCII"


def test_paste_unicode_command_excludes_original_text():
    text = "日本語テストと中文测试"
    cmd, _, _ = srv._paste_unicode_build_command(text)
    for ch in text:
        assert ch not in cmd, f"Original character {ch!r} found in command"


def test_paste_unicode_command_has_set_clipboard():
    cmd, _, _ = srv._paste_unicode_build_command("hello")
    assert "Set-Clipboard" in cmd


def test_paste_unicode_command_restores_base64_padding():
    cmd, _, _ = srv._paste_unicode_build_command("test")
    assert "while($b.Length%4){$b+='='}" in cmd


def test_paste_unicode_payload_roundtrip():
    import base64
    text = "日本語テストと中文测试 ABC 123"
    _, payload, utf8_bytes = srv._paste_unicode_build_command(text)
    # Re-add padding and decode
    padded = payload + "=" * ((-len(payload)) % 4)
    # Base64URL -> standard Base64
    standard = padded.replace("-", "+").replace("_", "/")
    decoded = base64.b64decode(standard).decode("utf-8")
    assert decoded == text


def test_paste_unicode_command_length_within_safety_cap():
    # 1200 CJK chars (max default) must not exceed the 7500-char hard limit.
    text = "亜" * 1200
    cmd, _, _ = srv._paste_unicode_build_command(text)
    assert len(cmd) <= srv._PASTE_CMD_MAX


def test_paste_unicode_estimate_seconds_basic():
    # 100 chars at 5+5 ms/char = 1.0 s
    assert srv._paste_unicode_estimate_seconds(100, 5, 5) == 1.0


def test_paste_unicode_estimate_seconds_normal_timing():
    # 1000 chars at 20+20 ms/char = 40.0 s
    assert srv._paste_unicode_estimate_seconds(1000, 20, 20) == 40.0


# --- ops tools: save_evidence label sanitizing ------------------------------

def test_sanitize_label_keeps_safe_chars():
    assert srv._sanitize_label("INC51031_F56-ST22") == "INC51031_F56-ST22"


def test_sanitize_label_replaces_unsafe_runs():
    assert srv._sanitize_label("INC 51031/F56:ST22 ") == "INC_51031_F56_ST22"


def test_sanitize_label_empty_inputs():
    assert srv._sanitize_label(None) == ""
    assert srv._sanitize_label("///") == ""


# --- ops tools: done-marker wrapping ----------------------------------------

def test_new_done_marker_uses_safe_alphabet():
    for _ in range(20):
        marker = srv._new_done_marker()
        assert marker.startswith("KVMDONE_")
        suffix = marker[len("KVMDONE_"):]
        assert len(suffix) == 4
        assert all(c in srv._MARKER_ALPHABET for c in suffix)


def test_wrap_command_with_marker_hides_marker_from_echo():
    marker = "KVMDONE_ACDE"
    wrapped = srv._wrap_command_with_marker("terraform plan", marker)
    # The typed line must NOT contain the assembled marker (would false-match
    # on the command echo), but must produce it when PowerShell evaluates it.
    assert marker not in wrapped
    assert wrapped.startswith("terraform plan; ")
    assert "Write-Output ('KV'+'MDONE_ACDE')" in wrapped


# --- ops tools: transfer_unicode_file command building -----------------------

def test_transfer_commands_are_pure_ascii():
    init_cmd, chunk_cmds, final_cmd, _, _, _ = srv._transfer_build_commands(
        "日本語の手順書\nライン2", r"C:\Temp\runbook.md")
    for cmd in [init_cmd, *chunk_cmds, final_cmd]:
        assert cmd.isascii()


def test_transfer_commands_roundtrip():
    import base64 as b64
    text = "日本語テスト ABC 123\n改行もOK"
    _, chunk_cmds, _, sha12, payload_chars, utf8_bytes = \
        srv._transfer_build_commands(text, r"C:\Temp\t.txt", chunk_chars=100)
    # Reassemble the payload exactly as the target-side PowerShell would.
    import re as _re
    payload = "".join(
        _re.search(r"-Value '([^']*)' -NoNewline$", c).group(1)
        for c in chunk_cmds)
    assert len(payload) == payload_chars
    padded = payload.replace("-", "+").replace("_", "/")
    padded += "=" * ((-len(padded)) % 4)
    decoded = b64.b64decode(padded)
    assert decoded.decode("utf-8") == text
    assert len(decoded) == utf8_bytes
    import hashlib as _hashlib
    assert _hashlib.sha256(decoded).hexdigest()[:12].upper() == sha12


def test_transfer_commands_escape_single_quotes_in_path():
    _, _, final_cmd, _, _, _ = srv._transfer_build_commands(
        "x", r"C:\Temp\o'brien.txt")
    assert r"o''brien.txt" in final_cmd


def test_transfer_final_command_hides_xfer_marker():
    _, _, final_cmd, _, _, _ = srv._transfer_build_commands("x", r"C:\t.txt")
    assert "XFER_OK" not in final_cmd          # echo-safety (split string)
    assert "'XF'+'ER_OK '" in final_cmd


def test_transfer_chunking_respects_chunk_size():
    text = "あ" * 500  # 500 CJK chars -> 2000 base64 chars
    _, chunk_cmds, _, _, payload_chars, _ = srv._transfer_build_commands(
        text, r"C:\t.txt", chunk_chars=300)
    assert len(chunk_cmds) == (payload_chars + 299) // 300


# --- ops tools: input lock ---------------------------------------------------

def _reset_lock():
    srv._input_lock = None


def test_input_lock_blocks_input_tools_only():
    _reset_lock()
    try:
        srv._do_set_input_lock(locked=True, reason="observing PRD F56")
        err = srv._check_input_lock("type_text", {})
        assert err is not None
        assert err["error"] == "input_locked"
        assert err["reason"] == "observing PRD F56"
        # Read-only tools stay available.
        assert srv._check_input_lock("get_screen_text_compact", {}) is None
        assert srv._check_input_lock("health", {}) is None
        assert srv._check_input_lock("save_evidence", {}) is None
    finally:
        _reset_lock()


def test_input_lock_allows_dry_run_variants():
    _reset_lock()
    try:
        srv._do_set_input_lock(locked=True)
        assert srv._check_input_lock("click_text", {"dry_run": True}) is None
        assert srv._check_input_lock("click_text", {}) is not None
        assert srv._check_input_lock("transfer_unicode_file", {"dry_run": True}) is None
    finally:
        _reset_lock()


def test_input_unlock_requires_confirm():
    _reset_lock()
    try:
        srv._do_set_input_lock(locked=True)
        res = srv._do_set_input_lock(locked=False)
        assert res["ok"] is False and res["error"] == "confirm_required"
        assert srv._input_lock is not None
        res = srv._do_set_input_lock(locked=False, confirm="UNLOCK")
        assert res["ok"] is True and res["locked"] is False
        assert srv._input_lock is None
    finally:
        _reset_lock()


def test_input_tools_cover_all_hid_generating_tools():
    expected = {
        "type_text", "send_key", "send_key_sequence",
        "mouse_move", "mouse_click", "mouse_drag", "mouse_scroll",
        "execute_and_read", "run_powershell_and_read", "run_wsl_and_read",
        "run_powershell_until_done", "open_shell", "click_text",
        "run_task_and_report", "paste_unicode_text", "transfer_unicode_file",
    }
    assert srv.INPUT_TOOLS == expected


# --- ops tools: multi-target config parsing ----------------------------------

def test_parse_targets_default_only():
    from mcp_serial_hid_kvm.config import parse_targets
    t = parse_targets(None, "127.0.0.1", 9329)
    assert t == {"default": {"host": "127.0.0.1", "port": 9329}}


def test_parse_targets_string_and_dict_forms():
    from mcp_serial_hid_kvm.config import parse_targets
    raw = ('{"aws-pc": "127.0.0.1:9329", '
           '"sap-pc": {"host": "192.168.1.20", "port": 9331}}')
    t = parse_targets(raw, "127.0.0.1", 9329)
    assert t["aws-pc"] == {"host": "127.0.0.1", "port": 9329}
    assert t["sap-pc"] == {"host": "192.168.1.20", "port": 9331}
    assert "default" in t


def test_parse_targets_ignores_bad_entries_and_bad_json():
    from mcp_serial_hid_kvm.config import parse_targets
    t = parse_targets('{"bad": 42, "ok": "h:1"}', "127.0.0.1", 9329)
    assert "bad" not in t
    assert t["ok"] == {"host": "h", "port": 1}
    t2 = parse_targets("not json", "127.0.0.1", 9329)
    assert list(t2) == ["default"]


def test_select_target_unknown_name_lists_known():
    res = srv._do_select_target(name="nope-such-target")
    assert res["ok"] is False
    assert res["error"] == "unknown_target"
    assert "default" in res["known"]


def test_select_target_requires_name_or_hostport():
    res = srv._do_select_target()
    assert res["ok"] is False
    assert res["error"] == "missing_target"


# --- hidden tools -------------------------------------------------------------

def test_parse_hidden_tools_default_and_overrides():
    from mcp_serial_hid_kvm.config import DEFAULT_HIDDEN_TOOLS, parse_hidden_tools
    assert parse_hidden_tools(None) == set(DEFAULT_HIDDEN_TOOLS)
    assert parse_hidden_tools("none") == set()
    assert parse_hidden_tools("") == set()
    assert parse_hidden_tools("a, b ,c") == {"a", "b", "c"}


def test_list_tools_filters_hidden_but_keeps_them_callable():
    import asyncio
    from mcp_serial_hid_kvm.config import DEFAULT_HIDDEN_TOOLS, config

    old = config.hidden_tools
    try:
        config.hidden_tools = set(DEFAULT_HIDDEN_TOOLS)
        names = {t.name for t in asyncio.run(srv.list_tools())}
        assert names.isdisjoint(DEFAULT_HIDDEN_TOOLS)
        # replacements are exposed
        assert {"run_powershell_and_read", "get_screen_text_compact",
                "wait_for_screen_change", "type_text", "send_key"} <= names
        # hidden tools are still dispatchable (hidden != disabled): the
        # dispatcher must not answer "Unknown tool" for them.
        config.hidden_tools = set()
        all_names = {t.name for t in asyncio.run(srv.list_tools())}
        assert DEFAULT_HIDDEN_TOOLS <= all_names
    finally:
        config.hidden_tools = old

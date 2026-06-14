"""Deterministic unit tests for the Route A wrapper-tool helpers.

These cover the pure logic (text bounding, image diff, WSL command shaping)
and structured OCR, without requiring KVM hardware or a running API server.
"""

from PIL import Image, ImageDraw, ImageFont

import mcp_serial_hid_kvm.server as srv
from mcp_serial_hid_kvm.ocr import TerminalOCR


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

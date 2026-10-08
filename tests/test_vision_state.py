"""Synthetic frames and a virtual clock; no hardware, network or wall-time waits."""

import asyncio

import numpy as np
import pytest
from PIL import Image, ImageDraw

from mcp_serial_hid_kvm.vision import StateEngine


def frame(value=0, size=(1920, 1080)):
    return Image.new("RGB", size, (value, value, value))


class Clock:
    def __init__(self):
        self.now = 0.0
        self.on_sleep = None

    def time(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        if self.on_sleep:
            self.on_sleep()


def run_wait(engine, method, capture, **kwargs):
    clock = Clock()
    return asyncio.run(getattr(engine, method)(
        capture, clock=clock.time, sleep=clock.sleep, **kwargs))


def test_identical_frames():
    result = StateEngine().compare(frame(), frame())
    assert result["score"] == 0 and not result["changed"] and result["regions"] == []


def test_full_screen_change():
    result = StateEngine().compare(frame(), frame(255))
    assert result["changed"] and result["score"] == 1
    assert result["regions"] == [{"x": 0, "y": 0, "w": 1920, "h": 1080, "area_ratio": 1}]


def test_sensor_and_sparse_pixel_noise_ignored():
    rng = np.random.default_rng(10)
    baseline = np.full((1080, 1920, 3), 100, dtype=np.uint8)
    noisy = np.clip(baseline.astype(np.int16) + rng.integers(-8, 9, baseline.shape), 0, 255).astype(np.uint8)
    noisy[100:105, 100:105] = 255
    result = StateEngine().compare(baseline, noisy)
    assert result["score"] == 0 and result["regions"] == []


def test_jpeg_noise_ignored():
    import io
    image = frame(100)
    ImageDraw.Draw(image).rectangle((300, 200, 1200, 800), fill="white")
    def jpeg(quality):
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=quality)
        return Image.open(io.BytesIO(buf.getvalue()))
    assert StateEngine().compare(jpeg(60), jpeg(95))["score"] == 0


@pytest.mark.parametrize("box", [(720, 330, 400, 300), (0, 0, 400, 300), (1520, 780, 400, 300)])
def test_dialog_box_maps_back_to_original_capture(box):
    current = frame()
    x, y, w, h = box
    ImageDraw.Draw(current).rectangle((x, y, x+w-1, y+h-1), fill="white")
    result = StateEngine().compare(frame(), current)
    assert result["changed"] and len(result["regions"]) == 1
    actual = result["regions"][0]
    for key, expected in zip(("x", "y", "w", "h"), box):
        assert abs(actual[key]-expected) <= 12
    assert 0 <= actual["x"] < 1920 and actual["x"]+actual["w"] <= 1920
    assert 0 <= actual["y"] < 1080 and actual["y"]+actual["h"] <= 1080
    assert abs(actual["area_ratio"] - 400*300/(1920*1080)) < 0.01


def test_roi_uses_original_offset_and_roi_score():
    current = frame()
    ImageDraw.Draw(current).rectangle((720, 330, 1119, 629), fill="white")
    result = StateEngine().compare(frame(), current, region=[600, 200, 800, 600])
    box = result["regions"][0]
    assert abs(box["x"]-720) <= 4 and abs(box["y"]-330) <= 4
    assert abs(result["score"]-0.25) < 0.02
    assert StateEngine().compare(frame(), current, region=[0, 0, 300, 200])["score"] == 0


@pytest.mark.parametrize("size", [(1920, 1280), (1919, 1279)])
def test_nonstandard_height_and_rounding_preserve_capture_coordinates(size):
    current = frame(size=size)
    ImageDraw.Draw(current).rectangle((700, 900, 1099, 1199), fill="white")
    result = StateEngine().compare(frame(size=size), current)
    assert (result["width"], result["height"]) == size
    assert result["changed"] and len(result["regions"]) == 1
    box = result["regions"][0]
    assert abs(box["y"]-900) <= 12 and abs(box["h"]-300) <= 16


@pytest.mark.parametrize("shape", [(2, 5, 10), (4, 5, 20), (20, 20, 300)])
def test_cursor_caret_and_small_animation_filtered(shape):
    w, h, x = shape
    current = frame()
    ImageDraw.Draw(current).rectangle((x, 200, x+w, 200+h), fill="white")
    assert StateEngine().compare(frame(), current)["score"] == 0


def test_minimum_area_controls_component_filter():
    current = frame()
    ImageDraw.Draw(current).rectangle((200, 200, 249, 249), fill="white")
    engine = StateEngine()
    assert engine.compare(frame(), current, min_changed_area=0.01)["regions"] == []
    assert engine.compare(frame(), current, min_changed_area=0, threshold=0)["changed"]


def test_resolution_change_requires_fresh_baseline():
    with pytest.raises(ValueError, match="resolution changed"):
        StateEngine().compare(frame(), frame(size=(1280, 720)))


@pytest.mark.parametrize("region", [[-1, 0, 10, 10], [0, 0, 0, 10], [0, 0, 9999, 10], [1, 2, 3], [0.5, 0, 10, 10]])
def test_invalid_roi_rejected(region):
    with pytest.raises(ValueError, match="region"):
        StateEngine().compare(frame(), frame(), region=region)


def test_baseline_copied_and_default_roi_inherited():
    engine = StateEngine()
    baseline = np.zeros((100, 100), dtype=np.uint8)
    engine.set_baseline(baseline, [0, 0, 50, 50])
    baseline[60:] = 255
    assert engine.observe(baseline)["score"] == 0
    assert engine.observe(baseline, region=[0, 0, 100, 100])["changed"]


def test_auto_baseline_uses_exact_current_frame():
    engine = StateEngine()
    assert engine.observe(frame())["error"] == "no_baseline"
    result = engine.observe(frame(), auto_baseline=True)
    assert result["baseline_created"] and not result["changed"]


def test_wait_for_change_timeout():
    engine = StateEngine()
    result = run_wait(engine, "wait_for_change", frame, timeout_seconds=0.5, poll_ms=200)
    assert not result["changed"] and result["timed_out"]
    assert result["attempts"] == 4 and result["elapsed_ms"] == 500


def test_wait_for_change_success_after_seed():
    engine = StateEngine()
    frames = iter([frame(), frame(), frame(255)])
    result = run_wait(engine, "wait_for_change", lambda: next(frames), poll_ms=200)
    assert result["changed"] and result["attempts"] == 3
    assert result["elapsed_ms"] == 400 and result["regions"]


def test_wait_for_change_sees_immediate_pre_action_change():
    engine = StateEngine()
    engine.set_baseline(frame())
    result = run_wait(engine, "wait_for_change", lambda: frame(255), timeout_seconds=0)
    assert result["changed"] and result["attempts"] == 1
    assert engine.observe(frame(255))["changed"]  # baseline retained


def test_wait_can_update_baseline():
    engine = StateEngine()
    engine.set_baseline(frame())
    result = run_wait(engine, "wait_for_change", lambda: frame(255), update_baseline_on_change=True)
    assert result["changed"] and engine.observe(frame(255))["score"] == 0


def test_wait_for_change_missing_baseline_no_reseed():
    result = run_wait(StateEngine(), "wait_for_change", frame, auto_baseline=False)
    assert result["error"] == "no_baseline"


def test_continuously_changing_never_stable():
    frames = iter([frame(0), frame(255), frame(0), frame(255)])
    result = run_wait(StateEngine(), "wait_for_stable", lambda: next(frames),
                      timeout_seconds=0.5, poll_ms=200, stable_frames=2)
    assert not result["stable"] and result["timed_out"] and result["stable_frames"] == 0


def test_stable_requires_n_consecutive_comparisons():
    frames = iter([frame(), frame(), frame(255), frame(255), frame(255), frame(255)])
    result = run_wait(StateEngine(), "wait_for_stable", lambda: next(frames), stable_frames=3)
    assert result["stable"] and result["stable_frames"] == 3 and result["attempts"] == 6


def test_single_capture_cannot_prove_stability():
    result = run_wait(StateEngine(), "wait_for_stable", frame, timeout_seconds=0)
    assert not result["stable"] and result["stable_frames"] == 0


@pytest.mark.parametrize("method", ["wait_for_change", "wait_for_stable"])
def test_reset_invalidates_inflight_wait(method):
    clock, engine = Clock(), StateEngine()
    clock.on_sleep = engine.reset
    result = asyncio.run(getattr(engine, method)(frame, clock=clock.time, sleep=clock.sleep))
    assert result["error"] == "context_changed" and result["attempts"] == 1
    assert engine.baseline is None and engine.stable_count == 0 and engine.last_regions == []


@pytest.mark.parametrize("kwargs", [{"poll_ms": 0}, {"threshold": -1}, {"threshold": float("nan")},
                                    {"timeout_seconds": -1}, {"timeout_seconds": float("inf")}])
@pytest.mark.parametrize("method", ["wait_for_change", "wait_for_stable"])
def test_bad_wait_arguments_rejected(method, kwargs):
    with pytest.raises(ValueError):
        run_wait(StateEngine(), method, frame, **kwargs)

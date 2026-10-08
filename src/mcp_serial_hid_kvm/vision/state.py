"""Transport-independent OpenCV state engine.

Accepts PIL images or uint8 numpy frames (OpenCV BGR/BGRA or grayscale).
Regions and result boxes use original capture pixels, never HID coordinates.
Scores measure the fraction of retained changed pixels within the selected ROI.
"""

import asyncio
import math
import time
from collections.abc import Callable

import cv2
import numpy as np
from PIL import Image


def _bounded(value, name, low, high):
    if isinstance(value, bool) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def _frame(image):
    if isinstance(image, Image.Image):
        image = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2BGR)
    array = np.asarray(image)
    if (array.dtype != np.uint8 or array.ndim not in (2, 3)
            or not array.size or (array.ndim == 3 and array.shape[2] not in (3, 4))):
        raise ValueError("frame must be a non-empty uint8 grayscale/BGR/BGRA image")
    return array


def _roi(region, width, height):
    if region is None:
        return 0, 0, width, height
    if (not isinstance(region, (list, tuple)) or len(region) != 4
            or any(type(v) is not int for v in region)):
        raise ValueError("region must be [x, y, w, h] in capture pixels")
    x, y, w, h = region
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
        raise ValueError("region must lie inside the capture frame and have positive size")
    return x, y, w, h


class StateEngine:
    def __init__(self, *, downscale_width=480, pixel_delta=20,
                 min_changed_area=0.001):
        self.downscale_width = int(_bounded(downscale_width, "downscale_width", 32, 4096))
        self.pixel_delta = int(_bounded(pixel_delta, "pixel_delta", 0, 255))
        self.min_changed_area = _bounded(min_changed_area, "min_changed_area", 0, 1)
        self.generation = 0
        self.reset()

    def reset(self):
        """Invalidate all observations, including waits currently suspended."""
        self.generation += 1
        self.baseline = None
        self.baseline_region = None
        self.last_regions = []
        self.stable_count = 0

    def set_baseline(self, image, region=None):
        frame = _frame(image)
        height, width = frame.shape[:2]
        _roi(region, width, height)
        self.baseline = frame.copy()
        self.baseline_region = list(region) if region is not None else None
        self.last_regions = []
        self.stable_count = 0
        return {"ok": True, "width": width, "height": height, "region": self.baseline_region}

    def compare(self, baseline, current, *, threshold=0.02, region=None,
                min_changed_area=None):
        _bounded(threshold, "threshold", 0, 1)
        minimum = self.min_changed_area if min_changed_area is None else min_changed_area
        _bounded(minimum, "min_changed_area", 0, 1)
        a, b = _frame(baseline), _frame(current)
        if a.shape[:2] != b.shape[:2]:
            raise ValueError("capture resolution changed; capture a fresh baseline")
        height, width = b.shape[:2]
        x, y, w, h = _roi(region, width, height)
        dw = min(w, self.downscale_width)
        dh = max(1, round(h * dw / w))

        def prepare(frame):
            frame = cv2.resize(frame[y:y+h, x:x+w], (dw, dh), interpolation=cv2.INTER_AREA)
            if frame.ndim == 3:
                code = cv2.COLOR_BGRA2GRAY if frame.shape[2] == 4 else cv2.COLOR_BGR2GRAY
                frame = cv2.cvtColor(frame, code)
            return cv2.GaussianBlur(frame, (5, 5), 0)

        delta = cv2.absdiff(prepare(a), prepare(b))
        _, mask = cv2.threshold(delta, self.pixel_delta, 255, cv2.THRESH_BINARY)
        kernel = np.ones((3, 3), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        count, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        regions = []
        changed_pixels = 0
        for index in range(1, count):
            left, top, bw, bh, area = (int(v) for v in stats[index])
            if area < max(9, math.ceil(minimum * dw * dh)):
                continue
            changed_pixels += area
            # Round outwards so the original-resolution box contains the component.
            rx = x + math.floor(left * w / dw)
            ry = y + math.floor(top * h / dh)
            right = x + min(w, math.ceil((left + bw) * w / dw))
            bottom = y + min(h, math.ceil((top + bh) * h / dh))
            regions.append({"x": rx, "y": ry, "w": right-rx, "h": bottom-ry,
                            "area_ratio": area / (dw * dh)})
        regions.sort(key=lambda box: (-box["area_ratio"], box["y"], box["x"]))
        score = changed_pixels / (dw * dh)
        return {"changed": score > threshold, "score": score, "threshold": threshold,
                "regions": regions, "width": width, "height": height}

    def observe(self, image, *, auto_baseline=False, region=None, **kwargs):
        # Validate even when seeding; invalid requests must not mutate state.
        _bounded(kwargs.get("threshold", 0.02), "threshold", 0, 1)
        if kwargs.get("min_changed_area") is not None:
            _bounded(kwargs["min_changed_area"], "min_changed_area", 0, 1)
        if self.baseline is None:
            if not auto_baseline:
                return {"ok": False, "error": "no_baseline",
                        "detail": "Call capture_screen/set_screen_baseline before acting."}
            self.set_baseline(image, region)
            return {"changed": False, "score": 0.0, "regions": [], "baseline_created": True}
        selected = self.baseline_region if region is None else region
        result = self.compare(self.baseline, image, region=selected, **kwargs)
        self.last_regions = result["regions"]
        return result

    @staticmethod
    def _wait_args(timeout_seconds, poll_ms, threshold):
        _bounded(timeout_seconds, "timeout_seconds", 0, 600)
        _bounded(poll_ms, "poll_ms", 10, 60000)
        _bounded(threshold, "threshold", 0, 1)

    async def wait_for_change(self, capture: Callable, *, timeout_seconds=30,
                              poll_ms=200, threshold=0.02, region=None,
                              min_changed_area=None, auto_baseline=True,
                              update_baseline_on_change=False,
                              clock=time.monotonic, sleep=asyncio.sleep):
        self._wait_args(timeout_seconds, poll_ms, threshold)
        generation = self.generation
        start = clock()
        attempts = 0
        # Keep a fixed baseline throughout this wait; another observation must
        # not move its reference frame while it is suspended between polls.
        baseline = None if self.baseline is None else self.baseline.copy()
        selected = self.baseline_region if region is None else region
        while True:
            if generation != self.generation:
                return {"ok": False, "error": "context_changed", "changed": False,
                        "attempts": attempts, "elapsed_ms": int((clock()-start)*1000)}
            image = capture()
            attempts += 1
            if baseline is None:
                result = self.observe(image, auto_baseline=auto_baseline, region=selected,
                                      threshold=threshold, min_changed_area=min_changed_area)
                if result.get("error"):
                    return result
                baseline = self.baseline.copy()
            else:
                result = self.compare(baseline, image, threshold=threshold,
                                      region=selected, min_changed_area=min_changed_area)
                self.last_regions = result["regions"]
            elapsed = clock()-start
            result.update(elapsed_ms=int(elapsed*1000), attempts=attempts)
            if result["changed"]:
                if update_baseline_on_change:
                    self.set_baseline(image, selected)
                return result
            if elapsed >= timeout_seconds:
                return {**result, "timed_out": True}
            await sleep(min(poll_ms/1000, timeout_seconds-elapsed))

    async def wait_for_stable(self, capture: Callable, *, timeout_seconds=30,
                              poll_ms=200, stable_frames=4, threshold=0.001,
                              region=None, min_changed_area=None,
                              clock=time.monotonic, sleep=asyncio.sleep):
        self._wait_args(timeout_seconds, poll_ms, threshold)
        if min_changed_area is not None:
            _bounded(min_changed_area, "min_changed_area", 0, 1)
        if type(stable_frames) is not int or not 1 <= stable_frames <= 1000:
            raise ValueError("stable_frames must be an integer between 1 and 1000")
        generation = self.generation
        start = clock()
        previous = None
        attempts = streak = 0
        self.stable_count = 0
        while True:
            if generation != self.generation:
                return {"ok": False, "error": "context_changed", "stable": False,
                        "attempts": attempts, "elapsed_ms": int((clock()-start)*1000)}
            image = _frame(capture()).copy()
            attempts += 1
            _roi(region, image.shape[1], image.shape[0])
            score = 0.0
            if previous is not None:
                result = self.compare(previous, image, threshold=threshold, region=region,
                                      min_changed_area=min_changed_area)
                score = result["score"]
                streak = streak+1 if score <= threshold else 0
            previous = image
            self.stable_count = streak
            elapsed = clock()-start
            result = {"stable": streak >= stable_frames, "score": score,
                      "elapsed_ms": int(elapsed*1000), "attempts": attempts,
                      "stable_frames": streak, "required_stable_frames": stable_frames}
            if result["stable"]:
                return result
            if elapsed >= timeout_seconds:
                return {**result, "timed_out": True}
            await sleep(min(poll_ms/1000, timeout_seconds-elapsed))

# Observe an action in one MCP call

`mouse_scroll`, `mouse_click`, `send_key` and `type_text` accept optional `after`.
Omit the field to keep the original behavior: only the JSON action result.
`after: {}` means no cursor parking, the usual stability wait and a full-screen
image. Typing and pressing Enter can still be separate calls.

```json
{
  "amount": -5,
  "x": 1750,
  "y": 608,
  "after": {
    "park_cursor": {"x": 98, "y": 50},
    "wait": {"mode": "stable", "timeout_seconds": 10},
    "screenshot": {"max_width": 1280}
  }
}
```

Send this as `mouse_scroll`. Positive amount scrolls up; negative scrolls down.
`park_cursor` moves **after** the action. Its x/y are absolute **HID screen
coordinates**, as reported by `get_device_info`, not resized screenshot pixels.
Omit it to leave the cursor where the action put it. It obeys the input lock,
active target context and file-copy ownership checks.

| `after.wait` | Behavior |
|---|---|
| `{"mode":"none"}` | Capture immediately after action/parking |
| `{"mode":"delay","seconds":0.5}` | Fixed delay, then capture |
| `{"mode":"stable"}` (default) | Existing `wait_for_stable` options and runtime defaults |

Stable options: `timeout_seconds`, `poll_ms`, `threshold`, `stable_frames`,
`region`, `min_changed_area`. Stable ROI uses original capture pixels and must
lie inside the frame. Delay requires finite nonnegative `seconds`. Both waits
are capped by runtime `max_wait_seconds`; options for a different mode are
rejected. Change waiting is available separately through `wait_for_change` with
a pre-action baseline; it is not an `after` mode.

`after.screenshot` accepts the existing `capture_screen` view options:
`region`, `zoom`, `max_width`, `grid`. Crop coordinates use original capture
pixels; zoom requires a region, max_width only shrinks, and grid labels use HID
coordinates. The response retains capture/HID/output dimensions, region, scale
and `to_hid` conversion. The baseline is always the full untransformed frame.

All action and after parameters are validated before sending HID input,
including the IME-off key. If ROI bounds need the actual frame dimensions, a
read-only capture is taken during preparation; invalid requests send no input.
With no ROI, the usual path needs no preparation capture. If the display changes
resolution after input, view processing can still fail; the action result is
retained.

## Result and failures

Returns an image followed by one JSON text object. If no image is available,
returns the JSON text alone. For example (times are illustrative milliseconds):

```json
{
  "ok": true,
  "tool": "mouse_scroll",
  "target": {"name": "target1", "host": "127.0.0.1", "port": 9329},
  "verified": false,
  "verification": "calling_model_required",
  "preparation_ms": 0.7,
  "action": {"status": "ok", "sent": true, "message": "Scrolled down by 5", "elapsed_ms": 75.1},
  "park_cursor": {"status": "ok", "x": 98, "y": 50, "elapsed_ms": 20.2},
  "wait": {"status": "ok", "mode": "stable", "stable": true, "score": 0, "attempts": 5, "stable_frames": 4, "required_stable_frames": 4, "elapsed_ms": 900.3},
  "screenshot": {"status": "ok", "source": "wait_last_frame", "capture": "1920x1080", "hid": "1920x1080", "image": "1280x720", "region": [0, 0, 1920, 1080], "scale": 0.6667, "to_hid": "hid_x = 0 + image_x * 1.5; hid_y = 0 + image_y * 1.5", "elapsed_ms": 30.4},
  "elapsed_ms": 1026.7
}
```

- Each stage has `status` and `elapsed_ms`; omitted parking/no wait reports
  `skipped`. Total `elapsed_ms` includes validation/preparation and all stages.
- `action.sent=true` means the action's HID requests were acknowledged. It does
  not verify focus, command execution or business success. Transport/partial
  input failure reports `status="unknown", sent=null` plus `error`; subsequent
  stages are skipped. Observe before deciding any recovery; never blindly replay.
- If input becomes locked or copy-owned during read-only preparation, the final
  guard refuses input with `status="not_sent", sent=false` and an `error`.
- Parking failure leaves the action status intact and still attempts observation.
- Stability timeout reports `wait.status="timeout", timed_out=true`, keeps the
  action result and returns the last observed frame. Top-level `ok` is false.
- A waiting/capture error reports `failed` and its `error`. A failed wait still
  attempts a current screenshot. A screenshot/encoding failure keeps all earlier
  stage results and timings. HID input is never automatically retried.
- Target/capture switches invalidate the observation. No screenshot or baseline
  from the new context is attached to an action on the previous context.
- Pixel stability is not business success: the calling model must inspect the
  image. No OCR, multi-screen collection or automatic scroll loop is involved.

Stable waits pass a per-call capture callback to the existing state engine.
The callback retains the original JPEG and its full decoded frame; the last
pair is used directly for the returned screenshot, including on timeout. No
extra capture is made after a successful stable wait. Untransformed images
reuse the exact JPEG bytes; crops/resizes/grids encode that same frame.
`wait_for_stable` and `wait_for_change` keep their existing standalone results.
No action-after images are saved to disk.

## Timing comparison

Use the same target, initial screen, scroll location/amount, parking location,
wait thresholds/ROI and screenshot view options for both paths. Record host-side
end-to-end time and MCP call/capture counts for the old sequence (action, move,
wait, capture) and the combined call. The combined response's stage timings
separate HID time, parking, polling/capture/processing time and screenshot
formatting/encoding. With `source="wait_last_frame"`, the acquisition cost is
already in the wait stage; the screenshot stage does not acquire another frame.
With none/delay, acquisition is in the screenshot stage. Preparation captures
and timing are reported separately from action delivery.

Compare repeated samples with the same operation; report measured results and
environment rather than promising a speedup ratio. Fewer MCP calls and duplicate
captures do not automatically reduce model round trips or per-screen judgments,
especially when the old tools were already batched into one model turn.

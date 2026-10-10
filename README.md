# mcp-serial-hid-kvm

Windows saved files can also be copied from Target to Host over the existing
HID/HDMI channels, with asynchronous jobs, verified chunks and whole-file SHA-256.
See [file copy workflow and limits](docs/file-copy.md) for the four tools, required
focus observations, cancellation/recovery and cleanup. This copies raw bytes;
exporting an application's unsaved state is a separate application workflow.

MCP server for keyboard, mouse, capture and image-state detection on physical
Target PCs. Hardware operations delegate to [serial-hid-kvm](https://github.com/cloudbt/serial-hid-kvm) over JSON Lines TCP.

**Vision interpretation belongs to the calling AI model.**
**mcp-serial-hid-kvm does NOT perform OCR or semantic UI understanding.**
The MCP server never calls a Vision model or imports a model SDK. Text, buttons,
window identity and command outcomes are interpreted by the MCP Client / Agent.

## Architecture

```text
Vision Model / Agent (screen interpretation and action decisions)
  ↕ MCP stdio
mcp-serial-hid-kvm   (capture + HID + OpenCV image state)
  ↕ TCP localhost:9329
serial-hid-kvm      (owns serial adapter and HDMI capture)
  ↕ USB serial + HDMI
Target PC
```

```text
src/mcp_serial_hid_kvm/
  server.py          MCP handlers → KvmClient calls
  config.py          endpoints, targets.json, capture/evidence settings
  runtime_config.py  timing and wait defaults
  vision/state.py    transport-independent image/frame algorithms and waits
  vision/tools.py    MCP image-state schemas
```

The image pipeline crops an optional ROI, downscales to at most 480 pixels wide
while keeping aspect ratio, converts to grayscale, then applies Gaussian blur,
absdiff, pixel-delta threshold (20), morphology and connected-component filtering.
JPEG bytes are never compared. Components below 0.001 of the ROI or below nine
processed pixels are ignored by default. This reduces capture/compression noise,
small cursor changes, blinking carets and small animations. Large animations
still count; select an ROI or tune thresholds. The engine accepts PIL images or
uint8 numpy frames in OpenCV BGR/BGRA/grayscale format.

## Installation

Requires Python 3.10+, CH9329-compatible HID, HDMI capture and a running TCP server:

```bash
pip install -e /path/to/serial-hid-kvm
serial-hid-kvm --api --headless
pip install -e .
```

Dependencies: `serial-hid-kvm`, `mcp>=1.0.0`, `pillow>=10.0.0`, `numpy>=1.24.0`,
`opencv-python>=4.8.0`. OpenCV matches the hardware server; do not install
`opencv-python-headless` alongside it. No external text-recognition executable
or language files are required.

## MCP client configuration

```json
{
  "mcpServers": {
    "kvm": {
      "command": "python",
      "args": ["-m", "mcp_serial_hid_kvm.server"],
      "env": {"SHKVM_API_HOST": "127.0.0.1", "SHKVM_API_PORT": "9329"}
    }
  }
}
```

Use your virtualenv Python path when needed. The `mcp-serial-hid-kvm` console
entry point is also available.

## Agent loop

```text
capture_screen (also stores the full capture as the pre-action baseline)
→ Vision model decides action
→ mouse_click / send_key / type_text
→ wait_for_change
→ wait_for_stable
→ capture_screen
→ Vision model verifies result
```

Capture before acting: `auto_baseline=true` seeds a missing baseline from the
first polling frame. An immediate change that occurred before that frame cannot
be recovered. `set_screen_baseline` explicitly sets a reference without returning
an image. Change comparisons retain the reference; optional
`update_baseline_on_change=true` replaces it on detection. Stability uses
consecutive frames and does not change the action baseline.

**Stable does not mean loaded, successful or command-complete.** A static error
page, stalled loader, or command with no visible output can all look stable.
The calling model must inspect the final capture.

## Available tools

29 tools are callable; 23 are advertised by default. Six tools marked hidden
are setup tools or compatibility aliases. `SHKVM_HIDDEN_TOOLS=none` advertises
all retained tools; removed tools cannot be exposed or invoked.

| Tool | Purpose |
|---|---|
| `type_text` | ASCII HID, inline tags, optional raw mode; actual newlines press Enter |
| `send_key` | One HID key with optional modifiers |
| `send_key_sequence` *(hidden)* | Multiple HID steps with delays |
| `mouse_move` | Absolute/relative HID cursor movement |
| `mouse_click` | Optional coordinate and button |
| `mouse_drag` | Press, move, release |
| `mouse_scroll` | Wheel movement |
| `capture_screen` | JPEG image and full-frame baseline |
| `set_screen_baseline` | Full capture and optional default ROI; compact metadata |
| `wait_for_change` | Wait against baseline; `{changed,score,elapsed_ms,attempts,regions}` |
| `wait_for_stable` | N consecutive low-change comparisons; `{stable,score,elapsed_ms,attempts,stable_frames}` |
| `get_changed_regions` | Filtered `{x,y,w,h,area_ratio}` boxes and score |
| `screen_changed` *(hidden, deprecated)* | Alias of `get_changed_regions`, same OpenCV engine |
| `wait_for_screen_change` *(hidden, deprecated)* | Alias of `wait_for_change`, same OpenCV engine |
| `cursor_crop` | Image crop around explicit or tracked capture coordinates |
| `health` | `{ok,api,serial,video,capture_device,resolution,target,input_locked,errors}` |
| `get_device_info` | Serial/capture/HID configuration |
| `list_capture_devices` *(hidden)* | Capture-device inventory |
| `set_capture_device` *(hidden)* | Select device; invalidate image state on success |
| `set_capture_resolution` *(hidden)* | Request resolution; invalidate image state on success |
| `open_shell` | HID shell launch only; `verified=false`, calling model verifies focus |
| `paste_unicode_text` | Base64URL → Target clipboard; dry-run sizes, SHA-256 and timing estimate |
| `transfer_unicode_file` | Chunked Base64URL → Target UTF-8 file; dry-run sizes and expected hash prefix |
| `save_evidence` | Save capture/ROI to Host evidence directory |
| `list_targets` | Configured endpoints and active Target |
| `select_target` | Ping before switching; keep old connection if unreachable |
| `set_input_lock` | Refuse HID tools; unlock requires `confirm="UNLOCK"` |
| `configure` | Runtime defaults/HID timing, optional persistence |
| `get_timing` | Effective defaults and source metadata |

### Image-state parameters

| Parameter | Meaning / default |
|---|---|
| `timeout_seconds` | 30; capped by runtime `max_wait_seconds` (60); 0 performs one observation |
| `poll_ms` | 200 ms delay between fresh captures; capture/processing time adds to cadence |
| `threshold` | Changed-pixel fraction of ROI; change `score > threshold` (0.02), stable `score <= threshold` (0.001) |
| `region` | Optional `[x,y,w,h]` in original capture pixels; positive size, fully inside frame |
| `min_changed_area` | Minimum connected-component area / ROI area; default 0.001 |
| `stable_frames` | Consecutive low-change comparisons, default 4; requires at least N+1 frames |
| `auto_baseline` | Change wait: true; one-shot regions: false |
| `update_baseline_on_change` | Optional change-wait/legacy parameter; false |

Score and `area_ratio` use retained component pixels / ROI pixels, rather than
bounding-box area. Boxes round outwards and map to **original capture resolution**
including ROI offsets. Capture coordinates may differ from HID dimensions; map
using `get_device_info` before clicking.

Change waits inherit the ROI saved by `set_screen_baseline` when no ROI is supplied.
Stable waits use the supplied ROI or full screen. Resolution mismatch is an error
requiring a fresh capture/baseline; different frame sizes are never silently
resized into comparable contexts. Timeout returns the last score and
`timed_out=true`. Capture/transport failures remain errors.

### Unicode and verification

Payloads are encoded on the Host and typed into Target PowerShell. Temporary
hardware timing is restored in `finally`; `dry_run=true` never sends input.
Clipboard results report `clipboard_requested=true, set_clipboard=null,
verified=false`; file results report `write_requested=true, written=null,
verified=false`. Delivery attempts do not claim target execution or verification.
The Target still echoes a marker/hash prefix for the calling model to inspect.

Automatic paste was removed: legacy `paste_after_set=true` returns
`vision_verification_required` before HID input. Capture and verify the clipboard
operation and intended field, then send Ctrl+V separately. `open_shell` and file
transfer no longer offer server-side `verify` parameters.

## Configuration and multiple Targets

| Variable | Default / purpose |
|---|---|
| `SHKVM_API_HOST` / `SHKVM_API_PORT` | `127.0.0.1` / `9329` |
| `SHKVM_TARGETS` | Named endpoint JSON, e.g. `{"target1":"127.0.0.1:9329","target2":"127.0.0.1:9331"}` |
| `SHKVM_TARGETS_CONFIG` | Shared `%LOCALAPPDATA%/serial-hid-kvm/targets.json`; only endpoints read |
| `SHKVM_CAPTURE_LOG_DIR` | Platform capture cache; empty disables |
| `SHKVM_EVIDENCE_DIR` | `~/Documents/kvm-evidence` on Windows, `~/kvm-evidence` elsewhere; empty disables |
| `SHKVM_HIDDEN_TOOLS` | Comma-separated override; `none` exposes all retained tools |
| `SHKVM_RUNTIME_CONFIG` | Runtime JSON; falls back to `MCP_SERIAL_HID_KVM_RUNTIME_CONFIG`, then platform user directory |
| `SHKVM_RT_<KEY>` | Runtime overrides, e.g. `WAIT_POLL_MS`, `SCREEN_STABLE_FRAMES`, `SCREEN_STABLE_THRESHOLD` |

Runtime layers: defaults → file → environment → `configure`. Removed keys in
existing JSON are ignored with warnings; remove them from your file. `get_timing`
lists supported keys. Hardware/layout settings belong to `serial-hid-kvm`.

Run independent hardware servers for two Targets (e.g. ports 9329 and 9331).
Successful `select_target` resets baseline, regions, stability history, screen
size, cursor and shell hints. Suspended waits return `context_changed`; restart
after capturing the intended Target. Failed candidate ping keeps the current
client and all state. The input lock persists across switches and allows capture,
all state tools, health and evidence. One MCP process has one active Target;
separate processes provide independent contexts.

## Migration / breaking changes

Removed from registration **and dispatch**, including hidden calls:
`get_screen_text`, `get_screen_text_compact`, `detect_text_elements`, `click_text`,
`wait_for_text`, `get_terminal_output`, `execute_and_read`, `run_powershell_and_read`,
`run_wsl_and_read`, `run_powershell_until_done`, `run_task_and_report`.
Use capture + Vision interpretation and explicit HID.

Health no longer returns a text-engine status field. Language/terminal settings
and the old PIL diff tuning key were removed. Old change aliases keep their
names but now use filtered OpenCV scores; thresholds may need retuning. Unicode
and shell verification semantics changed as described above.

Restart the MCP client process to load the new registry. Hardware server processes
can remain running. No Web UI, HTTP or HTTPS architecture changes are included.

## Tests and direct scripts

```bash
python -m pytest tests
```

Tests use synthetic images, mocked clients and virtual clocks without hardware.
[examples](examples/) retain direct TCP scripts. Their standalone PowerShell
watcher uses an optional compressed-size heuristic independent of the canonical
MCP engine; use MCP state tools for actual pixel analysis.

## License

[MIT](LICENSE.txt)

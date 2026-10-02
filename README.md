# mcp-serial-hid-kvm

MCP (Model Context Protocol) server that gives AI agents full keyboard, mouse, and screen access to a physical PC. Thin client for [serial-hid-kvm](https://github.com/sunasaji/serial-hid-kvm) — all hardware control is delegated via TCP.

## How It Works

```
Claude / AI Agent
  ↕ MCP (stdio)
mcp-serial-hid-kvm        ← this package (thin client + OCR)
  ↕ TCP (localhost:9329)
serial-hid-kvm             ← standalone KVM server (owns hardware)
  ↕ USB Serial + HDMI
Target PC
```

The KVM server (`serial-hid-kvm`) runs as a persistent process owning the serial port and capture device. This MCP server connects to it as a TCP client. Multiple MCP instances (multiple Claude sessions) can share a single KVM server without device conflicts.

## Prerequisites

1. **Hardware**: CH9329+CH340 USB HID cable + USB HDMI capture device (see [serial-hid-kvm](https://github.com/sunasaji/serial-hid-kvm) for details)
2. **serial-hid-kvm** installed and running:
   ```bash
   pip install -e /path/to/serial-hid-kvm
   serial-hid-kvm --api              # with preview window
   serial-hid-kvm --api --headless   # or headless
   ```
3. **Tesseract OCR** (for `get_screen_text` / `execute_and_read`):
   - Linux: `sudo apt install tesseract-ocr`
   - Windows: https://github.com/tesseract-ocr/tesseract

## Installation

```bash
pip install -e .
```

This automatically installs `serial-hid-kvm` as a dependency.

## MCP Client Configuration

### Claude Desktop / Claude Code

```json
{
  "mcpServers": {
    "kvm": {
      "command": "mcp-serial-hid-kvm"
    }
  }
}
```

Custom KVM server address:

```json
{
  "mcpServers": {
    "kvm": {
      "command": "mcp-serial-hid-kvm",
      "env": {
        "SHKVM_API_HOST": "127.0.0.1",
        "SHKVM_API_PORT": "9329"
      }
    }
  }
}
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `SHKVM_API_HOST` | `127.0.0.1` | KVM server address |
| `SHKVM_API_PORT` | `9329` | KVM server port |
| `SHKVM_TARGETS` | (none) | Named KVM targets for `select_target`, JSON: `{"aws-pc": "127.0.0.1:9329", "sap-pc": "127.0.0.1:9331"}` |
| `SHKVM_OCR_CMD` | auto-detect | Path to tesseract executable |
| `SHKVM_CAPTURE_LOG_DIR` | platform default | Capture log directory (empty string to disable) |
| `SHKVM_EVIDENCE_DIR` | `~/Documents/kvm-evidence` | `save_evidence` output directory (empty string to disable) |
| `SHKVM_HIDDEN_TOOLS` | superseded/setup tools | Comma-separated tool names to hide from `list_tools` (still callable). `none` shows everything. Default hides: `execute_and_read`, `get_screen_text`, `screen_changed`, `send_key_sequence`, `list_capture_devices`, `set_capture_device`, `set_capture_resolution` |

Hardware settings (`SHKVM_SERIAL_PORT`, `SHKVM_SCREEN_WIDTH`, etc.) are configured on the **KVM server side**, not here. If the target PC uses a non-US keyboard, set `--target-layout` (or `SHKVM_TARGET_LAYOUT`) on the KVM server so that `type_text` and `send_key` produce correct characters.

## Available Tools

Tools marked *(hidden by default)* are superseded or setup-time tools filtered
out of `list_tools` via `SHKVM_HIDDEN_TOOLS` defaults — they remain callable
and can be re-exposed with `SHKVM_HIDDEN_TOOLS=none`.

### Keyboard

| Tool | Description |
|------|-------------|
| `type_text` | Type text with inline tags: `ls -la{enter}`, `{ctrl+c}`, `{alt+f4}`. Whitelist-based: unknown `{content}` passes through literally. Raw mode (`raw=true`) disables tags; actual line breaks become Enter. `char_delay_ms`: delay between keystrokes in ms (default: 20). Only ASCII printable characters, tab, and line breaks are supported; unsupported characters (Unicode, CJK, etc.) cause an error — use base64 encoding as a workaround |
| `send_key` | Single key press with modifiers |
| `send_key_sequence` *(hidden by default)* | Multiple key steps with per-step delays. `default_delay_ms`: delay between steps in ms (default: 100); each step can override with `delay_ms` |

### Mouse

| Tool | Description |
|------|-------------|
| `mouse_move` | Move cursor (absolute or relative) |
| `mouse_click` | Click at optional position |
| `mouse_drag` | Drag from one position to another (drag-and-drop, text selection, etc.) |
| `mouse_scroll` | Scroll wheel |

### Screen

| Tool | Description |
|------|-------------|
| `capture_screen` | Capture screen as image (high token cost) |
| `get_screen_text` *(hidden by default)* | Capture + OCR to text (preferred for text content) |
| `execute_and_read` *(hidden by default)* | Type command, Enter, wait, capture + OCR |

### Device Management

| Tool | Description |
|------|-------------|
| `get_device_info` | Serial port, capture device, config info |
| `list_capture_devices` *(hidden by default)* | List available video devices |
| `set_capture_device` *(hidden by default)* | Switch capture device |
| `set_capture_resolution` *(hidden by default)* | Change capture resolution |

### Token-Efficient Wrapper Tools

These layer on the same KVM client + local OCR and return **compact JSON, never
images**. They move repeated loops, OCR post-processing, screen diffing, and
coordinate lookup into local code so agents spend fewer tokens. Every
text-returning tool is bounded (`max_lines` / `max_chars`) and every wait is
capped (60 s).

| Tool | Description |
|------|-------------|
| `health` | Compact readiness for the whole stack: `{ok, api, serial, video, ocr, capture_device, resolution, errors}` |
| `set_screen_baseline` | Capture the current frame into an in-memory baseline. Optional `region` = `[x,y,w,h]`. Returns `{ok, width, height, timestamp}` |
| `screen_changed` *(hidden by default)* | Diff current frame vs. baseline. Returns only `{changed, score, threshold}`. `auto_baseline=true` seeds a baseline if none exists |
| `get_screen_text_compact` | OCR the screen, normalize whitespace, bound by `max_lines`/`max_chars`. Returns `{text, line_count, truncated}` |
| `detect_text_elements` | Tesseract TSV → text boxes `{text, x, y, w, h, confidence}` for local click targeting. Optional `query` substring filter, `min_confidence`, `region` |
| `click_text` | Find text via OCR boxes and click its center. `match` = `contains`/`exact`, `index`, `dry_run=true` returns the coordinate without clicking |
| `run_powershell_and_read` | Type a PowerShell command on the **target** via HID, wait, OCR the result. `{command, wait_seconds, max_lines, max_chars}` |
| `run_wsl_and_read` | Same, wrapped as `wsl.exe -d <distro> -- bash -lc "<command>"`. `{command, distro, wait_seconds, max_lines, max_chars}` |

### Ops Tools

Operational tools for real-world workflows: evidence capture, long-running
commands, multi-PC setups, and a production-safety interlock.

| Tool | Description |
|------|-------------|
| `save_evidence` | Capture and save a screenshot on the **host** under `<evidence_dir>/<label>/<timestamp>[_<step>].png` — structured evidence for incident/change work (e.g. `label=INC51031_F56_ST22`, `step=before`) |
| `run_powershell_until_done` | Run a long target command (terraform, installers, batch jobs) with an OCR-safe completion marker appended; polls locally until the marker appears, then returns the terminal tail. No more guessing `wait_seconds` |
| `transfer_unicode_file` | Write Unicode text (Japanese runbooks, templates) to a **file** on the target as exact UTF-8 bytes via chunked Base64 typing; SHA-256 verified. Companion to `paste_unicode_text` (clipboard) |
| `paste_unicode_text` | Set Unicode clipboard text through Target PowerShell. Requires a per-call ASCII execution probe and clipboard readback with full SHA-256 before reporting success or sending optional Ctrl+V |
| `list_targets` | List configured KVM targets (`SHKVM_TARGETS`) and the active one |
| `select_target` | Switch the active KVM server by name or host:port (one serial-hid-kvm instance per target PC); resets baseline/cursor tracking and pings the new target |
| `set_input_lock` | Production interlock: while locked, all HID-generating tools are refused (capture/OCR stay available) — safe read-only observation of production screens. Unlock requires `confirm='UNLOCK'` |

**Command-tool limitations:** `run_powershell_and_read` / `run_wsl_and_read`
drive the target purely through HID typing + screen OCR — there is no target-side
agent. They assume a shell is **already focused** on the target. Correct
delivery of special characters (notably the `"` used by the WSL wrapper) depends
on the target keyboard layout matching the KVM server's `--target-layout`. On a
mismatched layout (e.g. a JP-layout target with `us104`), double quotes may not
arrive as ASCII straight quotes; prefer simple unquoted commands, or base64 for
complex payloads. These tools never execute anything on the **host**.

`paste_unicode_text` cancels pending IME composition and clears the input line,
then runs a harmless ASCII probe. It does not blindly toggle the IME. If Japanese
full-width/kana mode, a mismatched keyboard layout, or lost focus prevents the
probe from executing, it returns `ok: false`, `set_clipboard: false`,
`verified: false`, and `pasted: false`, with `failed_stage` and `detail`. Switch
the Target to half-width alphanumeric (`A`/ENG), check the KVM keyboard layout,
and retry after inspecting the screen.

Clipboard success requires standalone output for this call's random ID, the
readback's UTF-16 length and full UTF-8 SHA-256, a completion record, and a returned
default PowerShell `PS ...>` prompt. The command echo and earlier calls cannot
satisfy verification. Hash output is split into two lines to fit an 80-column
console. OCR failure, timeout, readback mismatch, or a missing completion/prompt
blocks automatic paste, even with `paste_after_set: true`. Verification is
mandatory; `verify_timeout_seconds` (default 5, capped by `max_wait_seconds`)
controls each polling phase. `text_chars` counts Unicode code points;
`utf16_chars` is the count checked against PowerShell's string length.

PowerShell stays open so its output can be verified. To return to the previous
app and paste, use `paste_after_set: true` with
`restore_focus_with_alt_tab: true`. Confirm the destination app beforehand and
inspect the resulting draft; `pasted` records Ctrl+V delivery, not destination
content verification. The tool never sends a message. Custom prompts or an
unreadable console fail verification; this tool does not change or restore IME
mode. `dry_run: true` only returns metadata and performs no HID/clipboard actions.

## Direct API Scripts (no MCP / no AI)

The KVM server's TCP API can be scripted directly — see [examples/](examples/)
for stdlib-only Python and PowerShell scripts (health check, evidence
screenshots, command + capture, Unicode clipboard transfer, screen watcher).
Useful when an AI agent is unnecessary or not allowed for the data involved.

## Architecture

This package is intentionally minimal (~4 files):

```
mcp_serial_hid_kvm/
  server.py    MCP tool handlers → KvmClient TCP calls
  config.py    KVM host/port, tesseract, log settings
  ocr.py       Tesseract OCR (runs locally on fetched frames)
  __init__.py
```

All keyboard/mouse/capture logic lives in `serial-hid-kvm`. This package only translates MCP tool calls to TCP API calls and runs OCR locally.

### Why Separate?

- **No device conflicts** — multiple Claude sessions share one KVM server
- **Independent restarts** — restart the MCP server without losing the KVM connection
- **Standalone use** — `serial-hid-kvm` works without MCP (interactive preview, scripts, other AI frameworks)

## License

[MIT](LICENSE.txt)

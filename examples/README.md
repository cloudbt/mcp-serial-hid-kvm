# examples — direct TCP API scripts (no MCP, no AI agent)

These scripts talk **directly** to the serial-hid-kvm TCP JSON Lines API
(default `127.0.0.1:9329`). They do not use the MCP server and never send any
screen content to an AI — everything stays on the host PC. Python scripts are
**stdlib-only** (any Python ≥ 3.10, no venv needed).

```
your script ──TCP 9329──▶ serial-hid-kvm ──USB/HDMI──▶ Target PC
```

## Setup

One serial-hid-kvm instance per target PC, each on its own port. Define the
targets once (same JSON convention as the MCP server):

```powershell
# e.g. in your PowerShell profile
$env:SHKVM_TARGETS = '{"aws-pc": "127.0.0.1:9329", "sap-pc": "127.0.0.1:9331"}'
```

## Scripts

| # | Script | What it does | Typical use |
|---|--------|--------------|-------------|
| 1 | `health_check.py` | Ping + serial/capture status for every target; exit 1 if any down | Morning checklist / Task Scheduler |
| 2 | `take_evidence.py` | Screenshot to `<dir>/<LABEL>/<timestamp>[_step].jpg` | 作業前/作業後エビデンス (`INC51031_F56_ST22 before`) |
| 3 | `run_cmd_and_capture.py` | Type a command (optionally WSL-wrapped), capture N screenshots while it runs | `terraform plan` on the AWS PC, `Get-Service` on a Windows target |
| 4 | `send_japanese_clipboard.py` | Unicode → Base64 → target clipboard (`Set-Clipboard`), optional Ctrl+V | Japanese mail/Teams templates on a KVM-only machine |
| 5 | `lock_all_targets.py` | Win+L on every target | End of day |
| 6 | `Invoke-Kvm.ps1` | Generic one-shot API caller from PowerShell (any method, JPEG decode via `-OutFile`) | Ad-hoc calls, building blocks for your own .ps1 |
| 7 | `Watch-Screen.ps1` | Poll the screen, save an evidence frame whenever it changes, plus start/end frames | Babysit `terraform apply`, JP1 jobs, installers |

`kvmclient.py` is the tiny shared client library the Python examples import —
also the starting point for your own scripts.

## Quick calls with Invoke-Kvm.ps1

```powershell
.\Invoke-Kvm.ps1 -Method ping
.\Invoke-Kvm.ps1 -Method type_text -Params @{ text = 'Get-Date{enter}' }
.\Invoke-Kvm.ps1 -Method get_device_info
.\Invoke-Kvm.ps1 -Port 9331 -Method capture_frame -Params @{ quality = 90 } -OutFile shot.jpg
```

## API method reference (TCP JSON Lines)

Request: `{"id": "…", "method": "…", "params": {…}}` + `\n`. One JSON response
per line: `{"id", "ok", "result"|"error"}`.

| Method | Params |
|--------|--------|
| `ping` | — |
| `type_text` | `text` (supports `{enter}`/`{ctrl+c}` tags), `raw`, `char_delay_ms` |
| `send_key` | `key`, `modifiers` (`ctrl/shift/alt/win`) |
| `send_key_sequence` | `steps: [{key, modifiers, delay_ms}]`, `default_delay_ms` |
| `mouse_move` | `x`, `y`, `relative` |
| `mouse_click` / `mouse_down` / `mouse_up` | `button`, `x`, `y` |
| `mouse_scroll` | `amount` (-127..127) |
| `capture_frame` | `quality` → `{jpeg_b64, width, height}` |
| `get_device_info` | — |
| `set_timing` / `get_timing` | seconds: `char_delay`, `type_key_hold`, `key_hold`, `combo_mod`, `type_shift`, `click_hold`, `click_after` |
| `list_capture_devices` / `set_capture_device` / `set_capture_resolution` | device/index, width/height |

## Safety notes

- Keyboard input is typed **blind** — always make sure the correct window is
  focused on the target before scripts that type (`run_cmd_and_capture.py`,
  `send_japanese_clipboard.py`).
- On production screens, prefer the read-only scripts (`health_check`,
  `take_evidence`, `Watch-Screen`) — they never send a single keystroke.
- `type_text` only accepts ASCII; anything Unicode must go through the
  clipboard/Base64 pattern shown in `send_japanese_clipboard.py`.

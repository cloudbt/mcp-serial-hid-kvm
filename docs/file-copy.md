# Windows Target to Host file copy

The MCP copies **saved local files as raw bytes**. File contents travel through
the existing HDMI capture channel; a fixed-purpose Windows Forms helper is
delivered through HID. The existing MCP → KVM TCP API remains local control
transport. There is no Target file server, share, upload, clipboard extraction,
format reconstruction, OCR, or arbitrary command execution tool.

## Tools and observation boundaries

1. `health`, `get_device_info`, then `capture_screen`; verify the intended device.
2. Open a **dedicated** Windows PowerShell session through the observed Run
   dialog: `powershell -NoProfile -NoExit -Command "Remove-Module PSReadLine -ErrorAction SilentlyContinue"`.
   Observe the launched shell, ASCII input mode and an empty prompt. Removing
   PSReadLine applies only to this process, prevents persistent command history,
   and leaves earlier user history intact. Do not change execution policy.
3. `copy_file_from_target(source_path, host_dir, dry_run=true)` checks local path
   syntax, live identity and Host conflicts without HID input. `host_dir` must be
   an existing absolute Host directory. UNC/device/ADS/traversal paths are rejected.
4. Prepare with `dry_run=false` (default `overwrite=false`); retain the `job_id`.
5. Capture and inspect the dedicated shell; call
   `advance_file_copy(job_id, action="type", focus_confirmed=true)`.
   Poll `get_file_copy_status` until `state="typed"`. This only types the fixed
   helper command; it does **not** press Enter. First bootstrap takes HID time.
6. Capture again, verify the entire command finished at the intended prompt and
   no IME candidate/error/extra input is present. Call `advance_file_copy` with
   `action="launch"`, `focus_confirmed=true`. It moves the cursor outside the
   grid, presses Enter and starts a background receiver. Calls return bounded
   metadata rather than holding a long MCP tool request open.
7. Poll status. Only `state="completed", verified=true` means size and whole-file
   SHA-256 passed and the destination was atomically published. Read-only status
   does not connect to the currently selected Target.
8. Capture the helper, then `action="cleanup"` sends Escape **only** after validating
   the session-bound protocol. Capture and verify the helper closed; `action="release"`
   with `focus_confirmed=true` records caller verification and releases HID ownership.
   Close only the dedicated shell. No Target temporary script/data files were created.

Every advance action requires a capture within 60 seconds and caller focus
confirmation. The server performs protocol decoding, not semantic UI interpretation.
After a successful copy in the same dedicated PowerShell process, `helper_cached=true`
on `action="type"` types a short invocation. The loaded helper's build fingerprint
must match. In a new shell use the full bootstrap. Compilation and helper lifetime
remain confined to the temporary PowerShell process.
The helper owns a separate STA UI thread and disposes its message loop there;
the calling PowerShell/Office COM apartment is not used for the Forms loop.

## Integrity, interruption and cleanup

The sender opens a read-only stream with `FileShare.Read`, denying concurrent
writers/deletion during the snapshot and helper lifetime. A source already held
by an incompatible writer can fail safely; copy a separately saved/exported file.
It snapshots at most 16 MiB in memory. This is a safety cap, not a claim that 16 MiB
has been hardware qualified. Helper inactivity closes its window/stream after
10 minutes; each navigation renews the timer. Receiver budget is one hour per run.

Each 15,000-byte block carries a version marker, random 128-bit session ID,
index/count/length, file size, full SHA-256, block SHA-256 and a 128-bit header
checksum. A magenta border locates the grid independently of HID and capture
coordinates. Cell sizes adapt to the display; decoder calibration also handles
independent capture-axis scaling and cropping. Minimum sender geometry is
1124×600; do not change system display settings to force a transfer.

Damaged/stale frames are recaptured; validated missing blocks are explicitly
requested by index. After 40 invalid frames reception pauses without blind HID.
`cancel_file_copy(job_id)` stops the worker between bounded I/O operations, keeps
verified chunks, and does not send Escape. Observe before `action="receive"` to
resume the same visible helper. After a process interruption, journals and chunks
remain; run status and observe before resuming. If the helper has closed, clear
any unexecuted input in the dedicated shell and use `action="type"`, then launch
with the **same job ID**. Source size/hash changes prohibit combining old chunks.

Host chunks live in a uniquely named `.kvmcopy-<job_id>` sibling of the destination;
successful publication removes only these self-created staging files. Failed or
cancelled jobs retain staging for recovery. Journals live in the platform user
data directory under `mcp-serial-hid-kvm/file-copy`. No raw frames or file bytes
are returned in copy progress. Standard explicit `capture_screen` retains its
existing configured capture-log behavior; protect logs as local evidence.

No-overwrite publication uses a same-filesystem atomic hard link. A filesystem
without hard-link support fails safely with staging preserved. Explicit
`overwrite=true` uses atomic replace; obtain authorization for an existing user
destination before requesting it. The tool never executes received files.

## Endpoint coordination

Jobs bind the starting API host/port, serial port/adapter hardware ID and video
device. Aliases for the same endpoint share an OS-held Host lease. Input,
target/capture switches and timing mutation are guarded while ownership remains.
Persisted pending-cleanup journals prevent stray input after restart, including
other updated MCP processes. The existing manual input interlock stays separate:
locking pauses the worker before further HID. It is not an ownership lease.

All controlling MCP processes must load this version to honor shared journals;
an older MCP process or manual keyboard/mouse cannot honor this new guard.
Use one controller during a transfer. Do not restart the hardware API or switch
devices. Restart only the relevant stdio MCP process to load the new tools.

## Tests

`tests/test_file_copy.py` covers empty/text/binary/Unicode samples, independent
axis scaling, native cell geometries, corrupt frames, nonce/endpoint/identity
isolation, invalid paths, source changes, lost/out-of-order blocks, retained
cancel/restart state, no-clobber conflict races, interlock and full-hash refusal.
Windows checks additionally parse the generated bootstrap with Windows
PowerShell. The helper is included in the built wheel as a package resource.
Hardware qualification and throughput are recorded separately in the integration
repository; synthetic coverage does not imply testing a second physical Target.

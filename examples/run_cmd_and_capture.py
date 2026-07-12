"""Example 3 — run a command on the target and capture the result.

Types a command into the *currently focused* shell on the target, then takes
one or more screenshots while it runs. There is no OCR here — the screenshots
ARE the deliverable (evidence / review material).

Usage:
    # AWS PC: terraform plan, wait 60 s, 3 evenly spaced screenshots
    python run_cmd_and_capture.py "terraform plan" --wait 60 --shots 3 --label tfplan --target aws-pc

    # AWS PC: run inside WSL Ubuntu-24.04
    python run_cmd_and_capture.py "kubectl get pods -A" --wsl --label pods --target aws-pc

    # SAP PC (大和VM focused): service status before a change window
    python run_cmd_and_capture.py "Get-Service | Where-Object Status -ne 'Running'" --label INC50900_svc_before --target sap-pc

Safety:
    The command is typed blind over HID. Make sure the right window has focus
    on the target before running (this script sends Esc first to clear any
    half-typed input on a PSReadLine prompt).
"""

import argparse
import datetime
import os
import sys
import time

from kvmclient import connect

DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "Documents", "kvm-evidence")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", help="command to type into the focused target shell")
    ap.add_argument("--wsl", action="store_true",
                    help="wrap as: wsl.exe -d <distro> -- bash -lc \"<command>\"")
    ap.add_argument("--distro", default="Ubuntu-24.04")
    ap.add_argument("--wait", type=float, default=10.0,
                    help="seconds to wait/capture after Enter (default 10)")
    ap.add_argument("--shots", type=int, default=1,
                    help="screenshots spread over the wait period (default 1, at the end)")
    ap.add_argument("--label", default="cmd", help="evidence folder label")
    ap.add_argument("--target", default="default", help="target name or host:port")
    ap.add_argument("--dir", default=DEFAULT_DIR)
    args = ap.parse_args()

    command = args.command
    if args.wsl:
        escaped = command.replace('"', '`"')
        command = f'wsl.exe -d {args.distro} -- bash -lc "{escaped}"'

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    outdir = os.path.join(args.dir, args.label)

    with connect(args.target) as kvm:
        kvm.send_key("escape")          # clear any leftover input line
        time.sleep(0.2)
        kvm.type_text(command, raw=True)
        time.sleep(0.2)
        kvm.send_key("enter")

        shots = max(1, args.shots)
        interval = args.wait / shots
        for i in range(1, shots + 1):
            time.sleep(interval)
            path = os.path.join(outdir, f"{ts}_shot{i}.jpg")
            kvm.save_screenshot(path)
            print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

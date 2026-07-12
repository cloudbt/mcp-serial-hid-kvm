"""Example 2 — incident-evidence screenshot with structured naming.

Captures the target screen and saves it as
    <dir>/<LABEL>/<YYYYMMDD_HHMMSS>[_<step>].jpg
matching an Incident-List evidence workflow (label = INC number + system).

Usage:
    python take_evidence.py INC51031_F56_ST22
    python take_evidence.py INC51031_F56_ST22 before
    python take_evidence.py INC51031_F56_ST22 after  --target sap-pc
    python take_evidence.py JP1_JobCheck step3 --dir "D:\\Evidence"

Project mapping:
    SAP PC  -> 作業前/作業後エビデンス for SAP GUI / JP1 / 大和VM screens.
               Runs 100% locally (KVM TCP API only) — no AI, no cloud.
    AWS PC  -> capture terraform plan/apply results for PR or issue comments.
"""

import argparse
import datetime
import os
import re
import sys

from kvmclient import connect

DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "Documents", "kvm-evidence")


def sanitize(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]+", "_", label.strip()).strip("_")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("label", help="e.g. INC51031_F56_ST22")
    ap.add_argument("step", nargs="?", default="", help="e.g. before / after / step3")
    ap.add_argument("--target", default="default", help="target name or host:port")
    ap.add_argument("--dir", default=DEFAULT_DIR, help="evidence base directory")
    ap.add_argument("--quality", type=int, default=90, help="JPEG quality (default 90)")
    args = ap.parse_args()

    label = sanitize(args.label)
    step = sanitize(args.step)
    if not label:
        print("error: label must contain letters/digits")
        return 2

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = ts + (f"_{step}" if step else "") + ".jpg"
    path = os.path.join(args.dir, label, filename)

    with connect(args.target) as kvm:
        kvm.save_screenshot(path, quality=args.quality)
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Example 4 — put Japanese (or any Unicode) text on the TARGET clipboard.

HID keyboards can only type ASCII, so this encodes the text as Base64URL,
types a short PowerShell one-liner on the target that decodes it and calls
Set-Clipboard, then (optionally) sends Ctrl+V. Ideal for Japanese mail /
Teams reply templates that must be entered on a machine you only reach via KVM.

Usage:
    # from a template file (UTF-8), clipboard only:
    python send_japanese_clipboard.py --file templates/mail_done.txt --target sap-pc

    # inline text, then paste into the focused app with Ctrl+V:
    python send_japanese_clipboard.py --text "お世話になっております。" --paste

Prerequisite: a PowerShell prompt must be FOCUSED on the target (the decode
command is typed into it). After the clipboard is set, switch focus yourself
(or pass --alt-tab together with --paste).
"""

import argparse
import base64
import sys
import time

from kvmclient import connect


def build_ps_command(text: str) -> str:
    payload = base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")
    return (
        f"$b='{payload}';"
        "$b=$b.Replace('-','+').Replace('_','/');"
        "while($b.Length%4){$b+='='};"
        "Set-Clipboard -Value ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b)));"
        "Write-Output ('CLIP'+'_OK')"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file", help="UTF-8 text file to send")
    src.add_argument("--text", help="inline text to send")
    ap.add_argument("--paste", action="store_true", help="send Ctrl+V afterwards")
    ap.add_argument("--alt-tab", action="store_true",
                    help="send Alt+Tab before Ctrl+V (return to previous app)")
    ap.add_argument("--target", default="default", help="target name or host:port")
    ap.add_argument("--fast", action="store_true", default=True,
                    help="temporarily speed up HID typing (default on)")
    args = ap.parse_args()

    text = args.text
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
    if not text:
        print("error: empty text")
        return 2

    command = build_ps_command(text)
    print(f"chars={len(text)} b64_command_chars={len(command)}")

    with connect(args.target) as kvm:
        old_timing = None
        if args.fast:
            try:
                old_timing = kvm.get_timing()
                kvm.set_timing(char_delay=0.005, type_key_hold=0.005, type_shift=0.0)
            except Exception as e:
                print(f"warning: fast timing not applied: {e}")
        try:
            kvm.send_key("escape")
            time.sleep(0.2)
            kvm.type_text(command, raw=True)
            time.sleep(0.2)
            kvm.send_key("enter")
            time.sleep(1.0)
            if args.paste:
                if args.alt_tab:
                    kvm.send_key("tab", ["alt"])
                    time.sleep(0.5)
                kvm.send_key("v", ["ctrl"])
                print("pasted with Ctrl+V")
            else:
                print("clipboard set on target (paste manually with Ctrl+V)")
        finally:
            if old_timing is not None:
                kvm.set_timing(**{k: v for k, v in old_timing.items()
                                  if isinstance(v, (int, float))})
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Example 1 — morning health check across all KVM targets.

Pings every target defined in SHKVM_TARGETS (plus "default") and prints
serial/capture status. Exit code 1 if any target is down — suitable for
Task Scheduler or a pre-work checklist.

Usage:
    python health_check.py
    python health_check.py aws-pc sap-pc     # only these targets

Project mapping:
    AWS PC  -> confirm the KVM stack is up before starting terraform work
    SAP PC  -> confirm the KVM stack is up before evidence capture
"""

import sys

from kvmclient import Kvm, KvmError, targets_from_env


def check(name: str, host: str, port: int) -> bool:
    try:
        with Kvm(host, port, timeout=5.0) as kvm:
            kvm.ping()
            info = kvm.device_info()
            serial = info.get("serial", {})
            cap = info.get("capture", {})
            serial_ok = bool(serial.get("connected"))
            cap_desc = (f"{cap.get('width')}x{cap.get('height')}"
                        if not cap.get("error") else f"ERROR {cap['error']}")
            status = "OK " if serial_ok and not cap.get("error") else "NG "
            print(f"[{status}] {name:10s} {host}:{port}  "
                  f"serial={serial.get('port')}({'up' if serial_ok else 'DOWN'})  "
                  f"capture={cap_desc}")
            return serial_ok and not cap.get("error")
    except (KvmError, OSError) as e:
        print(f"[NG ] {name:10s} {host}:{port}  unreachable: {e}")
        return False


def main() -> int:
    targets = targets_from_env()
    wanted = sys.argv[1:] or list(targets)
    all_ok = True
    for name in wanted:
        if name not in targets:
            print(f"[?? ] {name}: not in SHKVM_TARGETS, skipped")
            all_ok = False
            continue
        host, port = targets[name]
        all_ok &= check(name, host, port)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

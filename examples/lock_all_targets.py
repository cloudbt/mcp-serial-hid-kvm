"""Example 5 — end-of-day: lock the session on every KVM target (Win+L).

Sends Win+L to each configured target. Safe (locking never destroys state)
and useful when both project PCs must be left locked before you step away.

Usage:
    python lock_all_targets.py               # all targets in SHKVM_TARGETS
    python lock_all_targets.py aws-pc        # just one
"""

import sys

from kvmclient import Kvm, KvmError, targets_from_env


def main() -> int:
    targets = targets_from_env()
    wanted = sys.argv[1:] or [n for n in targets if n != "default"] or ["default"]
    rc = 0
    for name in wanted:
        if name not in targets:
            print(f"{name}: unknown target (known: {sorted(targets)})")
            rc = 1
            continue
        host, port = targets[name]
        try:
            with Kvm(host, port, timeout=5.0) as kvm:
                kvm.send_key("l", ["win"])
            print(f"{name}: locked (Win+L sent)")
        except (KvmError, OSError) as e:
            print(f"{name}: FAILED - {e}")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())

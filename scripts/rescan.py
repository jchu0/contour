#!/usr/bin/env python
"""Rescan every tracked company and report what changed.

For cron, or to run by hand:

    SEC_USER_AGENT="Org Ledger you@example.com" ./.venv/bin/python scripts/rescan.py

    # daily at 07:00
    0 7 * * *  cd /path/to/repo && SEC_USER_AGENT="…" ./.venv/bin/python scripts/rescan.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ledger.edgar import EdgarClient
from ledger.server import rescan_tracked


def main() -> int:
    results = rescan_tracked(EdgarClient())
    if not results:
        print("nothing tracked — take a baseline first")
        return 0

    changed = 0
    for ticker, delta in results:
        if isinstance(delta, str):
            print(f"{ticker:<6} {delta}")
            continue
        if delta is None:
            print(f"{ticker:<6} first scan on record")
            continue
        if delta.quiet:
            print(f"{ticker:<6} no change since {delta.from_scan}")
            continue
        changed += 1
        print(f"{ticker:<6} changed since {delta.from_scan}")
        for _key, headline in delta.appeared:
            print(f"       + {headline[:88]}")
        for _key, headline in delta.resolved:
            print(f"       - {headline[:88]}")

    print(f"\n{len(results)} scanned · {changed} changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

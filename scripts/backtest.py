#!/usr/bin/env python
"""Event study over filing-language change.

    SEC_USER_AGENT="Org Ledger you@example.com" ./.venv/bin/python scripts/backtest.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ledger.backtest import HORIZONS, compare, events_for, verdict
from ledger.edgar import EdgarClient

UNIVERSE = ["TSLA", "AAPL", "NVDA", "WMT", "KO", "JPM", "DIS", "PFE",
            "COST", "UNH", "F", "BA", "SBUX", "MCD", "GE", "XOM"]


def main(tickers: list[str]) -> None:
    client = EdgarClient()
    events = []
    for ticker in tickers:
        try:
            found = events_for(client, ticker)
        except Exception as exc:  # noqa: BLE001 — one bad name must not end the study
            print(f"  {ticker:<6} skipped — {type(exc).__name__}: {exc}")
            continue
        print(f"  {ticker:<6} {len(found)} events")
        events.extend(found)

    print(f"\n{'=' * 74}\n{len(events)} events total")
    print(f"{'ticker':<8}{'filed':<12}{'change':>8}{'add':>5}{'rem':>5}" +
          "".join(f"{h}d excess".rjust(12) for h in HORIZONS))
    print("-" * 74)
    for event in sorted(events, key=lambda e: e.change_score, reverse=True):
        cells = "".join(
            (f"{event.excess[h]:+.1f}%" if event.excess[h] is not None else "n/a").rjust(12)
            for h in HORIZONS
        )
        print(f"{event.ticker:<8}{event.filed.isoformat():<12}{event.change_score:>8.3f}"
              f"{event.added:>5}{event.removed:>5}{cells}")

    results, note = compare(events)
    print(f"\n{'=' * 74}")
    if note:
        print(note)
    for r in results:
        print(f"{r.horizon:>4}d   low-change {r.low_mean:+6.2f}% (n={r.low_n})   "
              f"high-change {r.high_mean:+6.2f}% (n={r.high_n})   spread {r.spread:+6.2f}pp")
    print(f"\n{verdict(results, events)}")


if __name__ == "__main__":
    main(sys.argv[1:] or UNIVERSE)

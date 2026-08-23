#!/usr/bin/env python
"""Primary-source scan for one or more companies.

    SEC_USER_AGENT="Org Ledger you@example.com" ./.venv/bin/python scripts/scan.py TSLA
    ./.venv/bin/python scripts/scan.py TSLA AAPL --html out/

Every check reports one of three outcomes: findings, nothing flagged, or
unavailable with a reason. A check that could not run never reads as clean.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ledger.edgar import EdgarClient, EdgarError
from ledger.render import render_html, render_terminal
from ledger.report import scan


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    flags = {a for a in argv if a.startswith("--")}
    html_dir = None
    for i, a in enumerate(argv):
        if a == "--html" and i + 1 < len(argv):
            html_dir = pathlib.Path(argv[i + 1])
            args = [x for x in args if x != argv[i + 1]]
    tickers = args or ["TSLA"]

    client = EdgarClient()
    failures = 0
    for ticker in tickers:
        try:
            report = scan(client, ticker, debug="--debug" in flags)
        except EdgarError as exc:
            print(f"\n{ticker}: {exc}")
            failures += 1
            continue
        print(render_terminal(report))
        if html_dir:
            html_dir.mkdir(parents=True, exist_ok=True)
            path = html_dir / f"{report.ticker.lower()}.html"
            path.write_text(render_html(report), encoding="utf-8")
            print(f"\nwrote {path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

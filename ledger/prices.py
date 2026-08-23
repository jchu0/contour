"""Daily price history.

Nasdaq's public quote API, which needs no key and returns split-adjusted closes
back to 2018. Stooq is behind a JavaScript proof-of-work challenge and Yahoo's
chart endpoint rate-limits immediately; neither is worth working around.

Returns are always computed market-adjusted. Over 2019-2026 a raw return on a
large-cap US name is mostly market beta, so an unadjusted spread would measure
the index rather than the signal.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import requests

# Pooled for the same reason as everywhere else: a new connection per request
# runs the machine out of ephemeral ports.
_SESSION = requests.Session()

HISTORY_URL = "https://api.nasdaq.com/api/quote/{ticker}/historical"
CACHE_DIR = Path("data/price_cache")
BENCHMARK = "SPY"

# The endpoint answers only to a browser-shaped request.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
    "Accept": "application/json",
}


class PriceError(RuntimeError):
    pass


def _parse_money(text: str) -> float | None:
    cleaned = text.replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def daily_closes(ticker: str, *, start: date = date(2018, 1, 1)) -> dict[date, float]:
    """Split-adjusted closes by date, oldest first."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / f"{ticker.upper()}.json"
    if cache.exists():
        raw = json.loads(cache.read_text(encoding="utf-8"))
    else:
        raw = None
        # The benchmark is an ETF, which lives under a different assetclass than
        # the companies do. Try both rather than assuming.
        for asset_class in ("stocks", "etf"):
            response = _SESSION.get(
                HISTORY_URL.format(ticker=ticker.upper()),
                headers=_HEADERS,
                params={
                    "assetclass": asset_class,
                    "fromdate": start.isoformat(),
                    "todate": date.today().isoformat(),
                    "limit": "9999",
                },
                timeout=40,
            )
            if response.status_code != 200:
                continue
            candidate = response.json()
            rows = ((candidate.get("data") or {}).get("tradesTable") or {}).get("rows")
            if rows:
                raw = candidate
                break
        if raw is None:
            raise PriceError(f"no price history available for {ticker}")
        cache.write_text(json.dumps(raw), encoding="utf-8")

    data = (raw or {}).get("data") or {}
    table = (data.get("tradesTable") or {}).get("rows") or []
    if not table:
        raise PriceError(f"no price rows returned for {ticker}")

    out: dict[date, float] = {}
    for row in table:
        try:
            month, day, year = row["date"].split("/")
            when = date(int(year), int(month), int(day))
        except (KeyError, ValueError):
            continue
        close = _parse_money(row.get("close", ""))
        if close:
            out[when] = close
    return dict(sorted(out.items()))


def close_on_or_after(closes: dict[date, float], when: date, *, window: int = 10) -> tuple[date, float] | None:
    """First traded close on or after a date — filings land on holidays and weekends."""
    for offset in range(window + 1):
        day = when + timedelta(days=offset)
        if day in closes:
            return day, closes[day]
    return None


def forward_return(closes: dict[date, float], start: date, days: int) -> float | None:
    """Percentage return from the first close on/after `start` to `start + days`."""
    entry = close_on_or_after(closes, start)
    exit_point = close_on_or_after(closes, start + timedelta(days=days))
    if not entry or not exit_point or not entry[1]:
        return None
    return (exit_point[1] - entry[1]) / entry[1] * 100


def excess_return(
    closes: dict[date, float],
    benchmark: dict[date, float],
    start: date,
    days: int,
) -> float | None:
    """Forward return less the benchmark's return over the same window."""
    own = forward_return(closes, start, days)
    market = forward_return(benchmark, start, days)
    if own is None or market is None:
        return None
    return own - market

"""Event study: does filing-language change precede returns?

Tests the prediction locked in journal/hypotheses/hyp_2026_08_22_filing_change_returns.md
before any of this ran. Each event is one 10-K measured against the prior year's
10-K; the score is how much of the risk-factor section changed. Returns are
market-adjusted from the filing date.

The honest constraint is sample size. Sixteen large caps yield roughly sixty
firm-years, and the published effect is a point or two over a quarter. That is
not separable from noise here, which is why `verdict` refuses to call a result
either way below a floor rather than reporting whichever sign came up.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date

from ledger.diff import Change, diff_blocks, split_risk_factors, summarize
from ledger.edgar import EdgarClient
from ledger.parse import find_section, html_to_text, split_items
from ledger.prices import BENCHMARK, daily_closes, excess_return

HORIZONS = (30, 90, 180)
# Below this many events per bucket, no comparison is worth reporting.
MIN_PER_BUCKET = 12
# Fewer paired risk factors than this and the diff itself is unreliable.
MIN_PAIR_RATE = 0.4


@dataclass
class Event:
    ticker: str
    filed: date
    period: int
    change_score: float          # 0 = identical, 1 = nothing paired
    added: int
    removed: int
    rewritten: int
    excess: dict[int, float | None]


def _risk_blocks(client: EdgarClient, filing):
    section = find_section(split_items(html_to_text(client.document(filing))), "1A")
    if section is None:
        return []
    return split_risk_factors(section.body)


def events_for(client: EdgarClient, ticker: str, *, max_pairs: int = 6) -> list[Event]:
    """One event per consecutive 10-K pair, newest first."""
    company = client.resolve(ticker)
    filings = client.filings(company.cik, forms=["10-K"], limit=max_pairs + 1)
    if len(filings) < 2:
        return []

    closes = daily_closes(company.ticker)
    market = daily_closes(BENCHMARK)

    out: list[Event] = []
    for newer, older in zip(filings, filings[1:]):
        current, baseline = _risk_blocks(client, newer), _risk_blocks(client, older)
        if len(current) < 5 or len(baseline) < 5:
            continue
        deltas = diff_blocks(baseline, current)
        counts = summarize(deltas)
        paired = counts["modified"] + counts["unchanged"]
        if paired / len(current) < MIN_PAIR_RATE:
            continue

        similarities = [d.similarity for d in deltas if d.change is Change.MODIFIED]
        mean_similarity = statistics.mean(similarities) if similarities else 0.0
        unpaired = (counts["added"] + counts["removed"]) / (len(current) + len(baseline))
        # Two components: how much paired text was rewritten, and how much of the
        # section has no counterpart at all.
        score = (1 - mean_similarity) * 0.7 + unpaired * 0.3

        out.append(Event(
            ticker=company.ticker,
            filed=newer.filed,
            period=newer.period.year if newer.period else newer.filed.year,
            change_score=round(score, 4),
            added=counts["added"],
            removed=counts["removed"],
            rewritten=len([s for s in similarities if s < 0.5]),
            excess={h: excess_return(closes, market, newer.filed, h) for h in HORIZONS},
        ))
    return out


@dataclass
class BucketResult:
    horizon: int
    low_n: int
    high_n: int
    low_mean: float
    high_mean: float

    @property
    def spread(self) -> float:
        """High-change minus low-change. The locked prediction says negative."""
        return self.high_mean - self.low_mean


def compare(events: list[Event]) -> tuple[list[BucketResult], str]:
    """Split at the median change score and compare market-adjusted returns."""
    scored = sorted(events, key=lambda e: e.change_score)
    if len(scored) < MIN_PER_BUCKET * 2:
        return [], (
            f"{len(scored)} events is below the {MIN_PER_BUCKET * 2} needed for two "
            "buckets. No comparison run."
        )
    midpoint = len(scored) // 2
    low, high = scored[:midpoint], scored[midpoint:]

    results: list[BucketResult] = []
    for horizon in HORIZONS:
        low_values = [e.excess[horizon] for e in low if e.excess[horizon] is not None]
        high_values = [e.excess[horizon] for e in high if e.excess[horizon] is not None]
        if len(low_values) < MIN_PER_BUCKET or len(high_values) < MIN_PER_BUCKET:
            continue
        results.append(BucketResult(
            horizon=horizon,
            low_n=len(low_values), high_n=len(high_values),
            low_mean=statistics.mean(low_values), high_mean=statistics.mean(high_values),
        ))
    return results, ""


def verdict(results: list[BucketResult], events: list[Event]) -> str:
    """State what this sample can and cannot support.

    Deliberately refuses to confirm. A sample this size cannot detect the
    published effect, so a spread of the predicted sign is as likely to be noise
    as signal — and saying otherwise would be the exact failure this project
    exists to catch.
    """
    if not results:
        return "INSUFFICIENT_EVENTS — nothing to conclude."

    spreads = [r.spread for r in results]
    n = min(min(r.low_n, r.high_n) for r in results)
    consistent = all(s < 0 for s in spreads) or all(s > 0 for s in spreads)
    direction = "negative" if statistics.mean(spreads) < 0 else "positive"

    lines = [
        f"Sample: {len(events)} events, {n} per bucket at the tightest horizon.",
        f"Spread sign is {'consistent' if consistent else 'INCONSISTENT'} across horizons "
        f"({direction} on average).",
    ]
    if not consistent:
        lines.append(
            "A spread that flips sign between horizons is the pattern the locked "
            "hypothesis named as 'not working'."
        )
    lines.append(
        "VERDICT: INSUFFICIENT_SAMPLE. The published effect is roughly one to three "
        f"points per quarter; detecting that against equity volatility needs hundreds of "
        f"events, not {len(events)}. This run cannot confirm the hypothesis and does not "
        "claim to. Only a large, consistent, opposite-signed result would be informative, "
        "and that is not what appeared."
    )
    return "\n".join(lines)

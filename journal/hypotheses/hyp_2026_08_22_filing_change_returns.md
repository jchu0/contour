---
hypothesis_id: hyp_2026_08_22_filing_change_returns
strategy_family: filing_language_drift
created_at: 2026-08-22
prediction_locked_at: 2026-08-22
expected_direction: negative
expected_spread_90d_pp: [0.5, 3.0]
expected_n: [40, 80]
assumed_regime: [us_large_cap, 2019_2026]
references:
  - "Cohen, Malloy & Nguyen (2020), Lazy Prices, Journal of Finance 75(3)"
---

## Author belief

Firms that materially rewrite their 10-K risk factors earn lower subsequent
returns than firms that leave them alone. The published finding is that changes
to filing language are informative and slow to be priced.

## Mechanism

Filings are written by counsel under liability pressure, not by IR under
narrative pressure. A risk factor is added or rewritten when the underlying
exposure has actually changed. Almost nobody reads the diff, so the information
enters price slowly.

## Prediction, locked before running anything

- Direction: **negative**. The high-change bucket underperforms the low-change
  bucket on market-adjusted forward returns.
- Magnitude: a **0.5 to 3.0 percentage point** spread at 90 days.
- Sample: roughly **40 to 80** firm-years from ~16 large caps with 5-6 10-Ks each.

## What I would see if working

High-change bucket mean excess return below low-change bucket at both 90 and
180 days, same sign at both horizons.

## What I would see if NOT working

No consistent spread, opposite sign, or a spread that flips between horizons.

## Most likely failure mode

`sample_too_small`. Sixty firm-years cannot separate a 1-2pp effect from noise.
The published result used thousands of firm-years across the full cross-section.
A "confirmation" at this sample size is not evidence; only a large opposite-signed
result would tell us anything, and even that weakly.

## Counter-evidence to look for early

All sixteen names are large-cap US survivors over a strong bull market. Survivor
bias and a common market factor will dominate raw returns, which is why the test
must be run on market-adjusted returns and why the spread, not the level, is the
only number worth reading.

---

## Result — recorded 2026-08-22, after running

**Sample:** 55 events across 11 companies (5 of the 16 produced none — JPM and
XOM had too few comparable 10-Ks, others fell below the pairing-reliability
floor). Within the locked expectation of 40-80.

| Horizon | Low-change | High-change | Spread | Predicted |
|---|---|---|---|---|
| 30d  | +1.97% (n=27) | −0.85% (n=28) | **−2.82pp** | negative ✓ |
| 90d  | −1.67% (n=27) | +1.34% (n=28) | **+3.01pp** | negative ✗ |
| 180d | −0.96% (n=25) | +0.36% (n=26) | **+1.33pp** | negative ✗ |

**Verdict: `surprise` — the prediction did not hold.**

The locked "what I would see if NOT working" named this exact pattern: *"a spread
that flips between horizons."* It flipped. The 30-day spread carries the
predicted sign and the 90- and 180-day spreads carry the opposite one.

**lesson_category: `sample_too_small`**

Not because the result is unwelcome, but because the design cannot support
either conclusion:

- 55 events against a published effect of 1-3pp per quarter. Quarterly equity
  volatility on these names is ~15pp. The standard error swamps the effect by an
  order of magnitude.
- The buckets barely differ. Change scores run 0.083 to 0.591 with an
  interquartile range of only 0.134 to 0.237 — the "high-change" and "low-change"
  halves are separated by about 0.10 of score. Splitting at the median of a tight
  distribution compares two nearly identical groups.
- Universe is 11 large-cap US survivors over a strong bull market. Survivorship
  and a common market factor dominate, and market-adjustment removes only the
  first-order part of that.

**What would make this testable:** several hundred firm-years across the full
cross-section rather than 55 across mega-caps, top-vs-bottom-quintile buckets
instead of a median split, and industry-adjusted rather than only market-adjusted
returns. That is a research project, not a hackathon afternoon.

**What this run does establish:** the pipeline end to end. Filings parse,
risk-factor blocks pair, change is scored, filing dates join to split-adjusted
prices, and returns come out market-adjusted. The plumbing is correct and reusable;
only the sample is inadequate.

# Contour

Tracks what a public company **said**, what it **filed**, and what **changed** —
then surfaces where the three disagree.

An initial scan builds a baseline state for an organization from primary sources.
Later scans diff against that baseline. The signal is the delta, not the document.

## Status

| Component | State |
|---|---|
| `ledger/edgar.py` — EDGAR client (filings, documents, exhibits, XBRL facts) | working |
| `ledger/parse.py` — HTML → text, Item section splitting | working |
| `ledger/diff.py` — risk-factor splitting, block pairing, delta classification | working |
| `ledger/statements.py` — income statement extraction from rendered R-files | working |
| `ledger/quality.py` — deterministic earnings-quality rules | working, 3 rules |
| `ledger/provenance.py` — source reliability classes A–F, verification rule | working |
| `ledger/nhtsa.py` — safety recall client with explicit entity gate | working |
| `ledger/statements.py` — member-labeled note tables, subtotal detection | working |
| `ledger/composition.py` — change attribution across revenue components | working |
| `ledger/claims.py` — claim extraction and substantiation from earnings releases | working |
| `ledger/report.py` — scan orchestration; no check may raise | working |
| `ledger/render.py` — terminal and HTML renderers | working |
| `ledger/api.py` — JSON API over the same reports | working |
| `ledger/summary.py` — model-written executive summary over computed findings | working, untested against a live key |
| `ledger/server.py` — local web frontend, stdlib only | working |
| `ledger/sources.py` — user-declared sources in TOML, env-var secrets | working |
| `ledger/config.py` — preset shortcuts and entity mappings in TOML | working |
| `ledger/store.py` — versioned as-filed state, restatement detection | working |
| `ledger/prices.py` — split-adjusted daily closes, market-adjusted returns | working |
| `ledger/backtest.py` — event study over filing-language change | working, result inconclusive |
| Schema-constrained state extractor | not started |
| Materiality scoring vs company's own change rate | not started |

## Setup

```sh
python3 -m venv .venv && ./.venv/bin/pip install requests beautifulsoup4 lxml anthropic
export SEC_USER_AGENT="Contour you@example.com"   # SEC returns 403 without a contact address
./.venv/bin/python scripts/scan.py TSLA                 # terminal report
./.venv/bin/python scripts/scan.py TSLA AAPL --html out/  # + standalone HTML
./.venv/bin/python scripts/serve.py --port 8077           # http://127.0.0.1:8077
./.venv/bin/python scripts/serve.py --port 8077 --daily   # + rescan tracked every 24h
CONTOUR_PROFILE=dev ./.venv/bin/python scripts/serve.py   # a clean slate; see scripts/profile.py
./.venv/bin/python scripts/rescan.py                      # rescan now, or from cron
./.venv/bin/python scripts/backtest.py                    # event study
```

Visit `/sources` to see configured sources and add your own.

### JSON API

Every report the pages render is available as JSON, keyless:

```
GET /api                        endpoint index
GET /api/scan?ticker=TSLA       full report, with provenance per finding
GET /api/tracked                companies with a recorded baseline
GET /api/changes?ticker=TSLA    what changed between the last two scans
GET /api/sources                configured sources and reliability classes
```

`status` on each check stays three-valued — `ok`, `clean`, `unavailable` — so a
consumer can tell "checked and found nothing" from "could not run". Findings
carry the accession they came from, so any figure can be re-derived from source.

**A rescan reports what changed, not that it ran.** Every scan records a
fingerprint per finding — the check it came from plus its headline, which carries
the figures — so the next scan can say what appeared and what went away. Rescan
by hand from `/tracked`, on a 24-hour background pass with `--daily`, or from
cron via `scripts/rescan.py`.

**Scanning and tracking are different acts.** A scan answers a question now.
Tracking takes a dated baseline — the as-filed figures held at that moment — so
later scans are measured against something. A baseline is taken once and never
overwritten; `/tracked` lists every company with the date it was taken, scans
since, and figures added since.

Adding a company is entirely in the app, at `/sources`:

1. **Scan it** — nothing to configure. Any of the ~10,400 tickers in SEC's
   register resolves and scans.
2. **Add a shortcut** so it is one click. The ticker is resolved against SEC's
   register before saving, so a shortcut cannot point at nothing.
3. **Map it** for sources not keyed on CIK — an NHTSA make, and a name per
   name-based source. Blank removes a mapping.

Steps 2 and 3 write `config/presets.toml` and `config/entities.toml`, both of
which stay hand-editable. Mappings layer over whatever a source file declares.

The frontend takes one or two tickers and scans them concurrently, so a
comparison costs the slower of the two rather than their sum.

Verified across 16 tickers with zero crashes. Every check reports one of three
outcomes: findings, nothing flagged, or unavailable with a reason.

## Where the model sits

One place, and only one: an executive summary that narrates findings already
computed. It receives the findings as text and nothing else — no filings, no
tools, no lookups — so it cannot introduce a figure that was not derived from a
filing first. Its output is labelled as model-written wherever it appears, so a
reader never has to wonder whether a number was computed or generated.

It needs `ANTHROPIC_API_KEY` (or an `ant auth login` profile) and uses
`claude-opus-5`. Without credentials it reports unavailable with a reason and the
report is otherwise unchanged — narration is an addition, never a dependency.

## Everything else: no model, no vendor, no key

No check depends on an LLM or a paid service. Findings are arithmetic over
parsed filings, and every enabled source is keyless. The JSON API exists so other
software can call the checks without adopting an agent to do it.

## Design notes

**Deterministic first.** Block pairing uses lexical overlap with no model in the
loop, so the same two filings always produce the same delta set. Judgment about
which deltas *matter* sits on top of that stable substrate.

**State is typed facts, not text.** Diffing raw filing text yields hundreds of
boilerplate changes. State should be a structured extraction (officers, risk
factors as discrete claims, concentration, segments, open proceedings) so a
change reads as a typed delta on a typed field.

**The backtest did not confirm the thesis, and says so.** The prediction was
locked in `journal/hypotheses/` before any backtest code was written. It failed
on its own pre-registered criteria — the spread flips sign between horizons,
which the hypothesis named in advance as the "not working" pattern. Recorded as
`sample_too_small`: 55 events cannot separate a 1-3pp quarterly effect from ~15pp
quarterly volatility, and the change-score buckets differ by only 0.10. The
pipeline is correct; the sample is not adequate to conclude anything.

**Observations are stored as filed, never overwritten.** The unique key on the
observations table includes the filing date, so a revised figure lands beside its
predecessor instead of replacing it. That is the entire point: overwriting makes
a restatement indistinguishable from a routine update.

**Restatement detection excludes per-share figures.** A stock split restates
every historical EPS wholesale — Apple's Q3 2020 EPS moves $2.58 to $0.65 across
filings on a 4-for-1 split. Reporting that as a restatement would be a confident
accusation about a routine corporate action. Net income, revenue and operating
income are unaffected by splits and are the meaningful targets.

**Secrets never live in a source file.** A header written as `${NAME}` is read
from the environment at request time, and a source whose variable is unset
reports as unavailable naming the variable rather than sending a blank header.

**Any source, no code.** Sources are declared in `sources/*.toml` and run beside
EDGAR and NHTSA. A declared source inherits both built-in rules: entities are
mapped by hand rather than searched by name, and the declared reliability class
travels with every finding it produces — so a Class-F source corroborates but can
never mark a finding verified. Where a URL keys on `{cik}` no mapping is needed,
because a CIK is exact where a name is not.

**A source URL is a fetch the server performs.** Schemes are restricted to
http/https and hosts resolving to private, loopback, link-local or reserved
addresses are refused, so a declared source cannot turn the app into a request
proxy into the host network.

**The frontend is standard library only.** No Flask, no build step, nothing to
install on a laptop at 11pm. `http.server` plus the existing renderer is enough.

**Cache the negative answers too.** NHTSA returns non-200 for a model/year with
no recalls, and not caching those meant most of ~40 requests re-ran on every
scan — a warm Tesla scan stayed at 5s until they were cached. It is now 0.8s.

**Three outcomes, never two.** A check reports OK, CLEAN, or UNAVAILABLE.
"Checked and found nothing" and "could not run" are different results and are
modelled separately rather than distinguished at render time.

**Withhold an unreliable comparison rather than publish it.** When fewer than
40% of risk factors pair across two years of the same company, the split changed
rather than the company. Reporting every block as an addition and a removal
would be confidently wrong, so the comparison is withheld with the pair rate
stated.

**Risk factors pair on heading, not body.** The heading is the risk factor's
identity; the body is what changed. Pairing on body text made a heavily
rewritten risk factor read as a removal plus an addition — Starbucks rewrote its
prose enough to score 0.07 body overlap on risk factors whose headings clearly
match. Pairing on headings lifted the match rate from 10% to 89%.

**Nothing is verified without a Class-A source.** Sources carry a reliability
class — A primary authoritative, B company primary, C independent reporting,
D structured commercial, E press release, F community signal. A finding is only
described as verified when a Class-A source supports it, so community signal can
corroborate a claim but never promote one.

**Entities are typed by a person, never guessed.** A form is fine; a name search
is not. Mappings are entered per company and layered over the shipped defaults —
nothing infers a manufacturer or a search term from a company name, because that
is what returns *Tesla Laboratories Inc.* for a Tesla query.

**Entities are linked by hand, never by name match.** There is no universal
company identifier across public databases: SEC keys on CIK, USAspending on UEI,
NHTSA on make. A name search for "Tesla" on USAspending returns Tesla
Laboratories Inc., an unrelated government contractor. Every non-SEC source is
gated on an explicit mapping plus a verification field from the source itself.

**A rule that cannot run says so.** `evaluate()` returns findings *and* skips.
Issuers label the same concept differently, and a lookup that silently returns
nothing would be indistinguishable from a clean result. Skips are printed.

**Risk factors split on the filing's own headings.** Emphasis-only blocks are
marked during flattening and carried through as a sentinel, so the document's
heading structure survives. Length-based blocking straddles risk factors and
reports alignment noise as change; it remains only as a degraded fallback for
filings that mark no headings.

**The heading sentinel must not be whitespace.** `\x1e` looks like a safe
control character but Python treats it as whitespace, so `.strip()` and `\s`
silently ate every marker. The sentinel is `U+E000`, private use.

**Claims are substantiated, never fact-checked.** Verdicts are SUPPORTED,
QUALIFIED, UNSUPPORTED or NOT_TESTABLE — never true/false. Management statements
are rarely false; they are selective. A record that holds only within one fiscal
quarter is QUALIFIED, and the period that beats it is named.

**Refusing to test is a feature.** Per-share figures are stored as filed and are
not split-adjusted, so a record test on EPS across Apple's 7:1 and 4:1 splits
would call a true statement false. Split-sensitive metrics, rolling-window
claims and qualitative statements return NOT_TESTABLE with the reason.

**Subtotals are not components.** Note tables interleave subtotals with the
lines that compose them, so summing every row double-counts. A row is treated as
a subtotal when it equals the sum of a contiguous run of the rows below it.

**A dominant component driving the change is arithmetic, not insight.** The
concentration rule excludes any line already above a quarter of the base — it
exists to catch small lines moving big numbers, not to restate that the biggest
business is the biggest business.

**XBRL companyfacts is not enough.** It exposes only `us-gaap` and `dei`, so
company extension tags — Tesla's automotive regulatory credits among them — are
absent. Statement-level work reads the rendered R-files listed in
`FilingSummary.xml`, which carry every line as filed.

**Watch for phantom deltas.** If extraction wobbles between runs, the diff
reports changes that never happened. Extraction must be schema-constrained, and
both sides of a comparison must be extracted with the same extractor version.

## Sources

| Source | Access | Role |
|---|---|---|
| SEC 10-K | free, no auth | annual baseline; richest diff target |
| SEC 10-Q | free, no auth | the heartbeat — Part II Item 1A carries risk-factor changes |
| SEC 8-K | free, no auth | events; Item 2.02 exhibits carry management quotes |
| SEC Form 4 | free, no auth | insider transactions, filed within 2 business days |
| SEC XBRL company facts | free, no auth | reported GAAP figures — the substantiation corpus |
| NHTSA recalls | free, no auth | dated safety events, manufacturer-verified |
| USPTO trademarks | free, needs key | unannounced product names |
| NHTSA models | capped at 10/year | a full line is ~70 models/year; the cap is reported, never silent |
| USPTO patents | free, needs key | published applications; **trademarks are not available** — every ODP trademark route 403s like a bogus path while `patent/applications/search` 401s, so only the patent route exists. Trademark data needs TSDR (per serial number) or bulk XML. |
| USAspending | free, no auth | federal award history — needs UEI-level entity gate |
| Federal Register | free, no auth | regulatory actions — blocked on entity resolution |

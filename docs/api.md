# JSON API

The checks are plain computation over public filings, so anything can call
them — no agent, no key, no subscription.

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

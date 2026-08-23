# Sources

What Contour reads from, what it costs to reach, and what each one is for.
Add your own from the app at `/sources`, or by dropping a TOML file in
`sources/`.

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

# Component status

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

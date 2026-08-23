# Contour

Tracks what a public company **said**, what it **filed**, and what **changed** —
then surfaces where the three disagree.

An initial scan builds a baseline state for a company from primary sources.
Later scans diff against that baseline. The signal is the delta, not the
document.

Every figure on screen is computed from a filing and cites the accession it came
from. No model decides what is true.

## Background

Built for the **AILaunch Arizona AI Agent Hackathon** (22–23 August 2026,
Chandler, AZ).

**Team:** James, Jim, Scott, Blake.

The premise: filings are public and free, but the interesting thing about them
is rarely in any single document — it is in what moved between two of them. A
risk factor that quietly disappeared, a segment carrying a fifth of the revenue
and half the growth, a closed period restated a year later without an
announcement. Contour reads the primary sources and reports those deltas, with
the citation attached to each one.

The constraint we set ourselves, and the one that shaped most of the design:

> **"We found nothing", "this does not apply here", and "we could not check"
> must never look the same.**

A tool that renders a gap identically to a clean result is worse than no tool,
because it converts ignorance into false confidence. Each of those is a distinct
state in Contour, and a check that could not run always says why.

## Install

Python 3.11+. No build step, no framework, no database server.

```sh
git clone https://github.com/jchu0/contour.git
cd contour
python3 -m venv .venv
./.venv/bin/pip install requests beautifulsoup4 lxml anthropic
```

The SEC rejects unidentified traffic, so set a contact address before scanning —
without it every EDGAR request returns `403`:

```sh
export SEC_USER_AGENT="Contour you@example.com"
```

Optional:

| Variable | Effect |
|---|---|
| `ANTHROPIC_API_KEY` | Enables the executive summary and model-written checks. Everything else runs without it. |
| `CONTOUR_PROFILE` | Which set of accumulated state to use. Unset is the default profile. |
| `CONTOUR_USER_NAME`, `CONTOUR_USER_EMAIL` | Name shown in the sidebar account block. |

## Usage

Run the web app:

```sh
./.venv/bin/python scripts/serve.py --port 8077          # http://127.0.0.1:8077
./.venv/bin/python scripts/serve.py --port 8077 --daily  # + rescan watched companies every 24h
```

Then open `http://127.0.0.1:8077` and scan a ticker — any of the ~10,400
registered with the SEC.

| Page | What it does |
|---|---|
| **Overview** | Watchlist with per-company deltas, recent activity, ledger totals |
| **Scan** | One company: findings, what was clean, what could not run, and why |
| **Compare** | Two companies aligned row-by-row on the same checks |
| **Add company** | Pick the checks that suit a company, then take a baseline |
| **Sources** | What Contour reads from; add your own feeds and name mappings |

From the terminal:

```sh
./.venv/bin/python scripts/scan.py TSLA                    # terminal report
./.venv/bin/python scripts/scan.py TSLA AAPL --html out/   # standalone HTML
./.venv/bin/python scripts/rescan.py                       # rescan now, or from cron
./.venv/bin/python scripts/backtest.py                     # event study over recorded flags
```

Profiles keep demo state separate from a clean slate:

```sh
./.venv/bin/python scripts/profile.py list        # what state exists where
./.venv/bin/python scripts/profile.py archive     # snapshot the current state
./.venv/bin/python scripts/profile.py reset dev   # empty a named profile
CONTOUR_PROFILE=dev ./.venv/bin/python scripts/serve.py --port 8078
```

HTTP caches are shared across profiles — they hold public documents, not state,
so a clean profile's first scan is still fast and the SEC is not re-hit.

## What it checks

Three layers, in order of how much judgment each involves:

1. **Fixed checks** — six that suit any registrant: financial condition,
   revenue composition and concentration, year-over-year risk-factor changes,
   management claims against reported figures, restatements of closed periods,
   and safety records. Declared feeds run beside them; three ship enabled.
2. **A catalogue** — 25 sector-specific checks. A bank gets loan-loss
   provisioning and deposit base; a retailer gets inventory against revenue; a
   chip designer gets R&D intensity and single-source supply language.
   Applicability is decided by code (SIC code, and whether the company actually
   reports the concept), relevance is ranked by a model, and the roster is
   confirmed by a person.
3. **Authored checks** — when nothing in the catalogue fits, a model writes a
   specification against a constrained language: which reported concept, which
   filing section, which phrase, which threshold. A deterministic executor runs
   it. A specification naming a concept the company never reported is reported
   as a gap, never as a pass.

## Documentation

| | |
|---|---|
| [docs/design.md](docs/design.md) | Why it is built this way, and where the one model sits |
| [docs/sources.md](docs/sources.md) | Every source, its access model, and its role |
| [docs/api.md](docs/api.md) | The JSON API — call the checks without an agent |
| [docs/status.md](docs/status.md) | Per-component state |

## Licence

Not yet chosen. Filings are public record; this code is not licensed for reuse
until we pick one.

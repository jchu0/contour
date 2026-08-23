"""JSON API.

The checks are plain computation over public filings — no model, no vendor, no
key. That only matters if something other than this app can call them, so the
same reports the pages render are available as JSON.

Every response carries the same provenance the HTML does: which filing a figure
came from, its reliability class, and whether that class permits calling it
verified. A consumer can re-derive any number from the accession it names.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from ledger.report import Report, Status
from ledger.store import Delta, Tracked

API_VERSION = "1"


def _source_dict(source: Any) -> dict | None:
    if source is None:
        return None
    return {
        "origin": source.origin,
        "reference": source.reference,
        "class": source.klass.letter,
        "class_label": source.klass.label,
        "weight": source.klass.weight,
        "verified": source.verified,
        "entity_proposed": source.entity_proposed,
        "document_date": source.document_date.isoformat(),
        "retrieved": source.retrieved.isoformat(),
        "url": source.url,
        "href": source.href,
        "passage": source.passage,
    }


def report_dict(report: Report) -> dict:
    return {
        "api_version": API_VERSION,
        "ticker": report.ticker,
        "company": report.company,
        "cik": report.cik,
        "generated": report.generated.isoformat(),
        "filing": (
            {
                "form": report.filing.form,
                "filed": report.filing.filed.isoformat(),
                "period": report.filing.period.isoformat() if report.filing.period else None,
                "accession": report.filing.accession,
                "url": report.filing.url,
            }
            if report.filing
            else None
        ),
        "findings": report.findings,
        "unavailable": len(report.unavailable),
        "not_applicable": len(report.not_applicable),
        "authored": len(report.authored),
        "checks": [
            {
                "key": check.key,
                "title": check.title,
                # ok | clean | not_applicable | unavailable. "found nothing",
                # "does not apply here" and "could not run" are three different
                # answers and stay different here.
                "status": check.status,
                "reason": check.reason,
                # Null for the fixed checks. When set, a model chose what this
                # check looks at; the figures are still computed from filings.
                "authored_by": check.authored_by,
                "rationale": check.rationale or None,
                "notes": list(check.notes),
                "items": [
                    {
                        "severity": item.severity,
                        "headline": item.headline,
                        "detail": item.detail,
                        "evidence": dict(item.evidence),
                        "quote": item.quote,
                        "attribution": item.attribution,
                        "source": _source_dict(item.source),
                    }
                    for item in check.items
                ],
                "attributions": [
                    {
                        "member": a.member,
                        "current": a.current,
                        "prior": a.prior,
                        "change": a.change,
                        "base_share": round(a.base_share, 6),
                        "change_share": round(a.change_share, 6),
                        "disproportion": round(a.disproportion, 4),
                    }
                    for a in check.attributions
                ],
            }
            for check in report.checks
        ],
    }


def tracked_dict(rows: list[Tracked]) -> dict:
    return {
        "api_version": API_VERSION,
        "tracked": [
            {
                "ticker": t.ticker,
                "company": t.company,
                "cik": t.cik,
                "tracked_since": t.tracked_since,
                "last_scan": t.last_scan,
                "scans": t.scans,
                "baseline_facts": t.baseline_facts,
                "facts_now": t.facts_now,
                "facts_added": t.facts_added,
            }
            for t in rows
        ],
    }


def delta_dict(ticker: str, delta: Delta | None) -> dict:
    if delta is None:
        return {
            "api_version": API_VERSION,
            "ticker": ticker.upper(),
            "comparable": False,
            "reason": "fewer than two recorded scans for this company",
        }
    return {
        "api_version": API_VERSION,
        "ticker": ticker.upper(),
        "comparable": True,
        "from_scan": delta.from_scan,
        "to_scan": delta.to_scan,
        "quiet": delta.quiet,
        "appeared": [{"check": k, "headline": h} for k, h in delta.appeared],
        "resolved": [{"check": k, "headline": h} for k, h in delta.resolved],
    }


def sources_dict(sources: list, problems: list[str]) -> dict:
    return {
        "api_version": API_VERSION,
        "problems": problems,
        "sources": [
            {
                "name": s.name,
                "class": s.klass.letter,
                "can_verify": s.klass.letter == "A",
                "kind": s.kind,
                "enabled": s.enabled,
                "note": s.note,
                "needs_entity": s.needs_entity,
                "coverage": s.coverage,
                "missing_env": s.missing_env,
            }
            for s in sources
        ],
    }


def index_dict() -> dict:
    """What is callable, so a consumer does not have to read the source."""
    return {
        "api_version": API_VERSION,
        "about": (
            "Primary-source checks over SEC EDGAR and NHTSA. Every figure is "
            "computed from a filing and cites it. No model decides what is true, "
            "and no endpoint requires a key."
        ),
        "endpoints": [
            {"path": "/api", "returns": "this index"},
            {"path": "/api/scan?ticker=TSLA", "returns": "a full report"},
            {"path": "/api/tracked", "returns": "companies with a recorded baseline"},
            {"path": "/api/changes?ticker=TSLA", "returns": "what changed between the last two scans"},
            {"path": "/api/sources", "returns": "configured sources and their reliability class"},
        ],
        "check_status_values": {
            "ok": "ran and found something",
            "clean": "ran and found nothing",
            "not_applicable": "ran far enough to establish the check does not apply to this company; see reason",
            "unavailable": "could not run; see reason",
        },
        "source_classes": {
            "A": "primary authoritative — the only class that marks a finding verified",
            "B": "company primary",
            "C": "independent reporting",
            "D": "structured commercial",
            "E": "press release",
            "F": "community signal",
        },
    }


def dumps(payload: dict) -> bytes:
    return json.dumps(payload, indent=2, sort_keys=False).encode("utf-8")

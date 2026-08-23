"""Scan orchestration.

Runs every check for one company and returns a structured result. No check may
raise: a check that cannot run reports UNAVAILABLE with the reason, which is a
different outcome from CLEAN. Conflating the two is the failure this whole
project argues against, so the distinction is first-class in the model rather
than a formatting decision made later.
"""

from __future__ import annotations

import re
import traceback
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Callable

from ledger.claims import Verdict, extract_claims, substantiate
from ledger.composition import concentration_findings, decompose, offsets
from ledger.diff import Change, diff_blocks, split_risk_factors, summarize
from ledger.edgar import EdgarClient, Filing
from ledger.nhtsa import NhtsaClient, builds_vehicles, make_for
from ledger.parse import find_section, html_to_text, split_items
from ledger.provenance import Source, SourceClass, text_fragment
from ledger.quality import evaluate
from ledger.sources import CustomSource, citation, clip, load_sources, query
from ledger.statements import (
    find_report,
    find_revenue_breakdown,
    quarterly_history,
    quarterly_observations,
)
from ledger.store import (
    connect,
    record_findings,
    record_observations,
    record_scan,
    restatements,
)

RECALL_YEARS = [2024, 2025, 2026]
MAX_RECALLS_SHOWN = 6
MAX_CLAIMS_SHOWN = 8
MIN_RISK_FACTORS = 5
MIN_PAIR_RATE = 0.4


class Status:
    OK = "ok"                    # ran, and found something
    CLEAN = "clean"              # ran, and found nothing — a real result
    UNAVAILABLE = "unavailable"  # could not run, reason given
    # Ran far enough to establish the check does not apply here. That is a
    # conclusion, not a gap: NHTSA holds no recalls for a chipmaker because a
    # chipmaker builds no vehicles, and filing it under "could not run" invites
    # a reader to think something is missing when nothing is.
    NOT_APPLICABLE = "not_applicable"


@dataclass
class Item:
    severity: str                       # high | medium | low | info
    headline: str
    detail: str = ""
    evidence: dict[str, str] = field(default_factory=dict)
    quote: str | None = None
    attribution: str | None = None
    source: Source | None = None


@dataclass
class Check:
    key: str
    title: str
    status: str = Status.CLEAN
    reason: str | None = None
    items: list[Item] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # Structured data a renderer may chart. Kept as data, not markup, so the
    # terminal renderer can ignore it and the HTML one can draw it.
    attributions: list = field(default_factory=list)
    # Set when a model wrote the rule. The figures below it are still computed
    # from the filing and still cite it; what the model chose was where to look.
    authored_by: str | None = None
    rationale: str = ""

    def add(self, item: Item) -> None:
        self.items.append(item)
        self.status = Status.OK


@dataclass
class Report:
    ticker: str
    company: str
    cik: int
    generated: date
    filing: Filing | None = None
    checks: list[Check] = field(default_factory=list)

    @property
    def findings(self) -> int:
        return sum(len(c.items) for c in self.checks)

    @property
    def unavailable(self) -> list[Check]:
        return [c for c in self.checks if c.status == Status.UNAVAILABLE]

    @property
    def authored(self) -> list[Check]:
        return [c for c in self.checks if c.authored_by]

    @property
    def not_applicable(self) -> list[Check]:
        return [c for c in self.checks if c.status == Status.NOT_APPLICABLE]

    @property
    def roster(self) -> list[Check]:
        """The checks this company actually gets.

        A check established as out of scope is not a result to display — NHTSA
        holds no recalls for a chipmaker because chipmakers build no vehicles,
        and printing that on every scan is noise, not provenance. It stays on
        the report so the roster can account for it, but nothing renders it as
        a row.
        """
        return [c for c in self.checks if c.status != Status.NOT_APPLICABLE]

    @property
    def ran(self) -> list[Check]:
        """Checks that produced an answer — including "does not apply"."""
        return [c for c in self.checks if c.status != Status.UNAVAILABLE]


def _money(value: float) -> str:
    for scale, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= scale:
            return f"${value / scale:,.2f}{suffix}"
    return f"${value:,.0f}"


def _run(check: Check, body: Callable[[Check], None], *, debug: bool = False) -> Check:
    """Run one check, converting any failure into a stated reason."""
    try:
        body(check)
    except Exception as exc:  # noqa: BLE001 — a failed check must never end the scan
        check.status = Status.UNAVAILABLE
        # A SourceError carries deliberate copy. Prefixing it with the class name
        # turns a considered position into what looks like a stack trace.
        from ledger.sources import SourceError

        check.reason = (str(exc) if isinstance(exc, SourceError)
                        else f"{type(exc).__name__}: {exc}")[:240]
        if debug:
            check.notes.append(traceback.format_exc())
    return check


# -- individual checks -----------------------------------------------------


def _financial_condition(client: EdgarClient, filing: Filing, source: Source):
    def body(check: Check) -> None:
        # Issuers name this five different ways; try each before giving up.
        statement = next(
            (r for r in (
                find_report(client, filing, title) for title in (
                    "Statements of Operations", "Statements of Income",
                    "Statements of Earnings", "Income Statements", "Statement of Operations",
                )
            ) if r is not None),
            None,
        )
        if statement is None:
            check.status = Status.UNAVAILABLE
            check.reason = "no income statement found in this filing"
            return
        located = replace(source, url=statement.url) if statement.url else source
        if statement.url:
            check.notes.append(f"read from: {statement.name}")
        findings, skipped = evaluate(statement, located)
        for f in findings:
            check.add(Item(
                severity=f.severity,
                headline=f.headline,
                detail=f.detail,
                evidence={k: _money(v) for k, v in f.evidence.items()},
                source=f.sources[0] if f.sources else located,
            ))
        for s in skipped:
            check.notes.append(
                f"Could not run {rule_label(s.rule)} — no {s.missing} in this filing"
            )

    return body


def _revenue_composition(client: EdgarClient, filing: Filing, source: Source):
    def body(check: Check) -> None:
        breakdown, why = find_revenue_breakdown(client, filing)
        located = source
        if breakdown is not None and breakdown.url:
            located = replace(source, url=breakdown.url)
        if breakdown is None:
            check.status = Status.UNAVAILABLE
            check.reason = why or "no usable revenue breakdown note in this filing"
            return
        check.notes.append(f"read from: {breakdown.name}")
        attributions = decompose(breakdown)
        check.attributions = attributions
        if not attributions:
            check.status = Status.UNAVAILABLE
            check.reason = "breakdown found, but the period change is too small to attribute"
            return
        for f in concentration_findings(breakdown, attributions):
            check.add(Item(
                severity=f.severity,
                headline=f.headline,
                detail=f.detail,
                evidence={k: _money(v) for k, v in f.evidence.items()},
                source=located,
            ))
        for a in offsets(attributions):
            check.add(Item(
                severity="info",
                headline=f"{a.member} grew {_money(a.change)}, masking {abs(a.change_share):.0%} of the decline",
                source=located,
            ))

    return body


def _management_claims(client: EdgarClient, ticker: str, cik: int):
    def body(check: Check) -> None:
        releases = [f for f in client.filings(cik, forms=["8-K"], limit=25) if "2.02" in f.items]
        if not releases:
            check.status = Status.UNAVAILABLE
            check.reason = "no 8-K Item 2.02 earnings release on file"
            return
        filing = releases[0]
        exhibit = client.earnings_exhibit(filing)
        if exhibit is None:
            check.status = Status.UNAVAILABLE
            check.reason = f"8-K {filing.accession} carries no EX-99 press release"
            return

        source = Source(
            klass=SourceClass.B_COMPANY,
            origin="Company earnings release (8-K EX-99)",
            reference=filing.accession,
            document_date=filing.filed,
            retrieved=date.today(),
            url=exhibit.url,
        )
        claims = extract_claims(html_to_text(client.get(exhibit.url)))
        history = quarterly_history(client.company_facts(cik))
        untestable = 0
        for claim in claims:
            result = substantiate(claim, history)
            if result.verdict is Verdict.NOT_TESTABLE:
                untestable += 1
                continue
            if len(check.items) >= MAX_CLAIMS_SHOWN:
                continue
            check.add(Item(
                severity={"qualified": "medium", "unsupported": "high", "supported": "low"}[result.verdict.value],
                headline=result.verdict.value.upper(),
                detail=result.reason,
                evidence=dict(result.figures),
                quote=claim.text,
                attribution=(
                    f"{claim.speaker}, {claim.speaker_title}" if claim.speaker
                    else "from the company's earnings release"
                ),
                source=replace(source, fragment=text_fragment(claim.text)),
            ))
        check.notes.append(
            f"{untestable} of {len(claims)} claims not testable"
        )

    return body


def _filing_language(client: EdgarClient, cik: int):
    def body(check: Check) -> None:
        filings = client.filings(cik, forms=["10-K"], limit=2)
        if len(filings) < 2:
            check.status = Status.UNAVAILABLE
            check.reason = f"only {len(filings)} 10-K on file; a year-over-year diff needs two"
            return

        def blocks(filing: Filing):
            section = find_section(split_items(html_to_text(client.document(filing))), "1A")
            if section is None:
                raise ValueError(f"no Item 1A in {filing.accession}")
            return split_risk_factors(section.body)

        current, baseline = blocks(filings[0]), blocks(filings[1])
        if len(current) < MIN_RISK_FACTORS or len(baseline) < MIN_RISK_FACTORS:
            check.status = Status.UNAVAILABLE
            check.reason = (
                f"only {min(len(current), len(baseline))} risk factors resolved — this filing "
                "does not mark headings in a way the splitter recognises"
            )
            return
        deltas = diff_blocks(baseline, current)
        counts = summarize(deltas)
        paired = counts["modified"] + counts["unchanged"]
        rate = paired / len(current)
        if rate < MIN_PAIR_RATE:
            # Almost nothing matching across two years of the same company means
            # the split, not the company, changed. Reporting every block as an
            # addition and a removal would be confidently wrong.
            check.status = Status.UNAVAILABLE
            check.reason = (
                f"only {paired} of {len(current)} risk factors paired across years ({rate:.0%}); "
                "the comparison is unreliable and has been withheld"
            )
            return
        check.notes.append(
            f"{len(current)} risk factors, {filings[1].period.year} to {filings[0].period.year}: "
            + ", ".join(f"{k} {v}" for k, v in counts.items())
        )
        source = Source(
            klass=SourceClass.A_PRIMARY,
            origin="SEC EDGAR",
            reference=filings[0].accession,
            document_date=filings[0].filed,
            retrieved=date.today(),
            url=filings[0].url,
        )

        older_year = filings[1].period.year if filings[1].period else filings[1].filed.year
        newer_year = filings[0].period.year if filings[0].period else filings[0].filed.year

        def clean(passage: str) -> str:
            """Drop the running page furniture a 10-K carries.

            Flattened filing text keeps its page footers — "Apple Inc. | 2024
            Form 10-K | 7" — and a stray footer mid-paragraph reads as though
            the extractor grabbed the wrong thing.
            """
            kept = []
            for line in passage.split("\n"):
                stripped = line.strip()
                if _PAGE_FURNITURE.match(stripped):
                    continue
                kept.append(line)
            return "\n".join(kept).strip()

        def cited(passage: str, filing, locate: str = "") -> Source:
            """A citation carrying the paragraph itself.

            The accession tells a reader where to look; the passage saves them
            the trip. For a claim about what a filing says, the filing's own
            words are the evidence.

            The date has to travel with the accession. A removed risk factor is
            evidence from the *prior* filing, and stamping it with the current
            filing's date reads as a claim about when that document was filed.
            """
            year = filing.period.year if filing.period else filing.filed.year
            body = clean(passage)
            # Point at the sentence, not the document: the same phrase can
            # appear a dozen times in a 10-K and the reader should not have to
            # guess which one the finding was read from. `locate` must be text
            # from THIS filing — a rewritten factor carries both years, and a
            # fragment built from the wrong half scrolls to nothing at all.
            return replace(
                source,
                passage=body or None,
                reference=f"{filing.accession} (FY{year})",
                document_date=filing.filed,
                url=filing.url or source.url,
                fragment=text_fragment(clean(locate or passage)),
            )

        for delta in deltas:
            if delta.change is Change.REMOVED:
                check.add(Item(
                    "high", "Risk factor removed", delta.lead,
                    source=cited(delta.old.text if delta.old else "", filings[1]),
                ))
            elif delta.change is Change.ADDED:
                check.add(Item(
                    "medium", "Risk factor added", delta.lead,
                    source=cited(delta.new.text if delta.new else "", filings[0]),
                ))
        rewritten = [d for d in deltas if d.change is Change.MODIFIED and d.similarity < 0.5]
        for delta in rewritten[:4]:
            both = ""
            if delta.old and delta.new:
                both = (f"As filed FY{older_year}:\n{delta.old.text.strip()}"
                        f"\n\nAs filed FY{newer_year}:\n{delta.new.text.strip()}")
            check.add(Item(
                "low", f"Substantially rewritten ({delta.similarity:.0%} overlap)",
                delta.lead,
                source=cited(both, filings[0],
                             locate=delta.new.text if delta.new else ""),
            ))

    return body


def _safety_record(ticker: str, company_name: str, profile: dict | None = None):
    def body(check: Check) -> None:
        make = make_for(ticker)
        if not make:
            sic = (profile or {}).get("sic", "")
            if sic and not builds_vehicles(sic):
                # Not a gap to be filled — a fact. NHTSA holds recalls for road
                # vehicle manufacturers, and this company is not one.
                label = (profile or {}).get("sic_description") or f"SIC {sic}"
                check.status = Status.NOT_APPLICABLE
                check.reason = (
                    f"not a vehicle manufacturer — {label.strip()} (SIC {sic}). "
                    "NHTSA holds no recalls for this registrant."
                )
                return
            check.status = Status.UNAVAILABLE
            check.reason = (
                f"{ticker.upper()} builds vehicles but has no NHTSA make mapped — "
                "add one on the Data sources page"
            )
            return
        recalls, cap_note = NhtsaClient().recalls(
            make, RECALL_YEARS, manufacturer_must_contain=company_name.split(",")[0]
        )
        check.notes.append(
            f"NHTSA make '{make}', model years {RECALL_YEARS[0]}-{RECALL_YEARS[-1]}"
        )
        if cap_note:
            check.notes.append(cap_note)
        if not recalls:
            return
        check.notes.append(f"{len(recalls)} recall campaigns on record")
        for recall in recalls[:MAX_RECALLS_SHOWN]:
            check.add(Item(
                severity="high" if recall.park_it else "medium",
                headline=f"{recall.component[:70]}",
                detail=clip(recall.summary, 220),
                evidence={"campaign": recall.campaign, "reported": recall.reported.isoformat()},
                source=Source(
                    klass=SourceClass.A_PRIMARY,
                    origin="NHTSA",
                    reference=recall.campaign,
                    document_date=recall.reported,
                    retrieved=date.today(),
                    url=recall.url,
                    # The summary says what is being recalled; the consequence
                    # says what happens if it is not. Only the first is on screen.
                    passage=recall.consequence.strip() or None,
                ),
            ))

    return body


def _custom_source(source: CustomSource, ticker: str, cik: int, company_name: str = ""):
    def body(check: Check) -> None:
        if source.note:
            check.notes.append(source.note)
        # A name nobody has confirmed. The records below may be this company's
        # or another's, so the check says so before listing any of them and
        # nothing here can carry a verified badge.
        proposal = source.proposed_for(ticker)
        if proposal:
            check.notes.append(
                f'searched as "{proposal["entity"]}" — a name proposed by '
                f'{proposal.get("model", "a model")} '
                f'({proposal.get("confidence", "unknown")} confidence), not yet confirmed. '
                f'Confirm it on the Data sources page to let these findings count as verified.'
            )
        hits = query(source, ticker, cik, company_name)
        if not hits:
            return
        for hit in hits:
            cite = citation(source, hit)
            if proposal and cite is not None:
                cite = replace(cite, entity_proposed=True)
            check.add(Item(
                severity=source.severity,
                headline=hit.title,
                detail=hit.detail,
                evidence={},
                source=cite,
            ))

    return body


def _restatements(client: EdgarClient, ticker: str, cik: int):
    def body(check: Check) -> None:
        facts = client.company_facts(cik)
        observations = quarterly_observations(facts)
        with connect() as connection:
            added = record_observations(connection, ticker, cik, observations)
            found = restatements(connection, ticker)
        check.notes.append(
            f"{len(observations)} reported figures on file, {added} new to the ledger"
        )
        check.notes.append(
            "per-share figures excluded (a split restates them wholesale)"
        )
        if not found:
            return
        for item in found[:6]:
            direction = "raised" if item.change > 0 else "lowered"
            pct = f"{item.change_pct:+.1f}%" if item.change_pct is not None else "n/a"
            check.add(Item(
                severity="high",
                headline=f"{item.metric.replace('_', ' ')} for the period ending "
                         f"{item.period_end} was {direction} after the fact",
                detail=(
                    "A closed-period revision is rarely announced and changes every "
                    "comparison drawn against it."
                ),
                evidence={
                    "as first filed": f"{item.original:,.2f} (filed {item.original_filed})",
                    "as later filed": f"{item.revised:,.2f} (filed {item.revised_filed})",
                    "change": pct,
                },
                source=Source(
                    klass=SourceClass.A_PRIMARY,
                    origin="SEC XBRL company facts",
                    reference=f"{item.metric} {item.period_end}",
                    document_date=date.fromisoformat(item.revised_filed),
                    retrieved=date.today(),
                    url=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
                ),
            ))

    return body


# -- orchestration ---------------------------------------------------------


# "Apple Inc. | 2024 Form 10-K | 7", "Table of Contents", a bare page number.
_PAGE_FURNITURE = re.compile(
    r"^(?:Table of Contents|\d{1,4}|.{0,90}\|\s*\d{1,4})$", re.I
)


def _item_text_pair(client: EdgarClient, cik: int, item: str) -> tuple[str, str]:
    """(current, prior) text of one 10-K item across the two latest annual filings.

    Either side may come back empty — a filing whose items the splitter cannot
    resolve. The caller must treat an empty side as "unknown", never as "the
    phrase was absent", or every watched phrase reads as newly appeared.
    """
    filings = client.filings(cik, forms=["10-K"], limit=2)
    out = []
    for filing in filings[:2]:
        try:
            section = find_section(
                split_items(html_to_text(client.document(filing))), item
            )
            out.append(section.body if section else "")
        except Exception:  # noqa: BLE001 — an unreadable filing is an empty side
            out.append("")
    while len(out) < 2:
        out.append("")
    return out[0], out[1]


def _authored_checks(client, company, filing, *, debug: bool = False) -> list[Check]:
    """Run whatever checks were written for this company, or nothing.

    A company with no authored checks adds no checks — silence, not an
    "unavailable" banner, because not having asked for them is not a gap.
    """
    from ledger import authored as A

    pinned = A.load(company.ticker)
    if not pinned.specs:
        return []

    stamp = f"{pinned.model or 'a model'}"
    if pinned.written_on:
        stamp += f", {pinned.written_on}"

    facts = None
    sections: dict[str, tuple[str, str]] = {}

    def load_facts():
        nonlocal facts
        if facts is None:
            facts = client.company_facts(company.cik)
        return facts

    def load_sections(item: str) -> tuple[str, str]:
        """(current, prior) text for one item, fetched once per scan."""
        if item not in sections:
            sections[item] = _item_text_pair(client, company.cik, item)
        return sections[item]

    out: list[Check] = []
    for spec in pinned.specs:
        check = Check(key=f"authored_{spec.key}", title=spec.label,
                      authored_by=stamp, rationale=spec.rationale)
        try:
            if spec.kind == A.KIND_TEXT:
                current, prior = load_sections(spec.section)
                result = A.run_text(spec, current, prior)
            elif spec.kind == A.KIND_RATIO:
                result = A.run_ratio(spec, load_facts())
            else:
                result = A.run_trend(spec, load_facts())
        except Exception as exc:  # noqa: BLE001 — one bad rule is one bad check
            if debug:
                raise
            check.status = Status.UNAVAILABLE
            check.reason = f"could not run — {type(exc).__name__}: {exc}"
            out.append(check)
            continue

        if result.unavailable:
            check.status = Status.UNAVAILABLE
            check.reason = result.unavailable
            out.append(check)
            continue

        check.notes.extend(result.notes)
        for found in result.findings:
            src = None
            if filing is not None:
                src = Source(
                    klass=SourceClass.A_PRIMARY, origin="SEC EDGAR",
                    reference=filing.accession, document_date=filing.filed,
                    retrieved=date.today(), url=filing.url,
                    passage=found.get("passage"),
                )
            check.add(Item(
                severity=spec.severity,
                headline=found["headline"],
                detail=found.get("detail", ""),
                evidence=found.get("evidence", {}),
                source=src,
            ))
        out.append(check)
    return out


def scan(client: EdgarClient, ticker: str, *, debug: bool = False) -> Report:
    company = client.resolve(ticker)
    report = Report(
        ticker=company.ticker, company=company.name, cik=company.cik, generated=date.today()
    )

    try:
        profile = client.profile(company.cik)
    except Exception:  # noqa: BLE001 — metadata is a nicety, not a dependency
        profile = {}

    try:
        annuals = client.filings(company.cik, forms=["10-K"], limit=1)
    except Exception as exc:  # noqa: BLE001
        # A transport failure here used to escape the per-check guard and
        # replace the entire company column with a traceback. It is one check's
        # problem, reported like any other.
        annuals = []
        report.checks.append(Check(
            key="filings_index", title="Filing index",
            status=Status.UNAVAILABLE,
            reason=f"could not reach EDGAR — {type(exc).__name__}. Rescan to retry.",
        ))
    filing = annuals[0] if annuals else None
    report.filing = filing

    if filing is None:
        # A ticker can resolve to a successor holding company whose filing
        # history lives under the predecessor CIK — XOM does exactly this.
        for key, title in (
            ("financial_condition", "Financial condition"),
            ("revenue_composition", "Revenue composition"),
            ("filing_language", "Filing language"),
        ):
            report.checks.append(Check(
                key=key, title=title, status=Status.UNAVAILABLE,
                reason=(
                    f"no 10-K on file under CIK {company.cik}"
                    if key != "financial_condition" else
                    f"CIK {company.cik} has no 10-K on file — likely a successor entity "
                    "whose filing history sits under a predecessor CIK"
                ),
            ))
    else:
        source = Source(
            klass=SourceClass.A_PRIMARY,
            origin="SEC EDGAR",
            reference=filing.accession,
            document_date=filing.filed,
            retrieved=date.today(),
            url=filing.url,
        )
        report.checks.append(_run(
            Check("financial_condition", "Financial condition"),
            _financial_condition(client, filing, source), debug=debug))
        report.checks.append(_run(
            Check("revenue_composition", "Revenue composition"),
            _revenue_composition(client, filing, source), debug=debug))
        report.checks.append(_run(
            Check("filing_language", "Filing language, year over year"),
            _filing_language(client, company.cik), debug=debug))

    report.checks.append(_run(
        Check("management_claims", "Management claims"),
        _management_claims(client, company.ticker, company.cik), debug=debug))
    report.checks.append(_run(
        Check("safety_record", "Safety record"),
        _safety_record(company.ticker, company.name, profile), debug=debug))
    report.checks.append(_run(
        Check("restatements", "Restatements"),
        _restatements(client, company.ticker, company.cik), debug=debug))

    # Model-authored checks, pinned at track time. They run over the same
    # filings as everything above and cite the same accessions; only the choice
    # of what to look at came from a model.
    for check in _authored_checks(client, company, filing, debug=debug):
        report.checks.append(check)

    # User-declared sources run last and are labelled by their own name, so a
    # scan visibly separates what shipped with the tool from what the user added.
    custom, problems = load_sources()
    for source in custom:
        if not source.enabled:
            continue
        report.checks.append(_run(
            Check(f"custom_{source.name.lower().replace(' ', '_')}", source.name),
            _custom_source(source, company.ticker, company.cik, company.name), debug=debug))
    for problem in problems:
        report.checks.append(Check(
            key="custom_error", title="Custom source",
            status=Status.UNAVAILABLE, reason=f"could not load — {problem}",
        ))

    try:
        with connect() as connection:
            scan_id = record_scan(
                connection, ticker=company.ticker, cik=company.cik, company=company.name,
                findings=report.findings, unavailable=len(report.unavailable),
            )
            record_findings(connection, scan_id, company.ticker, [
                (check.key, item.headline, item.severity)
                for check in report.checks for item in check.items
            ])
    except Exception:  # noqa: BLE001 — recording history must never fail a scan
        pass

    return report


# -- human-readable labels -------------------------------------------------

# Evidence keys are named for the code that produced them. On a page read by a
# person they should name the thing, not the variable.
_FIGURE_LABELS = {
    "revenue_current": "Revenue, this period",
    "revenue_prior": "Revenue, prior period",
    "operating_income_current": "Operating income, this period",
    "operating_income_prior": "Operating income, prior period",
    "interest_income": "Interest income",
    "pretax_income": "Income before taxes",
    "net_income": "Net income",
    "tax_provision": "Tax provision",
    "component_current": "This period",
    "component_prior": "Prior period",
    "component_change": "Change",
    "current": "This period",
    "prior best": "Best prior period",
    "higher period": "A period that beats it",
    "comparison set": "Compared against",
    "stated in release": "Stated in the release",
    "reported to SEC": "Reported to the SEC",
    "difference": "Difference",
    "as first filed": "As first filed",
    "as later filed": "As later filed",
    "change": "Change",
    "campaign": "NHTSA campaign",
    "reported": "Reported",
    "date": "Date",
}

# Rule names are identifiers; a skipped check should say what it was looking for.
_RULE_LABELS = {
    "tax_benefit_inflates_net_income": "the tax-benefit check",
    "nonoperating_income_dependence": "the interest-income check",
    "operating_decline_masked": "the operating-margin check",
    "change_concentrated_in_component": "the revenue-concentration check",
}


def figure_label(key: str) -> str:
    """A human name for an evidence key, falling back to a de-underscored form."""
    if key in _FIGURE_LABELS:
        return _FIGURE_LABELS[key]
    return key.replace("_", " ").strip().capitalize()


def rule_label(name: str) -> str:
    return _RULE_LABELS.get(name, name.replace("_", " "))

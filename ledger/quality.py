"""Earnings-quality rules.

Each rule is a deterministic computation over a parsed statement. A claim like
"revenue grew" or "net income doubled" can be literally true while the growth
comes from a source that will not repeat; these rules find that gap without a
model in the loop, so every finding is reproducible and auditable.

A rule that cannot run reports itself as SKIPPED rather than returning nothing.
Silence and a clean bill of health must never look the same.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ledger.provenance import Source, confidence, is_verified
from ledger.statements import Statement

# Issuers spell the same concept differently; list every variant seen in the wild.
REVENUE = ("Total net sales", "Net sales", "Total revenues", "Revenues", "Total revenue", "Revenue")
OPERATING_INCOME = ("Income from operations", "Operating income", "Operating income (loss)")
PRETAX_INCOME = (
    "Income before income taxes",
    "Income before provision for income taxes",
    "Income (loss) before income taxes",
    "Income before income tax",
)
NET_INCOME = ("Net income", "Net income (loss)", "Net income attributable to common stockholders")
INTEREST_INCOME = ("Interest income",)
TAX_PROVISION = ("Provision for income taxes", "Provision for (benefit from) income taxes")


class MissingInput(Exception):
    def __init__(self, concept: str):
        super().__init__(concept)
        self.concept = concept


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str          # high | medium | low
    headline: str
    detail: str
    evidence: dict[str, float]
    sources: tuple[Source, ...] = ()

    @property
    def verified(self) -> bool:
        """Only a Class-A primary source can promote a finding to verified."""
        return is_verified(self.sources)

    @property
    def confidence(self) -> int:
        return confidence(self.sources)


@dataclass(frozen=True)
class Skipped:
    rule: str
    missing: str


def require(st: Statement, concept: str, labels: tuple[str, ...], period: int = 0) -> float:
    value = st.value(*labels, period_index=period)
    if value is None:
        raise MissingInput(f"{concept} (period {period})")
    return value


def _pct(new: float, old: float) -> float | None:
    return None if not old else (new - old) / abs(old) * 100


def tax_benefit_inflates_net_income(st: Statement) -> Finding | None:
    pretax = require(st, "pre-tax income", PRETAX_INCOME)
    net = require(st, "net income", NET_INCOME)
    if pretax <= 0 or net <= pretax:
        return None
    lift = (net - pretax) / pretax * 100
    if lift < 10:
        return None
    tax = st.value(*TAX_PROVISION) or 0.0
    return Finding(
        rule="tax_benefit_inflates_net_income",
        severity="high" if lift > 30 else "medium",
        headline=f"Net income exceeds pre-tax income by {lift:.0f}% — a tax benefit, not operating performance",
        detail=(
            "Net income sits above income before taxes, so the provision was a net benefit. "
            "Benefits of this kind are typically non-recurring and will not repeat next period."
        ),
        evidence={"pretax_income": pretax, "net_income": net, "tax_provision": tax},
    )


def nonoperating_income_dependence(st: Statement) -> Finding | None:
    pretax = require(st, "pre-tax income", PRETAX_INCOME)
    interest = require(st, "interest income", INTEREST_INCOME)
    if pretax <= 0:
        return None
    share = interest / pretax * 100
    if share < 20:
        return None
    return Finding(
        rule="nonoperating_income_dependence",
        severity="high" if share > 35 else "medium",
        headline=f"Interest income is {share:.0f}% of pre-tax income — earnings lean on the balance sheet, not the business",
        detail=(
            "A large share of pre-tax income comes from interest on cash rather than operations. "
            "It moves with rates and cash balances, not with demand for the product."
        ),
        evidence={"interest_income": interest, "pretax_income": pretax},
    )


def operating_decline_masked(st: Statement) -> Finding | None:
    rev_now = require(st, "revenue", REVENUE, 0)
    rev_prior = require(st, "revenue", REVENUE, 1)
    op_now = require(st, "operating income", OPERATING_INCOME, 0)
    op_prior = require(st, "operating income", OPERATING_INCOME, 1)
    rev_change, op_change = _pct(rev_now, rev_prior), _pct(op_now, op_prior)
    if rev_change is None or op_change is None:
        return None
    if op_change > -15 or op_change >= rev_change:
        return None
    return Finding(
        rule="operating_decline_masked",
        severity="high",
        headline=f"Operating income fell {abs(op_change):.0f}% while revenue moved {rev_change:+.1f}%",
        detail=(
            "Operating income is deteriorating far faster than the top line, so the change "
            "is in cost structure or pricing rather than volume."
        ),
        evidence={
            "revenue_current": rev_now, "revenue_prior": rev_prior,
            "operating_income_current": op_now, "operating_income_prior": op_prior,
        },
    )


RULES: list[Callable[[Statement], Finding | None]] = [
    tax_benefit_inflates_net_income,
    nonoperating_income_dependence,
    operating_decline_masked,
]


def evaluate(
    statement: Statement, source: Source | None = None
) -> tuple[list[Finding], list[Skipped]]:
    """Returns (findings, skipped). Skipped rules are reported, never swallowed.

    Rules stay pure — they compute over the statement and know nothing about
    where it came from. `evaluate` attaches the citation, so every finding
    leaves here carrying the filing it was derived from.
    """
    findings: list[Finding] = []
    skipped: list[Skipped] = []
    for rule in RULES:
        try:
            result = rule(statement)
        except MissingInput as exc:
            skipped.append(Skipped(rule=rule.__name__, missing=exc.concept))
            continue
        if result:
            findings.append(
                result if source is None else Finding(**{**result.__dict__, "sources": (source,)})
            )
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(findings, key=lambda f: order[f.severity]), skipped

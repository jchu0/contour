"""Change decomposition.

"Revenue grew" is nearly always true. What moves a stock later is which part of
the business the change came from — and a component can be a rounding error in
the base while driving most of the movement. These functions attribute a period
change across its components so that gap becomes a number rather than a hunch.
"""

from __future__ import annotations

from dataclasses import dataclass

from ledger.quality import Finding
from ledger.statements import Breakdown

# A component must move this share of the total change before it is worth naming.
MIN_CONTRIBUTION = 0.15
# ...and its share of the change must exceed its share of the base by this much.
DISPROPORTION = 2.0
# A component that is already most of the business will dominate any change it
# makes. That is arithmetic, not insight — the rule is for small lines moving
# big numbers, so anything above this share of the base is excluded.
MAX_BASE_SHARE = 0.25


@dataclass(frozen=True)
class Attribution:
    member: str
    current: float
    prior: float
    change: float
    base_share: float          # component's share of the prior-period total
    change_share: float        # component's share of the total change
    disproportion: float       # change_share / base_share

    @property
    def direction(self) -> str:
        return "grew" if self.change > 0 else "fell"


def decompose(breakdown: Breakdown, current: int = 0, prior: int = 1) -> list[Attribution]:
    """Attribute the change in the total across its leaf components."""
    leaves = breakdown.leaves(current)
    total_current = sum(c.values[current] or 0 for c in leaves)
    total_prior = sum(c.values[prior] or 0 for c in leaves)
    total_change = total_current - total_prior
    if not total_prior or abs(total_change) < 1e-9:
        return []

    out: list[Attribution] = []
    for leaf in leaves:
        now = leaf.values[current] or 0
        was = leaf.values[prior] or 0
        change = now - was
        base_share = was / total_prior
        change_share = change / total_change
        out.append(
            Attribution(
                member=leaf.member,
                current=now * breakdown.scale,
                prior=was * breakdown.scale,
                change=change * breakdown.scale,
                base_share=base_share,
                change_share=change_share,
                disproportion=(change_share / base_share) if base_share else 0.0,
            )
        )
    return sorted(out, key=lambda a: abs(a.change_share), reverse=True)


def offsets(attributions: list[Attribution]) -> list[Attribution]:
    """Components that moved against the total, masking part of the change."""
    return [a for a in attributions if a.change_share < 0 and abs(a.change_share) >= MIN_CONTRIBUTION]


def concentration_findings(breakdown: Breakdown, attributions: list[Attribution]) -> list[Finding]:
    """Flag small components carrying far more of the change than of the business."""
    findings: list[Finding] = []
    for a in attributions:
        if a.base_share > MAX_BASE_SHARE:
            continue
        if a.change_share < MIN_CONTRIBUTION or a.disproportion < DISPROPORTION:
            continue
        findings.append(
            Finding(
                rule="change_concentrated_in_component",
                severity="high" if a.disproportion >= 5 else "medium",
                headline=(
                    f"{a.member} is {a.base_share:.1%} of revenue but "
                    f"{a.change_share:.1%} of the change"
                ),
                detail=(
                    f"{a.member} {a.direction} by ${abs(a.change) / 1e9:.2f}B, carrying "
                    f"{a.change_share:.0%} of the total movement while making up only "
                    f"{a.base_share:.1%} of the prior-period base — {a.disproportion:.1f}x its weight."
                ),
                evidence={
                    "component_current": a.current,
                    "component_prior": a.prior,
                    "component_change": a.change,
                },
            )
        )
    return findings

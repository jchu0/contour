"""The roster agent — ranking, never deciding.

Applicability is settled before this runs: `catalogue.applicable` has already
dropped every check that does not fit the sector or names a figure the company
never reported. What is left is a shortlist of things that all genuinely could
run, and the only judgement left is which of them matter most here.

The agent cannot add to that list and cannot remove from it — it orders it and
says why. Without credentials the shortlist falls back to severity order, which
is a worse ranking but an honest one.
"""

from __future__ import annotations

from dataclasses import dataclass

from ledger.agents import MODEL, credentials
from ledger.catalogue import Entry, by_key

RANK_SYSTEM = """You choose which checks a company should be given.

You are handed a shortlist that has ALREADY been filtered: every check on it is \
known to apply to this company's sector and to use figures the company actually \
reports. You cannot add to the list and you cannot decide what is true.

Pick the ones that matter most for THIS company and put them in order. Prefer \
checks that suit its specific business over ones that would suit any filer. \
Recommend between 4 and 8. Say briefly why each is worth running here.

Anything you leave out stays available for the user to add by hand, so leaving \
out a weak check costs nothing."""

RANK_TOOL = {
    "name": "rank_checks",
    "description": "Return the recommended checks in priority order.",
    "input_schema": {
        "type": "object",
        "properties": {
            "recommended": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "a key from the shortlist"},
                        "why": {"type": "string",
                                "description": "one sentence on why it suits this company"},
                    },
                    "required": ["key", "why"],
                },
            }
        },
        "required": ["recommended"],
    },
}


@dataclass(frozen=True)
class Recommendation:
    keys: tuple[str, ...] = ()
    why: dict | None = None
    model: str | None = None
    reason: str | None = None       # set when the model could not be consulted

    @property
    def from_model(self) -> bool:
        return self.model is not None


def default_order(entries: list[Entry]) -> list[Entry]:
    """The fallback ranking: severity first, sector-specific before universal.

    Used whenever the model cannot be reached, so the wizard always has a
    sensible pre-selection rather than an empty list.
    """
    weight = {"high": 0, "medium": 1, "low": 2}
    return sorted(
        entries,
        key=lambda e: (weight.get(e.spec.severity, 1), ANY in e.sectors, e.key),
    )


def recommend(ticker: str, company: str, sic: str, sic_label: str,
              entries: list[Entry], *, model: str | None = None) -> Recommendation:
    """Rank the shortlist. Falls back to `default_order` without credentials.

    This one keeps its own client rather than using `agents.call`: it ranks
    through a tool call, not free text, so the shared text helper does not fit.
    It shares the model and the credential check, which is what matters.
    """
    model = model or MODEL
    if not entries:
        return Recommendation(reason="nothing applies to this company")
    if credentials() is not None:
        return Recommendation(
            keys=tuple(e.key for e in default_order(entries)[:6]),
            reason="ordered by severity — no Anthropic credentials to rank with",
        )
    try:
        import anthropic
    except ImportError:
        return Recommendation(keys=tuple(e.key for e in default_order(entries)[:6]),
                              reason="the anthropic package is not installed")

    shortlist = "\n".join(
        f"- {e.key}: {e.spec.title} — {e.spec.rationale}" for e in entries
    )
    prompt = (f"Company: {company} ({ticker.upper()})\n"
              f"Industry: {sic_label} (SIC {sic})\n\n"
              f"Shortlist:\n{shortlist}")
    try:
        response = anthropic.Anthropic().messages.create(
            model=model, max_tokens=2000, system=RANK_SYSTEM,
            thinking={"type": "adaptive"}, tools=[RANK_TOOL],
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001 — ranking must never block the wizard
        return Recommendation(keys=tuple(e.key for e in default_order(entries)[:6]),
                              reason=f"ordered by severity — ranking failed "
                                     f"({type(exc).__name__})")

    payload = None
    for block in response.content:
        if getattr(block, "type", "") == "tool_use" and block.name == RANK_TOOL["name"]:
            payload = (block.input or {}).get("recommended")
            break
    if not payload:
        return Recommendation(keys=tuple(e.key for e in default_order(entries)[:6]),
                              reason="ordered by severity — the model returned no ranking")

    valid = {e.key for e in entries}
    keys, why = [], {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key", "")).strip()
        if key in valid and key not in keys:
            keys.append(key)
            why[key] = str(row.get("why", "")).strip()[:220]
    if not keys:
        return Recommendation(keys=tuple(e.key for e in default_order(entries)[:6]),
                              reason="ordered by severity — the model named no check on the shortlist")
    return Recommendation(keys=tuple(keys), why=why, model=model)

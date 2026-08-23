"""The check-authoring agent — drafting a specification, not a verdict.

When nothing in the catalogue fits a company, a model writes a check. What it
writes is a specification in a constrained language — which reported concept,
which filing section, which phrase, which threshold — and a deterministic
executor runs it. The model never sees a filing and never decides whether a
check passed.

A specification naming a concept the company never reported is reported as a
gap, not quietly dropped: an authored check that cannot run has to say so, the
same as any other.
"""

from __future__ import annotations

import json

from ledger.agents import MODEL
from ledger.authored import (
    MODES,
    SECTIONS,
    DIRECTIONS,
    SEVERITIES,
    KINDS,
    MAX_PHRASES,
    MAX_SPECS,
    MIN_THRESHOLD,
    Authored,
    CheckSpec,
    parse_specs,
)

SYSTEM = """You write checks for an automated review of a public company's SEC filings.

You do not decide what is true. You emit specifications; a deterministic executor \
runs them against the filings and computes every figure. Your job is to choose \
*where to look* for this particular company.

You are given the concepts this registrant actually reports. Use only those \
names. A concept not on the list does not exist for this company, and a check \
naming one is discarded.

What makes a good check here:

1. It is specific to this company's business. A chip designer competes on R&D \
intensity; a retailer carries inventory risk; a bank carries credit loss \
provisioning. Do not write checks that would suit any company equally.
2. It would not already be covered. The fixed checks already cover: revenue \
composition and concentration, restatements of previously reported figures, \
year-over-year risk-factor changes, and management claims in earnings releases. \
Do not duplicate them.
3. Its threshold is one a reasonable analyst would defend, and it would not fire \
on ordinary movement. Prefer thresholds that would have been quiet in a normal \
year.
4. Its rationale says plainly why this company warrants this check. The rationale \
is shown to the reader beside the finding.

Write 2 to 4 checks. Fewer good checks beat more weak ones."""

TOOL = {
    "name": "emit_checks",
    "description": "Return the checks to run for this company.",
    "input_schema": {
        "type": "object",
        "properties": {
            "checks": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_SPECS,
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "lower_snake_case, 3-40 chars"},
                        "title": {"type": "string", "description": "short heading a reader sees"},
                        "rationale": {"type": "string",
                                      "description": "why THIS company warrants THIS check"},
                        "kind": {"type": "string", "enum": list(KINDS)},
                        "severity": {"type": "string", "enum": list(SEVERITIES)},
                        "concept": {"type": "string",
                                    "description": "us-gaap concept, from the offered list"},
                        "denominator": {"type": "string",
                                        "description": "xbrl_ratio only; from the offered list"},
                        "direction": {"type": "string", "enum": list(DIRECTIONS)},
                        "threshold_pct": {"type": "number",
                                          "description": f"{MIN_THRESHOLD}-1000"},
                        "section": {"type": "string", "enum": list(SECTIONS)},
                        "phrases": {"type": "array", "items": {"type": "string"},
                                    "maxItems": MAX_PHRASES},
                        "mode": {"type": "string", "enum": list(MODES)},
                    },
                    "required": ["key", "title", "rationale", "kind"],
                },
            }
        },
        "required": ["checks"],
    },
}


def available_concepts(facts: dict) -> list[str]:
    """Concepts with at least two comparable annual periods — the ones a check
    can actually be written against."""
    us_gaap = facts.get("facts", {}).get("us-gaap", {}) or {}
    out = []
    for concept in us_gaap:
        if len(annual_series(facts, concept)) >= 2:
            out.append(concept)
    return sorted(out)


def brief(ticker: str, company: str, profile: dict, concepts: list[str]) -> str:
    sic = profile.get("sic", "")
    label = profile.get("sic_description") or ""
    offered = concepts[:MAX_CONCEPTS_OFFERED]
    more = len(concepts) - len(offered)
    return (
        f"Company: {company} ({ticker.upper()})\n"
        f"Industry: {label} (SIC {sic})\n\n"
        f"Concepts this registrant reports with two or more comparable annual "
        f"periods ({len(concepts)} total"
        f"{f', {more} not listed' if more > 0 else ''}):\n"
        + ", ".join(offered)
        + "\n\nSections available for a text watch: "
        + ", ".join(f"Item {k} ({v})" for k, v in SECTIONS.items())
    )


def write_checks(ticker: str, company: str, profile: dict, facts: dict,
                 *, model: str = MODEL) -> Authored:
    """Ask the model for this company's checks. Never raises — a failure here
    leaves the fixed checks untouched and is reported like any other gap."""
    try:
        import anthropic
    except ImportError:
        return Authored(available=False,
                        reason="the anthropic package is not installed — run: pip install anthropic")

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return Authored(available=False, reason=(
            "no Anthropic credentials found. Set ANTHROPIC_API_KEY, or run `ant auth login`. "
            "The nine fixed checks below are computed without a model and are unaffected."
        ))

    concepts = available_concepts(facts)
    if len(concepts) < 5:
        return Authored(available=False, reason=(
            f"only {len(concepts)} reported concepts have two comparable annual "
            f"periods — too little history to write a check against"
        ))

    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=model,
            max_tokens=4000,
            system=SYSTEM,
            thinking={"type": "adaptive"},
            tools=[TOOL],
            messages=[{"role": "user",
                       "content": brief(ticker, company, profile, concepts)}],
        )
    except anthropic.NotFoundError as exc:
        return Authored(available=False, reason=f"model not found: {exc}")
    except anthropic.RateLimitError:
        return Authored(available=False, reason="rate limited — try again shortly")
    except anthropic.APIStatusError as exc:
        return Authored(available=False, reason=f"API error {exc.status_code}: {exc.message}")
    except anthropic.APIConnectionError:
        return Authored(available=False, reason="could not reach the Anthropic API")

    payload = None
    for block in response.content:
        if getattr(block, "type", "") == "tool_use" and block.name == TOOL["name"]:
            payload = (block.input or {}).get("checks")
            break
    if payload is None:                      # no tool call — accept JSON in the text
        text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        match = re.search(r'\{.*"checks".*\}', text, re.S)
        if match:
            try:
                payload = json.loads(match.group(0)).get("checks")
            except json.JSONDecodeError:
                payload = None
    if not payload:
        return Authored(available=False, reason="the model returned no usable checks")

    specs, problems = parse_specs(payload)

    # A concept that survived the syntax check can still be one this registrant
    # never files. Catching it here names the offending check instead of letting
    # it surface later as a mystery gap.
    known = set(concepts)
    kept, dropped = [], list(problems)
    for spec in specs:
        missing = [c for c in (spec.concept, spec.denominator)
                   if c and c not in known]
        if missing:
            dropped.append(f"{spec.key}: {', '.join(missing)} not reported by this company")
            continue
        kept.append(spec)

    if not kept:
        return Authored(available=False, model=model, rejected=tuple(dropped),
                        reason="every proposed check was refused; see the rejections")
    return Authored(specs=tuple(kept), model=model, written_on=date.today().isoformat(),
                    rejected=tuple(dropped))


# -- persistence -----------------------------------------------------------

from pathlib import Path

from ledger import profile  # noqa: E402 — kept beside the code that uses it




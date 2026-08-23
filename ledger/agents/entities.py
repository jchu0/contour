"""The entity agent — proposing a name, never confirming one.

Sources that are not keyed on CIK have to be searched by name, and the wrong
name returns the wrong company's records. That is the one failure this system
will not accept quietly, so nothing here writes a mapping: it proposes, and a
person confirms. A proposed name is marked on every finding that rests on it
until someone says otherwise.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ledger.agents import MODEL

ENTITY_SYSTEM = """You map a public company to the name a given data source files it under.

Each source is searched by NAME, not by ticker or CIK. The registrant's legal \
name on EDGAR is frequently not the name a source uses: a registrant may file as \
"ALPHABET INC" while a docket names "Google", or as "Rivian Automotive, Inc. / DE" \
while a news source says "Rivian".

Return the search term most likely to retrieve THIS company and not another. \
Rules:

1. Prefer the shortest form that is still unambiguous. "Apple" is ambiguous in a \
news source and fine in a federal docket; say so in your reasoning.
2. If a shorter form would collide with an unrelated company, use the longer one.
3. Set confidence honestly. Low confidence is useful; a confident wrong name \
attributes another company's regulatory or safety record to this one.
4. If you cannot map the company for a source, omit it. Omitting is correct \
behaviour, not failure."""

ENTITY_TOOL = {
    "name": "emit_mappings",
    "description": "Return the search name for each source you can map.",
    "input_schema": {
        "type": "object",
        "properties": {
            "mappings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "description": "the source name given"},
                        "entity": {"type": "string", "description": "the search term to use"},
                        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                        "reasoning": {"type": "string",
                                      "description": "why this term, and what it might collide with"},
                    },
                    "required": ["source", "entity", "confidence", "reasoning"],
                },
            }
        },
        "required": ["mappings"],
    },
}


@dataclass(frozen=True)
class Mapping:
    source: str
    entity: str
    confidence: str
    reasoning: str
    model: str


@dataclass(frozen=True)
class Resolved:
    mappings: tuple[Mapping, ...] = ()
    available: bool = True
    reason: str | None = None


def resolve_entities(ticker: str, company: str, sources: list[str],
                     *, model: str = MODEL) -> Resolved:
    """Propose the name each source should be searched by. Never raises."""
    if not sources:
        return Resolved(available=False, reason="no sources need a name mapping")
    try:
        import anthropic
    except ImportError:
        return Resolved(available=False, reason="the anthropic package is not installed")
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return Resolved(available=False, reason=(
            "no Anthropic credentials found. Set ANTHROPIC_API_KEY, or map the name "
            "by hand on the Data sources page."
        ))

    client = anthropic.Anthropic()
    prompt = (f"Registrant: {company}\nTicker: {ticker.upper()}\n\n"
              f"Sources needing a name:\n"
              + "\n".join(f"- {s}" for s in sources))
    try:
        response = client.messages.create(
            model=model, max_tokens=2000, system=ENTITY_SYSTEM,
            thinking={"type": "adaptive"}, tools=[ENTITY_TOOL],
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIStatusError as exc:
        return Resolved(available=False, reason=f"API error {exc.status_code}: {exc.message}")
    except anthropic.APIConnectionError:
        return Resolved(available=False, reason="could not reach the Anthropic API")

    payload = None
    for block in response.content:
        if getattr(block, "type", "") == "tool_use" and block.name == ENTITY_TOOL["name"]:
            payload = (block.input or {}).get("mappings")
            break
    if not payload:
        return Resolved(available=False, reason="the model proposed no mappings")

    wanted = {s.lower(): s for s in sources}
    out = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        name = wanted.get(str(raw.get("source", "")).strip().lower())
        entity = str(raw.get("entity", "") or "").strip()[:120]
        if not name or not entity:
            continue
        conf = str(raw.get("confidence", "")).lower()
        out.append(Mapping(
            source=name, entity=entity,
            confidence=conf if conf in ("high", "medium", "low") else "low",
            reasoning=str(raw.get("reasoning", "") or "").strip()[:300],
            model=model,
        ))
    if not out:
        return Resolved(available=False, reason="no proposal matched a source that needs one")
    return Resolved(mappings=tuple(out))

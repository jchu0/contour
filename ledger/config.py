"""File-backed configuration.

Presets are the shortcuts on the scan page. They are pure convenience — nothing
about correctness depends on them — so they live in TOML beside the sources
rather than in code, and a missing or broken file falls back to the built-in
list rather than taking the app down before a demo.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from ledger import profile

# --- per-company entity mappings ------------------------------------------

ENTITIES_PATH = profile.config_dir() / "entities.toml"

_ENTITIES_HEADER = """# How each company is named in sources that are not keyed on CIK.
#
# Managed from the app at /sources. Every entry is stated by a person — that is
# the point. Sources like NHTSA are keyed on manufacturer, not CIK, and a name
# search against them returns the wrong company often enough that a false
# positive on a safety or enforcement record is worse than no record at all.
# A form is fine; guessing is not.
#
#   [TSLA]
#   nhtsa_make = "tesla"
#   "Federal Register" = "Tesla"
"""


def load_entities(path: Path = ENTITIES_PATH) -> tuple[dict[str, dict[str, str]], list[str]]:
    """{TICKER: {key: value}}. Layered over whatever the source files declare."""
    if not path.exists():
        return {}, []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {}, [f"{path}: {exc} — company mappings ignored"]
    out: dict[str, dict[str, str]] = {}
    for ticker, mapping in data.items():
        if isinstance(mapping, dict):
            out[str(ticker).upper()] = {
                str(k): str(v).strip() for k, v in mapping.items() if str(v).strip()
            }
    return out, []


def entity_for(ticker: str, key: str, path: Path = ENTITIES_PATH) -> str | None:
    """One mapping, or None. `key` is 'nhtsa_make' or a source's name."""
    entities, _ = load_entities(path)
    return entities.get(ticker.upper(), {}).get(key)


def save_entities(entities: dict[str, dict[str, str]], path: Path = ENTITIES_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    def literal(value: str) -> str:
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'

    def key_literal(key: str) -> str:
        return key if key.replace("_", "").isalnum() else literal(key)

    blocks = [_ENTITIES_HEADER]
    for ticker in sorted(entities):
        mapping = {k: v for k, v in entities[ticker].items() if v}
        if not mapping:
            continue
        rows = "\n".join(f"{key_literal(k)} = {literal(v)}" for k, v in sorted(mapping.items()))
        blocks.append(f"[{ticker}]\n{rows}\n")
    path.write_text("\n".join(blocks), encoding="utf-8")


def set_entities(ticker: str, mapping: dict[str, str], path: Path = ENTITIES_PATH) -> None:
    """Replace one company's mappings. Blank values remove that key."""
    ticker = ticker.strip().upper()
    if not ticker.isalnum():
        raise ValueError(f"{ticker or '(blank)'} is not a ticker symbol")
    entities, _ = load_entities(path)
    current = entities.get(ticker, {})
    for key, value in mapping.items():
        value = value.strip()
        if value:
            current[key] = value
        else:
            current.pop(key, None)
    if current:
        entities[ticker] = current
    else:
        entities.pop(ticker, None)
    save_entities(entities, path)


# --- model-proposed entity mappings ---------------------------------------

import json  # noqa: E402

PROPOSED_PATH = profile.config_dir() / "entities_proposed.json"


def load_proposed(path: Path = PROPOSED_PATH) -> dict[str, dict[str, dict]]:
    """{TICKER: {source: {entity, confidence, reasoning, model, proposed_on}}}.

    Kept apart from entities.toml on purpose. That file is the record of what a
    person stated; this one is the record of what a model guessed, and merging
    them would lose the only distinction that matters here.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k).upper(): v for k, v in data.items() if isinstance(v, dict)}


def save_proposed(data: dict, path: Path = PROPOSED_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def propose_entity(ticker: str, source: str, entity: str, *, confidence: str,
                   reasoning: str, model: str, on: str,
                   path: Path = PROPOSED_PATH) -> None:
    data = load_proposed(path)
    data.setdefault(ticker.upper(), {})[source] = {
        "entity": entity, "confidence": confidence, "reasoning": reasoning,
        "model": model, "proposed_on": on,
    }
    save_proposed(data, path)


def proposed_entity(ticker: str, source: str, path: Path = PROPOSED_PATH) -> dict | None:
    return load_proposed(path).get(ticker.upper(), {}).get(source)


def confirm_entity(ticker: str, source: str, entities_path: Path = ENTITIES_PATH,
                   path: Path = PROPOSED_PATH) -> str | None:
    """Promote one proposal to a stated mapping. This is the person saying so."""
    record = proposed_entity(ticker, source, path)
    if not record:
        return None
    set_entities(ticker, {source: record["entity"]}, entities_path)
    data = load_proposed(path)
    data.get(ticker.upper(), {}).pop(source, None)
    if not data.get(ticker.upper()):
        data.pop(ticker.upper(), None)
    save_proposed(data, path)
    return record["entity"]


def reject_entity(ticker: str, source: str, path: Path = PROPOSED_PATH) -> bool:
    data = load_proposed(path)
    removed = data.get(ticker.upper(), {}).pop(source, None) is not None
    if not data.get(ticker.upper()):
        data.pop(ticker.upper(), None)
    save_proposed(data, path)
    return removed

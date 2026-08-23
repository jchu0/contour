"""Every place a model is allowed to speak.

Contour computes its findings. A model never decides what is true here — it
narrates, ranks, or drafts a specification that a deterministic executor then
runs. Keeping those five jobs in one package is the point: it should be
possible to read everything a model is asked to do in this system by listing
one directory.

The agents, in order of how much they are trusted:

    summary   narrates findings that were already computed
    brief     reads those findings against corroborating sources
    roster    ranks a shortlist that code has already filtered
    checks    drafts check specifications in a constrained language
    entities  proposes a company's name in a source, for a person to confirm

This module holds what they share: the model to call, how to reach it, and how
to fail. Every agent fails the same way — a reason, never an exception — since
the report has to stand whether or not a model was available.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

MODEL = "claude-opus-5"


@dataclass(frozen=True)
class Refusal:
    """Why an agent produced nothing. Never raised, always reportable."""

    reason: str


def credentials() -> str | None:
    """The reason there are none, or None when there are."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return None
    return ("no Anthropic credentials found. Set ANTHROPIC_API_KEY, or run "
            "`ant auth login`. Every finding is computed without a model and "
            "stands on its own; this only adds to it.")


def call(system: str, user: str, *, model: str = MODEL, max_tokens: int = 1600,
         thinking: bool = True) -> tuple[str, str | None]:
    """(text, reason). Exactly one of the two is meaningful.

    Every failure a caller can hit — missing package, missing key, rate limit,
    refusal, empty response — comes back as a reason string rather than an
    exception, because none of them is a reason to lose a report.
    """
    missing = credentials()
    if missing:
        return "", missing
    try:
        import anthropic
    except ImportError:
        return "", "the anthropic package is not installed — run: pip install anthropic"

    client = anthropic.Anthropic()
    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    if thinking:
        kwargs["thinking"] = {"type": "adaptive"}
    try:
        response = client.messages.create(**kwargs)
    except anthropic.NotFoundError as exc:
        return "", f"model not found: {exc}"
    except anthropic.RateLimitError:
        return "", "rate limited — try again shortly"
    except anthropic.APIStatusError as exc:
        return "", f"API error {exc.status_code}: {exc.message}"
    except anthropic.APIConnectionError:
        return "", "could not reach the Anthropic API"
    except Exception as exc:  # noqa: BLE001 — a page must render regardless
        return "", f"{type(exc).__name__}: {exc}"

    if response.stop_reason == "refusal":
        return "", "the request was declined by the model"
    text = "\n".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        return "", "the model returned no text"
    return text, None

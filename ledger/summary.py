"""Executive summary.

The one place a model belongs in this system: writing prose over findings that
were already computed. It is handed the findings as text and nothing else — no
filings, no API access, no ability to look anything up — so it cannot introduce a
number that was not derived from a filing first.

The output is labelled as model-written wherever it appears. A reader must never
have to wonder whether a figure on the page was computed or generated.

Without credentials the summary reports unavailable with a reason, exactly like
any other check. It is an addition to the report, never a dependency of it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ledger.report import Report, Status

MODEL = "claude-opus-5"
MAX_TOKENS = 1600

SYSTEM = """You write a short executive summary of an automated review of a public \
company's SEC filings.

You are given findings that were already computed from those filings. Your job is \
to make them readable, in that order of importance, for someone who has thirty \
seconds.

Rules, in order of importance:

1. Use only figures that appear in the findings you are given. Never introduce a \
number, date, ticker, or company fact from your own knowledge, and never estimate \
or round in a way that changes a value. If a figure you want is not present, write \
around it.
2. Never assert that a company did something wrong. The findings describe what was \
reported and how it changed. A qualified record is a qualified record, not a lie.
3. Say what could not be checked. Checks that could not run are listed, and a \
reader who does not know a gap exists will assume there was none. A check marked \
DOES NOT APPLY is not a gap — it is a finding about the company, and calling it a \
gap is an error. Do not list it among things you could not check.
4. Do not repeat every finding. Lead with what a reader would most want to know, \
group the rest, and drop the trivia.

Write 120-200 words as two or three short paragraphs. No headings, no bullet \
lists, no preamble like "Here is a summary". Plain declarative sentences."""


@dataclass(frozen=True)
class Summary:
    text: str
    model: str | None = None
    available: bool = True
    reason: str | None = None


def findings_brief(report: Report) -> str:
    """The findings as text — the only thing the model is given."""
    lines = [
        f"Company: {report.company} ({report.ticker}), CIK {report.cik}",
    ]
    if report.filing:
        lines.append(
            f"Most recent annual report: {report.filing.form} filed {report.filing.filed}"
        )
    lines.append("")

    for check in report.checks:
        if check.status == Status.UNAVAILABLE:
            lines.append(f"[{check.title}] COULD NOT RUN — {check.reason}")
            continue
        if check.status == Status.NOT_APPLICABLE:
            lines.append(f"[{check.title}] DOES NOT APPLY TO THIS COMPANY — {check.reason}")
            continue
        if check.status == Status.CLEAN:
            lines.append(f"[{check.title}] checked, nothing flagged")
            continue
        lines.append(f"[{check.title}] {len(check.items)} finding(s)")
        for item in check.items:
            lines.append(f"  - ({item.severity}) {item.headline}")
            if item.quote:
                lines.append(f'    quoted: "{item.quote[:220]}" — {item.attribution}')
            if item.detail:
                lines.append(f"    {item.detail[:260]}")
            if item.evidence:
                figures = ", ".join(f"{k}={v}" for k, v in item.evidence.items())
                lines.append(f"    figures: {figures}")
        for note in check.notes:
            lines.append(f"  note: {note}")
    return "\n".join(lines)


def executive_summary(report: Report, *, model: str = MODEL) -> Summary:
    """Narrate the findings. Never a dependency — a failure here is reported,
    not raised, and the report stands without it."""
    try:
        import anthropic
    except ImportError:
        return Summary(
            "", available=False,
            reason="the anthropic package is not installed — run: pip install anthropic",
        )

    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        return Summary(
            "", available=False,
            reason="no Anthropic credentials found. Set ANTHROPIC_API_KEY, or run "
                   "`ant auth login`. Every finding in this report was computed "
                   "without a model and stands on its own; this only adds narration.",
        )

    if not report.findings and not report.unavailable:
        return Summary(
            "", available=False,
            reason="nothing to summarise — no findings and nothing withheld",
        )

    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": findings_brief(report)}],
        )
    except anthropic.NotFoundError as exc:            # bad model id
        return Summary("", available=False, reason=f"model not found: {exc}")
    except anthropic.RateLimitError:
        return Summary("", available=False, reason="rate limited — try again shortly")
    except anthropic.APIStatusError as exc:
        return Summary("", available=False, reason=f"API error {exc.status_code}: {exc.message}")
    except anthropic.APIConnectionError:
        return Summary("", available=False, reason="could not reach the Anthropic API")

    if response.stop_reason == "refusal":
        return Summary("", available=False, reason="the request was declined by the model")

    text = "\n".join(
        block.text for block in response.content if block.type == "text"
    ).strip()
    if not text:
        return Summary("", available=False, reason="the model returned no text")
    return Summary(text, model=model)

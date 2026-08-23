"""Report renderers.

Both renderers show CLEAN and UNAVAILABLE differently on purpose. A check that
returned nothing and a check that could not run must never look the same.
"""

from __future__ import annotations

import html as _html
import re

from ledger.charts import composition_svg
from ledger.report import Check, Item, Report, Status, figure_label
from ledger.sources import clip

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


def _sorted_items(check: Check) -> list[Item]:
    return sorted(check.items, key=lambda i: _SEVERITY_ORDER.get(i.severity, 9))


# -- terminal --------------------------------------------------------------

_BAR = "=" * 78


def render_terminal(report: Report) -> str:
    out: list[str] = [
        "", _BAR,
        f"{report.company} ({report.ticker})   CIK {report.cik}",
        f"scanned {report.generated.isoformat()}"
        + (f"   ·   latest 10-K {report.filing.filed}" if report.filing else ""),
        _BAR,
    ]

    for check in report.roster:
        out.append("")
        if check.authored_by:
            out.append(f"   rule written by {check.authored_by} — {check.rationale}")
        if check.status == Status.UNAVAILABLE:
            out.append(f"-- {check.title}: UNAVAILABLE")
            out.append(f"   {check.reason}")
            continue
        out.append(f"-- {check.title}")
        for note in check.notes:
            out.append(f"   ({note})")
        if check.status == Status.CLEAN:
            out.append("   checked, nothing flagged")
            continue
        for item in _sorted_items(check):
            out.append("")
            out.append(f"   [{item.severity.upper():<6}] {item.headline}")
            if item.quote:
                out.append(f'     "{clip(item.quote, 150)}"')
                out.append(f"       — {item.attribution}")
            if item.detail:
                out.append(f"     {item.detail[:220]}")
            for key, value in item.evidence.items():
                out.append(f"       {figure_label(key)}: {value}")
            if item.source:
                mark = "VERIFIED" if item.source.verified else "REPORTED"
                out.append(f"     {mark} · {item.source.cite()}")

    out.append("")
    out.append(_BAR)
    out.append(f"{report.findings} findings   ·   {len(report.unavailable)} checks "
               f"unavailable   ·   {len(report.not_applicable)} not applicable")
    out.append(_BAR)
    return "\n".join(out)


# -- html ------------------------------------------------------------------

CSS = """
:root{--paper:#F5F6F8;--surface:#fff;--surface-2:#EAEDF2;--ink:#141A22;--ink-2:#454F5E;
--ink-3:#6E7887;--rule:#D9DDE4;--rule-strong:#BAC1CC;--accent:#2E4A8F;--accent-soft:#E2E8F5;
--high:#A32C2C;--high-soft:#F7E5E4;--med:#8A6110;--med-soft:#F7EFDC;--pass:#2C6B4F;
--pass-soft:#E1EFE8;--sans:"Archivo",Helvetica,Arial,sans-serif;
--serif:"Source Serif 4",Georgia,serif;--mono:"IBM Plex Mono",ui-monospace,Menlo,monospace}
@media (prefers-color-scheme:dark){.chart{--pos:#3987e5;--neg:#e66767;
--grid:#242C36;--zero:#3B444F}
:root{--paper:#0E1218;--surface:#151B23;--surface-2:#1D242E;
--ink:#E8EBF0;--ink-2:#A9B3C1;--ink-3:#798393;--rule:#2A323D;--rule-strong:#3B444F;
--accent:#93AAE2;--accent-soft:#1B2740;--high:#E58A85;--high-soft:#331F1E;--med:#D9AE5F;
--med-soft:#2F2716;--pass:#7FC5A2;--pass-soft:#16291F}}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--serif);
font-size:1rem;line-height:1.55;-webkit-font-smoothing:antialiased}
.page{max-width:58rem;margin:0 auto;padding:2rem 1.5rem 3.5rem;display:flex;
flex-direction:column;gap:2.75rem}
h1{font-family:var(--sans);font-size:2.5rem;font-weight:700;letter-spacing:-.02em;
line-height:1.05;margin:0}
h2{font-family:var(--sans);font-size:1.5rem;font-weight:600;letter-spacing:-.015em;margin:0}
.eyebrow{font-family:var(--mono);font-size:.6875rem;letter-spacing:.13em;
text-transform:uppercase;color:var(--ink-3)}
.factbar{display:flex;flex-wrap:wrap;gap:0 2rem;padding-top:1rem;border-top:2px solid var(--ink)}
.fact{display:flex;flex-direction:column;gap:.15rem;padding:.35rem 0}
.fact dt{font-family:var(--mono);font-size:.75rem;letter-spacing:.1em;
text-transform:uppercase;color:var(--ink-3)}
.fact dd{margin:0;font-family:var(--mono);font-size:.875rem;font-variant-numeric:tabular-nums}
.check{display:flex;flex-direction:column;gap:.6rem}
.check-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:.75rem;
padding-bottom:.5rem;border-bottom:1px solid var(--rule-strong)}
.status{font-family:var(--mono);font-size:.625rem;font-weight:600;letter-spacing:.11em;
text-transform:uppercase;padding:.2rem .5rem}
.status.ok{background:var(--accent-soft);color:var(--accent)}
.status.clean{background:var(--pass-soft);color:var(--pass)}
/* Amber, not grey. Grey reads as "disabled"; this is a position, not a fault. */
.status.unavailable{background:var(--med-soft);color:var(--med)}
.status.not_applicable{background:var(--surface-2);color:var(--ink-3)}
.status.authored{background:var(--accent-soft,var(--surface-2));color:var(--accent,var(--ink-2));
  border:1px solid var(--accent,var(--rule-strong));letter-spacing:.06em;font-size:.625rem}
.authored-note{font-size:.8125rem;line-height:1.5;color:var(--ink-2);
  border-left:2px dashed var(--rule-strong);padding:.5rem .9rem;margin:.3rem 0 .5rem;
  background:var(--surface-2);border-radius:0 3px 3px 0}
.authored-note b{color:var(--ink-1)}
.authored-by{font-family:var(--mono);font-size:.6875rem;color:var(--ink-3)}
.jumps a.authored-jump{border-style:dashed}
.reason{font-size:.9375rem;color:var(--ink-2);margin:0}
.note{font-family:var(--mono);font-size:.75rem;color:var(--ink-3);margin:0}
.item{background:var(--surface);border:1px solid var(--rule);
border-left:6px solid var(--rule-strong);padding:.75rem 1rem;display:flex;
flex-direction:column;gap:.6rem}
.item.high{border-left-color:var(--high)}.item.medium{border-left-color:var(--med)}
.item.low{border-left-color:var(--pass)}.item.info{border-left-color:var(--accent)}
.item h3{font-family:var(--sans);font-size:1rem;font-weight:600;margin:0;line-height:1.3}
.item p{margin:0;font-size:.9375rem;color:var(--ink-2)}
blockquote{margin:0;padding-left:.9rem;border-left:2px solid var(--rule-strong);
font-size:1rem;color:var(--ink)}
blockquote cite{display:block;font-style:normal;font-family:var(--mono);
font-size:.6875rem;color:var(--ink-3);padding-top:.3rem}
.evidence{font-family:var(--mono);font-size:.75rem;font-variant-numeric:tabular-nums;
background:var(--surface-2);padding:.55rem .7rem;color:var(--ink-2);overflow-x:auto}
.evidence span{display:block;white-space:nowrap}
.cite{font-family:var(--mono);font-size:.6875rem;color:var(--ink-3)}
/* VERIFIED and REPORTED are the citation argument. They were the same size,
   weight and colour as the text beside them, so nobody found the distinction
   without being told. */
.cite .mark{display:inline-block;font-weight:600;letter-spacing:.08em;
padding:.12rem .4rem;margin-right:.5rem}
.cite .mark.verified{background:var(--pass-soft);color:var(--pass)}
.cite .mark.reported{background:var(--surface-2);color:var(--ink-2)}
.cite .when{white-space:nowrap}
/* Diverging pair validated against this app's surfaces in both modes. */
/* A 760-unit viewBox stretched to a 1400px column scales every label with it —
   12px type renders at 22px and fights the page. Cap at natural size; narrower
   viewports still scale it down. */
.chart{--pos:#2a78d6;--neg:#e34948;--grid:#E4E7EC;--zero:#BAC1CC;
width:100%;max-width:760px;height:auto;overflow:visible;display:block;
margin:.5rem 0 .3rem}
.chart-bar{transition:opacity 120ms ease}
.chart-bar:hover{opacity:.78}
.chart-pos{fill:var(--pos)}.chart-neg{fill:var(--neg)}
.chart-grid{stroke:var(--grid);stroke-width:1}
.chart-zero{stroke:var(--zero);stroke-width:1.5}
.chart-label{font-family:var(--mono);font-size:12px;fill:var(--ink-2)}
.chart-value{font-family:var(--mono);font-size:12px;font-weight:600;fill:var(--ink);
font-variant-numeric:tabular-nums}
.chart-inside{fill:#fff}
.chart-axis{font-family:var(--mono);font-size:10.5px;fill:var(--ink-3);
font-variant-numeric:tabular-nums}
.chart-note{margin:0;font-family:var(--mono);font-size:.6875rem;color:var(--ink-3)}
.brief{border:1px solid var(--rule);border-left:3px solid var(--accent);
background:var(--surface);padding:1.1rem 1.3rem;display:flex;flex-direction:column;gap:.7rem}
.brief h2{font-size:1.25rem}
.brief .who{font-family:var(--mono);font-size:.6875rem;letter-spacing:.06em;
text-transform:uppercase;color:var(--ink-3)}
.brief p{margin:0;font-size:1rem;line-height:1.6;color:var(--ink)}
.brief .off{font-size:.9375rem;color:var(--ink-2)}
.visuals{display:flex;flex-direction:column;gap:.7rem;padding-bottom:.4rem;
border-bottom:1px solid var(--rule)}
.visuals h2{font-size:1.25rem}
.jumps{display:flex;flex-wrap:wrap;gap:.4rem}
/* The whole chip carries the state, not a 7px dot. This strip is the product's
   thesis in one row — found / clean / could-not-run — and it has to survive
   being read from the back of a room. */
.jumps a{display:inline-flex;align-items:center;gap:.45rem;font-family:var(--mono);
font-size:.75rem;text-decoration:none;padding:.38rem .65rem;border:1px solid var(--rule);
background:var(--surface);color:var(--ink-2)}
.jumps a.ok{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}
.jumps a.clean{border-color:var(--pass);color:var(--pass);background:var(--pass-soft)}
.jumps a.unavailable{border-color:var(--med);color:var(--med);background:var(--med-soft)}
.check.quiet{margin:0;border-bottom:1px solid var(--rule)}
.check.quiet.flat>.check-head{padding:.55rem 0;border-bottom:0;margin:0}
.check.quiet.flat h2{font-size:.9375rem;font-weight:500;color:var(--ink-2)}
.check.quiet>summary{cursor:pointer;list-style:none;padding:.55rem 0;
  border-bottom:0;margin:0}
.check.quiet>summary::-webkit-details-marker{display:none}
.check.quiet>summary h2{font-size:.9375rem;font-weight:500;color:var(--ink-2)}
.check.quiet>summary::after{content:"›";margin-left:auto;color:var(--ink-3);
  font-size:1rem;line-height:1;transition:transform .12s ease}
.check.quiet[open]>summary::after{transform:rotate(90deg)}
.check.quiet>summary:hover h2{color:var(--ink)}
.quiet-body{padding:0 0 .9rem}
.jumps a.na{border-color:var(--rule);color:var(--ink-3);background:transparent;opacity:.72}
.roster{font-size:.75rem;font-family:var(--mono);color:var(--ink-3);margin:1rem 0}
.roster summary{cursor:pointer;list-style:none;width:fit-content}
.roster summary::-webkit-details-marker{display:none}
.roster summary::before{content:"› ";display:inline-block;transition:transform .12s ease}
.roster[open] summary::before{transform:rotate(90deg)}
.roster summary:hover{color:var(--ink-2)}
.roster ul{margin:.5rem 0 0;padding-left:1.1rem;font-family:var(--sans);
  font-size:.8125rem;color:var(--ink-3);line-height:1.6}
.roster b{color:var(--ink-2);font-weight:600}
.jumps a:hover{filter:brightness(1.15)}
.jumps a b{font-weight:700}
.check{scroll-margin-top:1rem}
.cite b{color:var(--pass)}
.sr{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;
  clip:rect(0 0 0 0);white-space:nowrap;border:0}
.cite a.out span[aria-hidden]{opacity:.75;font-size:.9em}
.passage{margin:.5rem 0 0}
.passage summary{font-family:var(--mono);font-size:.6875rem;color:var(--ink-3);
  cursor:pointer;padding:.15rem 0;list-style:none;width:fit-content}
.passage summary::-webkit-details-marker{display:none}
.passage summary::before{content:"› ";display:inline-block;
  transition:transform .12s ease}
.passage[open] summary::before{transform:rotate(90deg)}
.passage summary:hover{color:var(--ink-2)}
.passage-body{margin-top:.45rem;padding:.7rem .9rem;background:var(--surface-2);
  border-left:2px solid var(--rule-strong);border-radius:0 3px 3px 0;
  font-size:.8125rem;line-height:1.6;color:var(--ink-2)}
.passage-body p{margin:0 0 .6rem}
.passage-body p:last-child{margin-bottom:0}
.passage-mark{font-family:var(--mono);font-size:.6875rem;color:var(--ink-3);
  letter-spacing:.03em;margin-top:.9rem!important}
.passage-body>.passage-mark:first-child{margin-top:0!important}
.passage-cut{font-family:var(--mono);font-size:.6875rem;color:var(--ink-3);
  font-style:italic}
footer{border-top:1px solid var(--rule);padding-top:1.2rem;font-family:var(--mono);
font-size:.75rem;color:var(--ink-3)}
a{color:var(--accent)}
"""

FONTS = (
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    "family=Archivo:wght@600;700&family=IBM+Plex+Mono:wght@400;600&"
    'family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&display=swap">'
)


def esc(text: str) -> str:
    return _html.escape(str(text))


# A citation says where to look. This says what it says. Filing prose is where a
# reader's judgement actually happens, and an accession number alone makes them
# go fetch it — so carry the paragraph and let them read it in place.
PASSAGE_CHARS = 2200
_AS_FILED = re.compile(r"^(As filed FY\d{4}:)$")


def _clip_passage(text: str, limit: int = PASSAGE_CHARS) -> tuple[str, bool]:
    """Cut at a sentence end, so a fold never invents a broken sentence."""
    text = text.strip()
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    stop = max(cut.rfind(". "), cut.rfind(".\n"))
    return (cut[: stop + 1] if stop > limit // 2 else cut.rstrip()), True


# "as filed" is right for a filing and wrong for a recall notice.
PASSAGE_LABELS = {
    "NHTSA": "Read the stated consequence",
    "SEC EDGAR": "Read the passage as filed",
}


def passage_label(origin: str) -> str:
    return PASSAGE_LABELS.get(origin, "Read the source text")


def passage_html(passage: str, shown: str = "", origin: str = "SEC EDGAR") -> str:
    """The source paragraph, folded away until asked for.

    Verbatim: this is quoted filing text, so it is escaped and never trimmed
    mid-sentence without saying so. `shown` is text already on screen above —
    a risk factor's heading is both the finding's own line and the passage's
    first line, and printing it twice reads as a rendering bug.
    """
    text = passage.strip()

    def strip_heading(chunk: str) -> str:
        head, _, rest = chunk.strip().partition("\n")
        if shown and rest and head.strip() == shown.strip():
            return rest.strip()
        return chunk.strip()

    # A before/after passage is two halves. Clipping the whole string would
    # drop the second one entirely and leave a reader believing the first half
    # was all there was, so each half gets its own budget.
    segments: list[tuple[str, str]] = []
    label = ""
    body: list[str] = []
    for line in text.split("\n"):
        if _AS_FILED.match(line.strip()):
            if label or body:
                segments.append((label, "\n".join(body)))
            label, body = line.strip(), []
        else:
            body.append(line)
    segments.append((label, "\n".join(body)))

    blocks: list[str] = []
    truncated = False
    for label, chunk in segments:
        if label:
            blocks.append(f'<p class="passage-mark">{esc(label)}</p>')
        clipped, cut = _clip_passage(strip_heading(chunk))
        truncated = truncated or cut
        for para in clipped.split("\n"):
            para = para.strip()
            if para:
                blocks.append(f"<p>{esc(para)}</p>")
    if truncated:
        blocks.append('<p class="passage-cut">Truncated — full text in '
                      'the filing linked above.</p>')
    return (f'<details class="passage"><summary>{esc(passage_label(origin))}</summary>'
            f'<div class="passage-body">{"".join(blocks)}</div></details>')


def item_html(item: Item) -> str:
    parts = [f'<div class="item {esc(item.severity)}">',
             f"<h3>{esc(item.headline)}</h3>"]
    if item.quote:
        parts.append(
            f"<blockquote>{esc(clip(item.quote, 400))}"
            f"<cite>{esc(item.attribution or '')}</cite></blockquote>"
        )
    if item.detail:
        parts.append(f"<p>{esc(item.detail)}</p>")
    if item.evidence:
        rows = "".join(
            f"<span>{esc(figure_label(k))}: <b>{esc(v)}</b></span>"
            for k, v in item.evidence.items()
        )
        parts.append(f'<div class="evidence">{rows}</div>')
    if item.source:
        verified = item.source.verified
        mark = ('<span class="mark verified">VERIFIED</span>' if verified
                else '<span class="mark reported">REPORTED</span>')
        # The citation leaves the app — EDGAR, NHTSA, a declared source. Opening
        # it in place would throw away the report the reader is checking against,
        # which is the one thing they came here to hold on to.
        link = (f' · <a class="out" href="{esc(item.source.href)}" target="_blank" '
                f'rel="noopener noreferrer">source<span aria-hidden="true"> \u2197</span>'
                f'<span class="sr">opens in a new tab</span></a>'
                if item.source.href else "")
        cite = esc(item.source.cite()).replace(
            esc(item.source.document_date.isoformat()),
            f'<span class="when">{esc(item.source.document_date.isoformat())}</span>',
        )
        parts.append(f'<p class="cite">{mark}{cite}{link}</p>')
    if item.source and item.source.passage:
        parts.append(passage_html(item.source.passage, shown=item.detail or "",
                                    origin=item.source.origin))
    parts.append("</div>")
    return "".join(parts)


def anchor(check: Check, prefix: str = "") -> str:
    """Stable per-company id — two columns must not collide on the same anchor."""
    return f"{prefix}{check.key}".replace(" ", "-").lower()


def nav_html(report: Report, prefix: str = "") -> str:
    """Jump links to every check, carrying its outcome so the shape of the scan
    is readable before scrolling."""
    if not report.roster:
        return ""
    mark = {Status.OK: "ok", Status.CLEAN: "clean", Status.UNAVAILABLE: "unavailable",
            Status.NOT_APPLICABLE: "na"}
    links = "".join(
        f'<a class="jump {mark[c.status]}" href="#{anchor(c, prefix)}">'
        f'{esc(c.title)}<b>{len(c.items) if c.status == Status.OK else ""}</b></a>'
        for c in report.roster
    )
    return f'<nav class="jumps" aria-label="Checks">{links}</nav>'


def withheld_html(report: Report) -> str:
    """Checks that could not run, stated before any finding rather than after.

    A check that does not apply is listed separately and quietly. Both are
    honest, but only one is a gap in what the reader knows, and giving them the
    same alarm teaches a reader to ignore the alarm.
    """
    blocks = []
    if report.unavailable:
        items = "".join(
            f"<li><b>{esc(c.title)}</b> — {esc(c.reason or 'no reason recorded')}</li>"
            for c in report.unavailable
        )
        count = len(report.unavailable)
        blocks.append(f'<div class="withheld"><h2>{count} check'
                      f'{"s" if count != 1 else ""} could not run</h2>'
                      f"<ul>{items}</ul></div>")
    return "".join(blocks)


def roster_html(report: Report) -> str:
    """One line accounting for the roster, with the excluded checks behind it.

    Dropping a check silently would leave a reader unable to tell "considered
    and out of scope" from "never looked at". This is the difference, in one
    row instead of a banner per check.
    """
    excluded = report.not_applicable
    line = (f"{len(report.roster)} check{'s' if len(report.roster) != 1 else ''} "
            f"selected for {esc(report.ticker)}")
    if not excluded:
        return f'<p class="roster">{line}</p>'
    rows = "".join(
        f"<li><b>{esc(c.title)}</b> — {esc(c.reason or 'out of scope')}</li>"
        for c in excluded
    )
    return (f'<details class="roster"><summary>{line} · '
            f"{len(excluded)} not applicable</summary>"
            f"<ul>{rows}</ul></details>")


def summary_html(summary) -> str:
    """The one generated element on the page, labelled as such.

    A reader must never wonder whether a figure was computed or written. The
    findings below it are arithmetic; this paragraph is narration over them.
    """
    if summary is None:
        return ""
    if not summary.available:
        # Nothing to say. The findings below are complete without narration, so
        # an absent narrator prints nothing rather than a status line on every
        # page announcing a feature the reader did not ask for.
        return ""
    paragraphs = "".join(
        f"<p>{esc(p.strip())}</p>" for p in summary.text.split("\n") if p.strip()
    )
    return (
        f'<section class="brief"><h2>Executive summary</h2>'
        f'<span class="who">Written by {esc(summary.model or "a language model")} '
        f"from the computed findings below</span>"
        f"{paragraphs}</section>"
    )


def visuals_html(report: Report) -> str:
    """Every chart the report can draw, before the prose."""
    charts = [composition_svg(c.attributions) for c in report.roster if c.attributions]
    charts = [c for c in charts if c]
    if not charts:
        return ""
    return ('<section class="visuals"><h2>At a glance</h2>'
            '<p class="reason">Change in each revenue component vs the prior period.</p>'
            + "".join(charts) + "</section>")


def check_html(check: Check, prefix: str = "", with_chart: bool = True) -> str:
    """A check with findings renders open. One without folds to a single row.

    Deliberately a fold and not a tab: the status and the count stay on screen
    either way, so a reader never has to click to discover that something
    exists. Only the empty sections give back their vertical space.
    """
    untestable = next(
        (n for n in check.notes if "not testable" in n or "not plotted" in n), ""
    )
    extra = ""
    if (match := re.match(r"(\d+) of (\d+) claims not testable", untestable)):
        extra = (f'<span class="status unavailable">{match.group(1)} of '
                 f'{match.group(2)} not testable</span>')
    label = {
        Status.OK: f"{len(check.items)} found",
        Status.CLEAN: "nothing flagged",
        Status.UNAVAILABLE: "unavailable",
        Status.NOT_APPLICABLE: "does not apply",
    }[check.status]
    authored_badge = (
        '<span class="status authored" title="The rule was written by a model. '
        'The figures below are computed from the filing and cite it.">'
        'MODEL-WRITTEN RULE</span>'
    ) if check.authored_by else ""

    head = (f"<h2>{esc(check.title)}</h2>"
            f'<span class="status {esc(check.status)}">{esc(label)}</span>'
            f"{extra}{authored_badge}")

    body = []
    if check.authored_by:
        body.append(
            f'<p class="authored-note">{esc(check.rationale)}'
            f'<br><span class="authored-by">{esc(check.authored_by)}</span></p>'
        )
    if check.status in (Status.UNAVAILABLE, Status.NOT_APPLICABLE):
        body.append(f'<p class="reason">{esc(check.reason or "no reason recorded")}</p>')
    else:
        for note in check.notes:
            if note == untestable:      # already stated in the header badge
                continue
            body.append(f'<p class="note">{esc(note)}</p>')
        if check.status == Status.CLEAN:
            body.append('<p class="reason">No findings.</p>')
        if with_chart and check.attributions:
            body.append(composition_svg(check.attributions))
        for item in _sorted_items(check):
            body.append(item_html(item))

    if check.items:
        return (f'<section class="check" id="{anchor(check, prefix)}">'
                f'<div class="check-head">{head}</div>'
                f'{"".join(body)}</section>')

    # Nothing to show until asked for. The heading row still carries the title,
    # the status and any badge, so the fold hides no information.
    if not any(b.strip() for b in body):
        # A disclosure that reveals nothing is worse than no disclosure: it
        # invites a click and answers with an empty box.
        return (f'<div class="check quiet flat" id="{anchor(check, prefix)}">'
                f'<div class="check-head">{head}</div></div>')
    return (f'<details class="check quiet" id="{anchor(check, prefix)}">'
            f'<summary class="check-head">{head}</summary>'
            f'<div class="quiet-body">{"".join(body)}</div></details>')


def render_html(report: Report) -> str:
    checks = "".join(check_html(c, with_chart=False) for c in report.roster)
    lead = (nav_html(report) + withheld_html(report) + roster_html(report)
            + visuals_html(report))
    filed = report.filing.filed.isoformat() if report.filing else "none on file"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(report.company)} — Contour</title>
{FONTS}<style>{CSS}</style></head><body><div class="page">
<header>
<div class="eyebrow">Contour · primary-source scan</div>
<h1>{esc(report.company)}</h1>
<dl class="factbar">
<div class="fact"><dt>Ticker</dt><dd>{esc(report.ticker)}</dd></div>
<div class="fact"><dt>CIK</dt><dd>{report.cik}</dd></div>
<div class="fact"><dt>Latest 10-K</dt><dd>{esc(filed)}</dd></div>
<div class="fact"><dt>Findings</dt><dd>{report.findings}</dd></div>
<div class="fact"><dt>Unavailable</dt><dd>{len(report.unavailable)} checks</dd></div>
<div class="fact"><dt>Scanned</dt><dd>{report.generated.isoformat()}</dd></div>
</dl></header>
{lead}
{checks}
<footer>Every figure is computed from the filing named in its citation. No model
decides what is true. Checks that could not run say so rather than reporting clean.</footer>
</div></body></html>"""

"""Inline SVG for scan reports.

Coordinates are computed from the attribution data, never hand-written, so a
mark cannot drift out of alignment with the number it encodes. No JavaScript and
no chart library — the page must open from a file with no network.

Colours are the two poles of a diverging pair validated against this app's own
surfaces (white and #151B23): worst-pair normal-vision ΔE 32.3 light, 29.0 dark,
both clear of the 15 floor, and both above 3:1 contrast on their surface.
"""

from __future__ import annotations

import html

# Beyond this the bars are too thin to read and the labels collide. Disney files
# 28 revenue components; the rest are reported as a count, never dropped silently.
MAX_BARS = 8

BAR_H = 24
GAP = 9
PAD_L = 196
PAD_R = 62
WIDTH = 760
RADIUS = 4
LABEL_CHARS = 30
GLYPH = 7.0          # 12px IBM Plex Mono advance, for collision arithmetic


def _esc(text: str) -> str:
    return html.escape(str(text))


def _money(value: float) -> str:
    for scale, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= scale:
            return f"{value / scale:+,.2f}{suffix}"
    return f"{value:+,.0f}"


def _nice(value: float) -> float:
    """Round a tick up to 1, 2 or 5 times a power of ten, so the axis reads
    -8B rather than -7.59B."""
    if value <= 0:
        return 0.0
    import math

    power = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5, 10):
        if value <= step * power:
            return step * power
    return 10 * power


def _shorten(name: str, limit: int = LABEL_CHARS) -> str:
    """Keep the last word when trimming.

    "Energy generation and storage sales" and "…storage leasing" both cut to the
    same string at 30 characters, which puts two identical labels on the chart.
    """
    if len(name) <= limit:
        return name
    tail = name.rsplit(" ", 1)[-1]
    head_room = limit - len(tail) - 2
    if head_room < 6:
        return name[: limit - 1] + "…"
    # Cut the head at a space too, or the ellipsis lands mid-word:
    # "Energy generation and s…sales".
    head = name[:head_room].rsplit(" ", 1)[0] or name[:head_room]
    return f"{head.rstrip()}…{tail}"


def _bar_path(x: float, y: float, w: float, negative: bool) -> str:
    """Rounded on the data end only, square against the zero line."""
    r = min(RADIUS, max(w, 0.1))
    if negative:
        return (f"M{x + r:.1f},{y} h{w - r:.1f} v{BAR_H} h-{w - r:.1f} "
                f"a{r},{r} 0 0 1 -{r},-{r} v-{BAR_H - 2 * r} a{r},{r} 0 0 1 {r},-{r} z")
    return (f"M{x:.1f},{y} h{w - r:.1f} a{r},{r} 0 0 1 {r},{r} v{BAR_H - 2 * r} "
            f"a{r},{r} 0 0 1 -{r},{r} h-{w - r:.1f} z")


def composition_svg(attributions: list) -> str:
    """Diverging bars: the change in each revenue component, largest first.

    `attributions` are ledger.composition.Attribution records.
    """
    rows = sorted(attributions, key=lambda a: abs(a.change), reverse=True)
    hidden = max(0, len(rows) - MAX_BARS)
    rows = rows[:MAX_BARS]
    if len(rows) < 2:
        return ""

    raw_limit = max(abs(a.change) for a in rows)
    if not raw_limit:
        return ""
    limit = _nice(raw_limit * 1.14)

    height = len(rows) * (BAR_H + GAP) + 34
    plot_w = WIDTH - PAD_L - PAD_R
    zero_x = PAD_L + plot_w / 2
    total_change = sum(a.change for a in attributions)

    parts = [
        f'<svg class="chart" viewBox="0 0 {WIDTH} {height}" role="img" '
        f'aria-label="Change in each revenue component versus the prior period, '
        f'in dollars">'
    ]

    # Axis ticks at quarters of the range, rounded to something sayable.
    step = limit / 2
    for mult in (-2, -1, 0, 1, 2):
        value = step * mult
        x = zero_x + (value / limit) * (plot_w / 2)
        parts.append(f'<line class="chart-grid" x1="{x:.1f}" y1="6" '
                     f'x2="{x:.1f}" y2="{height - 26}"/>')
        # Axis ticks are round by construction, so drop the empty decimals.
        label = "0" if not mult else _money(value).replace("+", "").replace(".00", "")
        parts.append(f'<text class="chart-axis" x="{x:.1f}" y="{height - 9}" '
                     f'text-anchor="middle">{_esc(label)}</text>')

    for i, a in enumerate(rows):
        y = 6 + i * (BAR_H + GAP)
        w = abs(a.change) / limit * (plot_w / 2)
        negative = a.change < 0
        x = zero_x - w if negative else zero_x
        tone = "neg" if negative else "pos"
        share = f"{a.change / total_change:.0%}" if total_change else "n/a"

        parts.append(
            f'<path class="chart-bar chart-{tone}" d="{_bar_path(x, y, w, negative)}">'
            f'<title>{_esc(a.member)}: {_money(a.change)}, '
            f'{a.base_share:.1%} of prior-period revenue, {share} of the total change'
            f'</title></path>'
        )

        name = _shorten(a.member)
        parts.append(f'<text class="chart-label" x="{PAD_L - 12}" y="{y + BAR_H / 2 + 4}" '
                     f'text-anchor="end">{_esc(name)}</text>')

        text = _money(a.change)
        # The value is right-anchored on negative bars, so it extends leftward by
        # its own width. Measuring only the anchor lets it collide with the label
        # column on the longest bar.
        if negative and (x - 8 - len(text) * GLYPH) < (PAD_L - 4):
            parts.append(f'<text class="chart-value chart-inside" x="{x + 10:.1f}" '
                         f'y="{y + BAR_H / 2 + 4}" text-anchor="start">{_esc(text)}</text>')
        else:
            vx = x - 8 if negative else x + w + 8
            anchor = "end" if negative else "start"
            parts.append(f'<text class="chart-value" x="{vx:.1f}" y="{y + BAR_H / 2 + 4}" '
                         f'text-anchor="{anchor}">{_esc(text)}</text>')

    parts.append(f'<line class="chart-zero" x1="{zero_x:.1f}" y1="2" '
                 f'x2="{zero_x:.1f}" y2="{height - 26}"/>')
    parts.append("</svg>")

    if hidden:
        parts.append(f'<p class="chart-note">{hidden} smaller component'
                     f'{"s" if hidden != 1 else ""} not plotted</p>')
    return "".join(parts)


def sparkline_svg(closes: dict, *, days: int = 180, width: int = 132,
                  height: int = 30) -> tuple[str, float | None]:
    """(svg, change_pct) for the last `days` of closes, or ("", None).

    Market data, not filings — the caller is responsible for saying so. A price
    line beside computed findings is context for how the market read a company,
    never evidence of anything the company reported.
    """
    if not closes:
        return "", None
    from datetime import date, timedelta

    cutoff = date.today() - timedelta(days=days)
    points = [(d, v) for d, v in sorted(closes.items()) if d >= cutoff]
    if len(points) < 8:
        return "", None
    values = [v for _, v in points]
    low, high = min(values), max(values)
    span = (high - low) or 1.0
    step = width / (len(values) - 1)
    coords = [
        (i * step, height - 1 - ((v - low) / span) * (height - 2))
        for i, v in enumerate(values)
    ]
    path = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in coords)
    change = ((values[-1] - values[0]) / values[0] * 100) if values[0] else None
    tone = "up" if (change or 0) >= 0 else "down"
    area = (f"{path} L{coords[-1][0]:.1f} {height} L0 {height} Z")
    return (
        f'<svg class="spark {tone}" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" aria-hidden="true" '
        f'preserveAspectRatio="none">'
        f'<path class="spark-area" d="{area}"/>'
        f'<path class="spark-line" d="{path}"/>'
        f'<circle class="spark-end" cx="{coords[-1][0]:.1f}" cy="{coords[-1][1]:.1f}" r="2"/>'
        f"</svg>",
        change,
    )

"""Local web frontend.

Standard library only — no Flask, no build step, nothing to install on a laptop
at 11pm. The renderer already turns a Report into HTML, so the server is thin:
take a ticker, run the scan, return the page.

Two things this does that the CLI cannot: scan two companies side by side, and
put the checks that could not run at the top of the page instead of the bottom.
"""

from __future__ import annotations

from datetime import date
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from ledger.api import delta_dict, dumps, index_dict, report_dict, sources_dict, tracked_dict
from ledger.config import (
    add_preset,
    load_entities,
    load_presets,
    remove_preset,
    set_entities,
)
from ledger.edgar import EdgarClient, EdgarError
from ledger.render import (
    CSS,
    FONTS,
    _sorted_items,
    anchor,
    check_html,
    item_html,
    esc,
    nav_html,
    summary_html,
    visuals_html,
    withheld_html,
    roster_html,
)
from ledger.report import Report, Status, scan
from ledger.sources import CustomSource, SourceError, append_source, load_sources
from ledger.summary import executive_summary
from ledger.store import (
    connect,
    coverage,
    latest_delta,
    scan_history,
    track_company,
    tracked_companies,
    tracking,
    untrack_company,
)



# Daily-pass state, visible on the tracked page so nobody has to guess whether
# it is running.
DAILY: dict[str, object] = {"on": False, "last": None}

DAILY_INTERVAL_SECONDS = 24 * 60 * 60


def _daily_loop(client: EdgarClient, interval: int = DAILY_INTERVAL_SECONDS) -> None:
    """Rescan tracked companies once a day, in the background.

    Deliberately dumb: sleep, scan, repeat. It holds no schedule of its own, so
    restarting the server restarts the clock — which is the honest behaviour for
    something that lives only as long as the process does.
    """
    import datetime
    import time as _time

    while True:
        _time.sleep(interval)
        try:
            rescan_tracked(client)
            DAILY["last"] = datetime.date.today().isoformat()
        except Exception:  # noqa: BLE001 — a failed pass must not kill the thread
            continue


_EXTRA_CSS = """
.wide{display:block}
body:has(.wide) .shell,body:has(.cmp) .shell{max-width:88rem}
/* A three-pane report should fill its pane, not sit in a centred 72rem box
   with the reclaimed space wasted either side of it. The rails hug the edges;
   only the reading column keeps a measure, centred inside its own track. */
body:has(.report) .shell{max-width:none;padding-left:1.75rem;padding-right:1.75rem}
body:has(.report) .report-main{max-width:46rem;margin:0 auto;width:100%}
/* --- comparison: aligned by check, not two stacks --- */
.cmp{display:flex;flex-direction:column}
.cmp-band{display:grid;grid-template-columns:11rem minmax(0,1fr) minmax(0,1fr);
  gap:0 1.5rem;position:sticky;top:0;z-index:5;background:var(--paper);
  padding:.6rem 0 .75rem;border-bottom:2px solid var(--ink)}
.cmp-co{display:flex;flex-direction:column;gap:.1rem}
.cmp-co h1{font-size:1.125rem;font-weight:700;letter-spacing:-.01em}
.cmp-co .meta{font-family:var(--mono);font-size:.6875rem;color:var(--ink-3)}
.cmp-verdict{font-family:var(--mono);font-size:.75rem;color:var(--pass);padding-top:.15rem}
.cmp-verdict.warn{color:var(--med)}
.cmp-row{display:grid;grid-template-columns:11rem minmax(0,1fr) minmax(0,1fr);
  gap:0 1.5rem;padding:.9rem 0;border-bottom:1px solid var(--rule);align-items:start}
.cmp-key{font-family:var(--sans);font-size:.875rem;font-weight:600;padding-top:.2rem}
.cx{display:flex;flex-direction:column;gap:.5rem;min-width:0}
.cx .item{margin:0}
.cx.ok,.cx.gap{flex-direction:row;align-items:center;gap:.6rem;padding:.55rem .8rem;
  border-radius:3px;font-size:.8125rem;color:var(--ink-2);line-height:1.45}
.cx.ok{background:var(--pass-soft);border-left:3px solid var(--pass)}
.cx.gap{background:var(--med-soft);border-left:3px solid var(--med)}
.cx.none{padding:.55rem .8rem;border:1px dashed var(--rule);border-radius:3px;
  font-family:var(--mono);font-size:.75rem;color:var(--ink-3)}
.cx-more{font-family:var(--mono);font-size:.6875rem;color:var(--ink-3)}
.cmp-keep{display:grid;grid-template-columns:11rem minmax(0,1fr) minmax(0,1fr);
  gap:0 1.5rem;padding-top:1.25rem}
.cmp-keep .track{margin:0 0 .5rem}
@media (max-width:900px){
  .cmp-band,.cmp-row,.cmp-keep{grid-template-columns:1fr}
  .cmp-row{gap:.6rem}
}
.overview{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:0 40px;align-items:start}
@media (max-width:1100px){.overview{grid-template-columns:1fr}}
.ov-main{display:flex;flex-direction:column;gap:1.5rem;min-width:0}
.ov-side{display:flex;flex-direction:column;gap:1.5rem}
.facts{display:flex;flex-direction:column}
.facts .ix-label{padding-bottom:.4rem}
.fact{display:flex;justify-content:space-between;align-items:baseline;
  padding:.55rem 0;border-top:1px solid var(--rule)}
.fact:last-child{border-bottom:1px solid var(--rule)}
.fact span{font-family:var(--mono);font-size:.6875rem;text-transform:uppercase;
  letter-spacing:.06em;color:var(--ink-3)}
.fact b{font-family:var(--mono);font-size:1.0625rem;font-weight:500;
  font-variant-numeric:tabular-nums}
.act{display:flex;justify-content:space-between;gap:.6rem;align-items:baseline;
  padding:.4rem 0;border-bottom:1px solid var(--rule);font-size:.8125rem}
.act a{text-decoration:none;color:var(--ink)}
.act a:hover b{text-decoration:underline}
.act .warn{color:var(--med)}
.act .when{font-family:var(--mono);font-size:.6875rem;color:var(--ink-3);white-space:nowrap}
.panel-head{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:.6rem}
.panel-head h2{margin:0;font-size:1rem;font-weight:600}
.panel-head a{font-family:var(--mono);font-size:.75rem}
/* -- scan: one company ------------------------------------------------- */
form.scan input[type=text]{flex:0 1 21rem}
.form-note{display:flex;align-items:center;font-family:var(--mono);font-size:.75rem;
color:var(--ink-3)}
.head-link{margin-left:auto;font-family:var(--mono);font-size:.75rem}
.wl{border:1px solid var(--rule);overflow-x:auto}
.wl table{width:100%;min-width:44rem;border-collapse:collapse;font-size:.875rem}
.wl th{font-family:var(--mono);font-size:.6875rem;letter-spacing:.1em;
text-transform:uppercase;color:var(--ink-3);text-align:left;font-weight:400;
padding:.6rem .8rem;border-bottom:1px solid var(--rule)}
.wl td{padding:.65rem .8rem;border-bottom:1px solid var(--rule)}
.wl tr:last-child td{border-bottom:0}
.wl td.tk{font-family:var(--mono);font-weight:600;color:var(--accent)}
.wl td.sub{font-family:var(--mono);font-size:.8125rem;color:var(--ink-3)}
.wl td.num,.wl th.num{text-align:right;font-family:var(--mono);
font-variant-numeric:tabular-nums}
.wl td.go{text-align:right;white-space:nowrap}
.wl td.go a{font-family:var(--mono);font-size:.75rem}
.chip{font-family:var(--mono);font-size:.6875rem;padding:.15rem .5rem;
background:var(--surface-2);color:var(--ink-3);white-space:nowrap}
.chip.warn{background:var(--med-soft);color:var(--med)}
.chips{display:flex;flex-wrap:wrap;gap:.5rem}
.chip-link{font-family:var(--mono);font-size:.75rem;text-decoration:none;
border:1px solid var(--rule);background:var(--surface);padding:.45rem .7rem;
color:var(--ink-3)}
.chip-link b{color:var(--accent)}
.chip-link:hover{border-color:var(--accent)}
/* -- compare: two sides, picked from the watchlist --------------------- */
.picker{display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);
gap:0 1.25rem;align-items:start}
.vs-rail{display:flex;justify-content:center;padding-top:3.2rem}
.side{display:flex;flex-direction:column;gap:.6rem;min-width:0}
.side-head{display:flex;align-items:baseline;gap:.75rem;padding-bottom:.5rem;
border-bottom:1px solid var(--rule-strong)}
.side-head .tk{margin-left:auto;font-family:var(--mono);font-size:.875rem;
font-weight:600;color:var(--accent)}
.pick-filter{display:flex;align-items:center;gap:.7rem}
.pick-filter input[type=search]{flex:1 1 auto;min-width:0;font-family:var(--mono);
font-size:.8125rem;padding:.5rem .6rem;background:var(--surface);color:var(--ink);
border:1px solid var(--rule-strong);-webkit-appearance:none;appearance:none}
.pick-filter input::placeholder{color:var(--ink-3)}
.pick-filter .sub{font-family:var(--mono);font-size:.6875rem;color:var(--ink-3);
white-space:nowrap}
/* The list scrolls rather than growing: a hundred tracked companies would
   otherwise be a 7,000px column, and the commit bar would sit below both. */
.picks{display:flex;flex-direction:column;gap:1px;background:var(--rule);
border:1px solid var(--rule);max-height:24rem;overflow-y:auto}
.pick[hidden]{display:none}
.pick{display:flex;align-items:flex-start;gap:.7rem;padding:.7rem .85rem;
background:var(--surface);cursor:pointer}
.pick:hover{background:var(--surface-2)}
.pick input{margin:.25rem 0 0;accent-color:var(--accent);flex:none}
.pick:has(input:checked){background:var(--accent-soft)}
.pick.taken{cursor:default;opacity:.55}
.pick.taken:hover{background:var(--surface)}
.pick-body{display:flex;flex-direction:column;gap:.15rem;min-width:0}
.pick-id{display:flex;align-items:baseline;gap:.55rem;flex-wrap:wrap}
.pick-id .tk{font-family:var(--mono);font-size:.8125rem;color:var(--accent)}
.pick-id b{font-family:var(--sans);font-size:.875rem;font-weight:600}
.pick .sub{font-family:var(--mono);font-size:.6875rem;color:var(--ink-3)}
.pick-note{margin-left:auto;font-family:var(--mono);font-size:.625rem;
letter-spacing:.1em;text-transform:uppercase;color:var(--ink-3);white-space:nowrap}
.pick-alt{display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}
.pick-alt input[type=text]{flex:0 1 8rem;min-width:6rem;font-size:.8125rem;
padding:.5rem .6rem}
.pick-alt .sub{font-family:var(--mono);font-size:.6875rem;color:var(--ink-3)}
.commit{display:flex;align-items:center;gap:1.25rem;flex-wrap:wrap;
margin-top:1.5rem;padding-top:1.1rem;border-top:2px solid var(--ink)}
.commit button{margin-left:auto}
.commit-who,.commit-route{display:flex;flex-direction:column;gap:.2rem}
.commit-pair{display:flex;align-items:baseline;gap:.5rem;font-family:var(--mono);
font-size:1.0625rem;font-weight:600;color:var(--accent)}
.commit .sub{font-family:var(--mono);font-size:.6875rem;color:var(--ink-3)}
.commit-route code{font-family:var(--mono);font-size:.75rem;color:var(--ink-2)}
.switcher-line{margin-bottom:1.25rem;display:flex}
.switcher-line .head-link{margin-left:0}

.switcher{margin-bottom:1.25rem}
.switcher>summary{list-style:none;width:fit-content;cursor:pointer;
  font-family:var(--mono);font-size:.75rem;color:var(--ink-2);
  border:1px solid var(--rule-strong);border-radius:4px;padding:.35rem .8rem}
.switcher>summary::-webkit-details-marker{display:none}
.switcher>summary::before{content:"+ "}
.switcher[open]>summary::before{content:"\2212 "}
.switcher>summary:hover{border-color:var(--accent);color:var(--accent)}
.switcher>form,.switcher>.suggest{margin-top:.75rem}
/* --- report: index | findings | housekeeping --- */
/* The index is sticky, so the left column must belong to it for the whole
   height of the report. Letting another block span back under column 1 puts
   that block beneath the pinned rail near the foot of a short page. */
.report{display:grid;grid-template-columns:232px minmax(0,1fr);gap:0 40px;align-items:start}
.index{grid-column:1;grid-row:1/-1}
.report-main{grid-column:2}
.keep{grid-column:2}
@media (min-width:1500px){
  .report{grid-template-columns:232px minmax(0,1fr) 296px}
  .keep{grid-column:3;grid-row:1/-1}
}
.report-main{display:flex;flex-direction:column;gap:1.4rem;min-width:0;max-width:46rem}
.index{position:sticky;top:1rem;display:flex;flex-direction:column;gap:1.1rem;
  max-height:calc(100vh - 2rem);overflow-y:auto}
.ix-head{display:flex;flex-direction:column;gap:.15rem}
.ix-tick{font-family:var(--mono);font-size:.75rem;color:var(--ink-3)}
.ix-group{display:flex;flex-direction:column;gap:.15rem}
.ix-label{font-family:var(--mono);font-size:.6875rem;text-transform:uppercase;
  letter-spacing:.1em;color:var(--ink-3);padding-bottom:.2rem}
.ix-row{display:grid;grid-template-columns:3px minmax(0,1fr) auto;gap:.6rem;
  padding:.35rem .55rem .35rem 0;border-radius:4px;text-decoration:none;align-items:center}
.ix-row:hover{background:var(--surface-2)}
.ix-bar{border-radius:2px;align-self:stretch;min-height:1rem}
.ix-bar.high{background:var(--high)} .ix-bar.medium,.ix-bar.med{background:var(--med)}
.ix-bar.low{background:var(--ink-3)} .ix-bar.info{background:var(--accent)}
.ix-bar.pass{background:var(--pass)}
.ix-name{font-family:var(--sans);font-size:.8125rem;font-weight:500;color:var(--ink-2);
  display:flex;flex-direction:column;line-height:1.3}
.ix-row:hover .ix-name{color:var(--ink)}
.ix-why{font-family:var(--mono);font-size:.625rem;color:var(--ink-3);line-height:1.4}
.ix-n{font-family:var(--mono);font-size:.75rem;font-weight:500}
.ix-n.high{color:var(--high)} .ix-n.medium{color:var(--med)} .ix-n.info{color:var(--accent)}
.ix-n.low{color:var(--ink-3)}
.ix-out{display:flex;flex-direction:column;padding:.35rem 0 .35rem 13px;
  font-family:var(--sans);font-size:.8125rem;color:var(--ink-3);line-height:1.3}
/* verdict cells */
.verdict{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));margin:0;
  border-top:2px solid var(--ink);border-bottom:1px solid var(--rule)}
.verdict>div{display:flex;flex-direction:column;gap:.3rem;padding:.75rem 1.25rem}
.verdict>div:first-child{padding-left:0}
.verdict>div+div{border-left:1px solid var(--rule)}
.verdict>div:last-child{padding-right:0}
.verdict dt{font-family:var(--mono);font-size:.625rem;text-transform:uppercase;
  letter-spacing:.1em;color:var(--ink-3)}
.verdict dd{margin:0;font-family:var(--sans);font-size:1.5rem;font-weight:700;
  font-variant-numeric:tabular-nums;line-height:1}
.verdict dd.chips{display:flex;gap:.35rem;flex-wrap:wrap;font-size:inherit}
.verdict .of{font-size:.9375rem;font-weight:500;color:var(--ink-3)}
.kpi-delta{font-family:var(--mono);font-size:.6875rem;color:var(--ink-3)}
.kpi-delta.up{color:var(--med)} .kpi-delta.down{color:var(--pass)}
.kpi-delta.good{color:var(--pass)} .kpi-delta.warn{color:var(--med)}
.meter{display:block;height:4px;border-radius:2px;background:var(--surface-2);overflow:hidden}
.meter i{display:block;height:100%;background:var(--med)}
.meter i.ok{background:var(--pass)}
/* merged state sections */
.state{display:flex;flex-direction:column;margin:0}
.sr-head{display:flex;align-items:baseline;gap:.6rem;padding:.6rem .9rem;border-radius:3px 3px 0 0}
.sr-head h2{font-size:1.0625rem;font-weight:600;letter-spacing:-.01em}
.sr-head span{font-family:var(--mono);font-size:.75rem}
.state.pass .sr-head{background:var(--pass-soft);border-left:3px solid var(--pass)}
.state.pass .sr-head span{color:var(--pass)}
.state.med .sr-head{background:var(--med-soft);border-left:3px solid var(--med)}
.state.med .sr-head span{color:var(--med)}
.sr-row{display:grid;grid-template-columns:11rem minmax(0,1fr) auto;gap:0 1rem;
  align-items:baseline;padding:.6rem .9rem;border-bottom:1px solid var(--rule)}
.sr-name{font-family:var(--sans);font-size:.875rem;font-weight:600}
.sr-why{font-size:.875rem;color:var(--ink-2);line-height:1.5}
.sr-note{font-family:var(--mono);font-size:.75rem;color:var(--ink-3)}
.sr-tail{display:flex;gap:.35rem;align-items:center}
.ruled{font-family:var(--mono);font-size:.6875rem;color:var(--ink-3);margin:0;
  padding-top:.5rem;border-top:1px solid var(--rule)}
.scan-meta{font-family:var(--mono);font-size:.6875rem;color:var(--ink-3);margin:0}
/* housekeeping rail */
.keep{display:flex;flex-direction:column;gap:.75rem}
@media (max-width:1499px){.keep{flex-direction:row;flex-wrap:wrap;align-items:flex-start;
  margin-top:.75rem}}
.keep .track{margin:0;width:auto}
.legend{display:flex;flex-direction:column;gap:.25rem;padding:.7rem .9rem;
  border:1px solid var(--rule);border-radius:6px;font-family:var(--mono);
  font-size:.6875rem;color:var(--ink-3);line-height:1.6}
.legend b{color:var(--ink-2)} .legend b.cls-a{color:var(--pass)}
/* --- nav: two widths, same destinations --- */
.nav-group{display:flex;flex-direction:column;gap:2px}
.nav-group+.nav-group{margin-top:16px}
.nav-group-label{font-family:var(--mono);font-size:.625rem;text-transform:uppercase;
  letter-spacing:.12em;color:var(--ink-3);padding:0 8px 4px}
.nav-badge{margin-left:auto;font-family:var(--mono);font-size:.6875rem;font-weight:500;
  background:var(--surface-2);color:var(--ink-3);padding:1px 7px;border-radius:99px}
.nav-badge.warn{background:var(--med-soft);color:var(--med)}
.nav-toggle{display:flex;align-items:center;gap:.6rem;width:100%;padding:.45rem .5rem;
  margin-bottom:.35rem;border:0;border-radius:6px;background:transparent;cursor:pointer;
  color:var(--ink-3);font-family:var(--sans);font-size:.8125rem;text-align:left}
.nav-toggle:hover{background:var(--surface-2);color:var(--ink-2)}
.nav-toggle svg{width:1.05rem;height:1.05rem;flex:none;fill:currentColor}
.nav-toggle .ico-expand{display:none}
:root[data-nav="closed"] .app{grid-template-columns:3.5rem 1fr}
:root[data-nav="closed"] .sidebar{padding:1.25rem .5rem;align-items:center}
:root[data-nav="closed"] .nav-label{display:none}
:root[data-nav="closed"] .nav-group-label{display:none}
:root[data-nav="closed"] .nav-badge{position:absolute;top:2px;right:2px;margin:0;
  padding:0 4px;font-size:.5625rem;line-height:1.3}
:root[data-nav="closed"] .nav-item{position:relative;justify-content:center;padding:.45rem}
:root[data-nav="closed"] .nav-group+.nav-group{margin-top:10px;
  padding-top:10px;border-top:1px solid var(--rule)}
:root[data-nav="closed"] .brand{justify-content:center;padding:.25rem 0}
:root[data-nav="closed"] .account-text{display:none}
:root[data-nav="closed"] .account{justify-content:center;padding:.4rem 0}
:root[data-nav="closed"] .nav-toggle{justify-content:center;padding:.45rem;
  border:1px solid var(--rule)}
:root[data-nav="closed"] .nav-toggle .ico-collapse{display:none}
:root[data-nav="closed"] .nav-toggle .ico-expand{display:block}
.crumbs{display:flex;gap:.5rem;flex-wrap:wrap;font-family:var(--mono);font-size:.6875rem;
  color:var(--ink-3);margin-bottom:.5rem}
.crumb{padding:.2rem .55rem;border-radius:99px;background:var(--surface-2)}
.crumb.on{background:var(--accent-soft);color:var(--accent);font-weight:600}
.crumb.done{color:var(--ink-2)}
.found{padding:.7rem .9rem;border-radius:6px;background:var(--surface-2);
  border-left:2px solid var(--accent);margin-bottom:1rem}
.found b{font-size:1rem}
.found .sub{display:block;font-family:var(--mono);font-size:.75rem;color:var(--ink-3);
  margin-top:.15rem}
.picks{display:flex;flex-direction:column;gap:.35rem;margin:.9rem 0}
.pick{display:flex;align-items:flex-start;gap:.7rem;padding:.6rem .8rem;border-radius:6px;
  border:1px solid var(--rule);cursor:pointer}
.pick:hover{border-color:var(--rule-strong)}
/* This was .pick.on, stamped server-side: the row lit up on the
   pre-selection and then never went dark again when it was unticked. */
.pick:has(input:checked){border-color:var(--accent);background:var(--accent-soft)}
.pick.out{cursor:default;background:var(--surface)}
.pick.out:hover{background:var(--surface)}
.pick.out b{color:var(--ink-2)}
.pick-tag{margin-left:auto;font-family:var(--mono);font-size:.625rem;
letter-spacing:.1em;text-transform:uppercase;color:var(--ink-3);white-space:nowrap}
.pick input{margin-top:.2rem;flex:none;accent-color:var(--accent)}
.pick-body{display:flex;flex-direction:column;gap:.15rem;flex:1;min-width:0}
.pick-body b{font-size:.875rem;color:var(--ink)}
.pick-why{font-size:.8125rem;color:var(--ink-2);line-height:1.45}
.status.high{background:var(--high-soft);color:var(--high)}
.status.medium{background:var(--med-soft);color:var(--med)}
.status.low{background:var(--surface-2);color:var(--ink-3)}
.wizard-actions{display:flex;align-items:center;gap:1rem;margin-top:1rem}
.ghost-link{font-size:.8125rem;color:var(--ink-3)}
.actions{display:flex;flex-wrap:wrap;gap:.5rem;align-items:stretch}
.actions:empty{display:none}
.actions .track{flex:0 1 auto;margin:0;width:auto}
.actions .track.on:has(.proposal){flex:1 1 100%}
.lede{font-size:.9375rem;color:var(--ink-2);max-width:62ch;margin:.35rem 0 0}
.meta-line{font-family:var(--mono);font-size:.6875rem;color:var(--ink-3);
  margin-top:2rem;padding-top:.75rem;border-top:1px solid var(--rule)}
.panel{margin:1.1rem 0}
.panel h2{font-size:.9375rem;font-weight:600;margin:0 0 .7rem;color:var(--ink)}
.grid{width:100%;border-collapse:collapse;font-size:.875rem}
.grid th{text-align:left;font-weight:500;font-size:.6875rem;letter-spacing:.06em;
  text-transform:uppercase;color:var(--ink-3);padding:.4rem .7rem;
  border-bottom:1px solid var(--rule-strong)}
.grid td{padding:.6rem .7rem;border-bottom:1px solid var(--rule);vertical-align:middle}
.grid tbody tr:hover{background:var(--surface-2)}
.grid .num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}
.grid span.sub{display:block;font-size:.75rem;color:var(--ink-3);font-weight:400}
.grid td.sub{color:var(--ink-3);font-size:.8125rem}
.grid a{text-decoration:none;color:var(--ink)}
.grid a:hover b{text-decoration:underline}
.pill{display:inline-block;padding:.1rem .45rem;border-radius:99px;font-size:.6875rem;
  font-family:var(--mono);margin-right:.25rem}
.pill.quiet{background:var(--surface-2);color:var(--ink-3)}
.pill.up{background:var(--med-soft);color:var(--med)}
.pill.down{background:var(--pass-soft);color:var(--pass)}
.empty{padding:1.4rem;border:1px dashed var(--rule-strong);border-radius:6px;
  color:var(--ink-3);font-size:.875rem}
.empty p{margin:0}
.empty .sub{margin-top:.3rem;font-size:.8125rem}
.stat small{display:block;font-size:.6875rem;color:var(--ink-3);margin-top:.15rem}
.app{display:grid;grid-template-columns:15rem 1fr;min-height:100vh}
.main{min-width:0}
.sidebar{position:sticky;top:0;height:100vh;display:flex;flex-direction:column;
  gap:1.25rem;padding:1.25rem .85rem;background:var(--surface);
  border-right:1px solid var(--rule)}
.brand{display:flex;align-items:center;gap:.6rem;padding:.25rem .5rem;
  text-decoration:none;color:var(--ink)}
.brand-mark{display:grid;place-items:center;width:1.9rem;height:1.9rem;flex:none;
  border-radius:6px;background:var(--accent);color:var(--paper);
  font-family:var(--mono);font-size:.625rem;font-weight:700;letter-spacing:.02em}
.brand-name{font-weight:600;font-size:.9375rem;letter-spacing:-.01em}
.nav-list{display:flex;flex-direction:column;gap:.15rem}
.nav-item{display:flex;align-items:center;gap:.6rem;padding:.45rem .5rem;
  border-radius:6px;text-decoration:none;color:var(--ink-2);font-size:.875rem}
.nav-item svg{width:1.05rem;height:1.05rem;flex:none;fill:currentColor;opacity:.75}
.nav-item:hover{background:var(--surface-2);color:var(--ink)}
.nav-item.on{background:var(--accent-soft);color:var(--accent);font-weight:600}
.nav-item.on svg{opacity:1}
.sidebar-foot{margin-top:auto;padding-top:.75rem;border-top:1px solid var(--rule)}
.account{display:flex;align-items:center;gap:.6rem;padding:.4rem .5rem;border-radius:6px}
.avatar{display:grid;place-items:center;width:1.85rem;height:1.85rem;flex:none;
  border-radius:50%;background:var(--surface-2);color:var(--ink-2);
  font-family:var(--mono);font-size:.6875rem;font-weight:600;
  border:1px solid var(--rule-strong)}
.account-text{display:flex;flex-direction:column;min-width:0;line-height:1.25}
.account-text b{font-size:.8125rem;font-weight:600;color:var(--ink);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.account-text small{font-size:.6875rem;color:var(--ink-3);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media (max-width:820px){
  .app{grid-template-columns:1fr}
  .sidebar{position:static;height:auto;flex-direction:row;align-items:center;
    gap:.75rem;overflow-x:auto;border-right:0;border-bottom:1px solid var(--rule)}
  .nav-list{flex-direction:row}
  .sidebar-foot{display:none}
}
.proposal{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;width:100%;
  margin-top:.5rem;padding-top:.5rem;border-top:1px solid var(--rule);
  font-size:.8125rem;color:var(--ink-2)}
.proposal span{flex:1 1 22rem}
.notice{margin:0 0 1rem;padding:.6rem .9rem;border-radius:4px;font-size:.8125rem;
  background:var(--surface-2);border-left:2px solid var(--accent,var(--rule-strong));
  color:var(--ink-2)}
.shell{max-width:72rem;margin:0 auto;padding:1.75rem 1.5rem 3.5rem;display:flex;
flex-direction:column;gap:1.5rem}
.masthead{display:flex;flex-direction:column;gap:.85rem}
.masthead p{margin:0;max-width:40rem;color:var(--ink-2);font-size:1.1875rem;line-height:1.45}
form.scan{display:flex;flex-wrap:wrap;gap:.6rem;align-items:stretch;
padding-top:1.1rem;border-top:2px solid var(--ink)}
input[type=text]{font-family:var(--mono);font-size:1rem;text-transform:uppercase;
padding:.7rem .85rem;background:var(--surface);color:var(--ink);
border:1px solid var(--rule-strong);min-width:9rem;flex:1 1 9rem}
input[type=text]::placeholder{color:var(--ink-3);text-transform:none}
button{font-family:var(--sans);font-weight:600;font-size:.9375rem;padding:.7rem 1.4rem;
background:var(--accent);color:var(--paper);border:1px solid var(--accent);cursor:pointer}
button:hover{filter:brightness(1.1)}
.vs{display:flex;align-items:center;font-family:var(--mono);font-size:.75rem;
color:var(--ink-3);padding:0 .2rem}
.suggest{display:flex;flex-wrap:wrap;gap:.5rem}
.suggest a{font-family:var(--mono);font-size:.75rem;text-decoration:none;
padding:.35rem .6rem;border:1px solid var(--rule);background:var(--surface);color:var(--ink-2)}
.suggest a b{color:var(--accent)}
.summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(7.5rem,1fr));
gap:1px;background:var(--rule);border:1px solid var(--rule)}
.summary div{background:var(--surface);padding:.9rem 1rem;display:flex;
flex-direction:column;gap:.2rem}
.summary dt{font-family:var(--mono);font-size:.75rem;letter-spacing:.1em;
text-transform:uppercase;color:var(--ink-3)}
.summary dd{margin:0;font-family:var(--sans);font-size:1.5rem;font-weight:600;
font-variant-numeric:tabular-nums}
.summary dd.warn{color:var(--med)}
.withheld{background:var(--med-soft);border-left:3px solid var(--med);padding:1rem 1.2rem;
display:flex;flex-direction:column;gap:.55rem}
.withheld h2{font-size:1.0625rem}
.withheld ul{margin:0;padding-left:1.1rem;display:flex;flex-direction:column;gap:.4rem}
.withheld li{font-size:.9375rem;color:var(--ink-2)}
.withheld li b{font-family:var(--mono);font-size:.8125rem;color:var(--ink)}
.columns{display:grid;grid-template-columns:repeat(auto-fit,minmax(24rem,1fr));gap:1.75rem}
.column{display:flex;flex-direction:column;gap:1.15rem;min-width:0}
.company{display:flex;flex-direction:column;gap:.3rem}
.company h1{font-size:1.875rem}
.company .meta{font-family:var(--mono);font-size:.75rem;color:var(--ink-3)}
.error{background:var(--high-soft);border-left:3px solid var(--high);padding:1.1rem 1.3rem}
.error p{margin:0;color:var(--ink)}
.back{font-family:var(--mono);font-size:.75rem}
.nav{display:flex;gap:1.25rem;font-family:var(--mono);font-size:.75rem}
.health{display:flex;flex-wrap:wrap;gap:0 2.25rem;padding-top:.9rem;
border-top:2px solid var(--ink)}
.health-fact{display:flex;flex-direction:column;gap:.15rem;padding:.3rem 0}
.health-fact span{font-family:var(--mono);font-size:.6875rem;letter-spacing:.1em;
text-transform:uppercase;color:var(--ink-3)}
.health-fact b{font-family:var(--mono);font-size:1.0625rem;font-weight:500;
font-variant-numeric:tabular-nums}
.health-fact.pass b{color:var(--pass)}
.health-fact.warn b{color:var(--med)}
.head-note{margin-left:auto;font-family:var(--mono);font-size:.75rem;color:var(--ink-3)}
.src-tools{display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}
.src-search{display:flex;align-items:center;gap:.5rem;flex:0 1 20rem;min-width:12rem;
padding:0 .6rem;height:2.5rem;background:var(--surface);border:1px solid var(--rule-strong)}
.src-search svg{width:.875rem;height:.875rem;flex:none;fill:none;
stroke:var(--ink-3);stroke-width:1.6}
.src-search input{flex:1 1 auto;min-width:0;border:0;background:none;outline:0;
font-family:var(--mono);font-size:.8125rem;color:var(--ink)}
.segs{display:flex;gap:.4rem;margin-left:auto}
.seg{display:inline-flex;align-items:center;gap:.4rem;height:2.5rem;padding:0 .85rem;
border:1px solid var(--rule);background:var(--surface);color:var(--ink-2);
font-family:var(--mono);font-size:.78125rem;cursor:pointer}
.seg.on{border-color:var(--accent);background:var(--accent-soft);color:var(--accent);
font-weight:600}
.seg .dot{width:6px;height:6px;border-radius:50%;flex:none}
.seg .dot.live{background:var(--pass)}
.seg .dot.blocked{background:var(--med)}
.seg .dot.off{background:var(--rule-strong)}
.src-tools select{font-family:var(--mono);font-size:.78125rem;height:2.5rem;
padding:0 .6rem;background:var(--surface);color:var(--ink);
border:1px solid var(--rule-strong)}
.srclist{border:1px solid var(--rule);background:var(--surface);
display:flex;flex-direction:column}
.src-row{display:grid;grid-template-columns:8rem minmax(0,1fr) 14.75rem 12.5rem 9.25rem;
gap:0 1.25rem;padding:.9rem 1.25rem;align-items:start;
border-bottom:1px solid var(--rule)}
.src-row:last-child{border-bottom:0}
.src-row[hidden]{display:none}
.src-state{display:flex;flex-direction:column;gap:.3rem;padding-top:.1rem}
.pill{align-self:flex-start;font-family:var(--mono);font-size:.625rem;font-weight:600;
letter-spacing:.11em;text-transform:uppercase;padding:.2rem .5rem}
.pill.live{background:var(--pass-soft);color:var(--pass)}
.pill.blocked{background:var(--med-soft);color:var(--med)}
.pill.off{background:var(--surface-2);color:var(--ink-2)}
.src-row .sub{font-family:var(--mono);font-size:.625rem;color:var(--ink-3);
line-height:1.35}
.src-row .sub.warn{color:var(--med)}
.src-id{display:flex;flex-direction:column;gap:.2rem;min-width:0}
.src-id h3{font-family:var(--sans);font-size:.9375rem;font-weight:600;margin:0;
line-height:1.3}
.src-id p{margin:0;font-size:.8125rem;color:var(--ink-2);line-height:1.45}
.src-id .url{font-family:var(--mono);font-size:.6875rem;color:var(--ink-3);
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.src-klass{display:flex;gap:.6rem;align-items:flex-start}
.klass{display:grid;place-items:center;width:1.5rem;height:1.5rem;flex:none;
font-family:var(--mono);font-size:.75rem;font-weight:600;
background:var(--accent-soft);color:var(--accent)}
.klass.low{background:var(--surface-2);color:var(--ink-3)}
.src-klass-body{display:flex;flex-direction:column;gap:.1rem;line-height:1.3}
.src-klass-body b{font-family:var(--sans);font-size:.8125rem;font-weight:600}
.src-keyed{display:flex;flex-direction:column;gap:.25rem;line-height:1.3}
.keyed{align-self:flex-start;font-family:var(--mono);font-size:.625rem;font-weight:600;
letter-spacing:.08em;padding:.125rem .4rem;background:var(--surface-2);color:var(--ink-2)}
.src-file{display:flex;flex-direction:column;gap:.2rem;align-items:flex-end;
text-align:right}
.pager{display:flex;align-items:center;gap:.9rem;padding:.7rem 1.25rem;
border:1px solid var(--rule);border-top:0}
.pager.compact{border:0;border-top:1px solid var(--rule);padding:.7rem 0}
.pager .sub{font-family:var(--mono);font-size:.71875rem;color:var(--ink-3);
font-variant-numeric:tabular-nums}
.pg-right{margin-left:auto;display:flex;align-items:center;gap:.65rem}
.pg-right label{display:flex;align-items:center;gap:.45rem;font-family:var(--mono);
font-size:.6875rem;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-3)}
.pg-right select{font-family:var(--mono);font-size:.78125rem;height:2.125rem;
padding:0 .5rem;background:var(--surface);color:var(--ink);
border:1px solid var(--rule-strong);text-transform:none;letter-spacing:0}
.pg-arrows{display:flex;gap:.4rem}
.pg-arrow{display:grid;place-items:center;width:2.125rem;height:2.125rem;
border:1px solid var(--rule);background:var(--surface)}
.pg-arrow svg{width:.8125rem;height:.8125rem;fill:none;stroke:var(--rule-strong);
stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.maptable th b{display:block;font-family:var(--sans);font-size:.8125rem;font-weight:600}
.maptable th .sub{font-family:var(--mono);font-size:.625rem;color:var(--ink-3);
text-transform:none;letter-spacing:0;font-weight:400}
.srcband{display:grid;grid-template-columns:repeat(auto-fit,minmax(24rem,1fr));gap:2.5rem;
align-items:start}
.fieldset{display:flex;flex-direction:column;gap:.6rem;padding-bottom:1.1rem;
border-bottom:1px solid var(--rule)}
.fieldset:last-of-type{border-bottom:0}
.fieldset>.sub{font-family:var(--mono);font-size:.6875rem;color:var(--ink-3);
line-height:1.5}
.fieldset>.sub code{font-family:var(--mono);font-size:.6875rem;color:var(--ink-2)}
.fields{display:grid;grid-template-columns:minmax(0,1fr);gap:.75rem}
.fields.two{grid-template-columns:repeat(2,minmax(0,1fr))}
.rows{display:flex;flex-direction:column;gap:.55rem}
.rows .field{display:grid;grid-template-columns:11.5rem minmax(0,1fr);
align-items:center;gap:1rem}
.field{display:flex;flex-direction:column;gap:.3rem}
.field label{font-family:var(--mono);font-size:.6875rem;letter-spacing:.1em;
text-transform:uppercase;color:var(--ink-3)}
/* input[type=text] carries flex:1 1 9rem for the scan form's row. In this
   column that basis is read as a height, so every box grew to 144px. */
.field input,.field select,.field textarea{flex:none}
.field .req{color:var(--high);padding-left:.15rem}
.add-actions{display:flex;padding-top:.4rem}
.presets{display:flex;flex-wrap:wrap;gap:.5rem}
.preset{display:inline-flex;align-items:center;gap:.55rem;border:1px solid var(--rule);
background:var(--surface);padding:.35rem .4rem .35rem .6rem;font-family:var(--mono);
font-size:.75rem;color:var(--ink-2)}
.preset b{color:var(--accent)}
.preset form{display:inline}
.preset button{background:none;border:0;color:var(--ink-3);font-size:.9rem;
line-height:1;padding:0 .2rem;cursor:pointer;font-family:var(--mono)}
.preset button:hover{color:var(--high)}
form.inline{display:flex;flex-wrap:wrap;gap:.6rem;align-items:stretch;
padding-top:1rem;border-top:1px solid var(--rule)}
form.inline input{font-family:var(--mono);font-size:.875rem;padding:.55rem .7rem;
background:var(--surface);color:var(--ink);border:1px solid var(--rule-strong)}
form.inline input[name=ticker]{text-transform:uppercase;width:8rem}
form.inline input[name=note]{flex:1 1 16rem}
form.inline button{font-family:var(--sans);font-weight:600;font-size:.875rem;
padding:.55rem 1.1rem;background:var(--accent);color:var(--paper);
border:1px solid var(--accent);cursor:pointer}
.maptable{overflow-x:auto;border:1px solid var(--rule)}
.maptable table{min-width:30rem}
.maptable td,.maptable th{padding:.5rem .8rem;font-size:.8125rem}
.maptable td{font-family:var(--mono);color:var(--ink-2)}
.maptable td.tk{color:var(--accent);font-weight:600}
.maptable td.none{color:var(--ink-3)}
.maptable td.num{text-align:right;font-variant-numeric:tabular-nums}
.maptable th.num{text-align:right}
.track{display:flex;flex-wrap:wrap;align-items:center;gap:.7rem;padding:.7rem .95rem;
border:1px solid var(--rule);background:var(--surface);font-size:.875rem;color:var(--ink-2)}
.track.on{border-left:3px solid var(--pass)}
.track.off{border-left:3px solid var(--rule-strong)}
.track b{font-family:var(--mono);font-size:.75rem;color:var(--ink)}
.track form{margin:0}
.track button{font-family:var(--sans);font-weight:600;font-size:.8125rem;
padding:.4rem .9rem;background:var(--accent);color:var(--paper);
border:1px solid var(--accent);cursor:pointer}
/* Scoped to the class, not to .track — a destructive action must not inherit
   the primary button style wherever it happens to sit. */
button.ghost{background:none;color:var(--ink-3);border:1px solid var(--rule);
font-weight:500;padding:.35rem .75rem}
button.ghost:hover{color:var(--high);border-color:var(--high)}
.rescan{display:flex;flex-wrap:wrap;align-items:center;gap:.9rem;padding:.9rem 1.1rem;
border:1px solid var(--rule);background:var(--surface);font-size:.875rem;color:var(--ink-2)}
.rescan form{margin:0}
.delta{display:flex;flex-direction:column;gap:.25rem;font-size:.8125rem}
.delta .new{color:var(--med)}
.delta .gone{color:var(--pass)}
.delta .quiet{color:var(--ink-3);font-family:var(--mono);font-size:.75rem}
form.add{display:flex;flex-direction:column;gap:1.1rem}
form.add input,form.add select,form.add textarea{font-family:var(--mono);font-size:.8125rem;
padding:.55rem .6rem;background:var(--surface);color:var(--ink);
border:1px solid var(--rule-strong);text-transform:none;width:100%}
form.add textarea{min-height:5rem;resize:vertical}
.flash{padding:.9rem 1.1rem;border-left:3px solid var(--pass);background:var(--pass-soft);
font-size:.9375rem}
.flash.bad{border-left-color:var(--high);background:var(--high-soft)}
#veil{position:fixed;inset:0;background:var(--paper);opacity:.94;display:none;
align-items:center;justify-content:center;flex-direction:column;gap:1rem;z-index:9}
#veil.on{display:flex}
#veil .bar{width:12rem;height:2px;background:var(--rule);overflow:hidden}
#veil .bar i{display:block;height:100%;width:40%;background:var(--accent);
animation:slide 1.1s ease-in-out infinite}
@keyframes slide{0%{transform:translateX(-100%)}100%{transform:translateX(350%)}}
@media (prefers-reduced-motion:reduce){#veil .bar i{animation:none;width:100%}}
#veil p{font-family:var(--mono);font-size:.8125rem;color:var(--ink-2);margin:0}
"""

_VEIL = """<div id="veil"><div class="bar"><i></i></div>
<p>Reading filings from EDGAR…</p></div>
<script>
document.querySelectorAll('form.scan').forEach(function(f){
  f.addEventListener('submit', function(){
    document.getElementById('veil').classList.add('on');
  });
});
</script>"""


# (href, label, svg path, group). Adding a page is a row here plus a route.
NAV_ITEMS = [
    ("/", "Overview", "M3 9.5 10 4l7 5.5V16a1 1 0 0 1-1 1h-4v-4H8v4H4a1 1 0 0 1-1-1z", "Analyse"),
    ("/scan", "Scan", "M9 3a6 6 0 1 0 3.5 10.9l3.3 3.3 1.4-1.4-3.3-3.3A6 6 0 0 0 9 3m0 2a4 4 0 1 1 0 8 4 4 0 0 1 0-8", "Analyse"),
    ("/compare", "Compare", "M4 4h5v12H4zm7 0h5v12h-5z", "Analyse"),
    ("/tracked", "Watchlist", "M4 4h12v2H4zm0 5h12v2H4zm0 5h8v2H4z", "Manage"),
    ("/add", "Add company", "M9 4h2v5h5v2h-5v5H9v-5H4V9h5z", "Manage"),
    ("/sources", "Sources", "M10 3c3.9 0 7 1.3 7 3v8c0 1.7-3.1 3-7 3s-7-1.3-7-3V6c0-1.7 3.1-3 7-3m0 2C7 5 5 5.8 5 6s2 1 5 1 5-.8 5-1-2-1-5-1", "Manage"),
]

CHEVRON_L = "M12.5 5 7.5 10l5 5 1.4-1.4L10.3 10l3.6-3.6z"
CHEVRON_R = "M7.5 5 6.1 6.4 9.7 10l-3.6 3.6L7.5 15l5-5z"


def _nav_badges() -> dict[str, tuple[str, str]]:
    """{href: (text, tone)} — only counts a reader can act on."""
    out: dict[str, tuple[str, str]] = {}
    try:
        with connect() as connection:
            n = len(tracked_companies(connection))
        if n:
            out["/tracked"] = (str(n), "")
    except Exception:  # noqa: BLE001 — chrome must never take a page down
        pass
    try:
        from ledger.config import load_proposed

        pending = sum(len(v) for v in load_proposed().values())
        if pending:
            out["/sources"] = (str(pending), " warn")
    except Exception:  # noqa: BLE001
        pass
    return out


def _sidebar(current: str = "") -> str:
    """One nav for every page, in two widths.

    Every destination appears in both widths — a collapsed rail that quietly
    drops a page is a different nav, not a narrower one. Only the labels go.
    """
    badges = _nav_badges()
    groups: list[tuple[str, list]] = []
    for row in NAV_ITEMS:
        if not groups or groups[-1][0] != row[3]:
            groups.append((row[3], []))
        groups[-1][1].append(row)

    blocks = []
    for label, rows in groups:
        items = []
        for href, name, path, _ in rows:
            on = " on" if href == current else ""
            text, tone = badges.get(href, ("", ""))
            badge = f'<b class="nav-badge{tone}">{esc(text)}</b>' if text else ""
            items.append(
                f'<a class="nav-item{on}" href="{href}" title="{esc(name)}">'
                f'<svg viewBox="0 0 20 20" aria-hidden="true"><path d="{path}"/></svg>'
                f'<span class="nav-label">{esc(name)}</span>{badge}</a>'
            )
        blocks.append(f'<div class="nav-group"><span class="nav-group-label">{esc(label)}</span>'
                      f'{"".join(items)}</div>')

    toggle = (f'<button class="nav-toggle" type="button" data-nav-toggle '
              f'aria-label="Collapse or expand the sidebar">'
              f'<svg viewBox="0 0 20 20" aria-hidden="true" class="ico-collapse">'
              f'<path d="{CHEVRON_L}"/></svg>'
              f'<svg viewBox="0 0 20 20" aria-hidden="true" class="ico-expand">'
              f'<path d="{CHEVRON_R}"/></svg>'
              f'<span class="nav-label">Collapse</span></button>')

    return (f'<aside class="sidebar"><a class="brand" href="/">'
            f'<span class="brand-mark">CT</span>'
            f'<span class="brand-name nav-label">Contour</span></a>'
            f'<nav class="nav-list">{"".join(blocks)}</nav>'
            f'<div class="sidebar-foot">{toggle}{_account()}</div></aside>')


def _account() -> str:
    """The signed-in user block.

    Presentational: this app has no authentication, so the name comes from the
    environment rather than a session. It is deliberately not a sign-in form —
    a control that looks like it authenticates but does not is worse than none.
    """
    name = os.environ.get("CONTOUR_USER_NAME", "").strip() or "Account"
    detail = os.environ.get("CONTOUR_USER_EMAIL", "").strip() or "Local workspace"
    initials = "".join(part[0] for part in name.split()[:2] if part).upper() or "A"
    return (f'<div class="account"><span class="avatar" aria-hidden="true">'
            f"{esc(initials)}</span>"
            f'<span class="account-text"><b>{esc(name)}</b>'
            f"<small>{esc(detail)}</small></span></div>")


NAV_SCRIPT = """<script>
(function () {
  var root = document.documentElement;
  var stored = null;
  try { stored = localStorage.getItem('contour-nav'); } catch (e) { stored = null; }
  // A viewer's choice outranks the per-page default, on every page, forever.
  // Chrome that re-decides its own width on each navigation reads as unstable.
  if (stored === 'open' || stored === 'closed') root.dataset.nav = stored;
  document.addEventListener('click', function (ev) {
    var hit = ev.target.closest('[data-nav-toggle]');
    if (!hit) return;
    var next = root.dataset.nav === 'closed' ? 'open' : 'closed';
    root.dataset.nav = next;
    try { localStorage.setItem('contour-nav', next); } catch (e) {}
  });
})();
</script>"""


def _page(title: str, body: str, current: str = "") -> bytes:
    collapsed = current == "/scan"
    return f"""<!doctype html><html lang="en" data-nav="{'closed' if collapsed else 'open'}">
<head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>{FONTS}
<style>{CSS}{_EXTRA_CSS}</style></head><body>
<div class="app">{_sidebar(current)}<main class="main"><div class="shell">{body}</div></main></div>
{_VEIL}{NAV_SCRIPT}</body></html>""".encode()


def _form(primary: str = "") -> str:
    """One company, one report.

    This used to carry a second "Compare (optional)" field, which made /scan and
    /compare the same screen under different headings. Comparison is its own
    surface now, so the field that blurred them is gone.
    """
    return f"""<form class="scan" method="get" action="/scan">
<input type="text" name="a" placeholder="Ticker" value="{esc(primary)}"
       autocomplete="off" autofocus required>
<button type="submit">Scan</button>
<span class="form-note">one company · full report</span>
</form>"""



def _tracked() -> list:
    """Tracked companies, newest scan first. Chrome must never take a page down."""
    try:
        with connect() as connection:
            return list(tracked_companies(connection))
    except Exception:  # noqa: BLE001
        return []


def _watchlist_table() -> str:
    rows = _tracked()
    if not rows:
        return ('<p class="note">Nothing tracked yet. Scan a company, then take a '
                "baseline to start watching it.</p>")
    body = ""
    for t in rows:
        added = t.facts_now - t.baseline_facts
        if added > 0:
            delta = f'<span class="chip warn">+{added:,} figures</span>'
        else:
            delta = '<span class="chip">no change</span>'
        body += (f'<tr><td class="tk">{esc(t.ticker)}</td>'
                 f"<td>{esc(t.company)}</td>"
                 f'<td class="sub">{esc(t.last_scan or "—")}</td>'
                 f'<td class="num">{t.scans:,}</td>'
                 f'<td class="num">{t.baseline_facts:,}</td>'
                 f"<td>{delta}</td>"
                 f'<td class="go"><a href="/scan?a={esc(t.ticker)}">Scan ›</a></td></tr>')
    return (f'<div class="wl"><table><thead><tr><th scope="col">Ticker</th>'
            f'<th scope="col">Company</th><th scope="col">Last scan</th>'
            f'<th scope="col" class="num">Scans</th>'
            f'<th scope="col" class="num">Baseline figures</th>'
            f'<th scope="col">Since last scan</th><th scope="col"></th></tr></thead>'
            f"<tbody>{body}</tbody></table></div>")


def _recent_chips() -> str:
    """Scanned lately but not tracked — the only thing the watchlist cannot show."""
    tracked = {t.ticker for t in _tracked()}
    seen: list[str] = []
    try:
        with connect() as connection:
            for row in scan_history(connection, limit=12):
                if row["ticker"] not in tracked and row["ticker"] not in seen:
                    seen.append(row["ticker"])
    except Exception:  # noqa: BLE001
        return ""
    if not seen:
        return ""
    links = "".join(
        f'<a class="chip-link" href="/scan?a={esc(t)}"><b>{esc(t)}</b> · not tracked</a>'
        for t in seen[:8])
    return (f'<section class="check"><div class="check-head"><h2>Recently scanned</h2></div>'
            f'<div class="chips">{links}</div></section>')

def _suggestions() -> str:
    """Quick access to tracked companies, then recently scanned ones.

    These used to be captions describing what each ticker demonstrated. What a
    user wants here is their own companies, which the ledger already knows.
    """
    tickers: list[str] = []
    try:
        with connect() as connection:
            tickers = [t.ticker for t in tracked_companies(connection)]
            for row in scan_history(connection, limit=12):
                if row["ticker"] not in tickers:
                    tickers.append(row["ticker"])
    except Exception:  # noqa: BLE001 — chrome must never take the page down
        pass
    if not tickers:
        tickers = [t for t, _ in load_presets()[0]]
    links = "".join(f'<a href="/scan?a={esc(t)}">{esc(t)}</a>' for t in tickers[:8])
    return f'<div class="suggest">{links}</div>' if links else ""


def _ledger_line() -> str:
    try:
        with connect() as connection:
            stats = coverage(connection)
            recent = scan_history(connection, limit=1)
    except Exception:  # noqa: BLE001 — the page must render regardless
        return ""
    if not stats["observations"]:
        return ""
    last = f" · last scan {recent[0]['ticker']}" if recent else ""
    return (f"{stats['observations']:,} as-filed figures · {stats['companies']} companies "
            f"· {stats['periods']} periods · {stats['scans']} scans{last}")


def _nav(current: str = "") -> str:
    def link(href: str, label: str) -> str:
        return f"<span>{esc(label)}</span>" if href == current else f'<a href="{href}">{esc(label)}</a>'
    return f'<div class="nav">{link("/", "Scan")}{link("/sources", "Data sources")}</div>'


def _stat(label: str, value: str, sub: str = "") -> str:
    return (f'<div class="stat"><dt>{esc(label)}</dt><dd>{esc(value)}</dd>'
            f'{f"<small>{esc(sub)}</small>" if sub else ""}</div>')


def _overview_watchlist(connection) -> str:
    rows = tracked_companies(connection)
    if not rows:
        return ('<div class="empty"><p>No companies on your watchlist.</p>'
                '<p class="sub">Scan a ticker and add it to record a baseline that '
                'later scans are measured against.</p></div>')
    out = []
    for t in rows:
        delta = latest_delta(connection, t.ticker)
        if delta is None:
            change = '<span class="pill quiet">baseline only</span>'
        elif delta.quiet:
            change = '<span class="pill quiet">no change</span>'
        else:
            bits = []
            if delta.appeared:
                bits.append(f'<span class="pill up">+{len(delta.appeared)} new</span>')
            if delta.resolved:
                bits.append(f'<span class="pill down">−{len(delta.resolved)} resolved</span>')
            change = "".join(bits)
        out.append(
            f'<tr><td><a href="/scan?a={esc(t.ticker)}"><b>{esc(t.ticker)}</b></a>'
            f'<span class="sub">{esc(t.company)}</span></td>'
            f'<td class="num">{t.scans}</td>'
            f'<td class="num">{t.facts_now:,}</td>'
            f'<td class="num">{t.facts_added:,}</td>'
            f'<td>{change}</td>'
            f'<td class="num sub">{esc(t.last_scan or "—")}</td></tr>'
        )
    return (f'<table class="grid"><thead><tr><th>Company</th><th class="num">Scans</th>'
            f'<th class="num">Figures</th><th class="num">Added</th>'
            f'<th>Since last scan</th><th class="num">Last scan</th></tr></thead>'
            f'<tbody>{"".join(out)}</tbody></table>')


def _overview_recent(connection) -> str:
    rows = scan_history(connection, limit=8)
    if not rows:
        return ""
    out = "".join(
        f'<tr><td><a href="/scan?a={esc(r["ticker"])}"><b>{esc(r["ticker"])}</b></a>'
        f'<span class="sub">{esc(r["company"] or "")}</span></td>'
        f'<td class="num">{r["findings"]}</td>'
        f'<td class="num">{r["unavailable"]}</td>'
        f'<td class="num sub">{esc(r["scanned_at"])}</td></tr>'
        for r in rows
    )
    return (f'<table class="grid"><thead><tr><th>Company</th><th class="num">Findings</th>'
            f'<th class="num">Gaps</th><th class="num">Scanned</th></tr></thead>'
            f'<tbody>{out}</tbody></table>')


def _fact_rows(stats: dict) -> str:
    rows = [("As-filed figures", f"{stats.get('observations', 0):,}"),
            ("Companies", f"{stats.get('companies', 0):,}"),
            ("Periods", f"{stats.get('periods', 0):,}"),
            ("Scans", f"{stats.get('scans', 0):,}")]
    # Inventory numbers, not KPIs — a stacked fact list, not four hero cards
    # competing with the watchlist for the eye.
    return "".join(f'<div class="fact"><span>{esc(k)}</span><b>{esc(v)}</b></div>'
                   for k, v in rows)


def _activity(connection) -> str:
    """Recent scans, consecutive duplicates folded together."""
    rows = scan_history(connection, limit=24)
    folded: list[list] = []
    for r in rows:
        if folded and folded[-1][0]["ticker"] == r["ticker"] and \
                folded[-1][0]["findings"] == r["findings"]:
            folded[-1][1] += 1
            continue
        folded.append([r, 1])
        if len(folded) >= 6:
            break
    if not folded:
        return ""
    out = []
    for r, n in folded:
        gaps = (f'<span class="warn">{r["unavailable"]} gaps</span>'
                if r["unavailable"] else f'{r["findings"]} findings')
        out.append(f'<div class="act"><span><a href="/scan?a={esc(r["ticker"])}">'
                   f'<b>{esc(r["ticker"])}</b></a> {gaps}</span>'
                   f'<span class="when">{esc(r["scanned_at"])}'
                   f'{f" &times;{n}" if n > 1 else ""}</span></div>')
    return "".join(out)


def landing() -> bytes:
    stats, watchlist, activity = {}, "", ""
    try:
        with connect() as connection:
            stats = coverage(connection)
            watchlist = _overview_watchlist(connection)
            activity = _activity(connection)
    except Exception:  # noqa: BLE001 — the page must render on a cold database
        stats = {}

    body = f"""<div class="overview">
<div class="ov-main">
<header class="masthead"><h1>Overview</h1>{_form()}</header>
{_suggestions()}
<section class="panel"><div class="panel-head"><h2>Watchlist</h2>
<a href="/tracked">Manage &rarr;</a></div>{watchlist}</section>
</div>
<aside class="ov-side">
<div class="facts"><span class="ix-label">Ledger</span>{_fact_rows(stats)}</div>
<div class="facts"><span class="ix-label">Recent activity</span>{activity}</div>
</aside>
</div>"""
    return _page("Overview — Contour", body, current="/")


def _summary(report: Report, elapsed: float) -> str:
    unavailable = len(report.unavailable)
    return f"""<dl class="summary">
<div><dt>Findings</dt><dd>{report.findings}</dd></div>
<div><dt>Checks run</dt><dd class="{'warn' if unavailable else ''}">{len(report.roster) - unavailable}/{len(report.roster)}</dd></div>
<div><dt>Scan time</dt><dd>{elapsed:.1f}s</dd></div>
</dl>"""


def _entity_bar(report: Report) -> str:
    """Offer a name for sources that have none, and let a person settle one."""
    from ledger.config import load_proposed

    ticker = report.ticker
    pending = _unmapped_sources(ticker)
    proposed = load_proposed().get(ticker.upper(), {})

    rows = []
    for name, rec in sorted(proposed.items()):
        rows.append(
            f'<div class="proposal"><span><b>{esc(name)}</b> → '
            f'"{esc(rec.get("entity", ""))}" · {esc(rec.get("confidence", "?"))} confidence '
            f'· {esc(rec.get("reasoning", ""))[:140]}</span>'
            f'<form method="post" action="/entities/confirm">'
            f'<input type="hidden" name="ticker" value="{esc(ticker)}">'
            f'<input type="hidden" name="source" value="{esc(name)}">'
            f'<button class="ghost" type="submit">Confirm</button></form>'
            f'<form method="post" action="/entities/reject">'
            f'<input type="hidden" name="ticker" value="{esc(ticker)}">'
            f'<input type="hidden" name="source" value="{esc(name)}">'
            f'<button class="ghost" type="submit">Discard</button></form></div>'
        )

    if not rows and not pending:
        return ""
    ask = ""
    if pending:
        ask = (f'<form method="post" action="/entities/propose">'
               f'<input type="hidden" name="ticker" value="{esc(ticker)}">'
               f'<button class="ghost" type="submit">'
               f'Propose names ({len(pending)} unmapped)</button></form>')
    head = (f'{len(rows)} suggested name{"s" if len(rows) != 1 else ""} awaiting review'
            if rows else
            f'{len(pending)} source{"s" if len(pending) != 1 else ""} '
            f'{"needs" if len(pending) == 1 else "need"} a name for {esc(ticker)}')
    return (f'<div class="track {"on" if rows else "off"}">{head}{ask}'
            + "".join(rows) + "</div>")


def _authored_bar(report: Report) -> str:
    """Offer, and account for, this company's model-written checks.

    Writing them is a deliberate act with a visible result, not something a
    scan does behind the reader's back.
    """
    from ledger import authored as A

    pinned = A.load(report.ticker)
    write = (f'<form method="post" action="/authored/write">'
             f'<input type="hidden" name="ticker" value="{esc(report.ticker)}">'
             f'<button class="ghost" type="submit">'
             f'{"Rewrite" if pinned.specs else "Write checks"}'
             f'</button></form>')
    if not pinned.specs:
        return (f'<div class="track off">No custom checks.'
                f'{write}</div>')
    ran = [c for c in report.authored if c.status != "unavailable"]
    return (f'<div class="track on"><b>{len(pinned.specs)}</b> model-written check'
            f'{"s" if len(pinned.specs) != 1 else ""} '
            f'by <b>{esc(pinned.model or "a model")}</b>'
            f'{f" on {esc(pinned.written_on)}" if pinned.written_on else ""} · '
            f'<b>{len(ran)}</b> ran'
            f'{write}'
            f'<form method="post" action="/authored/forget">'
            f'<input type="hidden" name="ticker" value="{esc(report.ticker)}">'
            f'<button class="ghost" type="submit">Discard</button></form></div>')


def _track_bar(report: Report) -> str:
    """Searching and tracking are different acts. A search answers a question
    now; tracking takes a dated baseline that later scans are measured against."""
    try:
        with connect() as connection:
            state = tracking(connection, report.ticker)
    except Exception:  # noqa: BLE001 — the report must render regardless
        return ""
    if state:
        added = (f" · <b>{state.facts_added:,}</b> figures added since"
                 if state.facts_added else "")
        return (f'<div class="track on">Tracked since <b>{esc(state.tracked_since)}</b>'
                f' · <b>{state.scans}</b> scans · baseline <b>{state.baseline_facts:,}</b>'
                f' as-filed figures{added}'
                f'<form method="post" action="/untrack">'
                f'<input type="hidden" name="ticker" value="{esc(report.ticker)}">'
                f'<button class="ghost" type="submit">Remove</button></form></div>')
    return (f'<div class="track off">'
            f'<form method="post" action="/track">'
            f'<input type="hidden" name="ticker" value="{esc(report.ticker)}">'
            f'<button type="submit">Add to watchlist</button></form></div>')


def _brief(report: Report):
    """Narration is a nicety; a failure here must never cost a report."""
    try:
        return executive_summary(report)
    except Exception:  # noqa: BLE001
        return None


SEV_RANK = {"high": 0, "medium": 1, "low": 2, "info": 3}


def _is_feed(check) -> bool:
    """Corroboration sources — they carry volume, not weight."""
    return check.key.startswith("custom_")


def _partition(report: Report):
    """(flagged, feeds, clean, gaps) in the order a reader should meet them."""
    flagged, feeds, clean, gaps = [], [], [], []
    for c in report.roster:
        if c.status == Status.UNAVAILABLE:
            gaps.append(c)
        elif c.items and _is_feed(c):
            feeds.append(c)
        elif c.items:
            flagged.append(c)
        else:
            clean.append(c)
    flagged.sort(key=lambda c: (min((SEV_RANK.get(i.severity, 3) for i in c.items), default=3),
                                -len(c.items)))
    return flagged, feeds, clean, gaps


def _delta_line(report: Report) -> str:
    """What changed since the previous scan. This product detects change; a
    findings count with nothing to compare against states half the fact."""
    try:
        with connect() as connection:
            delta = latest_delta(connection, report.ticker)
    except Exception:  # noqa: BLE001
        return ""
    if delta is None:
        return '<span class="kpi-delta">first scan on record</span>'
    if delta.quiet:
        return '<span class="kpi-delta">no change since last scan</span>'
    bits = []
    if delta.appeared:
        bits.append(f'<span class="kpi-delta up">&uarr; {len(delta.appeared)} new</span>')
    if delta.resolved:
        bits.append(f'<span class="kpi-delta down">&darr; {len(delta.resolved)} resolved</span>')
    return "".join(bits) + '<span class="kpi-delta"> since last scan</span>'


def _verdict(report: Report, elapsed: float) -> str:
    counts: dict[str, int] = {}
    for c in report.roster:
        for i in c.items:
            counts[i.severity] = counts.get(i.severity, 0) + 1
    chips = "".join(
        f'<span class="status {esc(sev)}">{counts[sev]} {esc(sev.upper())}</span>'
        for sev in ("high", "medium", "low", "info") if counts.get(sev)
    )
    ran = len(report.roster) - len(report.unavailable)
    total = len(report.roster) or 1
    pct = round(ran / total * 100)
    complete = ran == len(report.roster)
    return f"""<dl class="verdict">
<div><dt>Findings</dt><dd>{report.findings}</dd>{_delta_line(report)}</div>
<div><dt>Severity</dt><dd class="chips">{chips or '<span class="kpi-delta">none</span>'}</dd></div>
<div><dt>Coverage</dt><dd>{ran}<span class="of"> of {len(report.roster)}</span></dd>
<span class="meter"><i style="width:{pct}%" class="{'ok' if complete else 'part'}"></i></span>
<span class="kpi-delta {'good' if complete else 'warn'}">
{'every check ran' if complete else f'{len(report.unavailable)} could not run'}</span></div>
</dl>"""


def _index_rail(report: Report, prefix: str) -> str:
    """Every check, grouped by state, always on screen."""
    flagged, feeds, clean, gaps = _partition(report)

    def rows(checks, tone_of):
        out = []
        for c in checks:
            tone = tone_of(c)
            count = (f'<b class="ix-n {tone}">{len(c.items)}</b>' if c.items else "")
            reason = (f'<span class="ix-why">{esc((c.reason or "")[:44])}</span>'
                      if c.status in (Status.UNAVAILABLE, Status.NOT_APPLICABLE) and c.reason else "")
            out.append(f'<a class="ix-row" href="#{anchor(c, prefix)}">'
                       f'<i class="ix-bar {tone}"></i>'
                       f'<span class="ix-name">{esc(c.title)}{reason}</span>{count}</a>')
        return "".join(out)

    def worst(c):
        return min((i.severity for i in c.items), key=lambda x: SEV_RANK.get(x, 3), default="info")

    blocks = []
    if flagged or feeds:
        blocks.append(f'<div class="ix-group"><span class="ix-label">Flagged '
                      f'({len(flagged) + len(feeds)})</span>'
                      f'{rows(flagged, worst)}{rows(feeds, lambda c: "info")}</div>')
    if clean:
        blocks.append(f'<div class="ix-group"><span class="ix-label">Clean ({len(clean)})</span>'
                      f'{rows(clean, lambda c: "pass")}</div>')
    if gaps:
        blocks.append(f'<div class="ix-group"><span class="ix-label">Could not run '
                      f'({len(gaps)})</span>{rows(gaps, lambda c: "med")}</div>')
    if report.not_applicable:
        items = "".join(f'<span class="ix-out">{esc(c.title)}'
                        f'<span class="ix-why">{esc((c.reason or "")[:48])}</span></span>'
                        for c in report.not_applicable)
        blocks.append(f'<div class="ix-group"><span class="ix-label">Ruled out '
                      f'({len(report.not_applicable)})</span>{items}</div>')

    return (f'<aside class="index"><div class="ix-head">'
            f'<span class="ix-tick">{esc(report.ticker)} · CIK {report.cik}</span></div>'
            f'{"".join(blocks)}</aside>')


def _state_rows(title: str, tone: str, checks, prefix: str) -> str:
    """Clean and could-not-run collapse to one section each, rows not accordions."""
    if not checks:
        return ""
    rows = []
    for c in checks:
        if c.status == Status.UNAVAILABLE:
            detail = f'<span class="sr-why">{esc(c.reason or "no reason recorded")}</span>'
            pill = ""
        else:
            note = c.notes[0] if c.notes else "checked, nothing crossed a threshold"
            detail = f'<span class="sr-note">{esc(note)}</span>'
            pill = '<span class="status clean">CLEAN</span>'
        badge = ('<span class="status authored">MODEL RULE</span>' if c.authored_by else "")
        rows.append(f'<div class="sr-row" id="{anchor(c, prefix)}">'
                    f'<span class="sr-name">{esc(c.title)}</span>{detail}'
                    f'<span class="sr-tail">{badge}{pill}</span></div>')
    return (f'<section class="state {esc(tone)}"><div class="sr-head"><h2>{esc(title)}</h2>'
            f'<span>{len(checks)} check{"s" if len(checks) != 1 else ""}</span></div>'
            f'{"".join(rows)}</section>')


def _housekeeping(report: Report) -> str:
    inner = f"{_track_bar(report)}{_authored_bar(report)}{_entity_bar(report)}"
    legend = ('<div class="legend"><span class="ix-label">Source classes</span>'
              '<span><b class="cls-a">A</b> primary record &mdash; can verify</span>'
              '<span><b>B</b> company-authored</span>'
              '<span><b>F</b> community signal</span></div>')
    return f'<aside class="keep">{inner}{legend}</aside>'


def _compare(reports: list, elapsed: list) -> str:
    """Two companies, one row per check.

    Two independent stacks drift out of alignment on the first check and
    nothing can then be compared. Rows are the union of both rosters so an
    asymmetry — flagged here, unavailable there — reads across in one glance.
    """
    a, b = reports
    order: list[str] = []
    seen: dict[str, dict] = {}
    for idx, rep in enumerate(reports):
        for c in rep.roster + rep.not_applicable:
            slot = seen.setdefault(c.title, {})
            if c.title not in order:
                order.append(c.title)
            slot[idx] = c

    def rank(title: str) -> tuple:
        pair = seen[title]
        worst = 9
        gap = 0
        for c in pair.values():
            if c.status == Status.UNAVAILABLE:
                gap = 1
            for i in c.items:
                worst = min(worst, SEV_RANK.get(i.severity, 3))
        feed = all(_is_feed(c) for c in pair.values())
        return (2 if feed else 0, worst, -gap)

    order.sort(key=rank)

    def cell(check, prefix: str, ticker: str) -> str:
        if check is None:
            return f'<div class="cx none">not selected for {esc(ticker)}</div>'
        if check.status == Status.UNAVAILABLE:
            return (f'<div class="cx gap"><span class="status unavailable">COULD NOT RUN</span>'
                    f'<span>{esc(check.reason or "no reason recorded")}</span></div>')
        if check.status == Status.NOT_APPLICABLE:
            return (f'<div class="cx none">{esc(check.reason or "out of scope")}</div>')
        if not check.items:
            note = check.notes[0] if check.notes else "nothing crossed a threshold"
            return (f'<div class="cx ok"><span class="status clean">CLEAN</span>'
                    f'<span>{esc(note)}</span></div>')
        return ('<div class="cx">'
                + "".join(item_html(i) for i in _sorted_items(check)[:3])
                + (f'<span class="cx-more">&rsaquo; {len(check.items) - 3} more</span>'
                   if len(check.items) > 3 else "")
                + "</div>")

    head = "".join(
        f'<div class="cmp-co"><h1>{esc(r.company)}</h1>'
        f'<span class="meta">{esc(r.ticker)} · CIK {r.cik}</span>'
        f'<span class="cmp-verdict {"warn" if r.unavailable else ""}">'
        f'{r.findings} findings · {len(r.roster) - len(r.unavailable)} of {len(r.roster)} ran'
        f'</span></div>' for r in reports)

    rows = []
    for title in order:
        pair = seen[title]
        cells = "".join(cell(pair.get(i), f"{reports[i].ticker.lower()}-", reports[i].ticker)
                        for i in (0, 1))
        rows.append(f'<div class="cmp-row"><span class="cmp-key">{esc(title)}</span>{cells}</div>')

    keep = "".join(f'<div>{_track_bar(r)}{_authored_bar(r)}{_entity_bar(r)}</div>'
                   for r in reports)
    return (f'<div class="cmp"><div class="cmp-band"><span></span>{head}</div>'
            f'{"".join(rows)}</div>'
            f'<div class="cmp-keep"><span></span>{keep}</div>')


def _column(report: Report, elapsed: float, solo: bool = True) -> str:
    """Index left, findings centre, housekeeping right.

    Ordered by evidentiary weight rather than check order: what was flagged,
    then what could not be checked, then what was clean, then corroboration.
    """
    filed = (f"latest 10-K {report.filing.filed.isoformat()}" if report.filing
             else "no 10-K on file")
    prefix = f"{report.ticker.lower()}-"
    flagged, feeds, clean, gaps = _partition(report)

    # When the gaps outnumber what ran, the gap is the headline.
    gaps_first = len(gaps) >= (len(report.roster) - len(gaps))
    gap_block = _state_rows("Could not run", "med", gaps, prefix)
    clean_block = _state_rows("Nothing flagged", "pass", clean, prefix)
    body = "".join(check_html(c, prefix=prefix, with_chart=False) for c in flagged)
    feed_body = "".join(check_html(c, prefix=prefix, with_chart=False) for c in feeds)

    ruled = ""
    if report.not_applicable:
        ruled = ('<p class="ruled">' + "; ".join(
            f"{esc(c.title)} &mdash; {esc(c.reason or 'out of scope')}"
            for c in report.not_applicable) + "</p>")

    centre = f"""<div class="report-main">
<div class="company"><h1>{esc(report.company)}</h1>
<span class="meta">{esc(report.ticker)} · CIK {report.cik} · {esc(filed)}</span></div>
{_verdict(report, elapsed)}
{summary_html(_brief(report))}
{visuals_html(report)}
{gap_block if gaps_first else ""}
{body}
{"" if gaps_first else gap_block}
{clean_block}
{feed_body}
{ruled}
<p class="scan-meta">scanned in {elapsed:.1f}s · {report.generated.isoformat()}</p>
</div>"""

    if not solo:
        return f'<div class="column">{centre}{_housekeeping(report)}</div>'
    return (f'<div class="report">{_index_rail(report, prefix)}{centre}'
            f'{_housekeeping(report)}</div>')


def _error(ticker: str, message: str) -> str:
    """A failed scan still gets a readable column. A raw exception string beside
    a working report reads as a broken product rather than a transient fault."""
    transient = any(w in message for w in ("Connection", "Timeout", "OSError", "Temporary"))
    hint = ("The network call failed on the way out. Nothing is wrong with the "
            "filings — press Scan again."
            if transient else
            "This company could not be scanned.")
    return f"""<div class="column"><div class="company"><h1>{esc(ticker.upper())}</h1></div>
<div class="withheld"><h2>Scan did not complete</h2>
<p>{esc(hint)}</p><ul><li><b>{esc(message[:180])}</b></li></ul></div></div>"""


def results(client: EdgarClient, tickers: list[str], notice: str = "",
            compare: bool = False) -> bytes:
    """Scan one or two companies; a second runs concurrently with the first."""
    def one(ticker: str):
        started = time.monotonic()
        try:
            return ticker, scan(client, ticker), time.monotonic() - started, None
        except EdgarError as exc:
            return ticker, None, 0.0, str(exc)
        except Exception as exc:  # noqa: BLE001 — the page must still render
            return ticker, None, 0.0, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=len(tickers)) as pool:
        outcomes = list(pool.map(one, tickers))

    good = [(t, r, e) for t, r, e, err in outcomes if r]
    comparing = len(outcomes) == 2 and len(good) == 2
    if comparing:
        columns = _compare([r for _, r, _ in good], [e for _, _, e in good])
    else:
        columns = "".join(
            _column(report, elapsed, solo=len(outcomes) == 1) if report
            else _error(ticker, error or "scan failed")
            for ticker, report, elapsed, error in outcomes
        )
    names = " vs ".join(t.upper() for t in tickers)
    banner = f'<div class="notice">{esc(notice)}</div>' if notice else ""
    # A report is a reading surface, not a launcher: the form folds away behind
    # one control instead of taking the first screen on every scan.
    if compare:
        switcher = (f'<a class="head-link" href="/compare?a={esc(tickers[0])}'
                    f'&amp;b={esc(tickers[1] if len(tickers) > 1 else "")}">'
                    "Change the pair ›</a>")
        opener = f'<div class="switcher-line">{switcher}</div>'
    else:
        opener = f"""<details class="switcher">
<summary>Scan another company</summary>
{_form(tickers[0] if tickers else "")}
{_suggestions()}
</details>"""
    body = f"""{opener}
{banner}
<div class="{'wide' if comparing else 'columns'}">{columns}</div>"""
    return _page(f"{names} — Contour", body,
                 current="/compare" if compare else "/scan")


_FIELDS = [
    ("name", "Name", "text", "e.g. Federal Register", True),
    ("url", "URL template", "text", "https://api.example.com/?q={entity}", True),
    ("note", "What it tells you", "text", "Regulatory actions naming the company", False),
    ("items", "Path to records", "text", "results", False),
    ("title", "Title field", "text", "title", False),
    ("detail", "Detail field", "text", "summary", False),
    ("date", "Date field", "text", "publication_date", False),
    ("link", "Link field", "text", "html_url", False),
    ("match", "Entity-gate field", "text", "title", False),
]

# The declare form groups these by purpose rather than by declaration order, so
# it needs to reach a field by name.
_FIELD_BY_KEY = {key: rest for key, *rest in _FIELDS}



# Like the compare picker, the list is correct without this: every row is in
# the page and readable. Filtering is a convenience for a long sources file.
SOURCES_SCRIPT = """<script>
(function(){
  var list = document.getElementById('srclist');
  if (!list) return;
  var search = document.getElementById('src-filter');
  var klass = document.getElementById('src-class');
  var segs = document.querySelectorAll('.src-tools .seg');
  var state = 'all';
  function apply(){
    var term = search ? search.value.trim().toLowerCase() : '';
    var want = klass ? klass.value : 'all';
    var shown = 0;
    list.querySelectorAll('.src-row').forEach(function(row){
      var hit = (!term || row.textContent.toLowerCase().indexOf(term) !== -1)
        && (state === 'all' || row.getAttribute('data-state') === state)
        && (want === 'all' || row.getAttribute('data-klass') === want);
      row.hidden = !hit;
      if (hit) shown++;
    });
    var pager = list.parentElement.querySelector('.pager .sub');
    if (pager) {
      pager.textContent = 'Showing 1\u2013' + shown + ' of '
        + list.querySelectorAll('.src-row').length;
    }
  }
  segs.forEach(function(seg){
    seg.addEventListener('click', function(){
      segs.forEach(function(o){ o.classList.remove('on'); });
      seg.classList.add('on');
      state = seg.getAttribute('data-filter');
      apply();
    });
  });
  if (search) search.addEventListener('input', apply);
  if (klass) klass.addEventListener('change', apply);
})();
</script>"""

def _source_state(source: CustomSource) -> tuple[str, str, list[str]]:
    """Live, Blocked or Off — and never two of them at once.

    These used to be one string ("disabled · needs USPTO_API_KEY"), which made a
    source switched off on purpose look identical to one Contour expects to run
    and cannot. They are different situations: a deliberate off-switch is a
    choice, a missing key is a gap. Once a source is off the key stops mattering,
    so `disabled` wins and the key note is demoted rather than dropped.
    """
    key_note = ("needs " + ", ".join(source.missing_env)) if source.missing_env else ""
    if not source.enabled:
        notes = ["disabled in TOML"]
        notes.append(f"{key_note} — not consulted" if key_note else "key present")
        return "off", "Off", notes
    if source.missing_env:
        return "blocked", "Blocked", ["enabled in TOML", key_note]
    return "live", "Live", []


def _source_row(source: CustomSource) -> str:
    low = source.klass.letter not in ("A", "B")
    state, label, notes = _source_state(source)
    note_html = "".join(
        f'<span class="sub{" warn" if state == "blocked" and i else ""}">{esc(n)}</span>'
        for i, n in enumerate(notes))
    keyed = "CIK" if not source.needs_entity else "NAME"
    return f"""<div class="src-row" data-state="{state}" data-klass="{source.klass.letter}">
<div class="src-state"><span class="pill {state}">{esc(label)}</span>{note_html}</div>
<div class="src-id"><h3>{esc(source.name)}</h3>
{f'<p>{esc(source.note)}</p>' if source.note else ''}
<span class="url" title="{esc(source.url)}">{esc(source.url)}</span></div>
<div class="src-klass"><span class="klass {'low' if low else ''}">{source.klass.letter}</span>
<span class="src-klass-body"><b>{esc(source.klass.label)}</b>
<span class="sub">{"can mark findings verified" if source.klass.letter == "A"
                  else "corroborates · never verifies"}</span></span></div>
<div class="src-keyed"><span class="keyed">{keyed}</span>
<span class="sub">{esc(source.coverage)}</span></div>
<div class="src-file"><span class="sub">{esc(source.path.name if source.path else "")}</span>
<span class="sub">{esc(source.kind)}</span></div>
</div>"""


def _source_health(sources) -> str:
    """What the page is for: how many sources can actually run right now."""
    states = [_source_state(s)[0] for s in sources]
    live = states.count("live")
    blocked = states.count("blocked")
    off = states.count("off")
    a_total = [s for s in sources if s.klass.letter == "A"]
    a_live = sum(1 for s in a_total if _source_state(s)[0] == "live")
    facts = [("Declared", f"{len(sources)}", "")]
    facts.append(("Live", f"{live}", "pass"))
    if blocked:
        facts.append(("Blocked", f"{blocked}", "warn"))
    if off:
        facts.append(("Off", f"{off}", ""))
    if a_total:
        facts.append(("Class A live", f"{a_live} of {len(a_total)}", ""))
    return '<div class="health">' + "".join(
        f'<div class="health-fact {tone}"><span>{esc(label)}</span><b>{esc(value)}</b></div>'
        for label, value, tone in facts) + "</div>"


def _source_toolbar(sources) -> str:
    states = [_source_state(s)[0] for s in sources]
    letters = sorted({s.klass.letter for s in sources})
    segs = "".join(
        f'<span class="seg{" on" if key == "all" else ""}" data-filter="{key}">'
        f'{"" if key == "all" else f"<i class=\'dot {key}\'></i>"}{esc(name)} '
        f"<b>{count}</b></span>"
        for key, name, count in (("all", "All", len(sources)),
                                 ("live", "Live", states.count("live")),
                                 ("blocked", "Blocked", states.count("blocked")),
                                 ("off", "Off", states.count("off")))
        if count or key in ("all", "live"))
    options = "".join(f'<option value="{esc(l)}">Class {esc(l)}</option>' for l in letters)
    return f"""<div class="src-tools">
<label class="src-search">
<svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="9" cy="9" r="5.2"/>
<path d="M12.8 12.8 17 17"/></svg>
<input type="search" id="src-filter" placeholder="Filter by name, host or file"
       autocomplete="off" aria-label="Filter sources">
</label>
<div class="segs">{segs}</div>
<select id="src-class" aria-label="Filter by reliability class">
<option value="all">All classes</option>{options}</select>
</div>"""


def _pager(shown: int, total: int, unit: str, compact: bool = False) -> str:
    """Every list on this page ends the same way, so none of them can grow
    without the reader being told how much they are not seeing."""
    per = ("" if compact else
           '<label>Per page<select aria-label="Rows per page">'
           "<option>25</option><option>50</option><option>100</option></select></label>")
    arrow = ('<span class="pg-arrow" aria-disabled="true">'
             '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="%s"/></svg></span>')
    return (f'<div class="pager{" compact" if compact else ""}">'
            f'<span class="sub">Showing 1–{shown:,} of {total:,}{" " + unit if unit else ""}</span>'
            f'<div class="pg-right">{per}'
            f'<div class="pg-arrows">{arrow % "M12.5 4 7 10l5.5 6"}'
            f'{arrow % "M7.5 4 13 10l-5.5 6"}</div>'
            f'<span class="sub">Page 1 of 1</span></div></div>')


def _preset_chips() -> str:
    presets, _ = load_presets()
    return "".join(
        f'<span class="preset"><b>{esc(t)}</b>{(" — " + esc(n)) if n else ""}'
        f'<form method="post" action="/presets/remove">'
        f'<input type="hidden" name="ticker" value="{esc(t)}">'
        f'<button type="submit" title="Remove {esc(t)}" '
        f'aria-label="Remove {esc(t)}">&times;</button></form></span>'
        for t, n in presets
    ) or '<span class="preset">none configured</span>'


def _mapped_keys(sources) -> list[tuple[str, str]]:
    """(storage key, label) for every mapping a company can carry."""
    keys = [("nhtsa_make", "NHTSA make")]
    keys += [(s.name, s.name) for s in sources if s.needs_entity]
    return keys


def _mapping_inputs(sources) -> str:
    return "".join(
        f'<input type="text" name="{esc(key)}" placeholder="{esc(label)}" autocomplete="off">'
        for key, label in _mapped_keys(sources)
    )


def _mapping_table(sources) -> str:
    """The columns *are* the sources, so each one carries its class.

    Split across the page these read as unrelated configuration; a company with
    no line here is why an entity-keyed source reports not applicable.
    """
    entities, _ = load_entities()
    keys = _mapped_keys(sources)
    by_name = {s.name: s for s in sources}
    if not entities:
        return ('<p class="note">No company mappings yet. Built-in NHTSA makes and '
                "any entities declared in a source file still apply.</p>")
    head = ""
    for key, label in keys:
        source = by_name.get(key)
        if source is None:
            tag = '<span class="sub">built in</span>'
        else:
            state = _source_state(source)[0]
            tag = (f'<span class="sub">Class {esc(source.klass.letter)}'
                   f'{" · blocked" if state == "blocked" else ""}'
                   f'{" · off" if state == "off" else ""}</span>')
        head += f"<th scope='col'><b>{esc(label)}</b>{tag}</th>"
    rows = ""
    for ticker in sorted(entities):
        cells = ""
        for key, _label in keys:
            value = entities[ticker].get(key)
            cells += (f"<td>{esc(value)}</td>" if value
                      else '<td class="none">—</td>')
        rows += f'<tr><td class="tk">{esc(ticker)}</td>{cells}</tr>'
    return (f'<div class="maptable"><table><thead><tr><th scope="col">Ticker</th>'
            f"{head}</tr></thead><tbody>{rows}</tbody></table></div>"
            + _pager(len(entities), len(entities), "companies"))


def sources_page(message: str = "", error: str = "") -> bytes:
    """Sources first, then what they need to run.

    The declared sources used to sit unlabelled in the middle of the page,
    below two configuration sections. They are the subject; the configuration
    exists to serve them.
    """
    sources, problems = load_sources()
    problems = problems + load_presets()[1]
    presets = load_presets()[0]
    mapping_table = _mapping_table(sources)
    mapping_inputs = _mapping_inputs(sources)
    rows = "".join(_source_row(s) for s in sources) or (
        '<p class="note">No sources configured yet.</p>'
    )
    flash = ""
    if message:
        flash = f'<div class="flash">{esc(message)}</div>'
    if error:
        flash += f'<div class="flash bad">{esc(error)}</div>'
    for problem in problems:
        flash += f'<div class="flash bad">{esc(problem)}</div>'

    options = "".join(
        f'<option value="{c.letter}">{c.letter} — {esc(c.label)}</option>'
        for c in [__import__("ledger.provenance", fromlist=["SourceClass"]).SourceClass[k]
                  for k in ("A_PRIMARY", "B_COMPANY", "C_INDEPENDENT", "D_COMMERCIAL",
                            "E_RELEASE", "F_COMMUNITY")]
    )
    def field(key: str) -> str:
        label, kind, placeholder, required = _FIELD_BY_KEY[key]
        mark = '<span class="req">*</span>' if required else ""
        return (f'<div class="field"><label for="src-{key}">{esc(label)}{mark}</label>'
                f'<input type="{kind}" id="src-{key}" name="{key}" '
                f'placeholder="{esc(placeholder)}"{" required" if required else ""}></div>')

    def fields(*keys: str) -> str:
        return "".join(field(k) for k in keys)

    body = f"""<header class="masthead">
<h1>Sources</h1>
<p class="lede">Declared sources run beside the built-in ones; only a Class-A source can mark a finding verified.</p>
{_source_health(sources)}
</header>
{flash}
<section class="check">
<div class="check-head"><h2>Declared sources</h2>
<span class="head-note">{len(sources)} declared · sources/*.toml</span></div>
{_source_toolbar(sources)}
<div class="srclist" id="srclist">{rows}</div>
{_pager(len(sources), len(sources), "")}
</section>

<section class="check">
<div class="check-head"><h2>Company name mappings</h2></div>
{mapping_table}
<form class="inline" method="post" action="/entities/set">
<input type="text" name="ticker" placeholder="TICKER" maxlength="8" required autocomplete="off">
{mapping_inputs}
<button type="submit">Save mappings</button>
</form>
</section>

<div class="srcband">
<section class="check">
<div class="check-head"><h2>Scan shortcuts</h2></div>
<div class="presets">{_preset_chips()}</div>
{_pager(len(presets), len(presets), "", compact=True) if presets else ""}
<form class="inline" method="post" action="/presets/add">
<input type="text" name="ticker" placeholder="TICKER" maxlength="8" required
       autocomplete="off">
<input type="text" name="note" placeholder="What it is good for showing" autocomplete="off">
<button type="submit">Add shortcut</button>
</form>
</section>

<section class="check">
<div class="check-head"><h2>Declare a source</h2></div>
<form class="add" method="post" action="/sources/add">
<div class="fieldset"><span class="eyebrow">1 · What it is</span>
<div class="fields two">{fields("name", "note")}</div>
</div>
<div class="fieldset"><span class="eyebrow">2 · Where it lives</span>
<div class="fields">{fields("url")}</div>
<span class="sub"><code>{{entity}}</code> · <code>{{ticker}}</code> · <code>{{cik}}</code>
— <code>{{cik}}</code> is exact and needs no mapping</span>
<div class="fields two">
<div class="field"><label for="src-class-pick">Class</label>
<select id="src-class-pick" name="class">{options}</select></div>
<div class="field"><label for="src-kind">Format</label>
<select id="src-kind" name="kind"><option value="json">JSON</option>
<option value="rss">RSS / Atom</option></select></div>
</div>
</div>
<div class="fieldset"><span class="eyebrow">3 · Extraction</span>
<div class="rows">{fields("items", "title", "detail", "date", "link", "match")}</div>
</div>
<div class="fieldset"><span class="eyebrow">4 · Company names</span>
<div class="field"><label for="src-entities">One per line, TICKER = Name as the source spells it</label>
<textarea id="src-entities" name="entities" placeholder="TSLA = Tesla&#10;AAPL = Apple Inc"></textarea></div>
</div>
<div class="add-actions"><button type="submit">Add source</button></div>
</form>
</section>
</div>
{SOURCES_SCRIPT}"""
    return _page("Sources — Contour", body, current="/sources")


def _parse_entities(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" not in line:
            continue
        ticker, name = line.split("=", 1)
        if ticker.strip() and name.strip():
            out[ticker.strip().upper()] = name.strip()
    return out


def add_source(form: dict[str, list[str]]) -> bytes:
    def one(key: str) -> str:
        return (form.get(key, [""])[0] or "").strip()

    extract = {k: one(k) for k in ("items", "title", "detail", "date", "link", "match") if one(k)}
    table = {
        "name": one("name"),
        "url": one("url"),
        "class": one("class") or "F",
        "kind": one("kind") or "json",
        "note": one("note"),
        "entities": _parse_entities(one("entities")),
        "extract": extract,
    }
    try:
        source = append_source(table)
    except (SourceError, TypeError, OSError) as exc:
        return sources_page(error=f"Not added — {exc}")
    return sources_page(message=f"Added '{source.name}' as Class {source.klass.letter}. "
                                f"It will run on the next scan.")


def add_preset_route(client: EdgarClient, form: dict[str, list[str]]) -> bytes:
    ticker = (form.get("ticker", [""])[0] or "").strip().upper()
    note = (form.get("note", [""])[0] or "").strip()
    try:
        # Resolve before saving. A shortcut that resolves to nothing is worse
        # than no shortcut, and the register is the same one the scan uses.
        company = client.resolve(ticker)
    except EdgarError as exc:
        return sources_page(error=f"Not added — {exc}")
    try:
        add_preset(ticker, note or company.name)
    except (ValueError, OSError) as exc:
        return sources_page(error=f"Not added — {exc}")
    return sources_page(message=f"Added {ticker} ({company.name}) to the scan shortcuts.")


def set_entities_route(client: EdgarClient, form: dict[str, list[str]]) -> bytes:
    ticker = (form.get("ticker", [""])[0] or "").strip().upper()
    try:
        company = client.resolve(ticker)
    except EdgarError as exc:
        return sources_page(error=f"Not saved — {exc}")

    sources, _ = load_sources()
    mapping = {
        key: (form.get(key, [""])[0] or "")
        for key, _label in _mapped_keys(sources)
    }
    if not any(v.strip() for v in mapping.values()):
        return sources_page(error=f"Not saved — no mappings given for {ticker}.")
    try:
        set_entities(ticker, mapping)
    except (ValueError, OSError) as exc:
        return sources_page(error=f"Not saved — {exc}")
    named = ", ".join(k for k, v in mapping.items() if v.strip())
    return sources_page(
        message=f"Saved mappings for {ticker} ({company.name}): {named}. "
                "They apply on the next scan."
    )


def remove_preset_route(form: dict[str, list[str]]) -> bytes:
    ticker = (form.get("ticker", [""])[0] or "").strip().upper()
    try:
        remove_preset(ticker)
    except (ValueError, OSError) as exc:
        return sources_page(error=f"Not removed — {exc}")
    return sources_page(message=f"Removed {ticker} from the scan shortcuts.")


def tracked_page(message: str = "", error: str = "") -> bytes:
    """When each company was first scanned, and what has accumulated since."""
    with connect() as connection:
        rows = tracked_companies(connection)
        stats = coverage(connection)

    flash = f'<div class="flash">{esc(message)}</div>' if message else ""
    if error:
        flash += f'<div class="flash bad">{esc(error)}</div>'

    def delta_cell(ticker: str) -> str:
        with connect() as connection:
            d = latest_delta(connection, ticker)
        if d is None:
            return '<span class="quiet">first scan</span>'
        if d.quiet:
            return '<span class="quiet">no change</span>'
        parts = []
        if d.appeared:
            parts.append(f'<span class="new">+{len(d.appeared)} new</span>')
        if d.resolved:
            parts.append(f'<span class="gone">−{len(d.resolved)} gone</span>')
        return f'<span class="delta">{"".join(parts)}</span>'

    if rows:
        body_rows = "".join(
            f'<tr><td class="tk">{esc(t.ticker)}</td><td>{esc(t.company[:38])}</td>'
            f'<td class="num">{esc(t.tracked_since)}</td>'
            f'<td class="num">{esc(t.last_scan or "—")}</td>'
            f'<td class="num">{t.scans}</td>'
            f'<td class="num">{t.baseline_facts:,}</td>'
            f'<td class="num">{("+" + format(t.facts_added, ",")) if t.facts_added else "—"}</td>'
            f"<td>{delta_cell(t.ticker)}</td>"
            f'<td><form method="post" action="/untrack">'
            f'<input type="hidden" name="ticker" value="{esc(t.ticker)}">'
            f'<button class="ghost" type="submit">stop</button></form></td></tr>'
            for t in rows
        )
        table = (
            '<div class="maptable"><table><thead><tr>'
            '<th scope="col">Ticker</th><th scope="col">Company</th>'
            '<th scope="col">Baseline taken</th><th scope="col">Last scan</th>'
            '<th scope="col" class="num">Scans</th><th scope="col" class="num">Baseline figures</th>'
            '<th scope="col" class="num">Added since</th><th scope="col">Since last scan</th>'
            '<th scope="col"></th>'
            f"</tr></thead><tbody>{body_rows}</tbody></table></div>"
        )
    else:
        table = ('<p class="reason">Nothing tracked yet. Scan a company and press '
                 "<em>Add to watchlist</em>.</p>")

    daily_state = (
        f'<span class="quiet">Daily pass on — last run {esc(DAILY["last"] or "— hasn\u2019t run yet")}.</span>'
        if DAILY["on"] else
        '<span class="quiet">Daily pass off — start the server with --daily to enable it.</span>'
    )
    body = f"""<header class="masthead">
<h1>Watchlist</h1>
</header>
{flash}
<div class="rescan">
<form method="post" action="/rescan"><button type="submit">Rescan all</button></form>
{daily_state}
</div>
{table}
<footer class="meta-line">{_ledger_line()}</footer>"""
    return _page("Watchlist — Contour", body, current="/tracked")


def rescan_tracked(client: EdgarClient) -> list[tuple[str, object]]:
    """Rescan every tracked company and return what changed for each.

    Sequential on purpose — these hit SEC, and a burst of parallel requests is
    the fastest way to get an IP throttled mid-demo.
    """
    with connect() as connection:
        tickers = [t.ticker for t in tracked_companies(connection)]
    out: list[tuple[str, object]] = []
    for ticker in tickers:
        try:
            scan(client, ticker)
        except Exception as exc:  # noqa: BLE001 — one failure must not stop the rest
            out.append((ticker, f"scan failed — {type(exc).__name__}"))
            continue
        with connect() as connection:
            out.append((ticker, latest_delta(connection, ticker)))
    return out


def rescan_route(client: EdgarClient) -> bytes:
    results = rescan_tracked(client)
    if not results:
        return tracked_page(error="Nothing tracked yet — take a baseline first.")
    changed = sum(
        1 for _, d in results
        if hasattr(d, "quiet") and not d.quiet
    )
    return tracked_page(
        message=f"Rescanned {len(results)} tracked "
                f"{'company' if len(results) == 1 else 'companies'}. "
                + (f"{changed} changed since the previous scan."
                   if changed else "Nothing changed since the previous scan.")
    )


def _unmapped_sources(ticker: str) -> list:
    """Sources that need a name and have neither a stated nor a proposed one."""
    sources, _ = load_sources()
    return [
        s for s in sources
        if s.enabled and s.needs_entity
        and not s.entity_for(ticker) and not s.proposed_for(ticker)
    ]


def propose_entities_route(client: EdgarClient, form: dict[str, list[str]]) -> bytes:
    """Ask the model what this company is called in each unmapped source."""
    from ledger.authored import resolve_entities
    from ledger.config import propose_entity
    from datetime import date as _date

    ticker = (form.get("ticker", [""])[0] or "").strip().upper()
    try:
        company = client.resolve(ticker)
    except EdgarError as exc:
        return results(client, [ticker], notice=f"Could not resolve {ticker} — {exc}")

    pending = _unmapped_sources(ticker)
    if not pending:
        return results(client, [ticker],
                       notice=f"Every source already has a name for {ticker}.")

    resolved = resolve_entities(ticker, company.name, [s.name for s in pending])
    if not resolved.mappings:
        return results(client, [ticker],
                       notice=f"No names proposed for {ticker} — {resolved.reason}")
    for m in resolved.mappings:
        propose_entity(ticker, m.source, m.entity, confidence=m.confidence,
                       reasoning=m.reasoning, model=m.model,
                       on=_date.today().isoformat())
    named = ", ".join(f'{m.source} → "{m.entity}"' for m in resolved.mappings)
    return results(client, [ticker], notice=(
        f"{len(resolved.mappings)} name(s) proposed for {ticker}: {named}. "
        f"Findings from them are marked REPORTED, never VERIFIED, until confirmed."
    ))


def confirm_entity_route(client: EdgarClient, form: dict[str, list[str]]) -> bytes:
    from ledger.config import confirm_entity

    ticker = (form.get("ticker", [""])[0] or "").strip().upper()
    source = (form.get("source", [""])[0] or "").strip()
    entity = confirm_entity(ticker, source)
    note = (f'Confirmed: {ticker} is "{entity}" in {source}. Findings from it can '
            f"now be verified." if entity else f"Nothing to confirm for {ticker} in {source}.")
    return results(client, [ticker], notice=note)


def reject_entity_route(client: EdgarClient, form: dict[str, list[str]]) -> bytes:
    from ledger.config import reject_entity

    ticker = (form.get("ticker", [""])[0] or "").strip().upper()
    source = (form.get("source", [""])[0] or "").strip()
    reject_entity(ticker, source)
    return results(client, [ticker],
                   notice=f"Discarded the proposed name for {ticker} in {source}.")


def write_checks_route(client: EdgarClient, form: dict[str, list[str]]) -> bytes:
    """Ask the model for this company's checks and pin them.

    Deliberately explicit rather than automatic on every scan: a check whose
    definition moved between two scans makes the delta between them unreadable,
    and a scan that silently calls out to a model is not a scan a reader can
    reproduce.
    """
    from ledger import authored as A

    ticker = (form.get("ticker", [""])[0] or "").strip().upper()
    try:
        company = client.resolve(ticker)
    except EdgarError as exc:
        return results(client, [ticker], notice=f"Could not write checks — {exc}")
    try:
        profile = client.profile(company.cik)
        facts = client.company_facts(company.cik)
    except Exception as exc:  # noqa: BLE001
        return results(client, [ticker],
                       notice=f"Could not write checks — EDGAR unreachable ({type(exc).__name__})")

    written = A.write_checks(ticker, company.name, profile, facts)
    if not written.specs:
        detail = "; ".join(written.rejected[:3])
        return results(client, [ticker], notice=(
            f"No checks written for {ticker} — {written.reason}"
            + (f" ({detail})" if detail else "")
        ))
    A.save(ticker, written)
    note = f"{len(written.specs)} check(s) written for {ticker} by {written.model}"
    if written.rejected:
        note += f"; {len(written.rejected)} proposal(s) refused"
    return results(client, [ticker], notice=note)


def forget_checks_route(form: dict[str, list[str]]) -> bytes:
    from ledger import authored as A

    ticker = (form.get("ticker", [""])[0] or "").strip().upper()
    A.forget(ticker)
    return tracked_page()


# -- add-company wizard ----------------------------------------------------
#
# Three steps, in the order the roster is decided: code establishes what can
# apply, a model ranks what matters, a person settles it. The person's step is
# last and is the one that writes anything.


def _wizard_shell(step: int, title: str, body: str, sub: str = "") -> bytes:
    steps = ["Company", "Checks", "Baseline"]
    crumbs = "".join(
        f'<span class="crumb{" on" if i == step else ""}'
        f'{" done" if i < step else ""}">{i + 1}. {esc(name)}</span>'
        for i, name in enumerate(steps)
    )
    return _page(f"{title} — Contour", f"""<header class="masthead">
<h1>{esc(title)}</h1>
{f'<p class="lede">{sub}</p>' if sub else ""}
</header>
<div class="crumbs">{crumbs}</div>
{body}""", current="/add")


def scan_page() -> bytes:
    """/scan with no ticker.

    This used to fall through to landing(), which rendered the Overview page
    verbatim — same heading, same active nav item — so clicking Scan looked
    like nothing had happened at all.
    """
    ledger = _ledger_line()
    body = f"""<header class="masthead">
<h1>Scan</h1>
<p class="lede">Any of the ~10,400 SEC-registered tickers. Every figure is computed
from a filing fetched now and cites the accession it came from.</p>
{_form()}
</header>
<section class="check">
<div class="check-head"><h2>Watchlist</h2>
<a class="head-link" href="/tracked">Manage ›</a></div>
{_watchlist_table()}
</section>
{_recent_chips()}
{f'<footer>{esc(ledger)}</footer>' if ledger else ""}"""
    return _page("Scan — Contour", body, current="/scan")



# The picker is server-rendered and correct without this: every state below is
# also produced by a round trip. It exists so the commit bar answers a click
# immediately rather than a page later — picking a company and still reading
# "pick a company on each side" makes the page look broken.
PICKER_SCRIPT = """<script>
(function(){
  var f = document.querySelector('.picker-form');
  if (!f) return;
  var who = f.querySelector('.commit-who');
  var route = f.querySelector('.commit-route');
  function side(name){
    var raw = f.querySelector('input[name="' + name + '_raw"]');
    if (raw && raw.value.trim()) return {t: raw.value.trim().toUpperCase(), c: ''};
    var hit = f.querySelector('input[name="' + name + '"]:checked');
    return hit ? {t: hit.value, c: hit.getAttribute('data-company') || ''} : {t: '', c: ''};
  }
  function paint(){
    var a = side('a'), b = side('b');
    f.querySelectorAll('.js-side-tk').forEach(function(el){
      el.textContent = (el.getAttribute('data-side') === 'a' ? a.t : b.t) || '\u2014';
    });
    f.querySelectorAll('.pick').forEach(function(row){
      var input = row.querySelector('input');
      if (!input) return;
      var far = input.name === 'a' ? b.t : a.t;
      var taken = input.value === far && far !== '';
      row.classList.toggle('taken', taken);
      input.disabled = taken;
      if (taken) input.checked = false;
      var note = row.querySelector('.pick-note');
      if (taken && !note) {
        note = document.createElement('span');
        note.className = 'pick-note';
        note.textContent = 'on side ' + (input.name === 'a' ? 'B' : 'A');
        row.appendChild(note);
      } else if (!taken && note) {
        note.remove();
      }
    });
    var ready = a.t && b.t && a.t !== b.t;
    who.innerHTML = '';
    route.innerHTML = '';
    if (!ready) {
      who.appendChild(sub('Pick a company on each side.'));
      return;
    }
    var pair = document.createElement('span');
    pair.className = 'commit-pair';
    pair.appendChild(bold(a.t));
    var vs = document.createElement('span');
    vs.className = 'vs';
    vs.textContent = 'vs';
    pair.appendChild(vs);
    pair.appendChild(bold(b.t));
    who.appendChild(pair);
    who.appendChild(sub((a.c || a.t) + ' \u00b7 ' + (b.c || b.t)));
    var code = document.createElement('code');
    code.textContent = '/compare?a=' + a.t + '&b=' + b.t;
    route.appendChild(code);
    route.appendChild(sub('one row per check \u00b7 union of both rosters'));
  }
  function sub(text){
    var el = document.createElement('span');
    el.className = 'sub';
    el.textContent = text;
    return el;
  }
  function bold(text){
    var el = document.createElement('b');
    el.className = 'tk';
    el.textContent = text;
    return el;
  }
  // A watchlist of a hundred companies is two 7,000px columns of radio cards.
  // Filtering in place beats paging here: on a picker you already know the
  // ticker you want, and a page number is one more thing to hunt through.
  function filter(box){
    var side = box.getAttribute('data-side');
    var term = box.value.trim().toLowerCase();
    var shown = 0, total = 0;
    f.querySelectorAll('.pick').forEach(function(row){
      var input = row.querySelector('input');
      if (!input || input.name !== side) return;
      total++;
      var hit = !term || row.textContent.toLowerCase().indexOf(term) !== -1;
      row.hidden = !hit;
      if (hit) shown++;
    });
    var count = f.querySelector('.js-pick-count[data-side="' + side + '"]');
    if (count) {
      count.textContent = term ? shown + ' of ' + total + ' shown'
                               : total + ' tracked';
    }
  }
  f.querySelectorAll('.js-pick-filter').forEach(function(box){
    box.addEventListener('input', function(){ filter(box); });
  });
  f.addEventListener('change', paint);
  f.addEventListener('input', paint);
  // An empty text box still submits as a_raw=&b_raw=, so the address bar
  // contradicts the route the commit bar just promised. Drop the empties.
  f.addEventListener('submit', function(){
    f.querySelectorAll('input[type=text]').forEach(function(box){
      if (!box.value.trim()) box.disabled = true;
    });
  });
  paint();
})();
</script>"""

def _pair(query: dict) -> tuple[str, str]:
    """Read one ticker per side.

    Each side offers two inputs — the watchlist radio and the untracked box.
    Typing is the more deliberate act, so a filled box wins over a radio that
    may simply have carried over from the last render.
    """
    def one(key: str) -> str:
        for name in (f"{key}_raw", key):
            for value in query.get(name, []):
                if value.strip():
                    return value.strip().upper()
        return ""
    return one("a"), one("b")


def _side(name: str, label: str, chosen: str, other: str) -> str:
    """One column of the compare picker.

    A company already picked on the far side is shown but not selectable: two
    columns of the same company is a comparison of nothing, and disabling it
    where it sits explains itself better than an error after the fact.
    """
    rows = ""
    for t in _tracked():
        taken = t.ticker == other
        note = (f'<span class="pick-note">on side {"A" if name == "b" else "B"}</span>'
                if taken else "")
        rows += (f'<label class="pick{" taken" if taken else ""}">'
                 f'<input type="radio" name="{name}" value="{esc(t.ticker)}"'
                 f' data-company="{esc(t.company)}"'
                 f'{" checked" if t.ticker == chosen and not taken else ""}'
                 f'{" disabled" if taken else ""}>'
                 f'<span class="pick-body"><span class="pick-id">'
                 f'<b class="tk">{esc(t.ticker)}</b>'
                 f"<b>{esc(t.company)}</b></span>"
                 f'<span class="sub">last scan {esc(t.last_scan or "never")} · '
                 f"{t.scans:,} scans · {t.baseline_facts:,} baseline figures</span></span>"
                 f"{note}</label>")
    if not rows:
        rows = '<p class="note">Nothing tracked yet.</p>'
    total = len(_tracked())
    return f"""<div class="side">
<div class="side-head"><span class="eyebrow">{esc(label)}</span>
<span class="tk js-side-tk" data-side="{name}">{esc(chosen or "—")}</span></div>
<div class="pick-filter">
<input type="search" class="js-pick-filter" data-side="{name}"
       placeholder="Filter tracked" autocomplete="off" aria-label="Filter {esc(label)}">
<span class="sub js-pick-count" data-side="{name}">{total} tracked</span>
</div>
<div class="picks">{rows}</div>
<div class="pick-alt"><span class="eyebrow">or untracked</span>
<input type="text" name="{name}_raw" placeholder="Ticker" maxlength="8" autocomplete="off">
<span class="sub">cold scan — no baseline</span></div>
</div>"""


def compare_page(a: str = "", b: str = "", error: str = "") -> bytes:
    """Pick two tracked companies. The result is /compare with both tickers.

    Comparison used to be the second field of the scan form, which made this
    page a relabelled /scan. You are almost always comparing companies you
    already track, so the watchlist — not a text box — is the instrument.
    """
    tracked = {t.ticker: t.company for t in _tracked()}
    ready = bool(a and b and a != b)
    pair = (f'<span class="commit-pair"><b class="tk">{esc(a)}</b>'
            f'<span class="vs">vs</span><b class="tk">{esc(b)}</b></span>'
            f'<span class="sub">{esc(tracked.get(a, a))} · {esc(tracked.get(b, b))}</span>'
            if ready else
            '<span class="sub">Pick a company on each side.</span>')
    # Both spans are always rendered, even empty. The picker script fills them
    # on every change; a span that only exists once a pair is chosen is a null
    # the first keystroke trips over.
    route = (f'<code>/compare?a={esc(a)}&amp;b={esc(b)}</code>'
             f'<span class="sub">one row per check · union of both rosters</span>'
             if ready else "")
    flash = f'<div class="flash bad">{esc(error)}</div>' if error else ""
    body = f"""<header class="masthead">
<h1>Compare</h1>
<p class="lede">Two companies, one row per check. A check flagged on one side and
unavailable on the other reads as a single horizontal glance.</p>
</header>
{flash}
<form class="picker-form" method="get" action="/compare">
<div class="picker">
{_side("a", "Side A", a, b)}
<div class="vs-rail"><span class="vs">vs</span></div>
{_side("b", "Side B", b, a)}
</div>
<div class="commit"><span class="commit-who">{pair}</span>
<span class="commit-route">{route}</span>
<button type="submit">Compare</button></div>
</form>
{PICKER_SCRIPT}"""
    return _page("Compare — Contour", body, current="/compare")


def add_page(error: str = "") -> bytes:
    flash = f'<div class="error"><p>{esc(error)}</p></div>' if error else ""
    body = f"""{flash}
<form class="scan" method="post" action="/add/review">
<input type="text" name="ticker" placeholder="Ticker" autocomplete="off" autofocus required>
<button type="submit">Continue</button>
</form>"""
    return _wizard_shell(0, "Add a company", body,
                         "Look up a registrant, choose its checks, and take a baseline.")


def add_review_route(client: EdgarClient, form: dict[str, list[str]]) -> bytes:
    """Step 2 — what applies, what is recommended, and what was ruled out."""
    from ledger.authored import available_concepts
    from ledger.catalogue import applicable, recommend

    ticker = (form.get("ticker", [""])[0] or "").strip().upper()
    if not ticker:
        return add_page(error="Enter a ticker.")
    try:
        company = client.resolve(ticker)
    except EdgarError as exc:
        return add_page(error=f"{ticker} — {exc}")
    try:
        profile = client.profile(company.cik)
        concepts = set(available_concepts(client.company_facts(company.cik)))
    except Exception as exc:  # noqa: BLE001
        return add_page(error=f"Could not read {ticker} from EDGAR — {type(exc).__name__}")

    sic = profile.get("sic", "")
    label = profile.get("sic_description") or ""
    eligible, excluded = applicable(sic, concepts)
    rec = recommend(ticker, company.name, sic, label, eligible)
    chosen = set(rec.keys)

    rows = []
    for entry in eligible:
        on = entry.key in chosen
        why = (rec.why or {}).get(entry.key) or entry.spec.rationale
        rows.append(
            f'<label class="pick">'
            f'<input type="checkbox" name="check" value="{esc(entry.key)}"'
            f'{" checked" if on else ""}>'
            f'<span class="pick-body"><b>{esc(entry.spec.title)}</b>'
            f'<span class="pick-why">{esc(why)}</span></span>'
            f'<span class="status {esc(entry.spec.severity)}">{esc(entry.spec.severity)}</span>'
            f"</label>"
        )
    # These used to be bare list items. A check that cannot run is the same
    # class of fact as one that can, so it gets the same row and states why.
    ruled_out = "".join(
        f'<div class="pick out"><span class="pick-body">'
        f"<b>{esc(e.spec.title)}</b>"
        f'<span class="pick-why">{esc(reason)}</span></span>'
        f'<span class="pick-tag">unavailable</span></div>'
        for e, reason in excluded
    )
    # The fallback reason is a fragment ("no Anthropic credentials — ordered by
    # severity instead"); it needs framing to read as a sentence beside the rest.
    source_line = (
        f"Ranked by {esc(rec.model)}."
        if rec.from_model
        else f"Pre-selection {esc(rec.reason or 'ordered by severity')}."
    )

    body = f"""<div class="found"><b>{esc(company.name)}</b>
<span class="sub">{esc(ticker)} · CIK {company.cik} · {esc(label)} (SIC {esc(sic)})</span></div>
<form method="post" action="/add/confirm">
<input type="hidden" name="ticker" value="{esc(ticker)}">
<p class="lede">{len(eligible)} of {len(eligible) + len(excluded)} catalogue checks
can run against this filer. {source_line} Change anything below — the roster is
yours, and nothing is saved until you confirm.</p>
<div class="picks">{"".join(rows)}</div>
<details class="roster"><summary>{len(excluded)} cannot run against this filer</summary>
<div class="picks out">{ruled_out}</div></details>
<div class="wizard-actions">
<button type="submit">Add {esc(ticker)} and take a baseline</button>
<a class="ghost-link" href="/add">Start over</a>
</div>
</form>"""
    return _wizard_shell(1, f"Checks for {ticker}", body)


def add_confirm_route(client: EdgarClient, form: dict[str, list[str]]) -> bytes:
    """Step 3 — pin the roster the person settled on, then take the baseline."""
    from ledger import authored as A
    from ledger.catalogue import by_key

    ticker = (form.get("ticker", [""])[0] or "").strip().upper()
    keys = [k.strip() for k in form.get("check", []) if k.strip()]
    if not ticker:
        return add_page(error="Enter a ticker.")

    specs = tuple(e.spec for k in keys if (e := by_key(k)))
    if specs:
        A.save(ticker, A.Authored(specs=specs, model="catalogue",
                                  written_on=date.today().isoformat()))
    else:
        A.forget(ticker)

    return track_route(client, {"ticker": [ticker]})


def track_route(client: EdgarClient, form: dict[str, list[str]]) -> bytes:
    ticker = (form.get("ticker", [""])[0] or "").strip().upper()
    try:
        company = client.resolve(ticker)
    except EdgarError as exc:
        return tracked_page(error=f"Not tracked — {exc}")
    # Take the baseline from a scan run now, so the recorded figure count is
    # what the ledger actually held at that moment.
    report = scan(client, ticker)
    with connect() as connection:
        facts = connection.execute(
            "SELECT COUNT(*) n FROM observations WHERE ticker = ?", (ticker,)
        ).fetchone()["n"]
        last = connection.execute(
            "SELECT id FROM scans WHERE ticker = ? ORDER BY id DESC LIMIT 1", (ticker,)
        ).fetchone()
        added = track_company(
            connection, ticker=ticker, cik=company.cik, company=company.name,
            baseline_scan=int(last["id"]) if last else 0, baseline_facts=facts,
        )
    if not added:
        return tracked_page(error=f"{ticker} was already tracked; the baseline is unchanged.")
    return tracked_page(
        message=f"Baseline taken for {ticker} ({company.name}): {facts:,} as-filed "
                f"figures, {report.findings} findings across {len(report.checks)} checks."
    )


def untrack_route(form: dict[str, list[str]]) -> bytes:
    ticker = (form.get("ticker", [""])[0] or "").strip().upper()
    with connect() as connection:
        removed = untrack_company(connection, ticker)
    if not removed:
        return tracked_page(error=f"{ticker} was not tracked.")
    return tracked_page(message=f"Stopped tracking {ticker}. Its recorded figures are kept.")


class Handler(BaseHTTPRequestHandler):
    client: EdgarClient

    def _api(self, path: str, query: dict) -> bytes | None:
        """JSON for anything that can call HTTP. Returns None if not an API path."""
        if path == "/api":
            return dumps(index_dict())
        if path == "/api/scan":
            ticker = (query.get("ticker") or query.get("a") or [""])[0].strip()
            if not ticker:
                return dumps({"error": "ticker is required, e.g. /api/scan?ticker=TSLA"})
            try:
                return dumps(report_dict(scan(self.client, ticker)))
            except EdgarError as exc:
                return dumps({"error": str(exc), "ticker": ticker.upper()})
        if path == "/api/tracked":
            with connect() as connection:
                return dumps(tracked_dict(tracked_companies(connection)))
        if path == "/api/changes":
            ticker = (query.get("ticker") or [""])[0].strip()
            if not ticker:
                return dumps({"error": "ticker is required, e.g. /api/changes?ticker=TSLA"})
            with connect() as connection:
                return dumps(delta_dict(ticker, latest_delta(connection, ticker)))
        if path == "/api/sources":
            return dumps(sources_dict(*load_sources()))
        return None

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        if parsed.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if parsed.path.startswith("/api"):
            try:
                payload = self._api(parsed.path, parse_qs(parsed.query))
            except Exception as exc:  # noqa: BLE001 — an API must answer, not hang
                payload = dumps({"error": f"{type(exc).__name__}: {exc}"})
            if payload is not None:
                self._respond(payload, "application/json; charset=utf-8")
                return
        try:
            if parsed.path == "/tracked":
                payload = tracked_page()
            elif parsed.path == "/sources":
                payload = sources_page()
            elif parsed.path == "/add":
                payload = add_page()
            elif parsed.path == "/compare":
                query = parse_qs(parsed.query)
                a, b = _pair(query)
                if a and b and a == b:
                    payload = compare_page(a, "", error=f"{a} cannot be compared "
                                           "with itself — pick a second company.")
                elif a and b:
                    payload = results(self.client, [a, b], compare=True)
                else:
                    payload = compare_page(a, b)
            elif parsed.path == "/scan":
                query = parse_qs(parsed.query)
                tickers = [
                    t.strip().upper()
                    for key in ("a", "b")
                    for t in query.get(key, [])
                    if t.strip()
                ]
                # A pair belongs to /compare now. Old links and bookmarks still
                # carry ?a=&b=, so send them on rather than rendering a second
                # comparison surface under the scan route.
                if len(tickers) > 1:
                    self.send_response(302)
                    self.send_header(
                        "Location", f"/compare?a={quote(tickers[0])}&b={quote(tickers[1])}")
                    self.end_headers()
                    return
                payload = results(self.client, tickers[:1]) if tickers else scan_page()
            else:
                payload = landing()
        except Exception:  # noqa: BLE001 — never return a bare 500 to the browser
            payload = _page("Error", f'<div class="error"><p>{esc(traceback.format_exc()[-600:])}</p></div>')
        self._respond(payload)

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode("utf-8")) if length else {}
            if parsed.path == "/sources/add":
                payload = add_source(form)
            elif parsed.path == "/presets/add":
                payload = add_preset_route(self.client, form)
            elif parsed.path == "/rescan":
                payload = rescan_route(self.client)
            elif parsed.path == "/track":
                payload = track_route(self.client, form)
            elif parsed.path == "/untrack":
                payload = untrack_route(form)
            elif parsed.path == "/authored/write":
                payload = write_checks_route(self.client, form)
            elif parsed.path == "/add/review":
                payload = add_review_route(self.client, form)
            elif parsed.path == "/add/confirm":
                payload = add_confirm_route(self.client, form)
            elif parsed.path == "/entities/propose":
                payload = propose_entities_route(self.client, form)
            elif parsed.path == "/entities/confirm":
                payload = confirm_entity_route(self.client, form)
            elif parsed.path == "/entities/reject":
                payload = reject_entity_route(self.client, form)
            elif parsed.path == "/authored/forget":
                payload = forget_checks_route(form)
            elif parsed.path == "/entities/set":
                payload = set_entities_route(self.client, form)
            elif parsed.path == "/presets/remove":
                payload = remove_preset_route(form)
            else:
                payload = sources_page()
        except Exception:  # noqa: BLE001 — never return a bare 500 to the browser
            payload = _page("Error", f'<div class="error"><p>{esc(traceback.format_exc()[-600:])}</p></div>')
        self._respond(payload)

    def _respond(self, payload: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        # Every page is generated fresh from a scan run seconds ago. A cached
        # copy is always wrong, and a stale page during a live demo is the worst
        # way to find that out.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:
        print(f"  {self.address_string()} {fmt % args}")


def serve(port: int = 8000, *, daily: bool = False) -> None:
    Handler.client = EdgarClient()
    if daily:
        DAILY["on"] = True
        threading.Thread(
            target=_daily_loop, args=(Handler.client,), daemon=True
        ).start()
        print("Daily pass enabled — tracked companies rescanned every 24h.")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Contour — http://127.0.0.1:{port}")
    print("Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()

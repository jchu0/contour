"""Versioned, point-in-time state.

Observations are stored *as filed*, never overwritten. That is the whole design:
when a company reports a figure for a period and later reports a different
figure for the same period, both rows survive and the disagreement becomes
visible. Overwriting would make a restatement indistinguishable from a routine
update, and restatements are among the least-announced things a company does.

SQLite because it is in the standard library and the file is the artifact.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ledger import profile

DB_PATH = profile.data_dir() / "ledger.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id          INTEGER PRIMARY KEY,
    ticker      TEXT NOT NULL,
    cik         INTEGER NOT NULL,
    company     TEXT NOT NULL,
    scanned_at  TEXT NOT NULL,
    findings    INTEGER NOT NULL DEFAULT 0,
    unavailable INTEGER NOT NULL DEFAULT 0,
    ran         INTEGER NOT NULL DEFAULT 0,
    not_applicable INTEGER NOT NULL DEFAULT 0
);

/* One row per (period, metric) per filing. The unique key includes `filed`
   so a restated figure lands beside its predecessor rather than replacing it. */
CREATE TABLE IF NOT EXISTS observations (
    ticker      TEXT NOT NULL,
    cik         INTEGER NOT NULL,
    metric      TEXT NOT NULL,
    period_end  TEXT NOT NULL,
    value       REAL NOT NULL,
    filed       TEXT NOT NULL,
    form        TEXT NOT NULL DEFAULT '',
    first_seen  TEXT NOT NULL,
    PRIMARY KEY (ticker, metric, period_end, filed)
);

CREATE INDEX IF NOT EXISTS observations_period
    ON observations (ticker, metric, period_end);
"""


@dataclass(frozen=True)
class Restatement:
    metric: str
    period_end: str
    original: float
    original_filed: str
    revised: float
    revised_filed: str

    @property
    def change(self) -> float:
        return self.revised - self.original

    @property
    def change_pct(self) -> float | None:
        return None if not self.original else (self.revised - self.original) / abs(self.original) * 100


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(_SCHEMA)
    return connection


def _ensure_scan_columns(connection: sqlite3.Connection) -> None:
    """A scan recorded only what it found and what failed.

    How many checks ran, and how many were out of scope, were never stored — so
    nothing outside a live report could say what a scan actually covered.
    """
    columns = {r["name"] for r in connection.execute("PRAGMA table_info(scans)")}
    for name in ("ran", "not_applicable"):
        if name not in columns:
            connection.execute(
                f"ALTER TABLE scans ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
    connection.commit()


def watchlist_coverage(connection: sqlite3.Connection,
                       tickers: list[str]) -> dict | None:
    """The most recent scan of each company, added up.

    Counts only scans that recorded coverage. A company last scanned before
    those columns existed is reported as unmeasured rather than folded in as a
    zero, which would read as "nothing failed" when it means "not known".
    """
    if not tickers:
        return None
    _ensure_scan_columns(connection)
    marks = ",".join("?" * len(tickers))
    rows = connection.execute(
        f"SELECT s.ticker, s.ran, s.not_applicable, s.unavailable, s.findings"
        f" FROM scans s JOIN (SELECT ticker, MAX(id) AS id FROM scans"
        f"   WHERE ticker IN ({marks}) GROUP BY ticker) latest"
        f"   ON latest.id = s.id",
        [t.upper() for t in tickers],
    ).fetchall()
    measured = [r for r in rows if r["ran"]]
    if not measured:
        return {"companies": 0, "unmeasured": len(rows), "ran": 0,
                "unavailable": 0, "not_applicable": 0, "findings": 0}
    return {
        "companies": len(measured),
        "unmeasured": len(rows) - len(measured),
        "ran": sum(r["ran"] for r in measured),
        "unavailable": sum(r["unavailable"] for r in measured),
        "not_applicable": sum(r["not_applicable"] for r in measured),
        "findings": sum(r["findings"] for r in measured),
    }


def record_scan(
    connection: sqlite3.Connection,
    *,
    ticker: str,
    cik: int,
    company: str,
    findings: int,
    unavailable: int,
    ran: int = 0,
    not_applicable: int = 0,
) -> int:
    _ensure_scan_columns(connection)
    cursor = connection.execute(
        "INSERT INTO scans (ticker, cik, company, scanned_at, findings, unavailable,"
        " ran, not_applicable) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ticker.upper(), cik, company, date.today().isoformat(), findings, unavailable,
         ran, not_applicable),
    )
    connection.commit()
    return int(cursor.lastrowid or 0)


def record_observations(
    connection: sqlite3.Connection,
    ticker: str,
    cik: int,
    rows: list[tuple[str, str, float, str, str]],
) -> int:
    """rows: (metric, period_end, value, filed, form). Existing rows are left alone."""
    today = date.today().isoformat()
    before = connection.execute("SELECT COUNT(*) FROM observations WHERE ticker = ?",
                                (ticker.upper(),)).fetchone()[0]
    connection.executemany(
        "INSERT OR IGNORE INTO observations"
        " (ticker, cik, metric, period_end, value, filed, form, first_seen)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(ticker.upper(), cik, m, p, v, f, form, today) for m, p, v, f, form in rows],
    )
    connection.commit()
    after = connection.execute("SELECT COUNT(*) FROM observations WHERE ticker = ?",
                               (ticker.upper(),)).fetchone()[0]
    return after - before


# Per-share figures are restated wholesale by a stock split, which is not a
# revision of anything. Apple's Q3 2020 EPS moved $2.58 -> $0.65 across filings:
# a 4-for-1 split, not a correction. Reporting that as a restatement would be a
# confident accusation about a routine corporate action.
SPLIT_SENSITIVE = ("eps",)


def restatements(
    connection: sqlite3.Connection,
    ticker: str,
    *,
    tolerance: float = 0.005,
    exclude: tuple[str, ...] = SPLIT_SENSITIVE,
) -> list[Restatement]:
    """Periods whose reported value changed between filings.

    Compares the earliest and latest filed value for each (metric, period). The
    tolerance absorbs rounding between a figure presented in millions and the
    same figure presented in thousands.
    """
    rows = connection.execute(
        "SELECT metric, period_end, value, filed FROM observations"
        " WHERE ticker = ? ORDER BY metric, period_end, filed",
        (ticker.upper(),),
    ).fetchall()
    rows = [r for r in rows if r["metric"] not in exclude]

    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault((row["metric"], row["period_end"]), []).append(row)

    out: list[Restatement] = []
    for (metric, period), versions in grouped.items():
        if len(versions) < 2:
            continue
        first, last = versions[0], versions[-1]
        original, revised = first["value"], last["value"]
        if not original:
            continue
        if abs(revised - original) / abs(original) <= tolerance:
            continue
        out.append(Restatement(
            metric=metric, period_end=period,
            original=original, original_filed=first["filed"],
            revised=revised, revised_filed=last["filed"],
        ))
    return sorted(out, key=lambda r: r.period_end, reverse=True)


def scan_history(connection: sqlite3.Connection, ticker: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
    if ticker:
        return connection.execute(
            "SELECT * FROM scans WHERE ticker = ? ORDER BY id DESC LIMIT ?",
            (ticker.upper(), limit),
        ).fetchall()
    return connection.execute(
        "SELECT * FROM scans ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def coverage(connection: sqlite3.Connection) -> dict[str, int]:
    with closing(connection.cursor()) as cursor:
        return {
            "companies": cursor.execute("SELECT COUNT(DISTINCT ticker) FROM observations").fetchone()[0],
            "observations": cursor.execute("SELECT COUNT(*) FROM observations").fetchone()[0],
            "scans": cursor.execute("SELECT COUNT(*) FROM scans").fetchone()[0],
            "periods": cursor.execute("SELECT COUNT(DISTINCT period_end) FROM observations").fetchone()[0],
        }


# -- tracked companies -----------------------------------------------------

_TRACKED_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracked (
    ticker         TEXT PRIMARY KEY,
    cik            INTEGER NOT NULL,
    company        TEXT NOT NULL,
    tracked_since  TEXT NOT NULL,
    baseline_scan  INTEGER,
    baseline_facts INTEGER NOT NULL DEFAULT 0,
    note           TEXT NOT NULL DEFAULT '',
    cadence        TEXT NOT NULL DEFAULT 'daily'
);
"""


@dataclass(frozen=True)
class Tracked:
    ticker: str
    company: str
    cik: int
    tracked_since: str
    baseline_facts: int
    scans: int
    last_scan: str | None
    facts_now: int
    cadence: str = "daily"

    @property
    def facts_added(self) -> int:
        """Figures the ledger has picked up since the baseline was taken."""
        return max(0, self.facts_now - self.baseline_facts)

    def due(self, today: date | None = None) -> bool:
        """Has enough time passed for the background pass to revisit this one?

        A company with no scan on record is always due: never scanned is not
        the same as scanned recently, and treating it as up to date would hide
        the gap rather than close it.
        """
        every = CADENCES.get(self.cadence, 1)
        if every is None:
            return False
        if not self.last_scan:
            return True
        try:
            last = date.fromisoformat(self.last_scan[:10])
        except ValueError:
            return True
        return ((today or date.today()) - last).days >= every


# How often the background pass should revisit a company. `manual` means it
# never does — the company stays tracked and is rescanned only on request.
CADENCES: dict[str, int | None] = {
    "daily": 1,
    "weekly": 7,
    "monthly": 30,
    "manual": None,
}


def _ensure_tracked(connection: sqlite3.Connection) -> None:
    connection.executescript(_TRACKED_SCHEMA)
    # CREATE TABLE IF NOT EXISTS leaves an existing table alone, so a database
    # written before cadence existed needs the column added explicitly.
    columns = {r["name"] for r in connection.execute("PRAGMA table_info(tracked)")}
    if "cadence" not in columns:
        connection.execute(
            "ALTER TABLE tracked ADD COLUMN cadence TEXT NOT NULL DEFAULT 'daily'")
        connection.commit()


def set_cadence(connection: sqlite3.Connection, ticker: str, cadence: str) -> bool:
    if cadence not in CADENCES:
        raise ValueError(f"{cadence} is not a cadence")
    _ensure_tracked(connection)
    cur = connection.execute(
        "UPDATE tracked SET cadence = ? WHERE ticker = ?", (cadence, ticker.upper()))
    connection.commit()
    return cur.rowcount > 0


def track_company(
    connection: sqlite3.Connection,
    *,
    ticker: str,
    cik: int,
    company: str,
    baseline_scan: int,
    baseline_facts: int,
    note: str = "",
) -> bool:
    """Record the baseline. Returns False if the company was already tracked —
    a baseline is taken once and never overwritten, or it is not a baseline."""
    _ensure_tracked(connection)
    existing = connection.execute(
        "SELECT 1 FROM tracked WHERE ticker = ?", (ticker.upper(),)
    ).fetchone()
    if existing:
        return False
    connection.execute(
        "INSERT INTO tracked (ticker, cik, company, tracked_since, baseline_scan,"
        " baseline_facts, note) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ticker.upper(), cik, company, date.today().isoformat(), baseline_scan,
         baseline_facts, note),
    )
    connection.commit()
    return True


def untrack_company(connection: sqlite3.Connection, ticker: str) -> bool:
    _ensure_tracked(connection)
    cur = connection.execute("DELETE FROM tracked WHERE ticker = ?", (ticker.upper(),))
    connection.commit()
    return cur.rowcount > 0


def _tracked_row(connection: sqlite3.Connection, row: sqlite3.Row) -> Tracked:
    stats = connection.execute(
        "SELECT COUNT(*) n, MAX(scanned_at) last FROM scans WHERE ticker = ?",
        (row["ticker"],),
    ).fetchone()
    facts = connection.execute(
        "SELECT COUNT(*) n FROM observations WHERE ticker = ?", (row["ticker"],)
    ).fetchone()["n"]
    return Tracked(
        ticker=row["ticker"], company=row["company"], cik=row["cik"],
        tracked_since=row["tracked_since"], baseline_facts=row["baseline_facts"],
        scans=stats["n"], last_scan=stats["last"], facts_now=facts,
        cadence=(row["cadence"] if "cadence" in row.keys() else "daily") or "daily",
    )


def tracked_companies(connection: sqlite3.Connection) -> list[Tracked]:
    _ensure_tracked(connection)
    rows = connection.execute("SELECT * FROM tracked ORDER BY tracked_since, ticker").fetchall()
    return [_tracked_row(connection, r) for r in rows]


def tracking(connection: sqlite3.Connection, ticker: str) -> Tracked | None:
    _ensure_tracked(connection)
    row = connection.execute(
        "SELECT * FROM tracked WHERE ticker = ?", (ticker.upper(),)
    ).fetchone()
    return _tracked_row(connection, row) if row else None


# -- what changed between scans -------------------------------------------

_FINDINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_findings (
    scan_id     INTEGER NOT NULL,
    ticker      TEXT NOT NULL,
    check_key   TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    headline    TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT '',
    klass       TEXT NOT NULL DEFAULT '',
    source_date TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (scan_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS scan_findings_ticker ON scan_findings (ticker, scan_id);
"""


@dataclass(frozen=True)
class Delta:
    appeared: list[tuple[str, str]]   # (check_key, headline)
    resolved: list[tuple[str, str]]
    from_scan: str
    to_scan: str

    @property
    def quiet(self) -> bool:
        return not self.appeared and not self.resolved


def _ensure_findings(connection: sqlite3.Connection) -> None:
    connection.executescript(_FINDINGS_SCHEMA)
    # A finding used to be recorded without where it came from, so nothing
    # outside a live scan could link back to the source. Databases written
    # before that get the columns added rather than rebuilt.
    columns = {r["name"] for r in connection.execute("PRAGMA table_info(scan_findings)")}
    for name in ("url", "klass", "source_date"):
        if name not in columns:
            connection.execute(
                f"ALTER TABLE scan_findings ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
    connection.commit()


def record_findings(
    connection: sqlite3.Connection,
    scan_id: int,
    ticker: str,
    rows: list[tuple],
) -> None:
    """rows: (check_key, headline, severity[, url, klass, source_date]).

    Fingerprint is what makes a finding 'the same finding' across scans — the
    check it came from plus its headline, which carries the figures. The
    source travels with it so a page that is not running a scan can still link
    back to what was read.
    """
    _ensure_findings(connection)
    connection.executemany(
        "INSERT OR IGNORE INTO scan_findings"
        " (scan_id, ticker, check_key, fingerprint, headline, severity,"
        "  url, klass, source_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(scan_id, ticker.upper(), row[0], f"{row[0]}|{row[1]}", row[1], row[2],
          row[3] if len(row) > 3 else "", row[4] if len(row) > 4 else "",
          row[5] if len(row) > 5 else "")
         for row in rows],
    )
    connection.commit()


def recent_source_items(connection: sqlite3.Connection, tickers: list[str],
                        limit: int = 8) -> list[dict]:
    """Latest corroborating hits for these companies, newest first.

    Class C-F only: this is what the declared feeds turned up, not a finding
    computed from a filing, and the two must not share a list.
    """
    if not tickers:
        return []
    _ensure_findings(connection)
    marks = ",".join("?" * len(tickers))
    rows = connection.execute(
        f"SELECT ticker, headline, url, klass, source_date, MAX(scan_id) AS seen"
        f" FROM scan_findings WHERE ticker IN ({marks}) AND url != ''"
        f"   AND klass NOT IN ('A', 'B')"
        f" GROUP BY url ORDER BY source_date DESC, seen DESC LIMIT ?",
        [t.upper() for t in tickers] + [limit],
    ).fetchall()
    return [dict(r) for r in rows]


def latest_delta(connection: sqlite3.Connection, ticker: str) -> Delta | None:
    """What appeared and what went away between the last two scans."""
    _ensure_findings(connection)
    scans = connection.execute(
        "SELECT DISTINCT s.id, s.scanned_at FROM scans s"
        " JOIN scan_findings f ON f.scan_id = s.id"
        " WHERE s.ticker = ? ORDER BY s.id DESC LIMIT 2",
        (ticker.upper(),),
    ).fetchall()
    if len(scans) < 2:
        return None
    newer, older = scans[0], scans[1]

    def rows(scan_id: int) -> dict[str, tuple[str, str]]:
        return {
            r["fingerprint"]: (r["check_key"], r["headline"])
            for r in connection.execute(
                "SELECT check_key, fingerprint, headline FROM scan_findings"
                " WHERE scan_id = ?", (scan_id,)
            )
        }

    now, before = rows(newer["id"]), rows(older["id"])
    return Delta(
        appeared=[v for k, v in now.items() if k not in before],
        resolved=[v for k, v in before.items() if k not in now],
        from_scan=older["scanned_at"],
        to_scan=newer["scanned_at"],
    )

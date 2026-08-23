#!/usr/bin/env python3
"""Manage the ledger's accumulated state.

    scripts/profile.py list                 what exists, live and per profile
    scripts/profile.py archive              copy the live state to archive/<stamp>/
    scripts/profile.py reset <name>         empty one profile's state (never live)
    scripts/profile.py restore <stamp>      copy an archive back over live

Archiving copies; it never removes. Resetting refuses to touch the live
profile at all — the way to get a clean slate is a fresh profile, not a
deletion of the state a demo depends on.

    CONTOUR_PROFILE=dev ./.venv/bin/python scripts/serve.py --port 8077
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ARCHIVE = Path("archive")
PROFILES = Path("profiles")
LIVE = [Path("data/ledger.db"), Path("config")]


def _rows(db: Path) -> str:
    if not db.exists():
        return "no database"
    try:
        con = sqlite3.connect(db)
        bits = []
        for table in ("scans", "observations", "tracked"):
            try:
                bits.append(f"{con.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]:,} {table}")
            except sqlite3.Error:
                pass
        tracked = [r[0] for r in con.execute("SELECT ticker FROM tracked")]
        con.close()
        return ", ".join(bits) + (f" [{', '.join(tracked)}]" if tracked else "")
    except sqlite3.Error as exc:
        return f"unreadable — {exc}"


def _describe(label: str, data: Path, config: Path) -> None:
    rosters = sorted(p.stem for p in (config / "authored").glob("*.json"))
    print(f"  {label:14} {_rows(data / 'ledger.db')}")
    print(f"  {'':14} rosters: {', '.join(rosters) or 'none'}")


def cmd_list() -> None:
    print("LIVE")
    _describe("(default)", Path("data"), Path("config"))
    if PROFILES.exists():
        for entry in sorted(p for p in PROFILES.iterdir() if p.is_dir()):
            print(f"\nPROFILE  CONTOUR_PROFILE={entry.name}")
            _describe(entry.name, entry / "data", entry / "config")
    if ARCHIVE.exists():
        stamps = sorted(p.name for p in ARCHIVE.iterdir() if p.is_dir())
        print(f"\nARCHIVES  {', '.join(stamps) or 'none'}")


def cmd_archive() -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = ARCHIVE / stamp
    dest.mkdir(parents=True, exist_ok=True)
    for src in LIVE:
        if not src.exists():
            continue
        target = dest / src.name
        if src.is_dir():
            shutil.copytree(src, target, dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
    print(f"archived live state to {dest}")
    _describe("archived", dest, dest / "config" if (dest / "config").exists() else dest)


def cmd_reset(name: str) -> None:
    if not name or name.lower() == "live":
        sys.exit("refusing to reset the live profile — archive it and use a named profile instead")
    root = PROFILES / name
    if root.exists():
        shutil.rmtree(root)
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "config" / "authored").mkdir(parents=True, exist_ok=True)
    # Sources are declarations, not accumulated state: a clean profile still
    # needs to know which feeds exist, or half the checks report unavailable
    # for reasons that have nothing to do with the company being scanned.
    print(f"profile {name!r} is empty — run it with:\n"
          f"  CONTOUR_PROFILE={name} ./.venv/bin/python scripts/serve.py --port 8077")


def cmd_restore(stamp: str) -> None:
    src = ARCHIVE / stamp
    if not src.exists():
        sys.exit(f"no archive named {stamp!r} — run: scripts/profile.py list")
    if (src / "ledger.db").exists():
        Path("data").mkdir(exist_ok=True)
        shutil.copy2(src / "ledger.db", Path("data/ledger.db"))
    if (src / "config").exists():
        shutil.copytree(src / "config", Path("config"), dirs_exist_ok=True)
    print(f"restored {stamp} over the live profile")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "list":
        cmd_list()
    elif args[0] == "archive":
        cmd_archive()
    elif args[0] == "reset":
        cmd_reset(args[1] if len(args) > 1 else "")
    elif args[0] == "restore":
        cmd_restore(args[1] if len(args) > 1 else "")
    else:
        sys.exit(__doc__)

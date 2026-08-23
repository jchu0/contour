"""Which set of accumulated state this process is using.

A scan writes: the ledger database (as-filed observations, scan history,
watchlist, findings) and the config a person has settled (pinned check
rosters, confirmed entity names, model-proposed names, shortcut chips). That
is the state you want a clean slate of when testing a first-run scan, and
exactly the state you do NOT want to lose before a demo.

`CONTOUR_PROFILE` picks it. Unset is the live profile and keeps the historical
paths, so nothing already on disk moves.

The HTTP caches are deliberately NOT profiled. They hold public documents
fetched from EDGAR, NHTSA and the price feeds — 400MB of them — and they are
a mirror, not state. Sharing them keeps a "clean" profile's first scan fast
and keeps the request load off the SEC.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

PROFILES_ROOT = Path("profiles")
_SAFE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def name() -> str:
    """The active profile, or "" for the live one."""
    raw = (os.environ.get("CONTOUR_PROFILE") or "").strip()
    if not raw or raw.lower() == "live":
        return ""
    if not _SAFE.match(raw):
        raise ValueError(
            f"CONTOUR_PROFILE={raw!r} is not a usable name — letters, digits, - and _ only"
        )
    return raw


def root() -> Path:
    """Where this profile's state lives."""
    active = name()
    return PROFILES_ROOT / active if active else Path(".")


def data_dir() -> Path:
    return root() / "data" if name() else Path("data")


def config_dir() -> Path:
    return root() / "config" if name() else Path("config")


def label() -> str:
    return name() or "live"

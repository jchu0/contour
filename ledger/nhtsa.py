"""NHTSA safety recall client.

Keyless public API. Recalls are dated events with a campaign number, so they
drop straight into state and diff cleanly against a prior scan.

Entity resolution is explicit on purpose: a company is linked to an NHTSA make
by hand, never by fuzzy name match. Searching public databases by company name
returns the wrong company often enough that a false positive on a safety or
enforcement record would be worse than no record at all.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime

import requests

MODELS_URL = "https://api.nhtsa.gov/products/vehicle/models"
RECALLS_URL = "https://api.nhtsa.gov/recalls/recallsByVehicle"
MIN_REQUEST_INTERVAL = 0.15

# A full-line manufacturer lists ~70 models per year; querying all of them takes
# 40s, which is dead air in a live scan. Enumeration is capped and the cap is
# reported, because a silent truncation reads as "no more recalls exist".
MAX_MODELS_PER_YEAR = 10

# Ticker -> NHTSA make. Curated, never inferred. These ship as defaults;
# config/entities.toml overlays them and is managed from the app.
MAKE_BY_TICKER = {
    "TSLA": "tesla",
    "F": "ford",
    "GM": "chevrolet",
    "RIVN": "rivian",
    "LCID": "lucid",
    "STLA": "chrysler",
    "HMC": "honda",
    "TM": "toyota",
}


@dataclass(frozen=True)
class Recall:
    campaign: str
    manufacturer: str
    reported: date
    component: str
    summary: str
    consequence: str
    over_the_air: bool
    park_it: bool
    affected: tuple[str, ...]   # the model/year combinations it surfaced under

    @property
    def url(self) -> str:
        return f"https://www.nhtsa.gov/recalls?nhtsaId={self.campaign}"


def _parse_date(value: str) -> date | None:
    """NHTSA reports dates as DD/MM/YYYY, not the US order the rest of the payload implies."""
    try:
        return datetime.strptime(value, "%d/%m/%Y").date()
    except (ValueError, TypeError):
        return None


class NhtsaClient:
    def __init__(
        self,
        user_agent: str = "OrgStateLedger/0.1 (research prototype)",
        cache_dir: str | None = "data/nhtsa_cache",
    ):
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": user_agent})
        self._lock = threading.Lock()
        self._last = 0.0
        self._cache_dir = cache_dir
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)

    def _cache_path(self, url: str, params: dict) -> str | None:
        if not self._cache_dir:
            return None
        key = url + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in key)[-180:]
        return os.path.join(self._cache_dir, safe + ".json")

    def _get(self, url: str, params: dict) -> dict:
        path = self._cache_path(url, params)
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh)
            except (OSError, ValueError):
                pass
        with self._lock:
            elapsed = time.monotonic() - self._last
            if elapsed < MIN_REQUEST_INTERVAL:
                time.sleep(MIN_REQUEST_INTERVAL - elapsed)
            self._last = time.monotonic()
        response = self._session.get(url, params=params, timeout=30)
        # A model/year with no recalls answers non-200. That is a fact worth
        # caching — without it every empty combination is re-requested on each
        # scan, which is most of them.
        data = response.json() if response.status_code == 200 else {}
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
        return data

    def models(self, make: str, model_year: int) -> list[str]:
        data = self._get(MODELS_URL, {"modelYear": model_year, "make": make, "issueType": "r"})
        return [r["model"] for r in data.get("results", []) if r.get("model")]

    def recalls(
        self,
        make: str,
        model_years: list[int],
        *,
        manufacturer_must_contain: str | None = None,
        max_models_per_year: int = MAX_MODELS_PER_YEAR,
    ) -> tuple[list[Recall], str | None]:
        """Recalls across every model in the given years, deduped by campaign.

        `manufacturer_must_contain` is the entity gate: NHTSA reports the filing
        manufacturer on each recall, so a mismatch means the make resolved to a
        different company and the record is dropped rather than reported.
        """
        found: dict[str, Recall] = {}
        capped = 0
        for year in model_years:
            models = self.models(make, year)
            if len(models) > max_models_per_year:
                capped += len(models) - max_models_per_year
                models = models[:max_models_per_year]
            for model in models:
                data = self._get(RECALLS_URL, {"make": make, "model": model, "modelYear": year})
                for raw in data.get("results", []):
                    campaign = raw.get("NHTSACampaignNumber")
                    manufacturer = raw.get("Manufacturer", "")
                    if not campaign:
                        continue
                    if manufacturer_must_contain and manufacturer_must_contain.lower() not in manufacturer.lower():
                        continue
                    reported = _parse_date(raw.get("ReportReceivedDate", ""))
                    if reported is None:
                        continue
                    tag = f"{year} {model}"
                    if campaign in found:
                        existing = found[campaign]
                        found[campaign] = Recall(**{**existing.__dict__, "affected": existing.affected + (tag,)})
                        continue
                    found[campaign] = Recall(
                        campaign=campaign,
                        manufacturer=manufacturer,
                        reported=reported,
                        component=raw.get("Component", ""),
                        summary=raw.get("Summary", ""),
                        consequence=raw.get("Consequence", ""),
                        over_the_air=str(raw.get("overTheAirUpdate", "")).lower() == "true",
                        park_it=str(raw.get("parkIt", "")).lower() == "true",
                        affected=(tag,),
                    )
        note = (
            f"{capped} further models not queried (capped at {max_models_per_year} per year)"
            if capped else None
        )
        return sorted(found.values(), key=lambda r: r.reported, reverse=True), note


def make_for(ticker: str) -> str | None:
    """The NHTSA make for a ticker: a user mapping if one exists, else the
    shipped default. Never guessed from the company name."""
    from ledger.config import entity_for

    return entity_for(ticker, "nhtsa_make") or MAKE_BY_TICKER.get(ticker.upper())


# SIC codes for companies that build road vehicles. Anything outside these is
# not a manufacturer NHTSA holds recalls for, so the check can say so as a fact
# rather than asking someone to supply a mapping that will never exist.
VEHICLE_SIC = {"3711", "3713", "3714", "3715", "3716", "3751", "3790", "3792"}


def builds_vehicles(sic: str) -> bool:
    return (sic or "").strip() in VEHICLE_SIC

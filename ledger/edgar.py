"""SEC EDGAR client.

Primary-source ingest for the org state ledger. Everything downstream (state
extraction, diffing, claim substantiation) reads through this module.

SEC fair-access rules this client must respect:
  * every request carries a User-Agent identifying a contact address
  * no more than 10 requests/second
See https://www.sec.gov/os/accessing-edgar-data
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Iterator

import requests

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"

# SEC allows 10 req/s; leave headroom so a burst never trips the throttle.
MIN_REQUEST_INTERVAL = 0.12


class EdgarError(RuntimeError):
    pass


@dataclass(frozen=True)
class Company:
    cik: int
    ticker: str
    name: str


@dataclass(frozen=True)
class Filing:
    cik: int
    accession: str          # with dashes, e.g. 0000320193-24-000123
    form: str               # 10-K, 10-Q, 8-K, 4, ...
    filed: date
    period: date | None     # reportDate; absent on some forms
    document: str           # primary document filename
    description: str
    items: tuple[str, ...]  # 8-K item numbers, e.g. ("2.02", "9.01")

    @property
    def url(self) -> str:
        return ARCHIVE_URL.format(
            cik=self.cik,
            accession=self.accession.replace("-", ""),
            document=self.document,
        )

    @property
    def index_url(self) -> str:
        return ARCHIVE_URL.format(
            cik=self.cik,
            accession=self.accession.replace("-", ""),
            document=f"{self.accession}-index.htm",
        )


def _parse_date(value: str) -> date | None:
    return date.fromisoformat(value) if value else None


class EdgarClient:
    """Rate-limited EDGAR reader.

    The User-Agent must identify a real contact address or SEC returns 403.
    Set SEC_USER_AGENT, e.g. "Org Ledger you@example.com".
    """

    def __init__(self, user_agent: str | None = None, cache_dir: str | None = "data/edgar_cache"):
        ua = user_agent or os.environ.get("SEC_USER_AGENT")
        if not ua:
            raise EdgarError(
                "SEC requires a contact address in the User-Agent header. "
                'Set SEC_USER_AGENT, e.g. export SEC_USER_AGENT="Org Ledger you@example.com"'
            )
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": ua, "Accept-Encoding": "gzip, deflate"})
        self._lock = threading.Lock()
        self._last_request = 0.0
        self._cache_dir = cache_dir
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
        self._ticker_map: dict[str, Company] | None = None

    # -- transport ---------------------------------------------------------

    def _throttle(self) -> None:
        with self._lock:
            elapsed = time.monotonic() - self._last_request
            if elapsed < MIN_REQUEST_INTERVAL:
                time.sleep(MIN_REQUEST_INTERVAL - elapsed)
            self._last_request = time.monotonic()

    def _cache_path(self, url: str) -> str | None:
        if not self._cache_dir:
            return None
        safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in url)[-180:]
        return os.path.join(self._cache_dir, safe)

    def get(self, url: str, *, use_cache: bool = True) -> str:
        path = self._cache_path(url) if use_cache else None
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        self._throttle()
        response = self._session.get(url, timeout=30)
        if response.status_code != 200:
            raise EdgarError(f"{response.status_code} fetching {url}")
        text = response.text
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        return text

    def get_json(self, url: str, *, use_cache: bool = True) -> dict:
        return json.loads(self.get(url, use_cache=use_cache))

    # -- lookups -----------------------------------------------------------

    def resolve(self, ticker: str) -> Company:
        if self._ticker_map is None:
            raw = self.get_json(TICKER_MAP_URL)
            self._ticker_map = {
                entry["ticker"].upper(): Company(
                    cik=int(entry["cik_str"]),
                    ticker=entry["ticker"].upper(),
                    name=entry["title"],
                )
                for entry in raw.values()
            }
        try:
            return self._ticker_map[ticker.upper()]
        except KeyError:
            raise EdgarError(f"no CIK on file for ticker {ticker!r}") from None

    def filings(
        self,
        cik: int,
        *,
        forms: Iterable[str] | None = None,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[Filing]:
        """Recent filings, newest first.

        Reads only the `recent` block — roughly the last 1000 filings or one
        year, whichever is larger. Deep backfill needs the overflow files in
        `filings.files`; not needed yet.
        """
        wanted = {f.upper() for f in forms} if forms else None
        data = self.get_json(SUBMISSIONS_URL.format(cik=cik), use_cache=False)
        recent = data["filings"]["recent"]
        out: list[Filing] = []
        for i in range(len(recent["accessionNumber"])):
            form = recent["form"][i]
            if wanted and form.upper() not in wanted:
                continue
            filed = _parse_date(recent["filingDate"][i])
            if since and filed and filed < since:
                continue
            items = recent.get("items", [""] * len(recent["accessionNumber"]))[i]
            out.append(
                Filing(
                    cik=cik,
                    accession=recent["accessionNumber"][i],
                    form=form,
                    filed=filed,
                    period=_parse_date(recent["reportDate"][i]),
                    document=recent["primaryDocument"][i],
                    description=recent["primaryDocDescription"][i],
                    items=tuple(s.strip() for s in items.split(",") if s.strip()),
                )
            )
            if limit and len(out) >= limit:
                break
        return out

    def document(self, filing: Filing) -> str:
        return self.get(filing.url)

    def profile(self, cik: int) -> dict:
        """Registrant metadata from the submissions file — SIC code and name."""
        if not hasattr(self, "_profiles"):
            self._profiles: dict[int, dict] = {}
        if cik not in self._profiles:
            data = self.get_json(SUBMISSIONS_URL.format(cik=cik), use_cache=False)
            self._profiles[cik] = {
                "sic": str(data.get("sic") or ""),
                "sic_description": str(data.get("sicDescription") or ""),
                "name": str(data.get("name") or ""),
            }
        return self._profiles[cik]

    def company_facts(self, cik: int) -> dict:
        """XBRL company facts — reported GAAP figures, by concept and period.

        This is the substantiation corpus: management's claims get tested
        against these numbers rather than against prose.
        """
        return self.get_json(COMPANY_FACTS_URL.format(cik=cik), use_cache=False)


# -- filing contents ------------------------------------------------------

@dataclass(frozen=True)
class FilingFile:
    name: str
    type: str
    size: int
    url: str


def _filing_files(client: "EdgarClient", filing: Filing) -> list[FilingFile]:
    base = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}".format(
        cik=filing.cik, acc=filing.accession.replace("-", "")
    )
    data = client.get_json(f"{base}/index.json")
    return [
        FilingFile(
            name=item["name"],
            type=item.get("type", ""),
            size=int(item.get("size") or 0),
            url=f"{base}/{item['name']}",
        )
        for item in data["directory"]["item"]
    ]


EdgarClient.filing_files = _filing_files  # type: ignore[attr-defined]


def _exhibit_index(client: "EdgarClient", filing: Filing) -> list[tuple[str, str, str]]:
    """(designation, description, filename) per document, from the filing index page.

    index.json's `type` field is an icon filename, not the exhibit designation —
    the real EX-99.1 label only appears in the rendered index table.
    """
    from bs4 import BeautifulSoup

    base = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}".format(
        cik=filing.cik, acc=filing.accession.replace("-", "")
    )
    soup = BeautifulSoup(client.get(f"{base}/{filing.accession}-index.htm"), "lxml")
    rows: list[tuple[str, str, str]] = []
    for tr in soup.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(cells) < 4:
            continue
        _seq, description, document, designation = cells[0], cells[1], cells[2], cells[3]
        filename = document.split(" ")[0]
        if filename:
            rows.append((designation, description, filename))
    return rows


def _earnings_exhibit(client: "EdgarClient", filing: Filing) -> FilingFile | None:
    """The press-release exhibit on an 8-K, keyed on its EX-99 designation.

    Issuers name these anything — `earningsreleasefy27q2.htm` has no "ex" in it
    at all — so a filename heuristic silently skips whole companies.
    """
    base = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}".format(
        cik=filing.cik, acc=filing.accession.replace("-", "")
    )
    sized = {f.name: f for f in client.filing_files(filing)}
    candidates = [
        (designation, filename)
        for designation, _desc, filename in _exhibit_index(client, filing)
        if designation.upper().startswith("EX-99")
        and filename.lower().endswith((".htm", ".html", ".txt"))
    ]
    if not candidates:
        return None
    # EX-99.1 is the release itself; later exhibits are decks and supplements.
    designation, filename = sorted(candidates)[0]
    known = sized.get(filename)
    return FilingFile(
        name=filename,
        type=designation,
        size=known.size if known else 0,
        url=f"{base}/{filename}",
    )


EdgarClient.exhibit_index = _exhibit_index  # type: ignore[attr-defined]
EdgarClient.earnings_exhibit = _earnings_exhibit  # type: ignore[attr-defined]

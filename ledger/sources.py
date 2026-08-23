"""User-defined data sources.

Everything the ledger reads is a source, and the built-in ones (EDGAR, NHTSA)
should not be the only ones a user gets. A source is declared in TOML — no code
— and plugs into the same machinery as the built-ins, which means it inherits
the two rules that make the built-ins trustworthy:

  * Entities are mapped by hand. A source with no mapping for a company is
    reported as not applicable, never searched by name. Name search on public
    databases returns the wrong company often enough that a false positive on a
    legal or safety record is worse than no record at all.

  * Reliability class is declared and enforced. Only a Class-A primary source
    can mark a finding verified, so a user adding a forum or a review site gets
    corroboration, never promotion.

TOML because `tomllib` is in the standard library and the file is meant to be
edited by hand.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from ledger.provenance import Source, SourceClass

# One pooled session for every declared source. Bare `requests.get` opens a new
# connection per call; a few hundred scans exhausts the ephemeral port range and
# the next request dies with OSError(49).
_SESSION = requests.Session()

SOURCES_DIR = Path("sources")
CACHE_DIR = Path("data/source_cache")
TIMEOUT = 20
MAX_ITEMS = 8

_CLASS_BY_LETTER = {c.letter: c for c in SourceClass}

# The server fetches whatever a source declares, so a URL pointing inside the
# host network would turn this into a request proxy. Public hosts only.
_BLOCKED_HOSTS = {"localhost", "metadata.google.internal", "169.254.169.254"}


class SourceError(RuntimeError):
    pass


@dataclass
class Extract:
    items: str = ""                 # dot-path to the list of records
    title: str = "title"
    detail: str = ""
    date: str = ""
    link: str = ""
    match: str = ""                 # field that must contain the entity name


# Secrets never live in a source file. A header value written as ${NAME} is
# read from the environment at request time, so a checked-in TOML can reference
# an API key without containing one.
_ENV_REF = re.compile(r"\$\{([A-Z0-9_]+)\}")


@dataclass
class CustomSource:
    name: str
    klass: SourceClass
    url: str                        # supports {entity} {ticker} {cik}
    kind: str = "json"              # json | rss
    severity: str = "info"
    enabled: bool = True
    note: str = ""
    entities: dict[str, str] = field(default_factory=dict)
    extract: Extract = field(default_factory=Extract)
    headers: dict[str, str] = field(default_factory=dict)
    path: Path | None = None

    @property
    def required_env(self) -> list[str]:
        return sorted({
            name
            for value in list(self.headers.values()) + [self.url]
            for name in _ENV_REF.findall(value)
        })

    @property
    def missing_env(self) -> list[str]:
        return [name for name in self.required_env if not os.environ.get(name)]

    def entity_for(self, ticker: str) -> str | None:
        """A mapping added in the app wins over the one declared in the file."""
        from ledger.config import entity_for as configured

        return configured(ticker, self.name) or self.entities.get(ticker.upper())

    def proposed_for(self, ticker: str) -> dict | None:
        """A model's proposal, used only when nobody has stated one.

        Returning it here is what lets a fresh ticker produce findings at once.
        Every caller must carry the proposal forward so the finding says the
        name was guessed — see Source.entity_proposed.
        """
        if self.entity_for(ticker):
            return None
        from ledger.config import proposed_entity

        return proposed_entity(ticker, self.name)

    @property
    def needs_entity(self) -> bool:
        """A source keyed on CIK or ticker is already exact and needs no mapping.

        Only name-based lookups need one, because a name search is the thing
        that returns the wrong company.
        """
        return "{entity}" in self.url or bool(self.extract.match)

    @property
    def coverage(self) -> str:
        if not self.needs_entity:
            return "keyed on CIK — applies to every company"
        if not self.entities:
            return "no entities mapped"
        return f"{len(self.entities)} companies mapped"


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SourceError(f"{parsed.scheme or 'missing'} scheme not allowed; use http or https")
    host = (parsed.hostname or "").lower()
    if not host or host in _BLOCKED_HOSTS:
        raise SourceError(f"host {host!r} is not reachable from a source")
    try:
        resolved = socket.gethostbyname(host)
        address = ipaddress.ip_address(resolved)
    except (OSError, ValueError):
        return  # unresolvable now is a fetch error later, not a security issue
    if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
        raise SourceError(f"host {host!r} resolves to a non-public address")


def _dig(payload, path: str):
    """Walk a dot-path, treating integer segments as list indices."""
    if not path:
        return payload
    current = payload
    for segment in path.split("."):
        if current is None:
            return None
        if segment.isdigit() and isinstance(current, list):
            index = int(segment)
            current = current[index] if index < len(current) else None
        elif isinstance(current, dict):
            current = current.get(segment)
        else:
            return None
    return current


_TAGS = re.compile(r"<[^>]+>")


def clip(text: str, limit: int) -> str:
    """Cut at a word boundary. A hard slice mid-word reads as a rendering fault,
    which is the last thing a report about evidence should look like."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return (cut or text[:limit]) + "…"


def _field(record: dict, path: str) -> str:
    """One extracted field as display text.

    An empty path used to fall through to `_dig`'s "return everything" branch,
    so a source declaring no `detail` rendered its entire raw JSON record as the
    card body. Markup is stripped too — Atom summaries are HTML, and escaping
    them printed the tags.
    """
    if not path:
        return ""
    text = _as_text(_dig(record, path))
    return " ".join(_TAGS.sub(" ", text).split())


def _as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(_as_text(v) for v in value if v is not None)
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in value.items())
    return str(value)


def _parse_date(value: str) -> date | None:
    text = _as_text(value)[:32].strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%a, %d %b %Y %H:%M:%S %z", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return date(*(int(g) for g in match.groups()))
    return None


def _cache_path(url: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in url)[-180:]
    return CACHE_DIR / f"{safe}.cache"


def _resolve_env(value: str) -> str:
    """Substitute ${NAME} from the environment, refusing rather than sending blanks."""
    def replace(match: re.Match) -> str:
        name = match.group(1)
        resolved = os.environ.get(name)
        if not resolved:
            raise SourceError(f"environment variable {name} is not set")
        return resolved

    return _ENV_REF.sub(replace, value)


def _user_agent(url: str) -> str:
    """SEC returns 403 without a contact address, so honour SEC_USER_AGENT there."""
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("sec.gov"):
        configured = os.environ.get("SEC_USER_AGENT")
        if configured:
            return configured
    return "OrgStateLedger/0.1 (research prototype)"


def _fetch(url: str, *, extra_headers: dict[str, str] | None = None, use_cache: bool = True) -> str:
    _validate_url(url)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(url)
    if use_cache and path.exists():
        return path.read_text(encoding="utf-8")
    headers = {"User-Agent": _user_agent(url)}
    for key, value in (extra_headers or {}).items():
        headers[key] = _resolve_env(value)
    response = _SESSION.get(url, timeout=TIMEOUT, headers=headers)
    if response.status_code != 200:
        raise SourceError(f"{response.status_code} from {urlparse(url).netloc}")
    path.write_text(response.text, encoding="utf-8")
    return response.text


def _records(source: CustomSource, body: str) -> list[dict]:
    if source.kind == "rss":
        root = ET.fromstring(body)
        out = []
        for node in root.iter():
            tag = node.tag.split("}")[-1]
            if tag not in ("item", "entry"):
                continue
            record = {}
            for child in node:
                key = child.tag.split("}")[-1]
                record[key] = (child.text or child.attrib.get("href", "")).strip()
            out.append(record)
        return out
    payload = json.loads(body)
    found = _dig(payload, source.extract.items)
    if isinstance(found, dict):
        found = [found]
    return [r for r in (found or []) if isinstance(r, dict)]


@dataclass
class Hit:
    title: str
    detail: str
    when: date | None
    link: str


# Legal-form noise that no source searches on.
_NAME_NOISE = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|holdings?|"
    r"group|the|llc|lp|nv|sa|ag|/de|/ de)\b\.?", re.IGNORECASE
)


def _suggest(company_name: str) -> str:
    """A search term worth proposing — never applied without a person saying so."""
    cleaned = _NAME_NOISE.sub(" ", company_name or "").replace(",", " ")
    # Registrant names carry a state-of-incorporation suffix and stray
    # conjunctions once the legal form is stripped: "Rivian Automotive / DE",
    # "JPMORGAN CHASE &".
    cleaned = re.sub(r"\s*/\s*[A-Z]{2}\s*$", "", cleaned)
    cleaned = " ".join(cleaned.split())
    cleaned = re.sub(r"[\s&/.,-]+$", "", cleaned)
    return cleaned.strip(" .-/")


def query(source: CustomSource, ticker: str, cik: int, company_name: str = "") -> list[Hit]:
    """Fetch and extract, applying the source's own entity gate."""
    entity = source.entity_for(ticker)
    proposal = None
    if entity is None:
        proposal = source.proposed_for(ticker)
        if proposal:
            entity = proposal.get("entity") or None
    if source.needs_entity and entity is None:
        hint = f" Try \"{suggestion}\"." if (suggestion := _suggest(company_name)) else ""
        raise SourceError(
            f"no name mapped for {ticker.upper()} in '{source.name}' — "
            f"set one on the Sources page.{hint}"
        )
    entity = entity or ""

    missing = source.missing_env
    if missing:
        raise SourceError(
            f"'{source.name}' needs {', '.join(missing)} in the environment. "
            "Set it and rescan; the source is declared but cannot authenticate."
        )

    url = _resolve_env(source.url).format(entity=quote(entity), ticker=ticker.upper(), cik=cik)
    records = _records(source, _fetch(url, extra_headers=source.headers))

    hits: list[Hit] = []
    needle = entity.split(",")[0].lower()
    for record in records:
        if source.extract.match and needle:
            confirm = _field(record, source.extract.match).lower()
            if needle not in confirm:
                continue
        hits.append(Hit(
            title=clip(_field(record, source.extract.title), 180) or "(untitled record)",
            detail=clip(_field(record, source.extract.detail), 300),
            when=_parse_date(_dig(record, source.extract.date)),
            link=_field(record, source.extract.link),
        ))
        if len(hits) >= MAX_ITEMS:
            break
    return hits


def citation(source: CustomSource, hit: Hit) -> Source:
    return Source(
        klass=source.klass,
        origin=source.name,
        reference=hit.title[:60],
        document_date=hit.when or date.today(),
        retrieved=date.today(),
        url=hit.link,
    )


# -- configuration ---------------------------------------------------------


def _from_table(table: dict, path: Path) -> CustomSource:
    letter = str(table.get("class", "F")).upper()[:1]
    if letter not in _CLASS_BY_LETTER:
        raise SourceError(f"unknown reliability class {letter!r}; use one of A-F")
    if "url" not in table or "name" not in table:
        raise SourceError("a source needs at least a name and a url")
    extract = table.get("extract", {})
    return CustomSource(
        name=str(table["name"]),
        klass=_CLASS_BY_LETTER[letter],
        url=str(table["url"]),
        kind=str(table.get("kind", "json")).lower(),
        severity=str(table.get("severity", "info")),
        enabled=bool(table.get("enabled", True)),
        note=str(table.get("note", "")),
        entities={str(k).upper(): str(v) for k, v in (table.get("entities") or {}).items()},
        headers={str(k): str(v) for k, v in (table.get("headers") or {}).items()},
        extract=Extract(**{k: str(v) for k, v in extract.items() if k in Extract.__annotations__}),
        path=path,
    )


def load_sources(directory: Path = SOURCES_DIR) -> tuple[list[CustomSource], list[str]]:
    """Every *.toml in the directory. Returns (sources, problems).

    A malformed file is reported rather than raised — one bad source must not
    stop the others, and silently dropping it would be worse than either.
    """
    sources: list[CustomSource] = []
    problems: list[str] = []
    if not directory.is_dir():
        return sources, problems
    for path in sorted(directory.glob("*.toml")):
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            problems.append(f"{path.name}: {exc}")
            continue
        tables = data.get("source")
        if isinstance(tables, dict):
            tables = [tables]
        for table in tables or []:
            try:
                sources.append(_from_table(table, path))
            except (SourceError, TypeError) as exc:
                problems.append(f"{path.name}: {exc}")
    return sources, problems


def append_source(table: dict, path: Path = SOURCES_DIR / "custom.toml") -> CustomSource:
    """Validate then append one source to a TOML file."""
    source = _from_table(table, path)          # raises before anything is written
    _validate_url(source.url.format(entity="x", ticker="X", cik=0))
    path.parent.mkdir(parents=True, exist_ok=True)

    def literal(value: str) -> str:
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = ["", "[[source]]", f"name = {literal(source.name)}",
             f'class = "{source.klass.letter}"', f"url = {literal(source.url)}",
             f'kind = "{source.kind}"', f'severity = "{source.severity}"']
    if source.note:
        lines.append(f"note = {literal(source.note)}")
    if source.headers:
        lines.append("")
        lines.append("[source.headers]")
        lines += [f"{k} = {literal(v)}" for k, v in sorted(source.headers.items())]
    if source.entities:
        lines.append("")
        lines.append("[source.entities]")
        lines += [f"{k} = {literal(v)}" for k, v in sorted(source.entities.items())]
    lines.append("")
    lines.append("[source.extract]")
    for field_name in ("items", "title", "detail", "date", "link", "match"):
        value = getattr(source.extract, field_name)
        if value:
            lines.append(f"{field_name} = {literal(value)}")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return source

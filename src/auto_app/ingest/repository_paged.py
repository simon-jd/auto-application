"""Employer boards read page by page from a public JSON search API.

Five repository formats read an employer's whole board, or a filtered part of it, into one JSON snapshot
per run, and their parsers turn a snapshot into records without network access: workday-board
(repository_workday), eightfold-board, icims-jibe-board, oracle-board and smartrecruiters-board. This
module is their registry (feed options, board URL, reader, parser) and holds what the last four share:
requests with the pipeline's User-Agent and a pause between two requests to the same site, paging up
to a page cap, and the rule that a search which ends before the site's total, even when read a second
time, makes the snapshot partial, so a posting that was not read is never closed (docs/step1-collect.md
§14.6).
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
from importlib import import_module
import json
import re
import time
from typing import Callable
from urllib.parse import urlsplit

import httpx

from auto_app.enrich.fetch import USER_AGENT
from auto_app.ingest.models import IngestRecord, ParsedFeed, ReadResult

PAUSE_SECONDS = 1.0  # between two requests to the same site
SEARCH_CHARS = 200
COUNTRY_CODE = re.compile(r"[A-Za-z]{2}")
BOARD_MODULES = {
    "workday-board": "auto_app.ingest.repository_workday",
    "eightfold-board": "auto_app.ingest.repository_eightfold",
    "icims-jibe-board": "auto_app.ingest.repository_jibe",
    "oracle-board": "auto_app.ingest.repository_oracle",
    "smartrecruiters-board": "auto_app.ingest.repository_smartrecruiters",
}

_sleep = time.sleep


@dataclass(frozen=True)
class PagedBoard:
    format: str
    label: str  # the system's name in messages: "Oracle"
    option_keys: tuple[str, ...]
    board_url: Callable[[object], str]  # a feed location → the canonical board URL, else ValueError
    options: Callable[[dict], dict]  # a feed definition → its validated options
    read: Callable[..., ReadResult]  # (board, *, client, today, **options) → one snapshot
    parse: Callable[..., ParsedFeed]  # (snapshot, *, company) → records and issues
    company_required: bool = False  # the board's list never names the employer


def paged_board(format: str) -> PagedBoard | None:
    """The paged board format's registry entry; None for a format read as one file (read_feed)."""
    module = BOARD_MODULES.get(format)
    return import_module(module).BOARD if module else None


def feed_options(definition: dict, *, format: str) -> dict | None:
    """Validated options of a paged board feed, None for another format; another format's option fails."""
    boards = [paged_board(name) for name in BOARD_MODULES]
    own = next((board for board in boards if board.format == format), None)
    stray = sorted({key for board in boards for key in board.option_keys
                    if key in definition and key not in (own.option_keys if own else ())})
    if stray:
        owners = sorted(board.format for board in boards if set(board.option_keys) & set(stray))
        raise ValueError(f"{', '.join(stray)} apply only to format {' or '.join(owners)}")
    return own.options(definition) if own else None


# ---- options and board URLs ----------------------------------------------------------------------------

def search_texts(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(text, str) or not text.strip() or len(text) > SEARCH_CHARS
                                          for text in value):
        raise ValueError("search must be a search text or a list of nonempty search texts")
    return list(dict.fromkeys(text.strip() for text in value))


def page_cap(definition: dict, default: int, most: int) -> int:
    value = definition.get("max_pages", default)
    if type(value) is not int or not 1 <= value <= most:
        raise ValueError(f"max_pages must be a whole number from 1 to {most}")
    return value


def option_text(definition: dict, key: str, *, hint: str, pattern: re.Pattern | None = None) -> str | None:
    value = definition.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > SEARCH_CHARS \
            or (pattern is not None and not pattern.fullmatch(value.strip())):
        raise ValueError(f"{key} must be {hint}")
    return value.strip()


def https_parts(location, usage: str):
    """The parts of an HTTP(S) URL without credentials or a port, else ValueError naming the expected form."""
    if not isinstance(location, str) or not location.strip():
        raise ValueError(f"location must be {usage}")
    try:
        parts = urlsplit(location.strip())
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"location must be {usage}") from exc
    if (parts.scheme not in ("http", "https") or not parts.hostname or port is not None
            or parts.username is not None or parts.password is not None):
        raise ValueError(f"location must be {usage}")
    return parts


# ---- reading ---------------------------------------------------------------------------------------

class Pager:
    """One site's requests in turn: the pipeline's User-Agent, a pause before every request after the first."""

    def __init__(self, client: httpx.Client, label: str, pause: float = PAUSE_SECONDS):
        self.client, self.label, self.pause, self.sent = client, label, pause, 0

    def get(self, url: str, params=None):
        if self.sent:
            _sleep(self.pause)
        self.sent += 1
        response = self.client.get(url, params=params, follow_redirects=False,
                                   headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        response.raise_for_status()
        try:
            return response.json()
        except ValueError as exc:
            raise ValueError(f"{self.label} did not return JSON") from exc


def read_pages(fetch_page, *, searches: list[str], page_size: int, max_pages: int, key) -> tuple[dict, list]:
    """fetch_page(search, page) → (rows, total) for pages 0, 1 …; the rows of every search merged by key(row).

    A read of a search is complete when it ends (a short page, or as many pages as its total needs) having
    read as many distinct postings as the largest total its pages reported. A posting added or closed
    while the pages are read shifts the newest-first order, so a read that ends short is repeated once:
    the search is complete when that second read is, and keeps the rows of both. Otherwise the snapshot
    is partial, and no posting is closed by mistake."""
    rows, summaries = {}, []

    def read_once(text: str) -> tuple[dict, int, int, bool]:
        found, total, pages = {}, 0, 0
        while pages < max_pages:
            batch, reported = fetch_page(text, pages)
            pages += 1
            total = max(total, reported)
            for row in batch:
                name = key(row)
                if not isinstance(name, str) or not name:
                    # Kept under its own text, so that the parser reports it.
                    name = "~" + json.dumps(row, sort_keys=True, ensure_ascii=False)
                found.setdefault(name, row)
            if len(batch) < page_size or pages * page_size >= total:
                return found, total, pages, True
        return found, total, pages, False

    for text in searches or [""]:
        found, total, pages, ended = read_once(text)
        reads = 1
        if ended and len(found) < total:
            again, total, more, ended = read_once(text)
            found, pages, reads = {**found, **again}, pages + more, 2
            read = len(again)
        else:
            read = len(found)
        for name, row in found.items():
            rows.setdefault(name, row)
        summaries.append({"search_text": text, "total": total, "read": read, "pages": pages, "reads": reads,
                          "complete": ended and read >= total})
    return rows, summaries


def snapshot(platform: str, board: str, searches: list, rows: dict, *, today: date | None) -> ReadResult:
    document = {"platform": platform, "board": board,
                "fetched_on": (today or datetime.now(timezone.utc).date()).isoformat(),
                "searches": searches, "jobs": [rows[name] for name in sorted(rows)]}
    content = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return ReadResult(content=content, revision="sha256:" + hashlib.sha256(content).hexdigest())


# ---- parsing --------------------------------------------------------------------------------------

class RowError(ValueError):
    """A job row the parser rejects; its message is the parser's own and never quotes the row."""


@dataclass(frozen=True)
class Snapshot:
    board: str
    fetched: date
    company: str | None  # the feed's company, when configured


def parse_snapshot(content: bytes, *, platform: str, label: str, board_url, company: str | None,
                   record: Callable[[object, Snapshot], IngestRecord]) -> ParsedFeed:
    if company is not None and (not isinstance(company, str) or not company.strip()):
        raise ValueError("company must be a nonempty string")
    try:
        document = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"a {label} board snapshot must contain valid JSON") from exc
    if not isinstance(document, dict) or document.get("platform") != platform \
            or not isinstance(document.get("jobs"), list) or not isinstance(document.get("searches"), list):
        raise ValueError(f"a {label} board snapshot must be an object with jobs and searches arrays")
    board = board_url(document.get("board"))
    try:
        fetched = date.fromisoformat(document.get("fetched_on"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"a {label} board snapshot needs its fetched_on date") from exc
    context = Snapshot(board, fetched, company.strip() if company else None)
    result, seen = ParsedFeed(), set()
    for index, row in enumerate(document["jobs"]):
        try:
            if not isinstance(row, dict):
                raise RowError("job must be an object")
            item = record(row, context)
            if item.external_id in seen:
                raise RowError("duplicate job id")
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError, OSError) as exc:
            # Other exceptions' messages can quote the row's values; only the type is reported for them.
            reason = str(exc) if isinstance(exc, RowError) else type(exc).__name__
            result.issues.append({"index": index, "reason": f"Invalid {label} job ({reason})"})
            continue
        seen.add(item.external_id)
        result.records.append(item)
    for search in document["searches"]:
        if not isinstance(search, dict) or search.get("complete") is not True:
            result.issues.append(_incomplete(label, search if isinstance(search, dict) else {}))
    return result


def _incomplete(label: str, search: dict) -> dict:
    text, total, read, pages = (search.get(key) for key in ("search_text", "total", "read", "pages"))
    name = f"{label} search {text!r}" if text else f"{label} listing"
    twice = ", read twice" if search.get("reads") == 2 else ""
    return {"search": text, "reason": f"{name} stopped after {pages} page(s){twice} with {read} of {total} "
                                      "postings; raise max_pages or narrow the feed (postings not read are kept, "
                                      "not closed)"}


# ---- row values -----------------------------------------------------------------------------------

def required_text(row: dict, key: str, what: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RowError(f"job needs {what}")
    return value.strip()


def optional_text(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def places(values) -> list[str]:
    """Distinct nonempty location strings, in order."""
    return list(dict.fromkeys(value.strip() for value in values if isinstance(value, str) and value.strip()))


def one_country(codes) -> str | None:
    """The ISO alpha-2 country every location shares, else None (screening reads it as structured evidence)."""
    found = {code.strip().upper() for code in codes if isinstance(code, str) and COUNTRY_CODE.fullmatch(code.strip())}
    return found.pop() if len(found) == 1 else None


def utc_time(value) -> datetime | None:
    """An ISO 8601 date-time ("…Z", "…+0000"); None when absent or unreadable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)

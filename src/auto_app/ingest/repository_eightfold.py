"""Eightfold career sites as employer boards, read through the public PCSX search (Qualcomm …).

A feed's location is the tenant's careers URL with its domain, https://<tenant>.eightfold.ai/careers?domain=<domain>.
The reader GETs /api/pcsx/search 10 positions a page (Eightfold refuses more), newest first, one search per
configured search text, with the configured country (Eightfold's location text) and filters, and writes
one snapshot. The old /api/apply/v2/jobs answers 403 "Not authorized for PCSX".

The list has no job description, so imported positions stay pending: the worker's enrichment step reads
each position that screening keeps from /api/pcsx/position_details (enrich/sites.py). Eightfold answers a
burst of requests with HTTP 429, so the reader pauses 2 seconds between requests; a 429 fails the read,
and the next round tries again.
"""

from datetime import date, datetime, timezone
import re
from urllib.parse import parse_qs, urlsplit

import httpx

from auto_app.ingest.models import IngestRecord, ParsedFeed, ReadResult
from auto_app.ingest.repository_paged import (PagedBoard, Pager, RowError, Snapshot, https_parts, one_country,
                                              option_text, optional_text, page_cap, parse_snapshot, places,
                                              read_pages, required_text, search_texts, snapshot)
from auto_app.ingest.urls import canonical_url

FORMAT = "eightfold-board"
PAGE_SIZE = 10  # a larger page_size answers HTTP 400 "Too many rows requested"
MAX_PAGES = 200
DEFAULT_MAX_PAGES = 30
PAUSE_SECONDS = 2.0
OPTION_KEYS = ("country", "search", "filters", "max_pages")
USAGE = "the careers URL https://<tenant>.eightfold.ai/careers?domain=<domain>"

HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.eightfold\.ai")
DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}")
FILTER = re.compile(r"[a-z][a-z0-9_]{0,40}")  # Eightfold's filter name: job_family, …
POSITION = re.compile(r"[0-9]{1,20}")


def eightfold_board_url(location) -> str:
    """The canonical careers URL, https://<tenant>.eightfold.ai/careers?domain=<domain>; a job path is dropped."""
    parts = https_parts(location, USAGE)
    host = parts.hostname.lower()
    if not HOST.fullmatch(host):
        raise ValueError(f"an eightfold-board location must be {USAGE} on eightfold.ai")
    domains = parse_qs(parts.query).get("domain", [])
    if len(domains) != 1 or not DOMAIN.fullmatch(domains[0].strip().lower()):
        raise ValueError(f"an eightfold-board location needs its domain: {USAGE}")
    return f"https://{host}/careers?domain={domains[0].strip().lower()}"


def eightfold_options(definition: dict) -> dict:
    filters = definition.get("filters", {})
    if not isinstance(filters, dict) or any(
            not isinstance(name, str) or not FILTER.fullmatch(name) or not isinstance(values, list) or not values
            or any(not isinstance(value, str) or not value.strip() or len(value) > 200 for value in values)
            for name, values in filters.items()):
        raise ValueError("filters must map an Eightfold filter name (job_family …) to a nonempty list of values")
    return {"country": option_text(definition, "country", hint="the place to search, as the site names it "
                                                               "(United States)"),
            "search": search_texts(definition.get("search")),
            "filters": {name: list(dict.fromkeys(value.strip() for value in values)) for name, values in filters.items()},
            "max_pages": page_cap(definition, DEFAULT_MAX_PAGES, MAX_PAGES)}


def read_eightfold_board(location: str, *, country: str | None = None, search=(), filters: dict | None = None,
                         max_pages: int = DEFAULT_MAX_PAGES, client: httpx.Client | None = None,
                         today: date | None = None) -> ReadResult:
    """Read every configured search into one snapshot; HTTP and response-shape errors fail the read."""
    board = eightfold_board_url(location)
    options = eightfold_options({"country": country, "search": search if isinstance(search, str) else list(search),
                                 "filters": filters or {}, "max_pages": max_pages})
    if client is None:
        with httpx.Client(timeout=30, follow_redirects=False) as owned:
            return read_eightfold_board(board, client=owned, today=today, **options)
    parts = urlsplit(board)
    api, domain = f"https://{parts.hostname}/api/pcsx/search", parse_qs(parts.query)["domain"][0]
    pager = Pager(client, "Eightfold search", pause=PAUSE_SECONDS)
    chosen = [(f"filter_{name}", value) for name, values in options["filters"].items() for value in values]

    def page(text: str, number: int):
        data = pager.get(api, [("domain", domain), ("query", text), ("location", options["country"] or ""),
                               ("start", number * PAGE_SIZE), ("page_size", PAGE_SIZE), ("sort_by", "timestamp"),
                               *chosen])
        found = data.get("data") if isinstance(data, dict) else None
        if not isinstance(found, dict) or not isinstance(found.get("positions"), list) \
                or type(found.get("count")) is not int or found["count"] < 0:
            raise ValueError("Eightfold search must return data.positions and data.count")
        return found["positions"], found["count"]

    rows, searches = read_pages(page, searches=options["search"], page_size=PAGE_SIZE,
                                max_pages=options["max_pages"], key=_key)
    return snapshot("eightfold", board, searches, rows, today=today)


def _key(row) -> str | None:
    value = row.get("id") if isinstance(row, dict) else None
    value = str(value) if type(value) is int else value
    return value if isinstance(value, str) and POSITION.fullmatch(value) else None


def parse_eightfold_board(content: bytes, *, company: str | None = None) -> ParsedFeed:
    return parse_snapshot(content, platform="eightfold", label="Eightfold", board_url=eightfold_board_url,
                          company=company, record=_record)


def _record(row: dict, snap: Snapshot) -> IngestRecord:
    position = row.get("id")
    position = str(position) if type(position) is int else position
    if not isinstance(position, str) or not POSITION.fullmatch(position):
        raise RowError("job needs a numeric id")
    title = required_text(row, "name", "a name")
    parts = urlsplit(snap.board)
    url = canonical_url(f"https://{parts.hostname}/careers/job/{position}?{parts.query}")
    if url is None:
        raise RowError("job id does not form an HTTPS URL")
    locations = row.get("locations")
    if locations is not None and not isinstance(locations, list):
        raise RowError("locations must be a list")
    standardized = row.get("standardizedLocations")  # "CO,US": state, country
    codes = [value.rsplit(",", 1)[-1] for value in standardized if isinstance(value, str)] \
        if isinstance(standardized, list) else []
    posted = row.get("postedTs")
    posted_at = datetime.fromtimestamp(posted, timezone.utc) if type(posted) is int and posted > 0 else None
    return IngestRecord(
        external_id=position, url=url, title=title,
        # The list never names the employer; without a configured name, the tenant stands in.
        company=snap.company or parts.hostname.split(".")[0],
        locations=places(locations or []), description="", posted_at=posted_at, active=True,
        metadata={"upstream": {"platform": "eightfold", "board_job_id": position,
                               "req_id": optional_text(row.get("displayJobId")), "country": one_country(codes),
                               # The department, as screening's category: Qualcomm titles are often just
                               # "Engineer", and "Software Engineering" / "Hardware Engineering" tell them apart.
                               "category": optional_text(row.get("department")),
                               "workplace": optional_text(row.get("workLocationOption"))}})


BOARD = PagedBoard(FORMAT, "Eightfold", OPTION_KEYS, eightfold_board_url, eightfold_options, read_eightfold_board,
                   parse_eightfold_board, company_required=True)

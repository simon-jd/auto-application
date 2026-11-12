"""iCIMS career sites on the Jibe front end as employer boards (Garmin, AMD, Lutron, Rivian …).

A feed's location is the careers site, https://careers.<company>.com: the site whose job pages are
/jobs/<id> and whose search is /api/jobs. The reader GETs /api/jobs 100 jobs a page, newest first, one
search per configured search text, with the configured country, and writes one snapshot. Unlike the
other paged boards, every job in the list carries its full description, so imported jobs need no
enrichment. The country filter keeps a job with any location in that country; a job's other
locations stay in its record.
"""

from datetime import date
import re

import httpx

from auto_app.ingest.models import IngestRecord, ParsedFeed, ReadResult
from auto_app.ingest.repository_paged import (PagedBoard, Pager, RowError, Snapshot, https_parts, one_country,
                                              option_text, optional_text, page_cap, parse_snapshot, places,
                                              read_pages, required_text, search_texts, snapshot, utc_time)
from auto_app.ingest.urls import canonical_url
from auto_app.sources.common.text import employment_of, html_to_text

FORMAT = "icims-jibe-board"
PAGE_SIZE = 100
MAX_PAGES = 100
DEFAULT_MAX_PAGES = 30
OPTION_KEYS = ("country", "search", "max_pages")
USAGE = "the careers site https://<host> whose jobs are /jobs/<id>, e.g. https://careers.garmin.com"

HOST = re.compile(r"(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")
SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
SECTIONS = (("responsibilities", "Responsibilities"), ("qualifications", "Qualifications"))


def jibe_board_url(location) -> str:
    """The canonical careers site, https://<host>; any path is dropped."""
    host = https_parts(location, USAGE).hostname.lower()
    if not HOST.fullmatch(host):
        raise ValueError(f"an icims-jibe-board location must be {USAGE}")
    return f"https://{host}"


def jibe_options(definition: dict) -> dict:
    return {"country": option_text(definition, "country", hint="a country as the site names it (United States)"),
            "search": search_texts(definition.get("search")),
            "max_pages": page_cap(definition, DEFAULT_MAX_PAGES, MAX_PAGES)}


def read_jibe_board(location: str, *, country: str | None = None, search=(), max_pages: int = DEFAULT_MAX_PAGES,
                    client: httpx.Client | None = None, today: date | None = None) -> ReadResult:
    """Read every configured search into one snapshot; HTTP and response-shape errors fail the read."""
    board = jibe_board_url(location)
    options = jibe_options({"country": country, "search": search if isinstance(search, str) else list(search),
                            "max_pages": max_pages})
    if client is None:
        with httpx.Client(timeout=60, follow_redirects=False) as owned:
            return read_jibe_board(board, client=owned, today=today, **options)
    pager = Pager(client, "Jibe search")

    def page(text: str, number: int):
        params = {"page": number + 1, "limit": PAGE_SIZE, "sortBy": "posted_date", "descending": "true"}
        if text:
            params["keywords"] = text
        if options["country"]:
            params["country"] = options["country"]
        data = pager.get(board + "/api/jobs", params)
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list) \
                or type(data.get("totalCount")) is not int or data["totalCount"] < 0:
            raise ValueError("Jibe search must return a jobs array and a totalCount")
        # Each job is {"data": {...}}; its meta_data (search-engine and ATS bookkeeping) is not kept.
        return [{key: value for key, value in job["data"].items() if key != "meta_data"}
                if isinstance(job, dict) and isinstance(job.get("data"), dict) else job
                for job in data["jobs"]], data["totalCount"]

    rows, searches = read_pages(page, searches=options["search"], page_size=PAGE_SIZE,
                                max_pages=options["max_pages"], key=_key)
    return snapshot("icims-jibe", board, searches, rows, today=today)


def _key(row) -> str | None:
    value = row.get("slug") if isinstance(row, dict) else None
    return value if isinstance(value, str) and SLUG.fullmatch(value) else None


def parse_jibe_board(content: bytes, *, company: str | None = None) -> ParsedFeed:
    return parse_snapshot(content, platform="icims-jibe", label="Jibe", board_url=jibe_board_url, company=company,
                          record=_record)


def _record(row: dict, snap: Snapshot) -> IngestRecord:
    slug = row.get("slug")
    if not isinstance(slug, str) or not SLUG.fullmatch(slug):
        raise RowError("job needs a slug like 20524")
    title = required_text(row, "title", "a title")
    url = canonical_url(f"{snap.board}/jobs/{slug}")
    if url is None:
        raise RowError("job slug does not form an HTTPS URL")
    others = row.get("additional_locations") or []
    if not isinstance(others, list) or any(not isinstance(place, dict) for place in others):
        raise RowError("additional_locations must be a list of objects")
    located = [row, *others]
    found = places(_place(place) for place in located) or places([row.get("full_location")])
    return IngestRecord(
        external_id=slug, url=url, title=title,
        company=snap.company or optional_text(row.get("hiring_organization")) or snap.board.split("//", 1)[1],
        locations=found, description=_description(row), posted_at=utc_time(row.get("posted_date")),
        employment_type=employment_of((optional_text(row.get("employment_type")) or "").replace("_", "-")),
        active=True,
        metadata={"upstream": {"platform": "icims-jibe", "board_job_id": slug,
                               "req_id": optional_text(row.get("req_id")) or slug,
                               "country": one_country(place.get("country_code") for place in located)}})


def _place(place: dict) -> str:
    """"Olathe, Kansas, United States": the country stays in the text, so a place abroad reads as abroad."""
    return ", ".join(value.strip() for value in (place.get("city"), place.get("state"), place.get("country"))
                     if isinstance(value, str) and value.strip())


def _description(row: dict) -> str:
    """The description, which normally contains the responsibilities and qualifications; either is added
    when it does not."""
    for key in ("description", *(key for key, _ in SECTIONS)):
        if row.get(key) is not None and not isinstance(row[key], str):
            raise RowError(f"{key} must be text")
    text = html_to_text(row.get("description"))
    flat = " ".join(text.split())
    for key, heading in SECTIONS:
        part = html_to_text(row.get(key))
        if part and " ".join(part.split())[:200] not in flat:
            text = f"{text}\n\n{heading}\n{part}" if text else f"{heading}\n{part}"
    return text


BOARD = PagedBoard(FORMAT, "Jibe", OPTION_KEYS, jibe_board_url, jibe_options, read_jibe_board, parse_jibe_board)

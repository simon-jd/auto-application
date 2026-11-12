"""SmartRecruiters companies as employer boards, read through the public Posting API (Bosch, Intuitive …).

A feed's location is the company's careers page, https://careers.smartrecruiters.com/<CompanyIdentifier>.
The reader GETs api.smartrecruiters.com/v1/companies/<id>/postings 100 postings a page (the API's most),
one search per configured search text, with the configured country (an ISO code such as us), and writes
one snapshot.

The list has no job description, so imported postings stay pending: the worker's enrichment step reads
each posting that screening keeps from /postings/<id> (enrich/sites.py), through the posting's public
link https://jobs.smartrecruiters.com/<CompanyIdentifier>/<id>.
"""

from datetime import date
import re

import httpx

from auto_app.enrich.sites import SR_EMPLOYMENT
from auto_app.ingest.models import IngestRecord, ParsedFeed, ReadResult
from auto_app.ingest.repository_paged import (PagedBoard, Pager, RowError, Snapshot, https_parts, one_country,
                                              option_text, optional_text, page_cap, parse_snapshot, places,
                                              read_pages, required_text, search_texts, snapshot, utc_time)
from auto_app.ingest.urls import canonical_url
from auto_app.sources.common.text import employment_of

FORMAT = "smartrecruiters-board"
PAGE_SIZE = 100  # the API's maximum
MAX_PAGES = 100
DEFAULT_MAX_PAGES = 20
OPTION_KEYS = ("country", "search", "max_pages")
USAGE = "the careers page https://careers.smartrecruiters.com/<CompanyIdentifier>"
API = "https://api.smartrecruiters.com/v1/companies/{}/postings"

PAGES = {"careers.smartrecruiters.com", "jobs.smartrecruiters.com"}
COMPANY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")
POSTING = re.compile(r"[0-9]{4,20}")
COUNTRY = re.compile(r"[A-Za-z]{2}")


def smartrecruiters_board_url(location) -> str:
    """The canonical careers page, https://careers.smartrecruiters.com/<CompanyIdentifier>."""
    parts = https_parts(location, USAGE)
    host, segments = parts.hostname.lower(), [segment for segment in parts.path.split("/") if segment]
    if host in PAGES and segments:
        company = segments[0]
    elif host == "api.smartrecruiters.com" and len(segments) >= 3 and segments[:2] == ["v1", "companies"]:
        company = segments[2]
    else:
        raise ValueError(f"a smartrecruiters-board location must be {USAGE}")
    if not COMPANY.fullmatch(company):
        raise ValueError(f"a smartrecruiters-board location needs its company identifier: {USAGE}")
    return f"https://careers.smartrecruiters.com/{company}"


def smartrecruiters_options(definition: dict) -> dict:
    country = option_text(definition, "country", hint="a two-letter ISO country code (us)", pattern=COUNTRY)
    return {"country": country.lower() if country else None, "search": search_texts(definition.get("search")),
            "max_pages": page_cap(definition, DEFAULT_MAX_PAGES, MAX_PAGES)}


def read_smartrecruiters_board(location: str, *, country: str | None = None, search=(),
                               max_pages: int = DEFAULT_MAX_PAGES, client: httpx.Client | None = None,
                               today: date | None = None) -> ReadResult:
    """Read every configured search into one snapshot; HTTP and response-shape errors fail the read."""
    board = smartrecruiters_board_url(location)
    options = smartrecruiters_options({"country": country, "max_pages": max_pages,
                                       "search": search if isinstance(search, str) else list(search)})
    if client is None:
        with httpx.Client(timeout=30, follow_redirects=False) as owned:
            return read_smartrecruiters_board(board, client=owned, today=today, **options)
    api = API.format(board.rsplit("/", 1)[1])
    pager = Pager(client, "SmartRecruiters postings")

    def page(text: str, number: int):
        params = {"limit": PAGE_SIZE, "offset": number * PAGE_SIZE}
        if options["country"]:
            params["country"] = options["country"]
        if text:
            params["q"] = text
        data = pager.get(api, params)
        if not isinstance(data, dict) or not isinstance(data.get("content"), list) \
                or type(data.get("totalFound")) is not int or data["totalFound"] < 0:
            raise ValueError("SmartRecruiters postings must return a content array and a totalFound")
        return data["content"], data["totalFound"]

    rows, searches = read_pages(page, searches=options["search"], page_size=PAGE_SIZE,
                                max_pages=options["max_pages"], key=_key)
    return snapshot("smartrecruiters", board, searches, rows, today=today)


def _key(row) -> str | None:
    value = row.get("id") if isinstance(row, dict) else None
    return value if isinstance(value, str) and POSTING.fullmatch(value) else None


def parse_smartrecruiters_board(content: bytes, *, company: str | None = None) -> ParsedFeed:
    return parse_snapshot(content, platform="smartrecruiters", label="SmartRecruiters",
                          board_url=smartrecruiters_board_url, company=company, record=_record)


def _record(row: dict, snap: Snapshot) -> IngestRecord:
    posting = row.get("id")
    if not isinstance(posting, str) or not POSTING.fullmatch(posting):
        raise RowError("job needs a numeric id")
    title = required_text(row, "name", "a name")
    identifier = snap.board.rsplit("/", 1)[1]
    url = canonical_url(f"https://jobs.smartrecruiters.com/{identifier}/{posting}")
    if url is None:
        raise RowError("job id does not form an HTTPS URL")
    place = row.get("location") or {}
    kind, level, employer = (row.get(key) or {} for key in ("typeOfEmployment", "experienceLevel", "company"))
    if not all(isinstance(value, dict) for value in (place, kind, level, employer)):
        raise RowError("location, typeOfEmployment, experienceLevel and company must be objects")
    found = places([place.get("fullLocation")]) or places([", ".join(
        value.strip() for value in (place.get("city"), place.get("region"), place.get("country"))
        if isinstance(value, str) and value.strip())])
    return IngestRecord(
        external_id=posting, url=url, title=title,
        company=snap.company or optional_text(employer.get("name")) or identifier,
        locations=found, description="", posted_at=utc_time(row.get("releasedDate")),
        employment_type=SR_EMPLOYMENT.get(kind.get("id")) or employment_of(optional_text(kind.get("label"))),
        active=True,
        metadata={"upstream": {"platform": "smartrecruiters", "board_job_id": posting,
                               "req_id": optional_text(row.get("refNumber")),
                               "country": one_country([place.get("country")]),
                               "experience_level": optional_text(level.get("id"))}})


BOARD = PagedBoard(FORMAT, "SmartRecruiters", OPTION_KEYS, smartrecruiters_board_url, smartrecruiters_options,
                   read_smartrecruiters_board, parse_smartrecruiters_board)

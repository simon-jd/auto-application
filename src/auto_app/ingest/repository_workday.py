"""Workday career sites as employer boards, read through the public CXS job search.

A ``workday-board`` feed's location is the careers URL, ``https://<tenant>.wd<N>.myworkdayjobs.com/<site>``.
The reader POSTs ``/wday/cxs/<tenant>/<site>/jobs`` page by page (20 postings, Workday's maximum), one
search per configured search text, with facets named by the site's own labels, pausing between requests,
and writes one JSON snapshot. The parser turns that snapshot into records without network access.

Workday's list carries no job description, so imported postings stay pending: the worker's enrichment
step reads each posting that screening keeps from the CXS detail endpoint (docs/linux-worker.md §6).
Workday reports ``total`` on the first page only (later pages say 0) and lists at most 2000 results. A
search that ends before its total, or at the page cap, makes the snapshot partial, so a posting that was
not read is never closed.
"""

from datetime import date, datetime, timezone
import hashlib
import json
import re
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx

from auto_app.ingest.models import IngestRecord, ParsedFeed, ReadResult
from auto_app.ingest.repository_paged import PagedBoard
from auto_app.ingest.urls import canonical_url
from auto_app.platforms.workday import WorkdayFilters, WorkdaySource, posted_from_text
from auto_app.sources.common.text import employment_of
from auto_app.sources.common.urls import workday_job_id

PAGE_SIZE = 20  # Workday answers a larger limit with HTTP 400
RESULT_CAP = 2000  # Workday lists no further, whatever the total
MAX_PAGES = RESULT_CAP // PAGE_SIZE
DEFAULT_MAX_PAGES = 25
PAUSE_SECONDS = 1.0  # between two requests to the same site
OPTION_KEYS = ("search", "facets", "max_pages")
USAGE = "the careers URL https://<tenant>.wd<N>.myworkdayjobs.com/<site>"

HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.wd\d{1,3}\.myworkdayjobs\.com")
SITE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
LOCALE = re.compile(r"[a-z]{2}-[A-Z]{2}")
JOB_PATH = re.compile(r"/job(?:/[^/?#\\\s]+)+")
SEVERAL_PLACES = re.compile(r"\d+\s+locations?", re.I)

_sleep = time.sleep


def workday_board_url(location) -> str:
    """The canonical careers URL, https://<host>/<site>; a locale or a job path after the site is dropped."""
    if not isinstance(location, str) or not location.strip():
        raise ValueError(f"a workday-board location must be {USAGE}")
    try:
        parts = urlsplit(location.strip())
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"a workday-board location must be {USAGE}") from exc
    host = (parts.hostname or "").lower()
    if (parts.scheme not in ("http", "https") or not HOST.fullmatch(host) or port is not None
            or parts.username is not None or parts.password is not None):
        raise ValueError(f"a workday-board location must be {USAGE} on myworkdayjobs.com")
    segments = [segment for segment in parts.path.split("/") if segment]
    if segments and LOCALE.fullmatch(segments[0]):
        segments = segments[1:]
    if not segments or not SITE.fullmatch(segments[0]):
        raise ValueError(f"a workday-board location needs its site name: {USAGE}")
    return f"https://{host}/{segments[0]}"


def workday_options(definition: dict, *, format: str) -> dict | None:
    """Validated search, facets and max_pages for a workday-board feed; None for another format."""
    given = [key for key in OPTION_KEYS if key in definition]
    if format != "workday-board":
        if given:
            raise ValueError(f"{', '.join(given)} apply only to format workday-board")
        return None
    search = definition.get("search", [])
    if isinstance(search, str):
        search = [search]
    if not isinstance(search, list) or any(not isinstance(text, str) or not text.strip() or len(text) > 200
                                           for text in search):
        raise ValueError("search must be a search text or a list of nonempty search texts")
    facets = definition.get("facets", {})
    if not isinstance(facets, dict) or any(
            not isinstance(name, str) or not name.strip() or not isinstance(values, list) or not values
            or any(not isinstance(value, str) or not value.strip() for value in values)
            for name, values in facets.items()):
        raise ValueError("facets must map a Workday facet name to a nonempty list of values as shown on the site")
    max_pages = definition.get("max_pages", DEFAULT_MAX_PAGES)
    if type(max_pages) is not int or not 1 <= max_pages <= MAX_PAGES:
        raise ValueError(f"max_pages must be a whole number from 1 to {MAX_PAGES}")
    return {"search": list(dict.fromkeys(text.strip() for text in search)),
            "facets": {name.strip(): [value.strip() for value in values] for name, values in facets.items()},
            "max_pages": max_pages}


def read_workday_board(location: str, *, search=(), facets: dict | None = None,
                       max_pages: int = DEFAULT_MAX_PAGES, client: httpx.Client | None = None,
                       today: date | None = None) -> ReadResult:
    """Read every configured search into one snapshot; HTTP and response-shape errors fail the read."""
    board = workday_board_url(location)
    options = workday_options({"search": list(search), "facets": facets or {}, "max_pages": max_pages},
                              format="workday-board")
    if client is None:
        with httpx.Client(timeout=30, follow_redirects=False) as owned:
            return read_workday_board(board, search=search, facets=facets, max_pages=max_pages, client=owned,
                                      today=today)
    host, site = urlsplit(board).hostname, board.rsplit("/", 1)[1]
    api = f"https://{host}/wday/cxs/{host.split('.')[0]}/{site}/jobs"
    sent = 0

    def post(body: dict) -> dict:
        nonlocal sent
        if sent:
            _sleep(PAUSE_SECONDS)
        sent += 1
        response = client.post(api, json=body, headers={"Accept": "application/json"})
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as exc:
            raise ValueError("Workday search did not return JSON") from exc
        total = data.get("total") if isinstance(data, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("jobPostings"), list) \
                or type(total) is not int or total < 0:
            raise ValueError("Workday search must return a jobPostings array and a total")
        return data

    applied = {}
    if options["facets"]:
        listing = post({"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
        if not isinstance(listing.get("facets"), list):
            raise ValueError("Workday search returned no facets to resolve")
        query = SimpleNamespace(locations=[], employment_types=[],
                                site_filters=WorkdayFilters(facets=options["facets"]))
        try:
            applied, _ = WorkdaySource().applied_facets(listing["facets"], query)
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError("Workday facets are malformed") from exc

    rows, searches = {}, []
    for text in options["search"] or [""]:
        total, read, pages, ended = None, set(), 0, False
        while pages < options["max_pages"] and pages * PAGE_SIZE < RESULT_CAP:
            data = post({"appliedFacets": applied, "limit": PAGE_SIZE, "offset": pages * PAGE_SIZE,
                         "searchText": text})
            pages += 1
            if total is None:
                total = data["total"]  # later pages report 0
            for row in data["jobPostings"]:
                path = row.get("externalPath") if isinstance(row, dict) else None
                key = path if isinstance(path, str) else json.dumps(row, sort_keys=True, ensure_ascii=False)
                rows.setdefault(key, row)
                read.add(key)
            if len(data["jobPostings"]) < PAGE_SIZE or pages * PAGE_SIZE >= total:
                ended = True
                break
        searches.append({"search_text": text, "total": total, "read": len(read), "pages": pages,
                         "complete": ended and total < RESULT_CAP and len(read) >= total})
    document = {"platform": "workday", "board": board,
                "fetched_on": (today or datetime.now(timezone.utc).date()).isoformat(),
                "applied_facets": applied, "searches": searches, "jobs": [rows[key] for key in sorted(rows)]}
    content = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return ReadResult(content=content, revision="sha256:" + hashlib.sha256(content).hexdigest())


def parse_workday_board(content: bytes, *, company: str | None = None) -> ParsedFeed:
    if company is not None and (not isinstance(company, str) or not company.strip()):
        raise ValueError("company must be a nonempty string")
    try:
        document = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("a workday-board snapshot must contain valid JSON") from exc
    if not isinstance(document, dict) or not isinstance(document.get("jobs"), list) \
            or not isinstance(document.get("searches"), list):
        raise ValueError("a workday-board snapshot must be an object with jobs and searches arrays")
    board = workday_board_url(document.get("board"))
    try:
        fetched = date.fromisoformat(document.get("fetched_on"))
    except (TypeError, ValueError) as exc:
        raise ValueError("a workday-board snapshot needs its fetched_on date") from exc
    # The list never names the employer; without a configured name, the tenant stands in.
    company = company.strip() if company else urlsplit(board).hostname.split(".")[0]
    now = datetime(fetched.year, fetched.month, fetched.day, tzinfo=timezone.utc)
    result, seen = ParsedFeed(), set()
    for index, row in enumerate(document["jobs"]):
        try:
            record = _record(row, board, now, company)
            if record.external_id in seen:
                raise ValueError("duplicate job id")
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            # Messages are the parser's own; a row's values are never echoed.
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            result.issues.append({"index": index, "reason": f"Invalid Workday job ({reason})"})
            continue
        seen.add(record.external_id)
        result.records.append(record)
    for search in document["searches"]:
        if not isinstance(search, dict) or search.get("complete") is not True:
            result.issues.append(_incomplete(search if isinstance(search, dict) else {}))
    return result


def _incomplete(search: dict) -> dict:
    text, total, read, pages = (search.get(key) for key in ("search_text", "total", "read", "pages"))
    label = f"Workday search {text!r}" if text else "Workday listing"
    if isinstance(total, int) and total >= RESULT_CAP:
        reason = (f"{label} has {total}+ postings; Workday lists at most {RESULT_CAP}, so narrow it with "
                  "search or facets")
    else:
        reason = (f"{label} stopped after {pages} page(s) with {read} of {total} postings; raise max_pages "
                  "or narrow it with search or facets")
    return {"search": text, "reason": reason + " (postings not read are kept, not closed)"}


def _record(row, board: str, now: datetime, company: str) -> IngestRecord:
    if not isinstance(row, dict):
        raise ValueError("job must be an object")
    path = row.get("externalPath")
    if not isinstance(path, str) or not JOB_PATH.fullmatch(path) or any(
            segment in (".", "..") for segment in path.split("/")):
        raise ValueError("job needs an externalPath like /job/<location>/<title>_<id>")
    title = row.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("job needs a title")
    url = canonical_url(board + path)
    if url is None:
        raise ValueError("job path does not form an HTTPS URL")
    segment = path.rsplit("/", 1)[-1]
    job_id = workday_job_id(segment)
    bullets = row.get("bulletFields")
    bullets = [value.strip() for value in bullets if isinstance(value, str)] if isinstance(bullets, list) else []
    # A location copy of a requisition ends in -1, -2 …; the requisition is listed among the bullets.
    req_id = next((value for value in bullets if value and (job_id == value or job_id.startswith(value + "-"))),
                  job_id if job_id != segment else None)
    places = row.get("locationsText")
    if places is not None and not isinstance(places, str):
        raise ValueError("locationsText must be text")
    places = (places or "").strip()
    posted_on = row.get("postedOn") if isinstance(row.get("postedOn"), str) else None
    posted_at = posted_from_text(posted_on, now) if posted_on else None
    time_type = row.get("timeType") if isinstance(row.get("timeType"), str) else None
    remote_type = row.get("remoteType") if isinstance(row.get("remoteType"), str) else None
    metadata = {"upstream": {"platform": "workday", "board_job_id": job_id, "req_id": req_id,
                             "locations_text": places or None, "remote_type": remote_type, "time_type": time_type},
                # Ingestion leaves this out of the content fingerprint: "Posted 3 Days Ago" is an observation.
                "source_observation": {"postedOn": posted_on, "fetched_on": now.date().isoformat()}}
    if posted_at is not None:
        # Relative to the read's UTC date, while the site counts days in its own time zone.
        metadata["posted_at_precision"] = "approx"
    return IngestRecord(
        external_id=job_id, url=url, title=title.strip(), company=company,
        # "4 Locations" only counts them; the job's detail lists them (enrichment).
        locations=[] if not places or SEVERAL_PLACES.fullmatch(places) else [places],
        description="", posted_at=posted_at, employment_type=employment_of(time_type), active=True,
        metadata=metadata)


BOARD = PagedBoard("workday-board", "Workday", OPTION_KEYS, workday_board_url,
                   lambda definition: workday_options(definition, format="workday-board"), read_workday_board,
                   parse_workday_board, company_required=True)

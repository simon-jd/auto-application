"""Oracle Recruiting Cloud career sites (Candidate Experience) as employer boards (Ford, TI, Honeywell …).

A feed's location is the careers site,
https://<pod>.fa.<region>.oraclecloud.com/hcmUI/CandidateExperience/en/sites/<site>. The reader GETs the
public recruitingCEJobRequisitions finder 100 requisitions a page (the site itself asks 25), newest first,
one search per configured search text, and writes one snapshot. A configured country names one of the
site's location facets ("United States"); one extra request resolves it to the facet's id.

The list carries only a short summary, so imported requisitions stay pending: the worker's enrichment
step reads each one that screening keeps from recruitingCEJobRequisitionDetails (enrich/sites.py).
"""

from datetime import date, datetime, timezone
import re
from urllib.parse import quote

import httpx

from auto_app.ingest.models import IngestRecord, ParsedFeed, ReadResult
from auto_app.ingest.repository_paged import (PagedBoard, Pager, RowError, Snapshot, https_parts, one_country,
                                              option_text, optional_text, page_cap, parse_snapshot, places,
                                              read_pages, required_text, search_texts, snapshot)
from auto_app.ingest.urls import canonical_url
from auto_app.sources.common.text import employment_of

FORMAT = "oracle-board"
PAGE_SIZE = 100
MAX_PAGES = 100
DEFAULT_MAX_PAGES = 20
OPTION_KEYS = ("country", "search", "max_pages")
USAGE = ("the careers site https://<pod>.fa.<region>.oraclecloud.com/hcmUI/CandidateExperience/en/sites/<site>")
API = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"

HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.fa\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.oraclecloud\.com")
LANGUAGE = re.compile(r"[a-z]{2}(?:-[A-Za-z]{2})?")
SITE = re.compile(r"[A-Za-z0-9_]{1,40}")
REQUISITION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}")  # mostly digits; imported ones look like req480976
POSTED = re.compile(r"\d{4}-\d{2}-\d{2}")


def oracle_board_url(location) -> str:
    """The canonical careers site, https://<host>/hcmUI/CandidateExperience/en/sites/<site>; a job path is dropped."""
    parts = https_parts(location, USAGE)
    host = parts.hostname.lower()
    if not HOST.fullmatch(host):
        raise ValueError(f"an oracle-board location must be {USAGE}")
    segments = [segment for segment in parts.path.split("/") if segment]
    if (len(segments) < 5 or [s.lower() for s in segments[:2]] != ["hcmui", "candidateexperience"]
            or not LANGUAGE.fullmatch(segments[2]) or segments[3] != "sites" or not SITE.fullmatch(segments[4])):
        raise ValueError(f"an oracle-board location needs its site: {USAGE}")
    return f"https://{host}/hcmUI/CandidateExperience/en/sites/{segments[4]}"


def oracle_options(definition: dict) -> dict:
    search = search_texts(definition.get("search"))
    if any(char in text for text in search for char in ',;"'):
        raise ValueError('an Oracle search text cannot contain , ; or " (the finder syntax uses them)')
    return {"country": option_text(definition, "country",
                                   hint="the name of one of the site's location facets (United States)"),
            "search": search, "max_pages": page_cap(definition, DEFAULT_MAX_PAGES, MAX_PAGES)}


def read_oracle_board(location: str, *, country: str | None = None, search=(), max_pages: int = DEFAULT_MAX_PAGES,
                      client: httpx.Client | None = None, today: date | None = None) -> ReadResult:
    """Read every configured search into one snapshot; HTTP and response-shape errors fail the read."""
    board = oracle_board_url(location)
    options = oracle_options({"country": country, "search": search if isinstance(search, str) else list(search),
                              "max_pages": max_pages})
    if client is None:
        with httpx.Client(timeout=60, follow_redirects=False) as owned:
            return read_oracle_board(board, client=owned, today=today, **options)
    host, site = board.split("/")[2], board.rsplit("/", 1)[1]
    pager = Pager(client, "Oracle search")

    def search_page(listed: bool, **fields) -> dict:
        # The finder's own syntax: findReqs;name=value,… (a keyword is quoted and percent-encoded).
        # Requisitions come only with expand; the facet request does without them.
        finder = ",".join(f"{name}={value}" for name, value in fields.items())
        data = pager.get(f"https://{host}{API}?onlyData=true"
                         + ("&expand=requisitionList.secondaryLocations" if listed else "")
                         + f"&finder=findReqs;siteNumber={site},facetsList=LOCATIONS,{finder}")
        items = data.get("items") if isinstance(data, dict) else None
        found = items[0] if isinstance(items, list) and items else None
        if not isinstance(found, dict) or (listed and not isinstance(found.get("requisitionList"), list)) \
                or type(found.get("TotalJobsCount")) is not int or found["TotalJobsCount"] < 0:
            raise ValueError("Oracle search must return a requisitionList and a TotalJobsCount")
        return found

    chosen = {}
    if options["country"]:
        facets = search_page(False, limit=1, offset=0).get("locationsFacet")
        if not isinstance(facets, list):
            raise ValueError("Oracle search returned no location facets to resolve")
        wanted = options["country"].casefold()
        match = next((facet["Id"] for facet in facets if isinstance(facet, dict) and type(facet.get("Id")) is int
                      and isinstance(facet.get("Name"), str) and facet["Name"].strip().casefold() == wanted), None)
        if match is None:
            raise ValueError(f"Oracle location {options['country']!r} is not one of the site's location facets")
        chosen["selectedLocationsFacet"] = match

    def page(text: str, number: int):
        keyword = {"keyword": "%22" + quote(text, safe="") + "%22"} if text else {}
        found = search_page(True, limit=PAGE_SIZE, offset=number * PAGE_SIZE, sortBy="POSTING_DATES_DESC",
                            **keyword, **chosen)
        return found["requisitionList"], found["TotalJobsCount"]

    rows, searches = read_pages(page, searches=options["search"], page_size=PAGE_SIZE,
                                max_pages=options["max_pages"], key=_key)
    return snapshot("oracle", board, searches, rows, today=today)


def _id(row: dict):
    value = row.get("Id")
    return str(value) if type(value) is int else value  # a string at every site seen; a number is the same id


def _key(row) -> str | None:
    value = _id(row) if isinstance(row, dict) else None
    return value if isinstance(value, str) and REQUISITION.fullmatch(value) else None


def parse_oracle_board(content: bytes, *, company: str | None = None) -> ParsedFeed:
    return parse_snapshot(content, platform="oracle", label="Oracle", board_url=oracle_board_url, company=company,
                          record=_record)


def _record(row: dict, snap: Snapshot) -> IngestRecord:
    requisition = _id(row)
    if not isinstance(requisition, str) or not REQUISITION.fullmatch(requisition):
        raise RowError("job needs an Id like 72872")
    title = required_text(row, "Title", "a title")
    url = canonical_url(f"{snap.board}/job/{requisition}")
    if url is None:
        raise RowError("job Id does not form an HTTPS URL")
    secondary = row.get("secondaryLocations") or []
    if not isinstance(secondary, list) or any(not isinstance(place, dict) for place in secondary):
        raise RowError("secondaryLocations must be a list of objects")
    posted = row.get("PostedDate")
    posted_at = None
    if isinstance(posted, str) and POSTED.fullmatch(posted.strip()):
        try:
            posted_at = datetime.combine(date.fromisoformat(posted.strip()), datetime.min.time(), timezone.utc)
        except ValueError:
            posted_at = None
    metadata = {"upstream": {"platform": "oracle", "board_job_id": requisition, "req_id": requisition,
                             "country": one_country([row.get("PrimaryLocationCountry"),
                                                     *(place.get("CountryCode") for place in secondary)])}}
    if posted_at is not None:
        metadata["posted_at_precision"] = "day"  # the list gives the date only
    return IngestRecord(
        external_id=requisition, url=url, title=title,
        # The list never names the employer; without a configured name, the pod stands in.
        company=snap.company or snap.board.split("/")[2].split(".")[0],
        locations=places([row.get("PrimaryLocation"), *(place.get("Name") for place in secondary)]),
        description="", posted_at=posted_at, employment_type=employment_of(optional_text(row.get("JobSchedule"))),
        active=True, metadata=metadata)


BOARD = PagedBoard(FORMAT, "Oracle", OPTION_KEYS, oracle_board_url, oracle_options, read_oracle_board,
                   parse_oracle_board, company_required=True)

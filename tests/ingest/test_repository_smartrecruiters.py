"""SmartRecruiters companies as employer boards: paged Posting API, enrichment links, snapshot safety.

The fixtures are recorded from Western Digital's postings (2026-11-06): the search "embedded" in the
United States, 4 postings a page.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import yaml

from auto_app.enrich.targets import target_for
from auto_app.ingest import repository_paged as paged
from auto_app.ingest import repository_smartrecruiters as sr
from auto_app.ingest.repository import parse_repository
from auto_app.ingest.runner import load_feed_config, run_config, run_repository
from tests.imported_jobs import make_store

FIXTURES = Path(__file__).parents[1] / "fixtures" / "smartrecruiters"
WDC = "https://careers.smartrecruiters.com/WesternDigital"
API = "https://api.smartrecruiters.com/v1/companies/WesternDigital/postings"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture(autouse=True)
def pauses(monkeypatch):
    slept = []
    monkeypatch.setattr(paged, "_sleep", slept.append)
    monkeypatch.setattr(sr, "PAGE_SIZE", 4)  # the fixtures' page size
    return slept


class Api:
    """Answers like the Posting API: recorded pages by offset, an empty page after them."""

    def __init__(self, pages=None):
        self.pages = pages or {0: fixture("wdc_embedded_us_p0"), 4: fixture("wdc_embedded_us_p1")}
        self.params = []

    def __call__(self, request):
        assert request.method == "GET" and str(request.url).startswith(API + "?")
        self.params.append(dict(request.url.params))
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json=self.pages.get(offset, {"offset": offset, "totalFound": 0, "content": []}))


def read(handler, **options):
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return sr.read_smartrecruiters_board(WDC, client=client, **options)


def parse(content, company=None):
    return parse_repository(content, format="smartrecruiters-board", company=company)


def by_id(parsed):
    return {r.external_id: r for r in parsed.records}


def test_reads_every_page(pauses):
    api = Api()
    result = read(api, country="US", search=["embedded"])
    assert api.params == [{"limit": "4", "offset": offset, "country": "us", "q": "embedded"} for offset in ("0", "4")]
    assert pauses == [paged.PAUSE_SECONDS]
    parsed = parse(result.content)
    assert len(parsed.records) == 7 and not parsed.issues


def test_records_carry_identity_level_and_an_enrichment_link():
    records = by_id(parse(read(Api(), country="us", search="embedded").content))
    engineer = records["744000138717897"]
    assert engineer.title == "Software Engineer" and engineer.company == "Western Digital"
    assert engineer.url == "https://jobs.smartrecruiters.com/WesternDigital/744000138717897"
    assert engineer.locations == ["San Jose, CA, United States"] and engineer.description == ""
    assert engineer.posted_at == datetime(2026, 7, 20, 19, 48, 3, 634000, tzinfo=timezone.utc)
    assert engineer.employment_type == "full_time"
    assert engineer.metadata == {"upstream": {"platform": "smartrecruiters", "board_job_id": "744000138717897",
                                              "req_id": "REF33900Q", "country": "US",
                                              "experience_level": "entry_level"}}
    assert records["744000143171017"].employment_type == "internship"
    # The worker's enrichment reads the posting's description from the Posting API.
    target = target_for(engineer.url)
    assert target.platform == "smartrecruiters" and target.api_url == API + "/744000138717897"


def test_configured_company_wins():
    assert {r.company for r in parse(read(Api()).content, company="WD").records} == {"WD"}


def test_page_cap_leaves_a_partial_snapshot():
    api = Api()
    parsed = parse(read(api, max_pages=1).content)
    assert len(api.params) == 1 and len(parsed.records) == 4 and "4 of 7" in parsed.issues[0]["reason"]


def test_a_listing_that_ends_before_its_total_is_partial():
    parsed = parse(read(Api({0: fixture("wdc_embedded_us_p0")})).content)
    assert len(parsed.records) == 4 and "4 of 7" in parsed.issues[0]["reason"]


def test_malformed_rows_do_not_prevent_valid_siblings():
    good = fixture("wdc_embedded_us_p1")["content"][2]
    rows = [good, {**good, "id": "abc"}, {**good, "id": "1234567", "name": None},
            {**good, "id": "2345678", "location": "San Jose"}, {**good, "id": "3456789", "typeOfEmployment": "permanent"},
            {**good, "id": "4567890", "releasedDate": "yesterday"}, None]
    parsed = parse(read(lambda request: httpx.Response(200, json={"totalFound": len(rows), "content": rows})).content)
    assert [r.external_id for r in parsed.records] == ["4567890", good["id"]]
    assert by_id(parsed)["4567890"].posted_at is None
    assert len(parsed.issues) == 5


@pytest.mark.parametrize("response", [
    httpx.Response(200, content=b"<html>maintenance</html>"), httpx.Response(200, json={"content": []}),
    httpx.Response(200, json={"totalFound": 2}), httpx.Response(200, json={"totalFound": "2", "content": []}),
])
def test_bad_postings_response_is_an_error_instead_of_an_empty_board(response):
    with pytest.raises(ValueError):
        read(lambda request: response)


def test_an_unknown_company_fails_the_read():
    with pytest.raises(httpx.HTTPStatusError):
        read(lambda request: httpx.Response(404, json={"message": "Company not found"}))


@pytest.mark.parametrize("location", [
    WDC, WDC + "/", "https://jobs.smartrecruiters.com/WesternDigital/744000138717897-software-engineer",
    "https://api.smartrecruiters.com/v1/companies/WesternDigital/postings?country=us",
])
def test_careers_page_is_normalized(location):
    assert sr.smartrecruiters_board_url(location) == WDC


@pytest.mark.parametrize("location", [
    "https://careers.smartrecruiters.com/", "https://www.smartrecruiters.com/WesternDigital",
    "https://api.smartrecruiters.com/v1/WesternDigital", "https://careers.smartrecruiters.com/Western%20Digital",
    "https://careers.smartrecruiters.com.evil.example/WesternDigital", "feed.json", None,
])
def test_other_locations_are_not_smartrecruiters_boards(location):
    with pytest.raises(ValueError):
        sr.smartrecruiters_board_url(location)


def config(tmp_path, **definition):
    path = tmp_path / "feeds.yaml"
    path.write_text(yaml.safe_dump({"sources": {"board-sr-wdc": {
        "kind": "repository", "format": "smartrecruiters-board", "location": WDC, **definition}}}))
    return path


def test_feed_config_normalizes_a_smartrecruiters_board(tmp_path):
    loaded = load_feed_config(config(tmp_path, country="US", max_pages=5))["board-sr-wdc"]
    assert loaded["location"] == WDC and loaded["company"] is None
    assert loaded["options"] == {"country": "us", "search": [], "max_pages": 5}


@pytest.mark.parametrize("changes,message", [
    ({"country": "United States"}, "two-letter"), ({"country": "u1"}, "two-letter"),
    ({"location": "https://jobs.lever.co/acme"}, "careers.smartrecruiters.com"),
    ({"search": [3]}, "search"), ({"max_pages": "5"}, "max_pages"), ({"facets": {"a": ["b"]}}, "workday-board"),
])
def test_feed_config_rejects_bad_smartrecruiters_settings(tmp_path, changes, message):
    with pytest.raises(ValueError, match=message):
        load_feed_config(config(tmp_path, **changes))


def test_config_import_stays_pending_for_enrichment(tmp_path):
    store = make_store(tmp_path)
    try:
        with httpx.Client(transport=httpx.MockTransport(Api())) as client:
            report = run_config(store, config(tmp_path, country="us", search="embedded"),
                                client=client)["sources"]["board-sr-wdc"]
        assert report["status"] == "ok" and report["created"] == 7
        statuses = {store.job_inputs(j["job_id"])[0]["extras"]["ingestion"]["enrichment_status"]
                    for j in store.jobs()}
        assert statuses == {"pending"} and {j["company"] for j in store.jobs()} == {"Western Digital"}
    finally:
        store.engine.dispose()


def test_command_line_import(tmp_path):
    store = make_store(tmp_path)
    try:
        with httpx.Client(transport=httpx.MockTransport(api := Api())) as client:
            report = run_repository(store, "board-sr-wdc", API, client=client, format="smartrecruiters-board")
        assert report["created"] == 7 and "country" not in api.params[0]
        assert store.source_health("board-sr-wdc")["ingest"]["location"] == WDC
    finally:
        store.engine.dispose()

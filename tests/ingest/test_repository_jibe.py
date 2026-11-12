"""iCIMS Jibe career sites as employer boards: paged /api/jobs, full descriptions, snapshot safety.

The fixtures are recorded from Garmin's careers site (2026-11-06): the search "GNSS" in the United
States, 4 jobs a page, each job's meta_data cut to its iCIMS dates.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import yaml

from auto_app.enrich.fetch import USER_AGENT
from auto_app.ingest import repository_jibe as jibe
from auto_app.ingest import repository_paged as paged
from auto_app.ingest.repository import parse_repository
from auto_app.ingest.runner import load_feed_config, run_config, run_repository
from tests.imported_jobs import make_store

FIXTURES = Path(__file__).parents[1] / "fixtures" / "jibe"
GARMIN = "https://careers.garmin.com"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture(autouse=True)
def pauses(monkeypatch):
    slept = []
    monkeypatch.setattr(paged, "_sleep", slept.append)
    monkeypatch.setattr(jibe, "PAGE_SIZE", 4)  # the fixtures' page size
    return slept


class Site:
    """Answers like a Jibe careers site: recorded pages by page number, an empty page after them."""

    def __init__(self, pages=None):
        self.pages = pages or {1: fixture("garmin_gnss_us_p1"), 2: fixture("garmin_gnss_us_p2")}
        self.requests = []

    def __call__(self, request):
        assert request.method == "GET" and str(request.url).startswith(GARMIN + "/api/jobs?")
        self.requests.append(request)
        return httpx.Response(200, json=self.pages.get(int(request.url.params["page"]), {"jobs": [], "totalCount": 0}))


def read(handler, **options):
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return jibe.read_jibe_board(GARMIN, client=client, **options)


def parse(content, company="Garmin"):
    return parse_repository(content, format="icims-jibe-board", company=company)


def by_id(parsed):
    return {r.external_id: r for r in parsed.records}


def test_reads_every_page_newest_first_with_the_pipeline_user_agent(pauses):
    site = Site()
    result = read(site, country="United States", search=["GNSS"])
    assert [dict(r.url.params) for r in site.requests] == [
        {"page": str(page), "limit": "4", "sortBy": "posted_date", "descending": "true", "keywords": "GNSS",
         "country": "United States"} for page in (1, 2)]
    assert {r.headers["user-agent"] for r in site.requests} == {USER_AGENT}
    assert pauses == [paged.PAUSE_SECONDS]  # a pause before every request after the first
    parsed = parse(result.content)
    assert len(parsed.records) == 6 and not parsed.issues
    assert result.revision.startswith("sha256:") and not result.not_modified
    assert "meta_data" not in json.loads(result.content)["jobs"][0]  # bookkeeping is not kept


def test_records_carry_the_full_description_places_and_dates():
    records = by_id(parse(read(Site(), country="United States", search="GNSS").content))
    boulder = records["20435"]
    assert boulder.title == "Senior Software Engineer - GNSS / INS" and boulder.company == "Garmin"
    assert boulder.url == GARMIN + "/jobs/20435" and boulder.active is True
    assert boulder.locations == ["Boulder, Colorado, United States"]
    assert boulder.posted_at == datetime(2026, 10, 20, 18, 6, tzinfo=timezone.utc)
    assert boulder.employment_type == "full_time"
    assert boulder.metadata["upstream"] == {"platform": "icims-jibe", "board_job_id": "20435", "req_id": "20435",
                                            "country": "US"}
    # The list's description already holds the responsibilities and qualifications: nothing is repeated.
    assert boulder.description.startswith("Overview We are seeking a full-time Senior Software Engineer")
    assert boulder.description.count("Basic Qualifications") == 1
    # A job in two countries keeps both places; its country is not one country.
    both = records["20375"]
    assert both.locations == ["Olathe, Kansas, United States", "Cochrane, Alberta, Canada"]
    assert both.metadata["upstream"]["country"] is None
    assert records["19848"].employment_type == "unknown"  # the intern row names no employment type


def test_company_defaults_to_the_hiring_organization():
    parsed = parse(read(Site()).content, company=None)
    assert {r.company for r in parsed.records} == {"Garmin International, Inc."}


def test_description_gains_the_sections_it_lacks():
    page = fixture("garmin_gnss_us_p2")
    row = page["jobs"][1]["data"]
    row["description"] = "<p>Overview</p><p>Test radios.</p>"
    parsed = parse(read(Site({1: {**page, "totalCount": 2}})).content)
    text = by_id(parsed)["19677"].description
    assert text.startswith("Overview\n\nTest radios.\n\nResponsibilities\nEssential Functions")
    assert "\n\nQualifications\nBasic Qualifications" in text


def test_page_cap_leaves_a_partial_snapshot():
    site = Site()
    parsed = parse(read(site, max_pages=1).content)
    assert len(site.requests) == 1 and len(parsed.records) == 4
    [issue] = parsed.issues
    assert "4 of 6" in issue["reason"] and "not closed" in issue["reason"]


def test_a_listing_that_ends_before_its_total_is_partial():
    parsed = parse(read(Site({1: fixture("garmin_gnss_us_p1")})).content)
    assert len(parsed.records) == 4 and "4 of 6" in parsed.issues[0]["reason"]


def test_malformed_rows_do_not_prevent_valid_siblings():
    good = fixture("garmin_gnss_us_p2")["jobs"][1]["data"]
    rows = [good, {**good, "slug": None}, {**good, "slug": "../admin"}, {**good, "slug": "1", "title": " "},
            {**good, "slug": "2", "additional_locations": "Canada"}, {**good, "slug": "3", "description": 5},
            7, {"data": "x"}]
    parsed = parse(read(lambda request: httpx.Response(200, json={
        "jobs": [r if not isinstance(r, dict) or "data" in r else {"data": r} for r in rows],
        "totalCount": len(rows)})).content)
    assert [r.external_id for r in parsed.records] == ["19677"]
    assert len(parsed.issues) == len(rows) - 1 and all("admin" not in i["reason"] for i in parsed.issues)


@pytest.mark.parametrize("response", [
    httpx.Response(200, content=b"<html>maintenance</html>"),
    httpx.Response(200, json={"jobs": []}), httpx.Response(200, json={"totalCount": 3}),
    httpx.Response(200, json={"jobs": [], "totalCount": -1}), httpx.Response(200, json=[]),
])
def test_bad_search_response_is_an_error_instead_of_an_empty_board(response):
    with pytest.raises(ValueError):
        read(lambda request: response)


@pytest.mark.parametrize("status", [301, 404, 503])
def test_http_errors_and_redirects_fail_the_read(status):
    with pytest.raises(httpx.HTTPStatusError):
        read(lambda request: httpx.Response(status, headers={"location": "https://elsewhere.example/"}))


@pytest.mark.parametrize("content", [b"bad json", b"[]", b'{"jobs": []}',
                                     json.dumps({"platform": "oracle", "board": GARMIN, "fetched_on": "2026-11-06",
                                                 "searches": [], "jobs": []}).encode(),
                                     json.dumps({"platform": "icims-jibe", "board": GARMIN,
                                                 "searches": [], "jobs": []}).encode()])
def test_bad_snapshot_is_an_error(content):
    with pytest.raises(ValueError):
        parse(content)


@pytest.mark.parametrize("location", ["https://careers.garmin.com/", "http://Careers.Garmin.com/jobs?page=2",
                                      "https://careers.garmin.com/jobs/20524?lang=en-us"])
def test_careers_site_is_normalized(location):
    assert jibe.jibe_board_url(location) == GARMIN


@pytest.mark.parametrize("location", ["feed.json", "ftp://careers.garmin.com", "https://u:p@careers.garmin.com",
                                      "https://careers.garmin.com:8443", "https://localhost/jobs", None, ""])
def test_other_locations_are_not_jibe_boards(location):
    with pytest.raises(ValueError):
        jibe.jibe_board_url(location)


def config(tmp_path, **definition):
    path = tmp_path / "feeds.yaml"
    path.write_text(yaml.safe_dump({"sources": {"board-jibe-garmin": {
        "kind": "repository", "format": "icims-jibe-board", "location": GARMIN, **definition}}}))
    return path


def test_feed_config_normalizes_a_jibe_board(tmp_path):
    loaded = load_feed_config(config(tmp_path, location=GARMIN + "/jobs", country=" United States ", search="GNSS",
                                     company="Garmin"))["board-jibe-garmin"]
    assert loaded["location"] == GARMIN and loaded["company"] == "Garmin"
    assert loaded["options"] == {"country": "United States", "search": ["GNSS"],
                                 "max_pages": jibe.DEFAULT_MAX_PAGES}
    assert load_feed_config(config(tmp_path))["board-jibe-garmin"]["options"]["country"] is None


@pytest.mark.parametrize("changes,message", [
    ({"location": "feed.json"}, "careers site"), ({"country": 3}, "country"), ({"country": ""}, "country"),
    ({"search": ["GNSS", ""]}, "search"), ({"max_pages": 0}, "max_pages"), ({"max_pages": 101}, "max_pages"),
    ({"facets": {"Country": ["US"]}}, "workday-board"), ({"filters": {"job_family": ["x"]}}, "eightfold-board"),
])
def test_feed_config_rejects_bad_jibe_settings(tmp_path, changes, message):
    with pytest.raises(ValueError, match=message):
        load_feed_config(config(tmp_path, **changes))


def test_config_import_is_complete_without_enrichment(tmp_path):
    store = make_store(tmp_path)
    path = config(tmp_path, country="United States", search="GNSS", company="Garmin")
    try:
        with httpx.Client(transport=httpx.MockTransport(Site())) as client:
            report = run_config(store, path, client=client)["sources"]["board-jibe-garmin"]
        assert report["status"] == "ok" and report["created"] == 6 and report["format"] == "icims-jibe-board"
        with httpx.Client(transport=httpx.MockTransport(Site({1: fixture("garmin_gnss_us_p1")}))) as client:
            partial = run_config(store, path, client=client)["sources"]["board-jibe-garmin"]
        assert partial["status"] == "partial" and partial["missing"] == 0
        jobs = store.jobs()
        assert {j["company"] for j in jobs} == {"Garmin"} and len(jobs) == 6
        assert all(store.job_inputs(j["job_id"])[0]["extras"]["ingestion"]["enrichment_status"] == "complete"
                   for j in jobs)
    finally:
        store.engine.dispose()


def test_command_line_import_uses_defaults(tmp_path):
    store = make_store(tmp_path)
    try:
        with httpx.Client(transport=httpx.MockTransport(site := Site())) as client:
            report = run_repository(store, "board-jibe-garmin", GARMIN + "/jobs", client=client,
                                    format="icims-jibe-board")
        assert report["created"] == 6 and "country" not in site.requests[0].url.params
        assert store.source_health("board-jibe-garmin")["ingest"]["location"] == GARMIN
    finally:
        store.engine.dispose()

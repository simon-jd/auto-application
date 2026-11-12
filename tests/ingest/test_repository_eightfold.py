"""Eightfold career sites as employer boards: paged PCSX search, filters, enrichment links, snapshot safety.

The fixtures are recorded from Qualcomm's careers site (2026-11-06): the search "GNSS firmware" in the
United States, newest first, 3 positions a page (the site's own page is 10), facet catalogs dropped.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import yaml

from auto_app.enrich.targets import target_for
from auto_app.ingest import repository_eightfold as ef
from auto_app.ingest import repository_paged as paged
from auto_app.ingest.repository import parse_repository
from auto_app.ingest.runner import load_feed_config, run_config, run_repository
from auto_app.screen.rules import ScreeningRules, screen
from auto_app.screen.store import load_facts
from tests.imported_jobs import make_store

FIXTURES = Path(__file__).parents[1] / "fixtures" / "eightfold"
QUALCOMM = "https://qualcomm.eightfold.ai/careers?domain=qualcomm.com"
API = "https://qualcomm.eightfold.ai/api/pcsx/search"


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture(autouse=True)
def pauses(monkeypatch):
    slept = []
    monkeypatch.setattr(paged, "_sleep", slept.append)
    monkeypatch.setattr(ef, "PAGE_SIZE", 3)  # the fixtures' page size
    return slept


class Site:
    """Answers like an Eightfold tenant: recorded pages by start, an empty page after them."""

    def __init__(self, pages=None):
        self.pages = pages or {0: fixture("qualcomm_gnss_p0"), 3: fixture("qualcomm_gnss_p1")}
        self.params = []

    def __call__(self, request):
        assert request.method == "GET" and str(request.url).startswith(API + "?")
        self.params.append(list(request.url.params.multi_items()))
        page = self.pages.get(int(request.url.params["start"]))
        return httpx.Response(200, json=page or {"status": 200, "data": {"positions": [], "count": 0}})


def read(handler, **options):
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return ef.read_eightfold_board(QUALCOMM, client=client, **options)


def parse(content, company="Qualcomm"):
    return parse_repository(content, format="eightfold-board", company=company)


def by_id(parsed):
    return {r.external_id: r for r in parsed.records}


def test_reads_every_page_newest_first_with_filters(pauses):
    site = Site()
    result = read(site, country="United States", search=["GNSS firmware"],
                  filters={"job_family": ["software engineering", "systems engineering"]})
    assert site.params == [[("domain", "qualcomm.com"), ("query", "GNSS firmware"), ("location", "United States"),
                            ("start", start), ("page_size", "3"), ("sort_by", "timestamp"),
                            ("filter_job_family", "software engineering"),
                            ("filter_job_family", "systems engineering")] for start in ("0", "3")]
    assert pauses == [ef.PAUSE_SECONDS]  # Eightfold answers bursts with HTTP 429
    parsed = parse(result.content)
    assert len(parsed.records) == 5 and not parsed.issues


def test_records_carry_identity_places_and_an_enrichment_link():
    records = by_id(parse(read(Site(), country="United States", search="GNSS firmware").content))
    embedded = records["446721159325"]
    assert embedded.title == "Senior Embedded Software Engineer" and embedded.company == "Qualcomm"
    assert embedded.url == "https://qualcomm.eightfold.ai/careers/job/446721159325?domain=qualcomm.com"
    assert embedded.locations == ["Santa Clara, California, United States of America"]
    assert embedded.posted_at == datetime(2026, 9, 28, tzinfo=timezone.utc) and embedded.description == ""
    assert embedded.metadata == {"upstream": {"platform": "eightfold", "board_job_id": "446721159325",
                                              "req_id": "3096868", "country": "US",
                                              "category": "Software Engineering", "workplace": "onsite"}}
    target = target_for(embedded.url)
    assert target.platform == "eightfold" and target.job_id == "446721159325"
    assert target.api_url == ("https://qualcomm.eightfold.ai/api/pcsx/position_details"
                              "?position_id=446721159325&domain=qualcomm.com&hl=en")


def test_without_settings_the_whole_board_is_read():
    site = Site({0: {**fixture("qualcomm_gnss_p0"), "data": {"positions": [], "count": 0}}})
    parsed = parse(read(site).content)
    assert dict(site.params[0])["location"] == "" and dict(site.params[0])["query"] == ""
    assert not parsed.records and not parsed.issues


def test_page_cap_leaves_a_partial_snapshot():
    site = Site()
    parsed = parse(read(site, search="GNSS firmware", max_pages=1).content)
    assert len(site.params) == 1 and len(parsed.records) == 3
    [issue] = parsed.issues
    assert issue["search"] == "GNSS firmware" and "3 of 5" in issue["reason"]


def test_a_search_that_ends_before_its_total_is_partial():
    parsed = parse(read(Site({0: fixture("qualcomm_gnss_p0")})).content)
    assert len(parsed.records) == 3 and "3 of 5" in parsed.issues[0]["reason"]


def test_malformed_rows_do_not_prevent_valid_siblings():
    good = fixture("qualcomm_gnss_p1")["data"]["positions"][0]
    rows = [good, {**good, "id": "x"}, {**good, "id": -4}, {**good, "id": 7, "name": " "},
            {**good, "id": 8, "locations": "San Diego"}, {**good, "id": 9, "postedTs": 10 ** 20}, []]
    parsed = parse(read(lambda request: httpx.Response(200, json={"data": {"positions": rows,
                                                                            "count": len(rows)}})).content)
    assert [r.external_id for r in parsed.records] == [str(good["id"])]
    assert len(parsed.issues) == 6 and all(str(10 ** 20) not in i["reason"] for i in parsed.issues)


@pytest.mark.parametrize("response", [
    httpx.Response(200, content=b"<html>maintenance</html>"), httpx.Response(200, json={"data": {"count": 3}}),
    httpx.Response(200, json={"data": {"positions": []}}), httpx.Response(200, json={"status": 500, "error": {}}),
])
def test_bad_search_response_is_an_error_instead_of_an_empty_board(response):
    with pytest.raises(ValueError):
        read(lambda request: response)


@pytest.mark.parametrize("status", [403, 429])
def test_refusals_and_rate_limits_fail_the_read(status):
    with pytest.raises(httpx.HTTPStatusError):
        read(lambda request: httpx.Response(status, text="Please try again later"))


@pytest.mark.parametrize("location", [
    QUALCOMM, "https://Qualcomm.eightfold.ai/careers?domain=Qualcomm.com&query=gnss",
    "https://qualcomm.eightfold.ai/careers/job/446721159325?domain=qualcomm.com",
])
def test_careers_url_is_normalized(location):
    assert ef.eightfold_board_url(location) == QUALCOMM


@pytest.mark.parametrize("location", [
    "https://qualcomm.eightfold.ai/careers", "https://qualcomm.eightfold.ai/careers?domain=a&domain=b",
    "https://qualcomm.eightfold.ai/careers?domain=not_a_domain", "https://careers.qualcomm.com/careers?domain=qualcomm.com",
    "https://eightfold.ai/careers?domain=qualcomm.com", "https://u:p@qualcomm.eightfold.ai/careers?domain=qualcomm.com",
    None,
])
def test_other_locations_are_not_eightfold_boards(location):
    with pytest.raises(ValueError):
        ef.eightfold_board_url(location)


def config(tmp_path, **definition):
    path = tmp_path / "feeds.yaml"
    path.write_text(yaml.safe_dump({"sources": {"board-eightfold-qualcomm": {
        "kind": "repository", "format": "eightfold-board", "company": "Qualcomm", "location": QUALCOMM,
        **definition}}}))
    return path


def test_feed_config_normalizes_an_eightfold_board(tmp_path):
    loaded = load_feed_config(config(tmp_path, country="United States",
                                     filters={"job_family": [" software engineering ", "software engineering"]},
                                     max_pages=15))["board-eightfold-qualcomm"]
    assert loaded["location"] == QUALCOMM
    assert loaded["options"] == {"country": "United States", "search": [],
                                 "filters": {"job_family": ["software engineering"]}, "max_pages": 15}


@pytest.mark.parametrize("changes,message", [
    ({"company": None}, "company is required for eightfold-board"), ({"location": QUALCOMM.split("?")[0]}, "domain"),
    ({"filters": ["job_family"]}, "filters"), ({"filters": {"Job Family": ["x"]}}, "filters"),
    ({"filters": {"job_family": []}}, "filters"), ({"filters": {"job_family": "x"}}, "filters"),
    ({"max_pages": 201}, "max_pages"), ({"facets": {"a": ["b"]}}, "workday-board"),
])
def test_feed_config_rejects_bad_eightfold_settings(tmp_path, changes, message):
    with pytest.raises(ValueError, match=message):
        load_feed_config(config(tmp_path, **changes))


def test_config_import_stays_pending_and_closes_only_after_a_complete_read(tmp_path):
    store = make_store(tmp_path)
    path = config(tmp_path, country="United States", search="GNSS firmware")
    page0, page1 = fixture("qualcomm_gnss_p0"), fixture("qualcomm_gnss_p1")
    page0["data"]["count"] = 4  # one position closed
    page1["data"].update(count=4, positions=page1["data"]["positions"][:1])
    try:
        with httpx.Client(transport=httpx.MockTransport(Site())) as client:
            first = run_config(store, path, client=client)["sources"]["board-eightfold-qualcomm"]
        assert first["status"] == "ok" and first["created"] == 5 and first["format"] == "eightfold-board"
        job = store.jobs()[0]
        assert store.job_inputs(job["job_id"])[0]["extras"]["ingestion"]["enrichment_status"] == "pending"
        with httpx.Client(transport=httpx.MockTransport(Site({0: fixture("qualcomm_gnss_p0")}))) as client:
            partial = run_config(store, path, client=client)["sources"]["board-eightfold-qualcomm"]
        assert partial["status"] == "partial" and partial["missing"] == 0
        with httpx.Client(transport=httpx.MockTransport(Site({0: page0, 3: page1}))) as client:
            complete = run_config(store, path, client=client)["sources"]["board-eightfold-qualcomm"]
        assert complete["status"] == "ok" and complete["missing"] == 1
        assert {j["company"] for j in store.jobs()} == {"Qualcomm"}
    finally:
        store.engine.dispose()


def test_the_department_is_screenings_category(tmp_path):
    # Qualcomm titles are often a bare "Engineer"; the department tells software from hardware.
    page = fixture("qualcomm_gnss_p1")
    first, second = page["data"]["positions"]
    page["data"].update(count=2, positions=[{**first, "name": "Engineer", "department": "Software Engineering"},
                                            {**second, "name": "Engineer", "department": "Hardware Engineering",
                                             "locations": ["Boulder, Colorado, United States of America"]}])
    store = make_store(tmp_path)
    try:
        with httpx.Client(transport=httpx.MockTransport(Site({0: page}))) as client:
            run_repository(store, "board-eightfold-qualcomm", QUALCOMM, client=client, format="eightfold-board",
                           company="Qualcomm")
        roles = {f.categories: screen(f, ScreeningRules()).signals["role"] for f in load_facts(store)}
        assert roles == {("Software Engineering",): "cs", ("Hardware Engineering",): "non_cs"}
    finally:
        store.engine.dispose()


def test_command_line_import_names_the_tenant(tmp_path):
    store = make_store(tmp_path)
    try:
        with httpx.Client(transport=httpx.MockTransport(Site())) as client:
            report = run_repository(store, "board-eightfold-qualcomm", QUALCOMM, client=client,
                                    format="eightfold-board")
        assert report["created"] == 5 and {j["company"] for j in store.jobs()} == {"qualcomm"}
    finally:
        store.engine.dispose()

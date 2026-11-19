"""Workday career sites as employer boards: paged CXS search, recorded responses, snapshot safety.

The fixtures are recorded from General Motors' and Cisco's public career sites (2026-11-06); search
pages keep every posting with their facets dropped (the reader ignores them), and the facet listing
keeps three of GM's facets, its nested Locations group cut to four values.
"""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import yaml

from auto_app.enrich.targets import target_for
from auto_app.ingest import repository_workday as wd
from auto_app.ingest.repository import parse_repository
from auto_app.ingest.runner import load_feed_config, run_config, run_repository
from auto_app.ingest.service import IngestService
from tests.imported_jobs import make_store

FIXTURES = Path(__file__).parents[1] / "fixtures" / "workday"
GM = "https://generalmotors.wd5.myworkdayjobs.com/Careers_GM"
GM_API = "https://generalmotors.wd5.myworkdayjobs.com/wday/cxs/generalmotors/Careers_GM/jobs"
CISCO = "https://cisco.wd5.myworkdayjobs.com/Cisco_Careers"
US = "bc33aa3152ec42d4995f4791a106ed09"
TODAY = date(2026, 11, 6)
MOMENT = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture(autouse=True)
def pauses(monkeypatch):
    slept = []
    monkeypatch.setattr(wd, "_sleep", slept.append)
    return slept


class Board:
    """Answers like a Workday tenant: the facet listing for the unfiltered probe, then recorded pages by offset."""

    def __init__(self, pages=None, api=GM_API, facets="gm_facets"):
        self.pages = pages or {0: fixture("gm_firmware_us_p0"), 20: fixture("gm_firmware_us_p1")}
        self.api, self.facets, self.bodies = api, facets, []

    def __call__(self, request):
        assert request.method == "POST" and str(request.url) == self.api
        body = json.loads(request.content)
        self.bodies.append(body)
        if body["limit"] == 1:
            return httpx.Response(200, json=fixture(self.facets))
        return httpx.Response(200, json=self.pages.get(body["offset"], {"total": 0, "jobPostings": []}))


def read(handler, board=GM, today=TODAY, **options):
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return wd.read_workday_board(board, client=client, today=today, **options)


def parse(content, company="GM"):
    return parse_repository(content, format="workday-board", company=company)


def by_id(parsed):
    return {r.external_id: r for r in parsed.records}


def test_reads_every_page_although_later_pages_report_total_zero(pauses):
    board = Board()
    result = read(board, search=["firmware"], facets={"Location Country/Territory": ["United States of America"]})
    assert board.bodies[0] == {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}
    assert board.bodies[1:] == [{"appliedFacets": {"Location_Country": [US]}, "limit": 20, "offset": offset,
                                 "searchText": "firmware"} for offset in (0, 20)]
    assert pauses == [wd.PAUSE_SECONDS] * 2  # a pause before every request after the first
    parsed = parse(result.content)
    assert len(parsed.records) == 21 and not parsed.issues
    assert result.revision.startswith("sha256:") and not result.not_modified


def test_records_carry_identity_url_locations_and_approximate_dates():
    records = by_id(parse(read(Board(), search=["firmware"]).content))
    kernel = records["JR-202622086"]
    assert kernel.title == "Senior Software Engineer, Linux Kernel & Driver Development"
    assert kernel.url == (GM + "/job/Austin-Texas-United-States-of-America/"
                          "Senior-Software-Engineer--Linux-Kernel---Driver-Development_JR-202622086")
    assert kernel.company == "GM" and kernel.active is True and kernel.description == ""
    assert kernel.locations == ["Austin, Texas, United States of America"]
    assert kernel.posted_at == datetime(2026, 10, 15, tzinfo=timezone.utc)  # "Posted 22 Days Ago"
    assert kernel.metadata["posted_at_precision"] == "approx"
    assert kernel.metadata["upstream"]["req_id"] == "JR-202622086"
    assert kernel.metadata["source_observation"] == {"postedOn": "Posted 22 Days Ago", "fetched_on": "2026-11-06"}
    # Several places: the list only counts them, so locations wait for the job's detail.
    build = records["JR-202620964-1"]
    assert build.locations == [] and build.metadata["upstream"]["locations_text"] == "4 Locations"
    assert build.metadata["upstream"]["req_id"] == "JR-202620964"
    assert build.posted_at == datetime(2026, 10, 6, tzinfo=timezone.utc)  # "Posted 30+ Days Ago": at least 31
    assert records["JR-202620554-1"].posted_at == datetime(2026, 11, 5, tzinfo=timezone.utc)  # Yesterday
    # The enrichment worker can read each record's description from the CXS detail endpoint.
    target = target_for(kernel.url)
    assert target.platform == "workday" and target.job_id == "JR-202622086"
    assert target.api_url.startswith("https://generalmotors.wd5.myworkdayjobs.com/wday/cxs/generalmotors/Careers_GM/job/")


def test_cisco_rows_keep_time_type_and_requisition():
    page = fixture("cisco_newgrad_p0")
    page["total"] = len(page["jobPostings"])
    board = Board({0: page}, api="https://cisco.wd5.myworkdayjobs.com/wday/cxs/cisco/Cisco_Careers/jobs")
    parsed = parse(read(board, board=CISCO, search=["new grad"]).content, company="Cisco")
    hardware = by_id(parsed)["2021697-1"]
    assert hardware.employment_type == "full_time" and hardware.metadata["upstream"]["req_id"] == "2021697"
    assert hardware.locations == ["San Jose, California, US"]
    assert len(parsed.records) == 20 and not parsed.issues


def test_page_cap_leaves_a_partial_snapshot():
    board = Board()
    parsed = parse(read(board, search=["firmware"], max_pages=1).content)
    assert len(board.bodies) == 1 and len(parsed.records) == 20
    [issue] = parsed.issues
    assert issue["search"] == "firmware" and "20 of 21" in issue["reason"]


def test_a_search_that_ends_before_its_total_is_partial():
    page = fixture("gm_firmware_us_p0")
    parsed = parse(read(Board({0: page, 20: {"total": 0, "jobPostings": []}})).content)
    assert len(parsed.records) == 20 and "20 of 21" in parsed.issues[0]["reason"]


def test_workday_result_cap_is_never_a_complete_read():
    page = {**fixture("gm_firmware_us_p1"), "total": 2000}
    [issue] = parse(read(Board({0: page})).content).issues
    assert "2000" in issue["reason"]


def test_nested_location_facet_and_several_search_texts():
    board = Board()
    parsed = parse(read(board, search=["firmware", "linux", "firmware"],
                        facets={"locations": ["Austin, Texas, United States of America"]}).content)
    searches = [b for b in board.bodies if b["limit"] == 20]
    assert [(b["searchText"], b["offset"]) for b in searches] == [("firmware", 0), ("firmware", 20),
                                                                   ("linux", 0), ("linux", 20)]
    assert searches[0]["appliedFacets"] == {"locations": ["e30192a1b9ad01b3b30272abb61faf4c"]}
    assert len(parsed.records) == 21 and not parsed.issues  # the same postings, once


@pytest.mark.parametrize("facets,message", [
    ({"Region": ["Ohio"]}, "Workday facet 'Region' not found"),
    ({"Location_Country": ["Atlantis"]}, "has no value"),
])
def test_unknown_facet_or_value_fails_the_read(facets, message):
    with pytest.raises(ValueError, match=message):
        read(Board(), facets=facets)


def test_malformed_rows_do_not_prevent_valid_siblings():
    good = fixture("gm_firmware_us_p1")["jobPostings"][0]
    rows = [good, {**good, "externalPath": None}, {**good, "externalPath": "//evil.example/job/x_JR-1"},
            {**good, "externalPath": "/job/../admin_JR-2"}, {**good, "title": " ", "externalPath": "/job/X/Blank_JR-3"}, 7,
            {**good, "externalPath": good["externalPath"] + "/"},
            {**good, "locationsText": 5, "externalPath": "/job/X/Places_JR-4"},
            {**good, "externalPath": "/job/Elsewhere/Fleet-Reliability-Engineer_JR-202619235"}]
    parsed = parse(read(Board({0: {"total": len(rows), "jobPostings": rows}})).content)
    assert [r.external_id for r in parsed.records] == ["JR-202619235"]
    assert len(parsed.issues) == len(rows) - 1 and all("evil" not in i["reason"] for i in parsed.issues)
    assert any("duplicate" in i["reason"] for i in parsed.issues)


@pytest.mark.parametrize("response", [
    httpx.Response(200, content=b"<html>maintenance</html>"),
    httpx.Response(200, json={"total": 3}),
    httpx.Response(200, json={"jobPostings": []}),
    httpx.Response(200, json=[]),
])
def test_bad_search_response_is_an_error_instead_of_an_empty_board(response):
    with pytest.raises(ValueError):
        read(lambda request: response)


def test_http_errors_fail_the_read():
    with pytest.raises(httpx.HTTPStatusError):
        read(lambda request: httpx.Response(400, json={"errorCode": "HTTP_400"}))


@pytest.mark.parametrize("content", [b"bad json", b"[]", b'{"jobs": []}',
                                     json.dumps({"board": "https://example.com/x", "fetched_on": "2026-11-06",
                                                 "searches": [], "jobs": []}).encode(),
                                     json.dumps({"board": GM, "searches": [], "jobs": []}).encode()])
def test_bad_snapshot_is_an_error(content):
    with pytest.raises(ValueError):
        parse(content)


@pytest.mark.parametrize("location,expected", [
    ("https://generalmotors.wd5.myworkdayjobs.com/en-US/Careers_GM/", GM),
    ("https://GeneralMotors.wd5.myworkdayjobs.com/Careers_GM/job/Austin/X_JR-1?source=a", GM),
])
def test_careers_url_is_normalized(location, expected):
    assert wd.workday_board_url(location) == expected


@pytest.mark.parametrize("location", [
    "https://boards-api.greenhouse.io/v1/boards/x/jobs", "feed.json", "https://generalmotors.wd5.myworkdayjobs.com/",
    "ftp://generalmotors.wd5.myworkdayjobs.com/Careers_GM", "https://u:p@generalmotors.wd5.myworkdayjobs.com/Careers_GM",
    "https://evil.example/generalmotors.wd5.myworkdayjobs.com/Careers_GM", None,
])
def test_other_locations_are_not_workday_boards(location):
    with pytest.raises(ValueError):
        wd.workday_board_url(location)


def config(tmp_path, **definition):
    path = tmp_path / "feeds.yaml"
    path.write_text(yaml.safe_dump({"sources": {"board-workday-gm": {
        "kind": "repository", "format": "workday-board", "company": "GM", "location": GM, **definition}}}))
    return path


def test_feed_config_normalizes_a_workday_board(tmp_path):
    loaded = load_feed_config(config(tmp_path, location=GM.replace(".com/", ".com/en-US/") + "/",
                                     search=" firmware ", facets={"Location Country/Territory": ["United States of America"]}))
    assert loaded["board-workday-gm"]["location"] == GM
    assert loaded["board-workday-gm"]["options"] == {
        "search": ["firmware"], "facets": {"Location Country/Territory": ["United States of America"]},
        "max_pages": wd.DEFAULT_MAX_PAGES}
    assert load_feed_config(config(tmp_path))["board-workday-gm"]["options"]["search"] == []


@pytest.mark.parametrize("changes,message", [
    ({"location": "https://boards-api.greenhouse.io/v1/boards/x/jobs"}, "myworkdayjobs"),
    ({"location": "feed.json"}, "myworkdayjobs"),
    ({"location": "https://generalmotors.wd5.myworkdayjobs.com/"}, "site"),
    ({"company": None}, "company"),
    ({"search": 3}, "search"), ({"search": ["firmware", ""]}, "search"),
    ({"facets": ["Country"]}, "facets"), ({"facets": {"Country": "United States of America"}}, "facets"),
    ({"facets": {"Country": []}}, "facets"),
    ({"max_pages": 0}, "max_pages"), ({"max_pages": 101}, "max_pages"), ({"max_pages": True}, "max_pages"),
])
def test_feed_config_rejects_bad_workday_settings(tmp_path, changes, message):
    with pytest.raises(ValueError, match=message):
        load_feed_config(config(tmp_path, **changes))


def test_workday_settings_on_another_format_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="workday-board"):
        load_feed_config(config(tmp_path, format="lever-board", location="feed.json", search="firmware"))


def test_config_import_closes_only_after_a_complete_read(tmp_path):
    store = make_store(tmp_path)
    path = config(tmp_path, search="firmware", facets={"Location_Country": ["United States of America"]})
    shrunk = {**fixture("gm_firmware_us_p0"), "total": 20}  # the 21st posting has closed
    try:
        with httpx.Client(transport=httpx.MockTransport(Board())) as client:
            first = run_config(store, path, client=client, now=MOMENT)["sources"]["board-workday-gm"]
        assert first["status"] == "ok" and first["created"] == 21 and first["format"] == "workday-board"
        assert store.source_health("board-workday-gm")["ingest"]["location"] == GM
        with httpx.Client(transport=httpx.MockTransport(Board({0: fixture("gm_firmware_us_p0")}))) as client:
            # Each read is a later turn: a Workday site is read at most every two hours (runner.WORKDAY_GAP).
            partial = run_config(store, path, client=client, now=MOMENT + timedelta(hours=3))["sources"]["board-workday-gm"]
        assert partial["status"] == "partial" and partial["missing"] == 0
        with httpx.Client(transport=httpx.MockTransport(Board({0: shrunk}))) as client:
            complete = run_config(store, path, client=client, now=MOMENT + timedelta(hours=6))["sources"]["board-workday-gm"]
        assert complete["status"] == "ok" and complete["missing"] == 1
        assert {j["company"] for j in store.jobs()} == {"GM"}
    finally:
        store.engine.dispose()


def test_command_line_import_uses_defaults_and_the_tenant_name(tmp_path):
    store = make_store(tmp_path)
    try:
        with httpx.Client(transport=httpx.MockTransport(board := Board())) as client:
            report = run_repository(store, "board-workday-gm", GM + "/en-US", client=client, format="workday-board")
        assert report["created"] == 21 and all(b["limit"] == 20 for b in board.bodies)  # no facet probe
        assert {j["company"] for j in store.jobs()} == {"generalmotors"}
    finally:
        store.engine.dispose()


def test_an_estimated_date_moving_with_the_clock_is_not_a_change(tmp_path):
    store = make_store(tmp_path)
    service = IngestService(store)
    page = fixture("gm_firmware_us_p1")
    page["jobPostings"][0]["postedOn"] = "Posted Today"
    try:
        for day, revision in ((TODAY, "r1"), (date(2026, 11, 7), "r2")):  # a time zone apart: still "Today"
            parsed = parse(read(Board({0: page}), today=day).content)
            report = service.ingest("board-workday-gm", parsed, kind="repository", origin=GM, revision=revision,
                                    complete_snapshot=True)
        assert report["unchanged"] == 1 and report["updated"] == 0
        page["jobPostings"][0]["title"] = "Fleet Reliability Engineer II"
        report = service.ingest("board-workday-gm", parse(read(Board({0: page})).content), kind="repository",
                                origin=GM, revision="r3", complete_snapshot=True)
        assert report["updated"] == 1
    finally:
        store.engine.dispose()

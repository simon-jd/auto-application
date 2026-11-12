"""Oracle Recruiting Cloud career sites as employer boards: paged finder, location facet, snapshot safety.

The fixtures are recorded from Ford's careers site (2026-11-06): its location facets (cut to six), and the
search "infotainment" in the United States facet, 15 requisitions a page, other facets dropped.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import yaml

from auto_app.enrich.targets import target_for
from auto_app.ingest import repository_oracle as ora
from auto_app.ingest import repository_paged as paged
from auto_app.ingest.repository import parse_repository
from auto_app.ingest.runner import load_feed_config, run_config, run_repository
from tests.imported_jobs import make_store

FIXTURES = Path(__file__).parents[1] / "fixtures" / "oracle"
HOST = "efds.fa.em5.oraclecloud.com"
FORD = f"https://{HOST}/hcmUI/CandidateExperience/en/sites/CX_1"
SEARCH = f"https://{HOST}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
API = SEARCH + "&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber=CX_1,facetsList=LOCATIONS,"
FACETS = SEARCH + "&finder=findReqs;siteNumber=CX_1,facetsList=LOCATIONS,limit=1,offset=0"  # no requisitions
US = 300000000425727


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def empty():
    return {"items": [{"TotalJobsCount": 0, "requisitionList": []}]}


@pytest.fixture(autouse=True)
def pauses(monkeypatch):
    slept = []
    monkeypatch.setattr(paged, "_sleep", slept.append)
    monkeypatch.setattr(ora, "PAGE_SIZE", 15)  # the fixtures' page size
    return slept


class Site:
    """Answers like an Oracle pod: the facet listing for limit=1, then recorded pages by offset."""

    def __init__(self, pages=None):
        self.pages = pages or {0: fixture("ford_infotainment_us_p0"), 15: fixture("ford_infotainment_us_p1")}
        self.urls, self.finders = [], []

    def __call__(self, request):
        assert request.method == "GET" and (str(request.url).startswith(API) or str(request.url) == FACETS)
        self.urls.append(str(request.url))
        self.finders.append(fields := dict(part.split("=", 1) for part in
                                           request.url.params["finder"].split(";", 1)[1].split(",")))
        if str(request.url) == FACETS:
            return httpx.Response(200, json=fixture("ford_facets"))
        return httpx.Response(200, json=self.pages.get(int(fields["offset"]), empty()))


def read(handler, **options):
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return ora.read_oracle_board(FORD, client=client, **options)


def parse(content, company="Ford"):
    return parse_repository(content, format="oracle-board", company=company)


def by_id(parsed):
    return {r.external_id: r for r in parsed.records}


def test_resolves_the_country_facet_then_reads_every_page_newest_first(pauses):
    site = Site()
    result = read(site, country="united states", search=["infotainment"])
    assert site.finders[0] == {"siteNumber": "CX_1", "facetsList": "LOCATIONS", "limit": "1", "offset": "0"}
    assert site.finders[1:] == [{"siteNumber": "CX_1", "facetsList": "LOCATIONS", "limit": "15", "offset": offset,
                                 "sortBy": "POSTING_DATES_DESC", "keyword": '"infotainment"',
                                 "selectedLocationsFacet": str(US)} for offset in ("0", "15")]
    assert "keyword=%22infotainment%22" in site.urls[1]  # the finder's own quoting survives the request
    assert pauses == [paged.PAUSE_SECONDS] * 2
    parsed = parse(result.content)
    assert len(parsed.records) == 21 and not parsed.issues


def test_records_carry_identity_place_and_the_posted_day():
    records = by_id(parse(read(Site(), country="United States", search="infotainment").content))
    audio = records["72872"]
    assert audio.title == "Embedded Software Engineer-Audio" and audio.company == "Ford"
    assert audio.url == FORD + "/job/72872" and audio.active is True and audio.description == ""
    assert audio.locations == ["Dearborn, MI, United States"]
    assert audio.posted_at == datetime(2026, 11, 5, tzinfo=timezone.utc)
    assert audio.metadata == {"upstream": {"platform": "oracle", "board_job_id": "72872", "req_id": "72872",
                                           "country": "US"}, "posted_at_precision": "day"}
    # Enrichment reads the description from the requisition's detail.
    target = target_for(audio.url)
    assert target.platform == "oracle" and target.board == f"{HOST}/CX_1" and target.job_id == "72872"
    assert target.api_url == (f"https://{HOST}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
                              "?expand=all&onlyData=true&finder=ById;Id=%2272872%22,siteNumber=CX_1")


def test_secondary_locations_join_the_primary():
    page = fixture("ford_infotainment_us_p1")
    row = page["items"][0]["requisitionList"][0]
    row["secondaryLocations"] = [{"Name": "Ontario, Canada", "CountryCode": "CA"}, {"Name": None}, "x"]
    parsed = parse(read(Site({0: page})).content)
    assert parsed.issues and not any(r.external_id == row["Id"] for r in parsed.records)  # "x" is not a place
    row["secondaryLocations"] = row["secondaryLocations"][:2]
    record = by_id(parse(read(Site({0: page})).content))[row["Id"]]
    assert record.locations == ["Long Beach, CA, United States", "Ontario, Canada"]
    assert record.metadata["upstream"]["country"] is None


def test_without_a_country_no_facet_request_is_made():
    site = Site()
    parse(read(site, search="infotainment").content)
    assert all(fields["limit"] == "15" and "selectedLocationsFacet" not in fields for fields in site.finders)


def test_an_unknown_country_fails_the_read():
    with pytest.raises(ValueError, match="'Atlantis' is not one of the site's location facets"):
        read(Site(), country="Atlantis")


def test_page_cap_leaves_a_partial_snapshot():
    site = Site()
    parsed = parse(read(site, search="infotainment", max_pages=1).content)
    assert len(site.finders) == 1 and len(parsed.records) == 15
    [issue] = parsed.issues
    assert issue["search"] == "infotainment" and "15 of 21" in issue["reason"]


def test_a_search_that_ends_before_its_total_is_partial():
    parsed = parse(read(Site({0: fixture("ford_infotainment_us_p0")})).content)
    assert len(parsed.records) == 15 and "15 of 21" in parsed.issues[0]["reason"]


def test_malformed_rows_do_not_prevent_valid_siblings():
    good = fixture("ford_infotainment_us_p1")["items"][0]["requisitionList"][0]
    rows = [good, {**good, "Id": None}, {**good, "Id": "../1"}, {**good, "Id": "2", "Title": ""},
            {**good, "Id": "3", "secondaryLocations": "Canada"}, {**good, "Id": "4", "PostedDate": "Sept 1"}, 7,
            {**good, "Id": "req480976"}]  # an imported requisition's Id (Honeywell)
    parsed = parse(read(lambda request: httpx.Response(200, json={
        "items": [{"TotalJobsCount": len(rows), "requisitionList": rows}]})).content)
    assert [r.external_id for r in parsed.records] == ["4", good["Id"], "req480976"]
    assert by_id(parsed)["req480976"].url == FORD + "/job/req480976"
    assert by_id(parsed)["4"].posted_at is None  # an unreadable date is left out, not guessed
    assert len(parsed.issues) == 5 and all("../" not in i["reason"] for i in parsed.issues)


@pytest.mark.parametrize("response", [
    httpx.Response(200, content=b"<html>maintenance</html>"), httpx.Response(200, json={"items": []}),
    httpx.Response(200, json={"items": [{"requisitionList": []}]}),
    httpx.Response(200, json={"items": [{"TotalJobsCount": 3}]}), httpx.Response(200, json=[]),
])
def test_bad_search_response_is_an_error_instead_of_an_empty_board(response):
    with pytest.raises(ValueError):
        read(lambda request: response)


def test_http_errors_fail_the_read():
    with pytest.raises(httpx.HTTPStatusError):
        read(lambda request: httpx.Response(401, json={"title": "Unauthorized"}))


@pytest.mark.parametrize("location", [
    FORD, FORD + "/", f"https://{HOST.upper()}/hcmUI/CandidateExperience/en-US/sites/CX_1/requisitions?mode=list",
    FORD + "/job/72872", f"http://{HOST}/hcmui/candidateexperience/en/sites/CX_1",
])
def test_careers_site_is_normalized(location):
    assert ora.oracle_board_url(location) == FORD


@pytest.mark.parametrize("location", [
    f"https://{HOST}/", f"https://{HOST}/hcmUI/CandidateExperience/en/sites/", f"https://{HOST}/hcmUI/sites/CX_1",
    "https://oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1",
    "https://evil.example/hcmUI/CandidateExperience/en/sites/CX_1",
    f"https://u:p@{HOST}/hcmUI/CandidateExperience/en/sites/CX_1",
    f"https://{HOST}/hcmUI/CandidateExperience/en/sites/CX%2F1", None,
])
def test_other_locations_are_not_oracle_boards(location):
    with pytest.raises(ValueError):
        ora.oracle_board_url(location)


def config(tmp_path, **definition):
    path = tmp_path / "feeds.yaml"
    path.write_text(yaml.safe_dump({"sources": {"board-oracle-ford": {
        "kind": "repository", "format": "oracle-board", "company": "Ford", "location": FORD, **definition}}}))
    return path


def test_feed_config_normalizes_an_oracle_board(tmp_path):
    loaded = load_feed_config(config(tmp_path, location=FORD + "/requisitions", country="United States",
                                     search=["infotainment", "infotainment"]))["board-oracle-ford"]
    assert loaded["location"] == FORD
    assert loaded["options"] == {"country": "United States", "search": ["infotainment"],
                                 "max_pages": ora.DEFAULT_MAX_PAGES}


@pytest.mark.parametrize("changes,message", [
    ({"company": None}, "company is required for oracle-board"), ({"location": "feed.json"}, "oraclecloud"),
    ({"search": "a, b"}, "cannot contain"), ({"search": 'say "hi"'}, "cannot contain"), ({"country": ["US"]}, "country"),
    ({"max_pages": 0}, "max_pages"), ({"max_pages": 101}, "max_pages"), ({"filters": {"a": ["b"]}}, "eightfold"),
])
def test_feed_config_rejects_bad_oracle_settings(tmp_path, changes, message):
    with pytest.raises(ValueError, match=message):
        load_feed_config(config(tmp_path, **changes))


def test_config_import_stays_pending_and_closes_only_after_a_complete_read(tmp_path):
    store = make_store(tmp_path)
    path = config(tmp_path, country="United States", search="infotainment")
    page0, page1 = fixture("ford_infotainment_us_p0"), fixture("ford_infotainment_us_p1")
    page0["items"][0]["TotalJobsCount"] = 20  # one requisition closed
    page1["items"][0].update(TotalJobsCount=20, requisitionList=page1["items"][0]["requisitionList"][1:])
    try:
        with httpx.Client(transport=httpx.MockTransport(Site())) as client:
            first = run_config(store, path, client=client)["sources"]["board-oracle-ford"]
        assert first["status"] == "ok" and first["created"] == 21 and first["format"] == "oracle-board"
        job = store.jobs()[0]
        assert store.job_inputs(job["job_id"])[0]["extras"]["ingestion"]["enrichment_status"] == "pending"
        with httpx.Client(transport=httpx.MockTransport(Site({0: fixture("ford_infotainment_us_p0")}))) as client:
            partial = run_config(store, path, client=client)["sources"]["board-oracle-ford"]
        assert partial["status"] == "partial" and partial["missing"] == 0
        with httpx.Client(transport=httpx.MockTransport(Site({0: page0, 15: page1}))) as client:
            complete = run_config(store, path, client=client)["sources"]["board-oracle-ford"]
        assert complete["status"] == "ok" and complete["missing"] == 1
    finally:
        store.engine.dispose()


def test_command_line_import_names_the_pod(tmp_path):
    store = make_store(tmp_path)
    try:
        with httpx.Client(transport=httpx.MockTransport(site := Site())) as client:
            report = run_repository(store, "board-oracle-ford", FORD + "/job/1", client=client, format="oracle-board")
        assert report["created"] == 21 and site.finders[0]["limit"] == "15"
        assert {j["company"] for j in store.jobs()} == {"efds"}
    finally:
        store.engine.dispose()

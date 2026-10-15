"""Test public HTTP boundary, parsers and per-host pacing without external requests."""

import httpx
import pytest

from auto_app.enrich.fetch import Fetcher, USER_AGENT
from auto_app.enrich.targets import Target, target_for

UUID = "91eed659-1111-2222-3333-444455556666"
OTHER = "aaaaaaaa-1111-2222-3333-444455556666"
GH_URL = "https://job-boards.greenhouse.io/acme/jobs/4012345"
WD_URL = "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/US-TX-Austin/Software-Engineer_JR2026343"
GREENHOUSE = {"id": 4012345, "title": "Software Engineer, New Grad", "updated_at": "2026-10-01T00:00:00Z",
              "absolute_url": GH_URL, "location": {"name": "Boston, MA"},
              "content": "&lt;p&gt;Build APIs in Python.&lt;/p&gt;"}
WORKDAY = {"jobPostingInfo": {"title": "Software Engineer", "jobDescription": "<p>Write C++.</p>",
                              "location": "US, TX, Austin", "timeType": "Full time", "startDate": "2026-10-01",
                              "jobReqId": "JR2026343"}, "hiringOrganization": {"name": "NVIDIA"}}
ASHBY = {"jobs": [{"id": UUID, "title": "Backend Engineer", "location": "New York", "jobUrl": "https://x",
                   "descriptionPlain": "Own services.", "employmentType": "FullTime", "isListed": True},
                  {"id": OTHER, "title": "Data Engineer", "location": "Remote", "jobUrl": "https://y",
                   "descriptionPlain": "Own pipelines.", "employmentType": "FullTime", "isListed": True}]}


class Clock:
    def __init__(self):
        self.t, self.sleeps = 100.0, []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.t += seconds


def fetcher(handler, clock=None, **kwargs):
    clock = clock or Clock()
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return Fetcher(client, clock=clock, sleep=clock.sleep, **kwargs)


def test_platform_records_reuse_collectors_parsers():
    lever = {"id": UUID, "text": "Backend Engineer", "descriptionPlain": "Own APIs.",
             "categories": {"location": "Boston", "commitment": "Full-time"}}
    def handler(request):
        payload = WORKDAY if "workday" in request.url.host else lever if "lever" in request.url.host else GREENHOUSE
        return httpx.Response(200, json=payload)
    f = fetcher(handler)
    gh, wd, lv = [f.fetch(target_for(url)) for url in (GH_URL, WD_URL, f"https://jobs.lever.co/acme/{UUID}")]
    assert gh.status == "ok" and gh.posting.description_text == "Build APIs in Python."
    assert wd.status == "ok" and wd.posting.description_text == "Write C++."
    assert wd.posting.employment_type == "full_time" and wd.posting.req_id == "JR2026343"
    assert lv.status == "ok" and lv.posting.employment_type == "full_time"


def test_ashby_board_is_read_once_and_missing_jobs_are_gone():
    f = fetcher(lambda request: httpx.Response(200, json=ASHBY))
    first = f.fetch(target_for(f"https://jobs.ashbyhq.com/acme/{UUID}"))
    second = f.fetch(target_for(f"https://jobs.ashbyhq.com/acme/{OTHER}/application"))
    missing = f.fetch(target_for("https://jobs.ashbyhq.com/acme/bbbbbbbb-1111-2222-3333-444455556666"))
    assert (first.status, second.status, missing.status) == ("ok", "ok", "gone")
    assert first.posting.description_text == "Own services."
    assert f.request_count == 1 and f.request_needed(target_for(f"https://jobs.ashbyhq.com/acme/{OTHER}")) == 0


@pytest.mark.parametrize("status,want", [(404, "gone"), (410, "gone"), (429, "failed"), (500, "failed")])
def test_http_outcomes(status, want):
    f = fetcher(lambda request: httpx.Response(status))
    result = f.fetch(target_for(GH_URL))
    assert result.status == want and result.posting is None
    assert f.request_count == 1


def test_no_redirects_and_user_agent_even_for_injected_clients():
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
    f = fetcher(handler)
    assert f.fetch(target_for(GH_URL)).status == "failed"
    assert len(seen) == 1 and seen[0].headers["user-agent"] == USER_AGENT


def test_requests_on_one_host_always_wait_at_least_one_second():
    clock = Clock()
    f = fetcher(lambda request: httpx.Response(404), clock, min_interval=0)
    f.fetch(target_for(GH_URL))
    f.fetch(target_for(WD_URL))
    f.fetch(target_for(GH_URL))
    assert clock.sleeps == [1.0]


@pytest.mark.parametrize("payload", [None, [], {}, {"jobs": None}])
def test_malformed_ashby_payload_is_failure_and_not_cached_as_gone(payload):
    f = fetcher(lambda request: httpx.Response(200, json=payload))
    first = f.fetch(target_for(f"https://jobs.ashbyhq.com/acme/{UUID}"))
    second = f.fetch(target_for(f"https://jobs.ashbyhq.com/acme/{OTHER}"))
    assert first.status == second.status == "failed"
    assert f.request_count == 1


@pytest.mark.parametrize("payload", [[], {}, {**GREENHOUSE, "location": "Boston"},
                                     {**GREENHOUSE, "content": []}, {**GREENHOUSE, "id": 123}])
def test_malformed_or_mismatched_record_does_not_enrich_the_job(payload):
    f = fetcher(lambda request: httpx.Response(200, json=payload))
    assert f.fetch(target_for(GH_URL)).status == "failed"


@pytest.mark.parametrize("url", ["http://127.0.0.1/internal", "https://evil.test/private",
                               "https://boards-api.greenhouse.io/v1/boards/acme/jobs/1/../../internal"])
def test_manually_constructed_unsafe_target_never_reaches_network(url):
    f = fetcher(lambda request: pytest.fail("unsafe target reached HTTP transport"))
    assert f.fetch(Target("greenhouse", "acme", "1", url)).status == "failed"
    assert f.request_count == 0


def test_network_failure_counts_as_a_request_and_hides_exception_details():
    def handler(request):
        raise httpx.ConnectError("sensitive credentials at https://secret.example", request=request)
    f = fetcher(handler)
    result = f.fetch(target_for(GH_URL))
    assert result.status == "failed" and "ConnectError" in result.error
    assert "secret" not in result.error and f.request_count == 1


@pytest.mark.parametrize("expected,found", [
    ("R123456-1", "R123456"), ("JR2026343", "JR-2026343"),
    ("2026343", "JR2026343"), ("REQ_0000082877-1", "REQ-0000082877"),
    ("R123", "r123"),
    ("2026-12345-1", "2026-12345"),  # a year-prefixed requisition with a copy suffix
    ("R0123456A-2", "R0123456A"),  # a requisition with a letter suffix
])
def test_workday_copy_suffixes_and_formats_match_the_requisition(expected, found):
    from auto_app.enrich.fetch import _same_requisition
    assert _same_requisition(expected, found)


@pytest.mark.parametrize("expected,found", [
    ("R123456", "R654321"), ("R123456", "JR123456"),
    ("JR2026343", "2026343-9999"), ("JR2026343", "OTHER2026343"),
    ("", ""), ("garbage2026343tail", "JR2026343"),
    ("REQ-2026-01", "REQ-2026-02"),  # two different copies are two different postings
])
def test_workday_requisition_matching_rejects_unrelated_ids(expected, found):
    from auto_app.enrich.fetch import _same_requisition
    assert not _same_requisition(expected, found)


def test_a_workday_record_with_a_suffixed_url_id_is_accepted():
    f = fetcher(lambda request: httpx.Response(200, json=WORKDAY))
    result = f.fetch(target_for(WD_URL + "-1"))
    assert result.status == "ok" and result.posting.description_text == "Write C++."


@pytest.mark.parametrize("req_id", [None, "", "JR-2026343"])
def test_workday_exact_posting_path_accepts_missing_or_reformatted_id(req_id):
    payload = {**WORKDAY, "jobPostingInfo": {**WORKDAY["jobPostingInfo"], "jobReqId": req_id}}
    f = fetcher(lambda request: httpx.Response(200, json=payload))
    assert f.fetch(target_for(WD_URL)).status == "ok"


@pytest.mark.parametrize("req_id", ["JR654321", "OTHER2026343", "2026343-9999"])
def test_workday_different_requisition_never_enriches_the_requested_job(req_id):
    payload = {**WORKDAY, "jobPostingInfo": {**WORKDAY["jobPostingInfo"], "jobReqId": req_id}}
    f = fetcher(lambda request: httpx.Response(200, json=payload))
    assert f.fetch(target_for(WD_URL)).status == "failed"


@pytest.mark.parametrize("bad", [None, "job", {"title": "Missing id"}, {"id": OTHER, "title": ""}])
def test_malformed_ashby_rows_do_not_fail_unlisted_jobs(bad):
    board = {"jobs": [bad, {**ASHBY["jobs"][0], "isListed": False}]}
    f = fetcher(lambda request: httpx.Response(200, json=board))
    result = f.fetch(target_for(f"https://jobs.ashbyhq.com/acme/{UUID}"))
    assert result.status == "ok" and result.posting.description_text == "Own services."
    broken = f.fetch(target_for(f"https://jobs.ashbyhq.com/acme/{OTHER}"))
    assert broken.status == ("failed" if isinstance(bad, dict) and bad.get("id") == OTHER else "gone")
    assert f.request_count == 1


def test_ashby_record_parse_failure_does_not_poison_cached_board(monkeypatch):
    from auto_app.sources.parsing import ParseError
    from auto_app.platforms.ashby import AshbySource
    parse = AshbySource.parse
    def fail_one(self, raw):
        if raw.record["id"] == UUID:
            raise ParseError("invalid fixture record")
        return parse(self, raw)
    monkeypatch.setattr(AshbySource, "parse", fail_one)
    f = fetcher(lambda request: httpx.Response(200, json=ASHBY))
    assert f.fetch(target_for(f"https://jobs.ashbyhq.com/acme/{UUID}")).status == "failed"
    assert f.fetch(target_for(f"https://jobs.ashbyhq.com/acme/{OTHER}")).status == "ok"
    assert f.request_count == 1

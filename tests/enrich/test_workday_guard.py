"""One Workday cool-down for the feed worker and JD enrichment.

On 2026-11-19 117 of enrichment's JD requests drew HTTP 403 from dozens of Workday tenants, between good reads,
while the feed worker had drawn 429: Workday blocks the address, not one site. So a 403 or 429 from any
Workday host pauses every Workday request, enrichment reads at most WORKDAY_REQUESTS Workday jobs per round, and
Workday requests are spaced out across tenants.
"""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from auto_app.enrich.fetch import FetchResult
from auto_app.enrich.service import WORKDAY_REQUESTS, enrich_jobs
from auto_app.enrich.targets import target_for
from auto_app.platforms.workday_guard import FILE_NAME, WorkdayGuard
from tests.enrich.test_fetch import Clock, fetcher
from tests.imported_jobs import FakeFetcher, ingest, make_store, ok, record

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)


def wd_url(i, tenant=None):
    return (f"https://{tenant or f't{i}'}.wd5.myworkdayjobs.com/en-US/External/job/US-TX-Austin/"
            f"Software-Engineer_JR2026{i:03d}")


@pytest.fixture
def store(tmp_path):
    result = make_store(tmp_path)
    ingest(result, [record(f"w{i}", f"Workday Engineer {i}", url=wd_url(i)) for i in range(1, 16)]
           + [record(str(100 + i), f"Greenhouse Engineer {i}") for i in range(1, 4)])
    yield result
    result.engine.dispose()


def guard_of(store):
    return WorkdayGuard.beside(store.db_path)


def answer(blocked=(), status=403):
    """ok for everything, except Workday tenants in `blocked` (or all Workday with blocked="all")."""
    def result(target):
        tenant = target.board.split("/")[0]
        if target.platform == "workday" and (blocked == "all" or tenant in blocked):
            return FetchResult("failed", error=f"HTTPStatusError: HTTP {status}", http_status=status)
        return ok("Build things.")
    return result


def workday_calls(fetcher):
    return [c for c in fetcher.calls if "myworkdayjobs" in c]


# ---- the guard ------------------------------------------------------------------------------------------------

def test_strikes_grow_the_cool_down_and_retry_after_can_lengthen_it(tmp_path):
    guard = WorkdayGuard(tmp_path / FILE_NAME)
    assert guard.cooling_until(NOW) is None
    assert guard.strike(NOW, status=403, by="enrich") == NOW + timedelta(hours=1)
    assert guard.cooling_until(NOW + timedelta(minutes=59)) == NOW + timedelta(hours=1)
    assert guard.cooling_until(NOW + timedelta(hours=1)) is None
    later = NOW + timedelta(hours=1)
    assert guard.strike(later, status=429, by="ingest") == later + timedelta(hours=2)
    last = later + timedelta(hours=2)
    assert guard.strike(last, status=429, by="ingest", retry_after=6 * 3600) == last + timedelta(hours=6)
    state = guard.state()
    assert state["strikes"] == 3 and state["status"] == 429 and state["by"] == "ingest"


def test_a_strike_while_cooling_does_not_escalate_or_shorten(tmp_path):
    guard = WorkdayGuard(tmp_path / FILE_NAME)
    until = guard.strike(NOW, status=429, by="ingest", retry_after=3 * 3600)
    assert guard.strike(NOW + timedelta(minutes=5), status=403, by="enrich") == until
    assert guard.state()["strikes"] == 1


def test_the_strikes_backoff_tops_out_at_twelve_hours(tmp_path):
    guard = WorkdayGuard(tmp_path / FILE_NAME)
    moment = NOW
    for _ in range(7):
        until = guard.strike(moment, status=403, by="enrich")
        moment = until
    assert until - guard_strike_time(guard) == timedelta(hours=12)


def guard_strike_time(guard):
    return datetime.fromisoformat(guard.state()["at"])


def test_a_good_read_clears_only_a_strike_made_before_it_started(tmp_path):
    guard = WorkdayGuard(tmp_path / FILE_NAME)
    guard.strike(NOW, status=403, by="enrich")
    guard.clear(started=NOW - timedelta(seconds=1))  # began before the strike: it may have raced past it
    assert guard.state()["strikes"] == 1
    guard.clear(started=NOW + timedelta(hours=1))
    assert guard.state() == {} and not (tmp_path / FILE_NAME).exists()


def test_an_unreadable_file_means_no_cool_down(tmp_path):
    (tmp_path / FILE_NAME).write_text("{not json")
    guard = WorkdayGuard(tmp_path / FILE_NAME)
    assert guard.cooling_until(NOW) is None
    (tmp_path / FILE_NAME).write_text(json.dumps({"strikes": "x", "until": "soon"}))
    assert guard.cooling_until(NOW) is None
    assert guard.strike(NOW, status=403, by="enrich") == NOW + timedelta(hours=1)


# ---- enrichment -----------------------------------------------------------------------------------------------

def test_enrichment_reads_at_most_ten_workday_jobs_a_round(store):
    fake = FakeFetcher(answer())
    report = enrich_jobs(store, fake, now=NOW)
    assert len(workday_calls(fake)) == WORKDAY_REQUESTS == 10
    assert len(fake.calls) - WORKDAY_REQUESTS == 3  # the Greenhouse jobs are not held back
    assert report["workday_deferred"] == 5 and report["skipped_limit"] == 0
    again = FakeFetcher(answer())
    enrich_jobs(store, again, now=NOW + timedelta(minutes=15))
    assert len(workday_calls(again)) == 5  # the rest, next round


def test_a_workday_block_stops_workday_requests_and_cools_both_sides(store):
    fake = FakeFetcher(answer(blocked="all"))
    report = enrich_jobs(store, fake, now=NOW)
    assert len(workday_calls(fake)) == 1  # the first 403 ends Workday reads for the round
    assert len(fake.calls) == 4 and report["ok"] == 3 and report["failed"] == 1
    assert report["workday_deferred"] == 14
    assert report["workday_cooling_until"] == (NOW + timedelta(hours=1)).isoformat()
    assert guard_of(store).cooling_until(NOW) == NOW + timedelta(hours=1)
    assert guard_of(store).state()["by"] == "enrich"


def test_while_cooling_no_workday_request_and_a_success_afterwards_clears_it(store):
    guard_of(store).strike(NOW, status=429, by="ingest")
    fake = FakeFetcher(answer())
    report = enrich_jobs(store, fake, now=NOW + timedelta(minutes=30))
    assert workday_calls(fake) == [] and report["workday_deferred"] == 15
    assert report["workday_cooling_until"] == (NOW + timedelta(hours=1)).isoformat()
    after = FakeFetcher(answer())
    report = enrich_jobs(store, after, now=NOW + timedelta(hours=1, minutes=5))
    assert len(workday_calls(after)) == 10 and report["workday_cooling_until"] is None
    assert guard_of(store).state() == {}


def test_a_429_also_cools_and_a_failing_url_backs_off_on_its_own(store):
    fake = FakeFetcher(answer(blocked={"t15"}, status=429))  # newest first: t15 is read first
    enrich_jobs(store, fake, now=NOW)
    assert len(workday_calls(fake)) == 1 and "t15" in workday_calls(fake)[0]
    later = FakeFetcher(answer(blocked={"t15"}, status=429))
    enrich_jobs(store, later, now=NOW + timedelta(hours=1, minutes=1))
    assert workday_calls(later) and all("t15" not in c for c in workday_calls(later))  # its own 30-min wait


# ---- the fetcher ----------------------------------------------------------------------------------------------

def test_workday_requests_wait_three_seconds_across_tenants_and_report_the_status():
    clock = Clock()
    f = fetcher(lambda request: httpx.Response(403, headers={"Retry-After": "120"}), clock)
    first = f.fetch(target_for(wd_url(1)))
    f.fetch(target_for(wd_url(2)))  # another tenant, another host: still three seconds
    f.fetch(target_for("https://job-boards.greenhouse.io/acme/jobs/101"))  # other platforms are not held
    assert clock.sleeps == [3.0]
    assert first.status == "failed" and first.http_status == 403 and first.retry_after == 120

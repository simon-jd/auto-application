# tests/worker/test_round.py
"""One worker round: the steps in order, each isolated; no backlog flood."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from auto_app.core.models import utcnow
from auto_app.worker.round import WorkerOptions, run_round
from tests.imported_jobs import FakeFetcher, ingest, make_store, ok, record
from tests.worker.test_digest import FakeDiscord

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
LONG_AGO = "2000-01-01T00:00:00+00:00"


@pytest.fixture
def store(tmp_path):
    result = make_store(tmp_path)
    ingest(result, [record("101", "Software Engineer, New Grad"), record("102", "Software Engineer Intern")])
    yield result
    result.engine.dispose()


def options(tmp_path, **kw):
    return WorkerOptions(screening_path=tmp_path / "config" / "screening.json", **kw)


def test_a_round_screens_enriches_and_notifies(store, tmp_path):
    fetcher = FakeFetcher(ok("Build APIs in Python. Visa sponsorship is available."))
    discord = FakeDiscord()
    report = run_round(store, options(tmp_path), fetcher=fetcher, discord=discord,
                       state={"notify_since": LONG_AGO}, now=NOW)
    assert list(report["steps"]) == ["screen", "enrich", "rescreen", "notify", "handoff", "summary", "health"]
    assert report["status"] == "ok"
    assert report["steps"]["screen"]["excluded"] == 1  # the internship
    assert len(fetcher.calls) == 1  # the internship is never fetched
    assert report["steps"]["notify"]["sent"] == 1
    first = discord.posts[0][1][0]
    assert first["title"].endswith("Software Engineer, New Grad — Acme")
    assert first["fields"][1]["value"] == "🟢 sponsors visas"  # from the enriched description


def test_a_locked_database_in_one_step_does_not_stop_the_others(store, tmp_path, monkeypatch):
    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("auto_app.screen.store.screen_jobs", locked)
    report = run_round(store, options(tmp_path), fetcher=FakeFetcher(ok("Build APIs.")), discord=FakeDiscord(),
                       state={}, now=NOW)
    assert report["status"] == "partial"
    assert report["steps"]["screen"]["status"] == "error"
    assert "database is locked" in report["steps"]["screen"]["error"]
    assert {"enrich", "notify"} <= set(report["steps"])


def test_first_round_does_not_push_the_backlog(store, tmp_path):
    later = utcnow() + timedelta(hours=3)  # past the title-only wait
    discord, state = FakeDiscord(), {}
    run_round(store, options(tmp_path, steps=("screen", "notify")), fetcher=None, discord=discord,
              state=state, now=later)
    assert discord.posts == [] and state["notify_since"] == later.isoformat()
    state["notify_since"] = LONG_AGO
    report = run_round(store, options(tmp_path, steps=("notify",)), fetcher=None, discord=discord,
                       state=state, now=later)
    assert report["steps"]["notify"]["sent"] == 1  # the new-grad job; the internship is screened out


def test_without_a_webhook_notify_is_skipped(store, tmp_path):
    report = run_round(store, options(tmp_path, steps=("screen", "notify")), fetcher=None, discord=None,
                       state={}, now=NOW)
    assert report["steps"]["notify"]["status"] == "skipped" and report["status"] == "ok"


def test_employer_404_is_rescreened_before_notify(store, tmp_path):
    from auto_app.enrich.fetch import FetchResult
    report = run_round(store, options(tmp_path), fetcher=FakeFetcher(FetchResult("gone")),
                       discord=FakeDiscord(), state={"notify_since": LONG_AGO}, now=utcnow() + timedelta(hours=3))
    assert report["steps"]["rescreen"]["excluded"] == 2
    assert report["steps"]["notify"]["sent"] == 0


def test_failed_screening_does_not_push_stale_verdicts(store, tmp_path, monkeypatch):
    from auto_app.screen.rules import ScreeningRules
    from auto_app.screen.store import screen_jobs
    screen_jobs(store, ScreeningRules())
    def broken(*a, **kw):
        raise ValueError("invalid screening preferences")
    monkeypatch.setattr("auto_app.screen.store.screen_jobs", broken)
    discord = FakeDiscord()
    report = run_round(store, options(tmp_path, steps=("screen", "notify")), fetcher=None,
                       discord=discord, state={"notify_since": LONG_AGO}, now=utcnow() + timedelta(hours=3))
    assert report["status"] == "partial"
    assert report["steps"]["notify"]["status"] == "skipped"
    assert discord.posts == []


def test_transient_fetch_failures_remain_visible_without_failing_the_round(store, tmp_path):
    from auto_app.enrich.fetch import FetchResult
    report = run_round(store, options(tmp_path), fetcher=FakeFetcher(FetchResult("failed", error="HTTP 503")),
                       discord=None, state={}, now=NOW)
    assert report["status"] == "ok" and report["steps"]["enrich"]["failed"] == 1
    assert "rescreen" not in report["steps"]


def test_different_failure_counts_do_not_repeat_the_same_alert(store, tmp_path, monkeypatch):
    from auto_app.worker.digest import send_alerts
    state, discord = {}, FakeDiscord()
    for count in (3, 4):
        monkeypatch.setattr("auto_app.enrich.service.enrich_jobs", lambda *a, **k: {"failed": count})
        report = run_round(store, options(tmp_path, steps=("enrich",)), fetcher=object(), discord=None,
                           state=state, now=NOW)
        send_alerts(discord, state, report, {}, NOW + timedelta(minutes=count))
    assert len(discord.posts) == 1


def test_a_few_failed_requests_do_not_fail_the_round(store, tmp_path):
    from auto_app.enrich.fetch import FetchResult
    ingest(store, [record(str(200 + i), f"Backend Engineer {i}") for i in range(4)])
    results = iter([FetchResult("failed", error="HTTP 503")] + [ok("Build APIs.")] * 10)
    report = run_round(store, options(tmp_path, steps=("screen", "enrich")),
                       fetcher=FakeFetcher(lambda t: next(results)), discord=None, state={}, now=NOW)
    assert report["steps"]["enrich"]["failed"] == 1
    assert report["steps"]["enrich"]["status"] == report["status"] == "ok"


def test_when_every_request_fails_the_round_reports_an_outage(store, tmp_path):
    from auto_app.enrich.fetch import FetchResult
    ingest(store, [record(str(200 + i), f"Backend Engineer {i}") for i in range(4)])
    report = run_round(store, options(tmp_path, steps=("screen", "enrich")),
                       fetcher=FakeFetcher(FetchResult("failed", error="ConnectError")),
                       discord=None, state={}, now=NOW)
    assert report["steps"]["enrich"]["status"] == "error" and report["status"] == "partial"
    assert "rescreen" not in report["steps"]


def test_exception_after_partial_enrichment_still_rescreens_saved_facts(store, tmp_path):
    from auto_app.core.db import tables as T
    from sqlalchemy import select
    ingest(store, [record("200", "Backend Engineer")])
    calls = 0
    def interrupted(target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("interrupted after the first persisted result")
        return ok("Requires 5+ years of professional experience.")
    report = run_round(store, options(tmp_path, steps=("screen", "enrich")),
                       fetcher=FakeFetcher(interrupted), discord=None, state={}, now=NOW)
    assert report["steps"]["enrich"]["status"] == "error"
    assert report["steps"]["rescreen"]["excluded"] == 2
    with store.engine.connect() as conn:
        assert ["experience_required:5"] in list(conn.execute(select(T.job_screenings.c.reasons)).scalars())

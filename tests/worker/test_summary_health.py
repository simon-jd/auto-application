"""Weekly and disk status use the same isolated worker steps as collection."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from auto_app.worker.round import WorkerOptions, run_round
from tests.imported_jobs import make_store
from tests.worker.test_digest import FakeDiscord

NOW = datetime(2026, 10, 19, 9, tzinfo=timezone.utc)


def test_summary_is_weekly_and_only_acknowledged_after_delivery(tmp_path):
    store, state = make_store(tmp_path), {}
    opts = WorkerOptions(screening_path=tmp_path / "s.json", steps=("summary",))
    failed = run_round(store, opts, fetcher=None, discord=FakeDiscord(fail_on=0), state=state, now=NOW)
    assert failed["status"] == "partial" and "summary_at" not in state
    discord = FakeDiscord()
    for offset in (0, 1, 8):
        run_round(store, opts, fetcher=None, discord=discord, state=state, now=NOW + timedelta(days=offset))
    assert len(discord.posts) == 2
    assert discord.posts[0][0].startswith("📊 Weekly")
    assert state["summary_at"] == (NOW + timedelta(days=8)).isoformat()
    store.engine.dispose()


def test_summary_mark_is_saved_before_posting_and_restored_on_failure(tmp_path):
    store, state, saved = make_store(tmp_path), {}, []
    opts = WorkerOptions(screening_path=tmp_path / "s.json", steps=("summary",))

    class Recording(FakeDiscord):
        def post(self, content, **kwargs):
            assert saved[-1].get("summary_at") == NOW.isoformat()  # persisted before delivery
            return super().post(content, **kwargs)

    save = lambda: saved.append(dict(state))
    run_round(store, opts, fetcher=None, discord=Recording(), state=state, now=NOW, save_state=save)
    assert state["summary_at"] == NOW.isoformat()
    later = NOW + timedelta(days=7)
    failed = run_round(store, opts, fetcher=None, discord=FakeDiscord(fail_on=0), state=state, now=later,
                       save_state=save)
    assert failed["status"] == "partial"
    assert state["summary_at"] == saved[-1]["summary_at"] == NOW.isoformat()
    store.engine.dispose()


def test_disk_alert_dedup_survives_changing_free_space(tmp_path, monkeypatch):
    from auto_app.worker.digest import send_alerts
    import shutil

    store, state, discord = make_store(tmp_path), {}, FakeDiscord()
    opts = WorkerOptions(screening_path=tmp_path / "s.json", steps=("health",))
    for free_gb in (1, .9):
        monkeypatch.setattr(shutil, "disk_usage", lambda p: SimpleNamespace(free=free_gb * 2**30))
        result = run_round(store, opts, fetcher=None, discord=discord, state=state, now=NOW)
        assert result["status"] == "partial" and result["steps"]["health"]["free_gb"] == free_gb
        send_alerts(discord, state, result, {}, NOW)
    assert len(discord.posts) == 1
    store.engine.dispose()


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), True])
def test_invalid_disk_threshold_is_rejected(tmp_path, value):
    with pytest.raises(ValueError):
        WorkerOptions(screening_path=tmp_path / "s.json", min_free_gb=value)


# ---- the pipeline's own numbers ----------------------------------------------------------------------------
# Every step can report ok while the library produces nothing: for weeks every kept job sat unjudged and this
# step said ok because the disk was fine.

def test_health_reports_what_the_pipeline_holds(tmp_path):
    from sqlalchemy import insert, update

    from auto_app.core.db import tables as T
    from tests.imported_jobs import ingest, job_id_of, record

    store, state = make_store(tmp_path), {}
    ingest(store, [record("101", "Backend Engineer"), record("102", "Data Engineer")])
    with store.engine.begin() as conn:
        conn.execute(update(T.jobs).values(first_seen_at=NOW - timedelta(days=9)))
    opts = WorkerOptions(screening_path=tmp_path / "s.json", steps=("health",))
    health = run_round(store, opts, fetcher=None, discord=FakeDiscord(), state=state, now=NOW)["steps"]["health"]
    assert health["status"] == "ok"  # an unjudged backlog is the owner's manual step, not a fault
    assert (health["kept_jobs"], health["judged_jobs"], health["unjudged_jobs"]) == (2, 0, 2)
    assert health["oldest_unjudged_days"] == 9 and health["stalled"] is True
    assert health["pending_enrichment"] == 2 and health["expired_claims"] == 0

    store.update_job(job_id_of(store, "Backend Engineer"), step2="recommended")
    with store.engine.begin() as conn:
        conn.execute(insert(T.job_screenings).values(
            job_id=job_id_of(store, "Data Engineer"), rules_version="t", facts_hash="h",
            outcome="excluded", reasons=["internship"], signals={}, screened_at=NOW))
    health = run_round(store, opts, fetcher=None, discord=FakeDiscord(), state=state, now=NOW)["steps"]["health"]
    assert (health["kept_jobs"], health["judged_jobs"], health["stalled"]) == (1, 1, False)
    assert health["oldest_unjudged_days"] is None
    store.engine.dispose()


def test_health_still_reports_the_disk_when_the_pipeline_query_fails(tmp_path, monkeypatch):
    store, state = make_store(tmp_path), {}
    monkeypatch.setattr("auto_app.worker.round._pipeline",
                        lambda store, now: (_ for _ in ()).throw(RuntimeError("boom")))
    opts = WorkerOptions(screening_path=tmp_path / "s.json", steps=("health",))
    health = run_round(store, opts, fetcher=None, discord=FakeDiscord(), state=state, now=NOW)["steps"]["health"]
    assert health["status"] == "ok" and health["pipeline_error"] == "RuntimeError" and "free_gb" in health
    store.engine.dispose()


def test_stalled_follows_the_oldest_wait_not_whether_anything_was_judged(tmp_path):
    """One judged job among thousands read as not stalled while the oldest had waited eleven days."""
    from sqlalchemy import update

    from auto_app.core.db import tables as T
    from auto_app.worker.round import STALLED_AFTER_DAYS
    from tests.imported_jobs import ingest, job_id_of, record

    store, state = make_store(tmp_path), {}
    ingest(store, [record("101", "Backend Engineer"), record("102", "Data Engineer")])
    store.update_job(job_id_of(store, "Backend Engineer"), step2="recommended")  # one of two is judged
    opts = WorkerOptions(screening_path=tmp_path / "s.json", steps=("health",))

    def health(age_days):
        with store.engine.begin() as conn:
            conn.execute(update(T.jobs).values(first_seen_at=NOW - timedelta(days=age_days)))
        return run_round(store, opts, fetcher=None, discord=FakeDiscord(), state=state,
                         now=NOW)["steps"]["health"]

    fresh = health(STALLED_AFTER_DAYS - 1)
    assert (fresh["judged_jobs"], fresh["unjudged_jobs"]) == (1, 1)
    assert fresh["stalled"] is False and fresh["oldest_unjudged_days"] == STALLED_AFTER_DAYS - 1
    assert health(STALLED_AFTER_DAYS)["stalled"] is True  # judged_jobs is still 1

    store.update_job(job_id_of(store, "Data Engineer"), step2="un_recommended")
    done = health(365)
    assert done["oldest_unjudged_days"] is None and done["stalled"] is False  # nothing to be behind on
    store.engine.dispose()

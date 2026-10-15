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

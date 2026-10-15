# tests/worker/test_loop.py
from auto_app.worker import loop


def test_watch_once_persists_state_and_status_reads_it(tmp_path):
    db = tmp_path / "data" / "auto_app.db"
    db.parent.mkdir(parents=True)
    rounds = []
    state = loop.watch(db, lambda st: rounds.append(dict(st)) or {"status": "ok"}, once=True, clock=lambda: 1000.0)
    assert len(rounds) == 1 and state["status"] == "ok" and state["consecutive_failures"] == 0
    saved = loop.status(db)
    assert saved["status"] == "ok" and saved["running"] is False and saved["last_report"] == {"status": "ok"}


def test_a_restart_waits_for_the_saved_next_round_and_failures_count(tmp_path):
    db = tmp_path / "data" / "auto_app.db"
    db.parent.mkdir(parents=True)
    loop.watch(db, lambda st: {"status": "partial"}, once=True, clock=lambda: 1000.0, interval_seconds=900)
    calls, sleeps, stops = [], [], iter([False, True])
    loop.watch(db, lambda st: calls.append(1) or {"status": "ok"}, clock=lambda: 1100.0, sleep=sleeps.append,
               should_stop=lambda: next(stops))
    assert calls == [] and sleeps == [30]
    assert loop.status(db)["consecutive_failures"] == 1


def test_status_before_any_round(tmp_path):
    assert loop.status(tmp_path / "data" / "auto_app.db")["status"] == "not_started"


def test_first_baseline_is_durable_before_any_round_side_effect(tmp_path):
    db = tmp_path / "jobs.db"
    seen = []
    def round_fn(state):
        saved = loop.status(db)
        seen.append(saved["notify_since"])
        assert saved["running"] is True
        assert state["notify_since"] == saved["notify_since"]
        raise RuntimeError("interrupted before finishing the first round")
    loop.watch(db, round_fn, once=True, clock=lambda: 1000.0)
    assert seen == ["1970-01-01T00:16:40+00:00"]
    loop.watch(db, lambda state: {"status": "ok"}, once=True, clock=lambda: 2000.0)
    assert loop.status(db)["notify_since"] == seen[0]


def test_round_duration_does_not_extend_the_fifteen_minute_schedule(tmp_path):
    stamps = iter([1000.0, 1000.0, 1120.0])
    result = loop.watch(tmp_path / "jobs.db", lambda state: {"status": "ok"},
                        once=True, clock=lambda: next(stamps))
    assert result["next_run_at"] == "1970-01-01T00:31:40+00:00"

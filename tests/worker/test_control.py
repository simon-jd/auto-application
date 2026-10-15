# tests/worker/test_control.py
from datetime import datetime, timedelta, timezone

from auto_app.worker import control, loop

import json
import pytest


def test_pause_until_expires_by_itself(tmp_path):
    db = tmp_path / "data" / "auto_app.db"
    db.parent.mkdir(parents=True)
    now = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
    control.write_control(db, paused=True, paused_until=now + timedelta(hours=2), reason="busy week", now=now)
    saved = control.read_control(db)
    assert saved["reason"] == "busy week"
    assert control.is_paused(saved, now) and not control.is_paused(saved, now + timedelta(hours=3))
    control.write_control(db, paused=False, now=now)
    assert not control.is_paused(control.read_control(db), now)


def test_a_paused_worker_waits_and_resumes(tmp_path):
    db = tmp_path / "data" / "auto_app.db"
    db.parent.mkdir(parents=True)
    control.write_control(db, paused=True)
    calls, sleeps = [], []
    ticks = iter([False, False, False])  # two paused ticks, then the round; afterwards next(ticks, True) stops

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            control.write_control(db, paused=False)

    loop.watch(db, lambda st: calls.append(1) or {"status": "ok"}, once=False, clock=lambda: 1000.0,
               sleep=sleep, should_stop=lambda: next(ticks, True) if calls else next(ticks))
    assert sleeps[:2] == [30, 30] and calls == [1]
    assert loop.status(db)["status"] == "ok"


def test_status_reports_paused_while_paused(tmp_path):
    db = tmp_path / "data" / "auto_app.db"
    db.parent.mkdir(parents=True)
    control.write_control(db, paused=True)
    stops = iter([False, True])
    loop.watch(db, lambda st: {"status": "ok"}, clock=lambda: 1000.0, sleep=lambda s: None,
               should_stop=lambda: next(stops))
    assert loop.status(db)["status"] == "paused"


def test_once_returns_paused_without_running_or_waiting(tmp_path):
    db = tmp_path / "jobs.db"
    control.write_control(db, paused=True)
    result = loop.watch(db, lambda _: pytest.fail("paused round ran"), once=True,
                        sleep=lambda _: pytest.fail("one-shot paused command waited"))
    assert result["status"] == "paused"


def test_resume_preserves_the_saved_deadline(tmp_path):
    from auto_app.ingest.scheduler import _write_state, _stamp

    db = tmp_path / "jobs.db"
    now, calls = [1000.0], []
    _write_state(db, {"next_run_at": _stamp(1100)}, loop.STATE)
    control.write_control(db, paused=True)

    def sleep(seconds):
        now[0] += seconds
        control.write_control(db, paused=False)

    loop.watch(db, lambda _: calls.append(now[0]) or {"status": "ok"}, clock=lambda: now[0], sleep=sleep,
               should_stop=lambda: bool(calls))
    assert calls == [1100]


@pytest.mark.parametrize("payload", ['{}', '{"paused": "false"}', '{"paused": true, "paused_until": "bad"}', "[]", "broken"])
def test_malformed_control_fails_closed_without_crashing(tmp_path, payload):
    db = tmp_path / "jobs.db"
    db.with_suffix(db.suffix + control.CONTROL).write_text(payload)
    state = loop.watch(db, lambda _: pytest.fail("invalid control must not start a round"), once=True)
    assert state["status"] == "control_invalid" and "resume" in state["control_error"]
    assert loop.status(db)["status"] == "control_invalid"


def test_an_invalid_control_file_is_reported_once_and_a_rewrite_resumes_rounds(tmp_path):
    db = tmp_path / "jobs.db"
    path = db.with_suffix(db.suffix + control.CONTROL)
    path.write_text("broken")
    problems, calls, sleeps = [], [], []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            control.write_control(db, paused=False)  # what /resume does

    loop.watch(db, lambda _: calls.append(1) or {"status": "ok"}, clock=lambda: 1000.0, sleep=sleep,
               should_stop=lambda: bool(calls), on_problem=problems.append)
    assert len(problems) == 1 and "control file" in problems[0]
    assert sleeps == [30, 30, 30] and calls == [1]
    saved = loop.status(db)
    assert saved["status"] == "ok" and "control_error" not in saved


def test_naive_expiry_is_interpreted_as_utc(tmp_path):
    db = tmp_path / "jobs.db"
    control.write_control(db, paused=True, paused_until=datetime(2026, 10, 13))
    assert control.is_paused(control.read_control(db), datetime(2026, 10, 12, tzinfo=timezone.utc))

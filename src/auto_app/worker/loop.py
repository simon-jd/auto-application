# src/auto_app/worker/loop.py
"""The worker's durable loop: one round now, then one every interval (docs/linux-worker.md §4).

Same mechanics as the feed worker (ingest/scheduler.py): a process lock beside the database, a JSON state
file written atomically, and a restart that waits for the saved next-run time.
"""

import os
from datetime import datetime, timezone

from auto_app.core.discord import redact_webhook_secrets
import time
from pathlib import Path
from auto_app.worker.control import is_paused, read_control

from auto_app.ingest.scheduler import (_due, _lock, _path, _read_state, _running, _stamp, _write_state,
                                       validate_interval)

STATE = ".worker-state.json"
LOCK = ".worker.lock"


def status(db_path: Path) -> dict:
    """Read the state file and lock without opening the database."""
    try:
        return {**_read_state(Path(db_path), STATE), "running": _running(Path(db_path), LOCK)}
    except (OSError, ValueError) as exc:
        return {"status": "error", "running": False, "error": str(exc)}


CONTROL_INVALID = ("the worker control file is invalid, so no rounds run; "
                   "resume (Discord /resume or the resume_pipeline tool) rewrites it")


def watch(db_path: Path, round_fn, *, interval_seconds: float = 900, once: bool = False, clock=time.time,
          sleep=time.sleep, should_stop=None, emit=None, on_problem=None) -> dict:
    """round_fn(state) -> report; `state` is persisted after every round (round_fn may update it).

    on_problem(message) is told once when the control file becomes unreadable: the worker then fails
    closed (no rounds) but stays alive, so a rewrite by resume/pause restarts rounds without a restart.
    """
    validate_interval(interval_seconds)
    should_stop = should_stop or (lambda: False)
    db_path = Path(db_path).expanduser().resolve()
    with _lock(_path(db_path, LOCK), "worker"):
        state = _read_state(db_path, STATE)
        due = 0 if once else _due(state.get("next_run_at"))
        while not should_stop():
            current = clock()
            try:
                control = read_control(db_path)
            except (OSError, ValueError):
                if state.get("status") != "control_invalid":
                    state.update(status="control_invalid", control_error=CONTROL_INVALID)
                    _write_state(db_path, state, STATE)
                    if on_problem is not None:
                        on_problem(CONTROL_INVALID)
                if once:
                    break
                sleep(30)
                continue
            if state.pop("control_error", None) is not None:
                state["status"] = "waiting"
                _write_state(db_path, state, STATE)
            if is_paused(control, datetime.fromtimestamp(current, timezone.utc)):
                if state.get("status") != "paused":
                    state["status"] = "paused"
                    _write_state(db_path, state, STATE)
                if once:
                    break
                sleep(30)
                continue
            if state.get("status") == "paused":
                state["status"] = "waiting"
                _write_state(db_path, state, STATE)
            remaining = due - current
            if remaining > 0:
                sleep(min(remaining, 30))
                continue
            started = clock()
            state.setdefault("notify_since", _stamp(started))
            state.update(status="running", last_run_at=_stamp(started), pid=os.getpid(),
                         interval_seconds=interval_seconds)
            _write_state(db_path, state, STATE)
            interrupted = False
            try:
                report = round_fn(state)
            except KeyboardInterrupt:
                interrupted, report = True, {"status": "interrupted"}
            except Exception as exc:
                report = {"status": "error", "error": redact_webhook_secrets(f"{type(exc).__name__}: {exc}")[:400]}
            finished = clock()
            # Keep a start-to-start cadence. An overrun never causes overlapping rounds.
            due = max(started + interval_seconds, finished)
            failures = 0 if report["status"] == "ok" else state.get("consecutive_failures", 0) + 1
            state.update(status=report["status"], last_finished_at=_stamp(finished), next_run_at=_stamp(due),
                         consecutive_failures=failures, last_report=report)
            _write_state(db_path, state, STATE)
            if emit is not None:
                emit(report)
            if once or interrupted:
                break
    return state

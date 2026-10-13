"""A small durable feed loop, independent of browser collection and model filters."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import time


class ImportLocked(OSError):
    """Another process owns this database's worker or import round."""


def _path(db_path: Path, suffix: str) -> Path:
    return Path(str(Path(db_path).expanduser().resolve()) + suffix)


@contextmanager
def _lock(path: Path, description: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ImportLocked(f"another {description} is already running") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    # Keep the inode: removing a lock file can let a third process lock a new one.


def import_lock(db_path: Path):
    """Serialize scheduled and manual imports into the same database."""
    return _lock(_path(db_path, ".ingest-import.lock"), "feed import")


def _read_state(db_path: Path) -> dict:
    path = _path(db_path, ".ingest-state.json")
    if not path.exists():
        return {"status": "not_started", "last_run_at": None, "last_finished_at": None,
                "next_run_at": None, "consecutive_failures": 0}
    try:
        state = json.loads(path.read_text())
        if not isinstance(state, dict):
            raise ValueError("expected an object")
        return state
    except (ValueError, OSError) as exc:
        raise ValueError(f"cannot read scheduler state: {path}") from exc


def _running(db_path: Path) -> bool:
    try:
        with _path(db_path, ".ingest-watch.lock").open("rb") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except FileNotFoundError:
        pass
    return False


def status(db_path: Path) -> dict:
    """Read the sidecar and lock state without creating or opening the database."""
    try:
        state = _read_state(db_path)
        return {**state, "running": _running(db_path)}
    except (OSError, ValueError) as exc:
        return {"status": "error", "running": False, "error": str(exc)}


def _write_state(db_path: Path, state: dict) -> None:
    path = _path(db_path, ".ingest-state.json")
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _stamp(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).isoformat()


def _due(value: str | None) -> float:
    return datetime.fromisoformat(value).timestamp() if value else 0


def validate_interval(interval_seconds: float) -> None:
    if isinstance(interval_seconds, bool) or not math.isfinite(interval_seconds) or interval_seconds <= 0:
        raise ValueError("interval must be a positive finite number of seconds")


def watch(config_path: Path, db_path: Path, *, interval_seconds: float = 1800,
          once: bool = False, clock=time.time, sleep=time.sleep, runner=None,
          should_stop=None, emit=None) -> dict:
    """Run immediately when new, then wait between rounds using durable due times.

    A restart waits until the previous next-run time. ``once`` explicitly runs
    one immediate round. SIGTERM handling belongs to the CLI; its stop predicate
    lets an in-progress round finish and stops an idle worker immediately.
    """
    validate_interval(interval_seconds)
    from auto_app.core.db.store import DbStore
    from auto_app.settings import get_paths
    if runner is None:
        from auto_app.ingest.runner import run_config
        runner = run_config
    should_stop = should_stop or (lambda: False)
    config_path = Path(config_path).expanduser().resolve()
    db_path = Path(db_path).expanduser().resolve()
    with _lock(_path(db_path, ".ingest-watch.lock"), "feed worker"):
        state = _read_state(db_path)
        due = 0 if once else _due(state.get("next_run_at"))
        try:
            while not should_stop():
                remaining = due - clock()
                if remaining > 0:
                    sleep(min(remaining, 30))
                    continue
                started = clock()
                state.update(status="running", config=str(config_path), interval_seconds=interval_seconds,
                             last_run_at=_stamp(started), pid=os.getpid())
                _write_state(db_path, state)
                interrupted = False
                try:
                    with import_lock(db_path):
                        store = DbStore(db_path, get_paths())
                        try:
                            # run_config reloads the YAML for every round.
                            report = runner(store, config_path)
                        finally:
                            store.engine.dispose()
                except ImportLocked as exc:
                    report = {"status": "busy", "error": str(exc)}
                except KeyboardInterrupt:
                    interrupted = True
                    report = {"status": "interrupted"}
                except Exception as exc:
                    report = {"status": "error", "error": str(exc)}
                finished = clock()
                due = finished + interval_seconds
                failures = state.get("consecutive_failures", 0) + 1 if report["status"] != "ok" else 0
                state.update(status=report["status"], last_finished_at=_stamp(finished),
                             next_run_at=_stamp(due), consecutive_failures=failures, last_report=report)
                _write_state(db_path, state)
                if emit is not None:
                    emit(report)
                if once or interrupted:
                    break
        except KeyboardInterrupt:
            pass
        return state

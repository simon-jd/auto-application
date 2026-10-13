"""Exclusive, restartable preparation execution outside the MCP server process.

The worker only operates its separate ledger and fictional artifacts. It does
not import the legacy appliers or own a connection to the job database.
"""

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
from typing import Awaitable, Callable


class WorkerBusy(ValueError):
    """Another process owns this preparation directory."""


class WorkerLock:
    """Keep one open flock descriptor for the entire worker lifetime.

    The lock file must never be unlinked: replacing it could let two processes
    lock different inodes and both consider themselves exclusive owners.
    """

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self._handle = None

    def __enter__(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        handle = (self.root / "worker.lock").open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise WorkerBusy("A preparation worker already owns this directory") from exc
        except BaseException:
            handle.close()
            raise
        self._handle = handle
        return self

    def __exit__(self, *exc):
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


def worker_status(root: Path) -> dict:
    """Report availability only with a fresh heartbeat, live PID and held lock."""
    root = Path(root)
    try:
        data = json.loads((root / "worker.json").read_text())
        if not isinstance(data, dict):
            raise ValueError("Invalid heartbeat")
        status = {key: data.get(key) for key in ("state", "pid", "updated_at", "driver", "active_run_id")}
        status["available"] = False
        if status["state"] != "running":
            return status
        updated = datetime.fromisoformat(status["updated_at"])
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        pid = status["pid"]
        if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1 or not 0 <= age <= 30:
            return {**status, "state": "stale"}
        os.kill(pid, 0)
        with (root / "worker.lock").open("r") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                status["available"] = True
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                status["state"] = "stale"
        return status
    except (OSError, ValueError, TypeError, OverflowError):
        return {"available": False, "state": "stopped"}


def _heartbeat(root: Path, *, state: str, driver: str, active_run_id: str | None) -> None:
    data = {"state": state, "pid": os.getpid(), "updated_at": datetime.now(timezone.utc).isoformat(),
            "driver": driver, "active_run_id": active_run_id}
    temporary = root / "worker.json.tmp"
    temporary.write_text(json.dumps(data) + "\n")
    temporary.replace(root / "worker.json")


async def _browser_executor(run, artifact_dir, **options):
    from auto_app.prepare.browser import fill_ashby

    return await fill_ashby(run, artifact_dir, **options)


def _failure(code: str, message: str) -> dict:
    return {"status": "failed", "field_checks": [], "missing_questions": [], "test_only": True,
            "error": {"code": code, "message": message}}


def _validate_attachment(run: dict, root: Path, artifact_dir: Path) -> None:
    from auto_app.prepare.candidate import validate_run_candidate

    candidate = run.get("candidate")
    if not isinstance(candidate, dict):
        raise ValueError("Invalid fictional candidate package")
    resume = candidate.get("resume_path")
    expected = artifact_dir / "fictional-sales-resume.pdf"
    # Do this before reading bytes: even an otherwise matching candidate must
    # not select a file or symlink outside its own immutable run directory.
    if (not isinstance(resume, str) or Path(resume) != expected
            or expected.resolve() != expected or not expected.is_relative_to(root)):
        raise ValueError("Candidate attachment must remain inside its own preparation run")
    validate_run_candidate(candidate)


async def run_worker(
    preparation_dir: Path,
    *,
    once: bool = False,
    driver: str = "rules",
    browser_channel: str | None = None,
    task_timeout: float = 120,
    poll_interval: float = 2,
    executor: Callable[..., Awaitable[dict]] | None = None,
) -> int:
    """Process tasks serially, keeping results durable before claiming the next.

    Cancellation deliberately leaves the claimed task running; the next owner
    recovers it only after acquiring the process lock. RunStore bounds that
    recovery to two attempts. A normal task timeout/error is terminal, not an
    automatic retry. executor is the browser boundary used by offline tests.
    """
    if driver not in {"rules", "claude"}:
        raise ValueError("driver must be rules or claude")
    if not math.isfinite(task_timeout) or not 0 < task_timeout <= 900:
        raise ValueError("task_timeout must be positive and at most 900 seconds")
    if not math.isfinite(poll_interval) or not 0 < poll_interval <= 10:
        raise ValueError("poll_interval must be positive and at most 10 seconds")
    root = Path(preparation_dir).expanduser().resolve()
    completed = 0
    execute = executor or _browser_executor
    with WorkerLock(root):
        from auto_app.prepare.store import RunStore

        store = RunStore(root)
        # Never recover before lock ownership; a second process could otherwise
        # requeue a task whose first owner is still using the browser.
        store.recover_interrupted()
        active_run_id = None

        async def pulse():
            while True:
                _heartbeat(root, state="running", driver=driver, active_run_id=active_run_id)
                await asyncio.sleep(5)

        _heartbeat(root, state="running", driver=driver, active_run_id=None)
        heartbeat_task = asyncio.create_task(pulse())
        try:
            while True:
                run = store.claim()
                if run is None:
                    if once:
                        break
                    await asyncio.sleep(poll_interval)
                    continue
                active_run_id = run["run_id"]
                _heartbeat(root, state="running", driver=driver, active_run_id=active_run_id)
                artifact_dir = root / "runs" / active_run_id
                try:
                    _validate_attachment(run, root, artifact_dir)
                except ValueError as exc:
                    store.finish(active_run_id, _failure("invalid_candidate", str(exc)))
                    completed += 1
                    active_run_id = None
                    if once:
                        break
                    continue
                try:
                    result = await asyncio.wait_for(
                        execute(run, artifact_dir, driver="claude" if driver == "claude" else None,
                                browser_channel=browser_channel),
                        timeout=task_timeout,
                    )
                    if not isinstance(result, dict) or result.get("status") not in {
                        "ready_for_review", "needs_input", "action_needed", "failed", "interrupted",
                    }:
                        result = _failure("invalid_result", "Preparation executor returned no valid terminal result")
                except TimeoutError:
                    result = _failure("timeout", f"Preparation exceeded the {task_timeout:g}-second task limit")
                except Exception as exc:
                    result = _failure("executor_error", f"{type(exc).__name__}: {exc}")
                store.finish(active_run_id, result)
                completed += 1
                active_run_id = None
                _heartbeat(root, state="running", driver=driver, active_run_id=None)
                if once:
                    break
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            _heartbeat(root, state="stopped", driver=driver, active_run_id=None)
    return completed

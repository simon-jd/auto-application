"""Single-worker ownership, durable results, crash recovery and task time bounds."""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
URL = "https://jobs.ashbyhq.com/example/83eec8f4-c37d-43c9-860b-27223eee3643/application"


@pytest.fixture
def queued(tmp_path):
    from auto_app.core.db.store import DbStore
    from auto_app.settings import Paths
    from auto_app.prepare.service import PreparationService

    db = tmp_path / "jobs.db"
    db_store = DbStore(db, Paths(home=tmp_path))
    db_store.engine.dispose()
    root = tmp_path / "preparation"
    service = PreparationService(db, root)
    return root, service.prepare_application(application_url=URL)


async def _checked(run, artifact_dir, **options):
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "proof.txt").write_text("checked fictional fixture")
    return {"status": "ready_for_review", "field_checks": [{"label": "Email", "status": "matched"}],
            "missing_questions": [], "stopped_before_submit": True,
            "executor_driver": options.get("driver"), "browser_channel": options.get("browser_channel")}


async def test_once_persists_result_and_restart_does_not_repeat(queued):
    from auto_app.prepare.store import RunStore
    from auto_app.prepare.worker import run_worker, worker_status

    root, queued_run = queued
    assert worker_status(root)["available"] is False
    assert await run_worker(root, once=True, executor=_checked) == 1
    result = RunStore(root).get(queued_run["run_id"])
    assert result["status"] == "ready_for_review"
    assert result["attempt"] == 1
    assert result["result"]["field_checks"] == [{"label": "Email", "status": "matched"}]
    assert result["result"]["executor_driver"] is None
    assert worker_status(root)["available"] is False
    assert await run_worker(root, once=True, executor=_checked) == 0
    assert RunStore(root).get(queued_run["run_id"])["attempt"] == 1
    assert (root / "runs" / queued_run["run_id"] / "proof.txt").exists()


async def test_competing_worker_cannot_recover_or_claim_active_run(queued):
    from auto_app.prepare.store import RunStore
    from auto_app.prepare.worker import WorkerBusy, run_worker, worker_status

    root, queued_run = queued
    started, release = asyncio.Event(), asyncio.Event()

    async def held(run, artifact_dir, **options):
        started.set()
        await release.wait()
        return await _checked(run, artifact_dir, **options)

    first = asyncio.create_task(run_worker(root, once=True, executor=held))
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert worker_status(root)["available"] is True
        with pytest.raises(WorkerBusy):
            await run_worker(root, once=True, executor=_checked)
        current = RunStore(root).get(queued_run["run_id"])
        assert current["status"] == "running" and current["attempt"] == 1
    finally:
        release.set()
        await first
    assert RunStore(root).get(queued_run["run_id"])["attempt"] == 1


async def test_cancelled_worker_recovers_once_and_exhausted_run_stays_interrupted(queued):
    from auto_app.prepare.store import RunStore
    from auto_app.prepare.worker import run_worker

    root, queued_run = queued
    store = RunStore(root)
    for expected_attempt in (1, 2):
        started = asyncio.Event()

        async def interrupted(run, artifact_dir, **options):
            started.set()
            await asyncio.sleep(3600)

        task = asyncio.create_task(run_worker(root, once=True, executor=interrupted))
        await asyncio.wait_for(started.wait(), 2)
        assert store.get(queued_run["run_id"])["attempt"] == expected_attempt
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert await run_worker(root, once=True, executor=_checked) == 0
    result = store.get(queued_run["run_id"])
    assert result["status"] == "interrupted" and result["attempt"] == 2


async def test_timeout_cancels_browser_and_stores_failure(queued):
    from auto_app.prepare.store import RunStore
    from auto_app.prepare.worker import run_worker

    root, queued_run = queued
    cancelled = asyncio.Event()

    async def slow(run, artifact_dir, **options):
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    assert await run_worker(root, once=True, executor=slow, task_timeout=0.02) == 1
    result = RunStore(root).get(queued_run["run_id"])
    assert cancelled.is_set()
    assert result["status"] == "failed"
    assert result["result"]["error"]["code"] == "timeout"
    assert result["result"]["field_checks"] == []


async def test_explicit_claude_driver_and_browser_channel_reach_executor(queued):
    from auto_app.prepare.store import RunStore
    from auto_app.prepare.worker import run_worker

    root, queued_run = queued
    await run_worker(root, once=True, executor=_checked, driver="claude", browser_channel="chrome")
    result = RunStore(root).get(queued_run["run_id"])["result"]
    assert result["executor_driver"] == "claude"
    assert result["browser_channel"] == "chrome"


async def test_executor_failure_is_persisted_and_worker_releases_lock(queued):
    from auto_app.prepare.store import RunStore
    from auto_app.prepare.worker import run_worker, worker_status

    root, queued_run = queued

    async def broken(run, artifact_dir, **options):
        raise RuntimeError("fixture browser startup failed")

    await run_worker(root, once=True, executor=broken)
    result = RunStore(root).get(queued_run["run_id"])
    assert result["status"] == "failed"
    assert result["result"]["error"]["code"] == "executor_error"
    assert worker_status(root)["available"] is False


def test_cli_empty_once_requires_no_browser_or_job_database(tmp_path):
    root = tmp_path / "empty-worker"
    result = subprocess.run(
        [sys.executable, "-m", "auto_app.agent.cli", "prepare-worker", "--preparation-dir", str(root), "--once"],
        cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads((root / "worker.json").read_text())["state"] == "stopped"


def test_cli_rejects_unbounded_timeout_before_creating_directory(tmp_path):
    root = tmp_path / "invalid"
    result = subprocess.run(
        [sys.executable, "-m", "auto_app.agent.cli", "prepare-worker", "--preparation-dir", str(root),
         "--task-timeout", "901", "--once"],
        cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert not root.exists()


def test_heartbeat_alone_does_not_claim_worker_available(tmp_path):
    from auto_app.prepare.worker import WorkerLock, worker_status
    from datetime import datetime, timezone

    root = tmp_path / "status"
    root.mkdir()
    heartbeat = {"state": "running", "pid": os.getpid(), "updated_at": datetime.now(timezone.utc).isoformat(),
                 "driver": "rules", "active_run_id": None}
    (root / "worker.json").write_text(json.dumps(heartbeat))
    assert worker_status(root)["available"] is False
    with WorkerLock(root):
        assert worker_status(root)["available"] is True
        heartbeat["updated_at"] = "2000-01-01T00:00:00+00:00"
        (root / "worker.json").write_text(json.dumps(heartbeat))
        assert worker_status(root)["available"] is False
    assert worker_status(root)["available"] is False


async def test_worker_rejects_attachment_outside_its_run_directory(queued, tmp_path):
    import sqlite3
    from auto_app.prepare.store import RunStore
    from auto_app.prepare.worker import run_worker

    root, queued_run = queued
    outside = tmp_path / "fictional-sales-resume.pdf"
    outside.write_bytes(Path(queued_run["candidate"]["resume_path"]).read_bytes())
    substituted = queued_run["candidate"]
    substituted["resume_path"] = str(outside)
    substituted["profile"]["cv"] = str(outside)
    with sqlite3.connect(RunStore(root).path) as conn:
        conn.execute("UPDATE preparation_runs SET candidate=? WHERE run_id=?",
                     (json.dumps(substituted), queued_run["run_id"]))
    await run_worker(root, once=True, executor=_checked)
    result = RunStore(root).get(queued_run["run_id"])
    assert result["status"] == "failed"
    assert result["result"]["error"]["code"] == "invalid_candidate"
    assert not (root / "runs" / queued_run["run_id"] / "proof.txt").exists()


def test_second_process_cannot_take_the_worker_lock(queued):
    from auto_app.prepare.worker import WorkerLock
    from auto_app.prepare.store import RunStore

    root, queued_run = queued
    with WorkerLock(root):
        result = subprocess.run(
            [sys.executable, "-m", "auto_app.agent.cli", "prepare-worker", "--preparation-dir", str(root), "--once"],
            cwd=root.parent, env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            capture_output=True, text=True, timeout=10,
        )
    assert result.returncode == 1
    assert "already owns" in result.stderr
    assert RunStore(root).get(queued_run["run_id"])["status"] == "queued"

"""Preparation requests own a separate ledger and never mutate the job library."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier

import pytest

from tests.agent.test_service import database, snapshot  # noqa: F401


URL = "https://jobs.ashbyhq.com/ramp/83eec8f4-c37d-43c9-860b-27223eee3643/application"


def service(database, tmp_path):
    from auto_app.prepare.service import PreparationService

    return PreparationService(database, tmp_path / "preparation")


def target():
    return {"job_id": None, "title": "Ashby preparation", "company": "ramp", "apply_url": URL}


def test_normalized_requests_reuse_durable_immutable_candidate(database, tmp_path):
    agent = service(database, tmp_path)
    before = snapshot(database)
    first = agent.prepare_application(application_url=URL.removesuffix("/application") + "?utm_source=test")
    second = service(database, tmp_path).prepare_application(application_url=URL)
    assert first["run_id"] == second["run_id"]
    assert first["apply_url"] == URL
    assert first["status"] == "queued" and first["attempt"] == 0
    candidate = first["candidate"]
    assert candidate["candidate_id"] == "fictional-sales-v1" and candidate["test_only"] is True
    assert candidate["profile"]["email"].endswith("@example.invalid")
    assert "work_authorization" not in candidate["answers"]
    assert "requires_sponsorship" not in candidate["answers"]
    resume = Path(candidate["resume_path"])
    assert resume.is_relative_to(tmp_path / "preparation" / "runs" / first["run_id"])
    assert sha256(resume.read_bytes()).hexdigest() == candidate["resume_sha256"]
    candidate["profile"]["email"] = "intruder@example.com"
    assert agent.get_application_run(first["run_id"])["candidate"]["profile"]["email"] == "alex.sandbox@example.invalid"
    assert snapshot(database) == before


@pytest.mark.parametrize("url", [
    "http://jobs.ashbyhq.com/ramp/83eec8f4-c37d-43c9-860b-27223eee3643",
    URL.replace("jobs.ashbyhq.com", "evil.example"),
    URL.replace("jobs.ashbyhq.com", "jobs.ashbyhq.com.evil.example"),
    URL.replace("jobs.ashbyhq.com", "user@jobs.ashbyhq.com"),
    URL.replace("jobs.ashbyhq.com", "jobs.ashbyhq.com:443"),
    URL.replace("/ramp/", "/../ramp/"),
    URL.replace("/ramp/", "/%72amp/"),
    URL + "/submit", "https://jobs.ashbyhq.com/ramp/not-a-uuid/application", URL + "\n",
    "file:///tmp/form.html", "", None, 123,
])
def test_rejects_unsupported_targets_without_library_writes(database, tmp_path, url):
    before = snapshot(database)
    with pytest.raises(ValueError):
        service(database, tmp_path).prepare_application(application_url=url)
    assert snapshot(database) == before


def test_exactly_one_target_required(database, tmp_path):
    agent = service(database, tmp_path)
    for kwargs in ({}, {"job_id": 1, "application_url": URL}, {"job_id": True}):
        with pytest.raises(ValueError):
            agent.prepare_application(**kwargs)


def test_saved_job_selects_ashby_posting_preserving_evidence(database, tmp_path):
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE postings SET apply_url=? WHERE job_id=2", (URL,))
    before = snapshot(database)
    run = service(database, tmp_path).prepare_application(job_id=2)
    assert (run["job_id"], run["title"], run["company"], run["apply_url"]) == (
        2, "Platform Engineer", "Acme", URL)
    assert snapshot(database) == before


@pytest.mark.parametrize("job_id, message", [(3, "excluded"), (4, "archived"), (5, "duplicate"), (999, "missing")])
def test_saved_job_safety_checks_precede_target_resolution(database, tmp_path, job_id, message):
    before = snapshot(database)
    with pytest.raises(ValueError, match=message):
        service(database, tmp_path).prepare_application(job_id=job_id)
    assert snapshot(database) == before


def test_applied_and_non_ashby_saved_jobs_are_rejected(database, tmp_path):
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE jobs SET decision='applied' WHERE job_id=2")
    with pytest.raises(ValueError, match="applied"):
        service(database, tmp_path).prepare_application(job_id=2)
    with pytest.raises(ValueError, match="Ashby"):
        service(database, tmp_path).prepare_application(job_id=1)


def test_concurrent_first_requests_and_claims_do_not_duplicate_runs(database, tmp_path):
    barrier = Barrier(8)

    def enqueue(_):
        barrier.wait(timeout=10)
        return service(database, tmp_path).prepare_application(application_url=URL)

    with ThreadPoolExecutor(max_workers=8) as pool:
        runs = list(pool.map(enqueue, range(8)))
    assert len({run["run_id"] for run in runs}) == 1
    from auto_app.prepare.store import RunStore

    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(lambda _: RunStore(tmp_path / "preparation").claim(), range(8)))
    assert len([run for run in claimed if run is not None]) == 1
    assert [run for run in claimed if run is not None][0]["attempt"] == 1


def test_candidate_substitutions_and_unknown_run_ids_are_rejected(tmp_path):
    from auto_app.prepare.candidate import fictional_candidate
    from auto_app.prepare.store import RunStore

    store = RunStore(tmp_path / "preparation")
    for field, value in (("candidate_id", "real"), ("resume_path", "/etc/passwd"), ("package_hash", "bad")):
        candidate = fictional_candidate()
        candidate[field] = value
        with pytest.raises(ValueError, match="candidate"):
            store.enqueue(target(), candidate)
    candidate = fictional_candidate()
    candidate["profile"]["email"] = "real@example.com"
    with pytest.raises(ValueError, match="candidate"):
        store.enqueue(target(), candidate)
    for run_id in ("../../etc/passwd", "missing", None):
        with pytest.raises(ValueError, match="run"):
            store.get(run_id)


def test_run_candidate_rejects_changed_facts_or_attachment(tmp_path):
    from auto_app.prepare.candidate import fictional_candidate, validate_run_candidate

    candidate = fictional_candidate()
    resume = tmp_path / "fictional-sales-resume.pdf"
    resume.write_bytes(Path(candidate["resume_path"]).read_bytes())
    candidate["resume_path"] = str(resume)
    candidate["profile"]["cv"] = str(resume)
    validate_run_candidate(candidate)
    candidate["profile"]["email"] = "real@example.com"
    with pytest.raises(ValueError, match="candidate"):
        validate_run_candidate(candidate)
    candidate["profile"]["email"] = "alex.sandbox@example.invalid"
    resume.write_bytes(b"changed attachment")
    with pytest.raises(ValueError, match="resume"):
        validate_run_candidate(candidate)


def test_recovery_is_bounded_and_results_persist_across_instances(tmp_path):
    from auto_app.prepare.candidate import fictional_candidate
    from auto_app.prepare.store import RunStore

    root = tmp_path / "preparation"
    store = RunStore(root)
    run = store.enqueue(target(), fictional_candidate())
    assert store.claim()["attempt"] == 1
    assert RunStore(root).recover_interrupted() == 1
    assert store.get(run["run_id"])["status"] == "queued"
    retry = store.claim()
    assert retry["attempt"] == 2
    assert retry["result"] is None
    assert RunStore(root).recover_interrupted() == 1
    assert store.get(run["run_id"])["status"] == "interrupted"
    assert store.claim() is None
    assert store.recover_interrupted() == 0
    assert store.enqueue(target(), fictional_candidate())["run_id"] == run["run_id"]


def test_preparation_ledger_cannot_alias_job_library(tmp_path):
    from auto_app.prepare.service import PreparationService

    root = tmp_path / "library"
    root.mkdir()
    db = root / "preparation.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE marker(value TEXT)")
    before = db.read_bytes()
    with pytest.raises(ValueError, match="separate"):
        PreparationService(db, root)
    assert db.read_bytes() == before


def test_finish_enforces_terminal_state_and_does_not_reopen_run(tmp_path):
    from auto_app.prepare.candidate import fictional_candidate
    from auto_app.prepare.store import RunStore

    store = RunStore(tmp_path / "preparation")
    run = store.enqueue(target(), fictional_candidate())
    with pytest.raises(ValueError, match="running"):
        store.finish(run["run_id"], {"status": "needs_input"})
    store.claim()
    for status in ("applied", "submitted", "queued", "running"):
        with pytest.raises(ValueError, match="status"):
            store.finish(run["run_id"], {"status": status})
    result = {"status": "needs_input", "field_checks": [], "missing_questions": [{"label": "Sponsorship"}]}
    finished = store.finish(run["run_id"], result)
    result["missing_questions"].clear()
    assert finished["status"] == "needs_input"
    assert store.get(run["run_id"])["result"]["missing_questions"] == [{"label": "Sponsorship"}]
    assert store.claim() is None


def test_preparation_does_not_import_browser_models_or_read_profile(database, tmp_path):
    code = '''
import importlib.abc
from pathlib import Path
import sys
blocked = ("auto_app.cli", "auto_app.apply", "anthropic", "claude_agent_sdk", "playwright")
class RejectRuntimeImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise AssertionError("Forbidden runtime import: " + fullname)
def inspect_open(event, args):
    if event == "open" and isinstance(args[0], str):
        if ".env" in Path(args[0]).parts or "profile" in Path(args[0]).parts:
            raise AssertionError("Forbidden secret read")
sys.meta_path.insert(0, RejectRuntimeImports())
sys.addaudithook(inspect_open)
from auto_app.prepare.service import PreparationService
service = PreparationService(Path(sys.argv[1]), Path(sys.argv[2]))
run = service.prepare_application(application_url=sys.argv[3])
assert service.get_application_run(run["run_id"])["status"] == "queued"
assert service.capabilities()["automated_submission"] is False
'''
    result = subprocess.run([sys.executable, "-c", code, str(database), str(tmp_path / "preparation"), URL],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr

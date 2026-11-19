"""A submitted job's package is frozen: apply-kit/submitted/<job>/ keeps what went out, whatever is republished later."""

import json

from typer.testing import CliRunner

from auto_app.handoff.archive import SUBMITTED_DIR, freeze_package
from tests.handoff.test_requests import tmp_path  # noqa: F401  (the runtime one level down, so apply-kit is per test)
from tests.handoff.test_service import (  # noqa: F401  (env is a fixture)
    NOW,
    env,
    service,
)

RESUME = b"%PDF-1.7 fictional resume\n"


def publish(home, job, resume=RESUME):
    package = home.parent / "apply-kit" / "jobs" / str(job)
    package.mkdir(parents=True, exist_ok=True)
    (package / "resume.pdf").write_bytes(resume)
    (package / "answers.md").write_text("## Why us?\nA fictional answer.\n")
    (package / "job.json").write_text(json.dumps({"job_id": job}))
    (package / "notes.txt").write_text("not a package file")
    if not (package / "linked.md").is_symlink():
        (package / "linked.md").symlink_to(package / "answers.md")
    return package


def frozen(home, job):
    return home.parent / "apply-kit" / SUBMITTED_DIR / str(job)


def submit(s, job):
    token = s.claim(job, "grok-bot", now=NOW)["token"]
    return s.record_attempt(token, "submitted", "submitted (test)", now=NOW)


def test_a_submitted_outcome_freezes_the_package(env):  # noqa: F811
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    publish(home, job)
    assert submit(s, job)["outcome"] == "submitted"
    copy = frozen(home, job)
    assert sorted(p.name for p in copy.iterdir()) == ["answers.md", "frozen.json", "job.json", "resume.pdf"]
    assert (copy / "resume.pdf").read_bytes() == RESUME
    meta = json.loads((copy / "frozen.json").read_text())
    assert meta["job_id"] == job and meta["backfilled"] is False and meta["frozen_at"].startswith("2026-10-12T12:00")
    assert set(meta["files"]) == {"answers.md", "job.json", "resume.pdf"}
    assert len(meta["files"]["resume.pdf"]["sha256"]) == 64 and meta["files"]["resume.pdf"]["bytes"] == len(RESUME)
    assert s.status(job, now=NOW)["package"]["submitted_copy"] == meta["frozen_at"]


def test_the_first_frozen_copy_is_kept(env):  # noqa: F811
    home, ids = env
    job = ids["Backend Engineer"]
    publish(home, job)
    first = freeze_package(home.parent / "apply-kit", job, NOW)
    publish(home, job, resume=b"%PDF-1.7 a later version\n")  # republished after the submission
    assert freeze_package(home.parent / "apply-kit", job, NOW) == first
    assert (frozen(home, job) / "resume.pdf").read_bytes() == RESUME


def test_no_package_or_an_unwritable_folder_never_fails_the_record(env):  # noqa: F811
    home, ids = env
    s = service(home)
    assert submit(s, ids["Backend Engineer"])["outcome"] == "submitted"  # no apply-kit at all
    assert not frozen(home, ids["Backend Engineer"]).exists()
    publish(home, ids["Data Engineer"])
    (home.parent / "apply-kit" / SUBMITTED_DIR).write_text("a file where the folder should be")
    assert submit(s, ids["Data Engineer"])["outcome"] == "submitted"
    assert s.status(ids["Data Engineer"], now=NOW)["package"]["submitted_copy"] is None


def test_a_manual_applied_freezes_too(env):  # noqa: F811
    from auto_app.agent.service import AgentService

    home, ids = env
    job = ids["ML Engineer"]
    publish(home, job)
    AgentService(home / "data" / "auto_app.db").record_application_status(job, "applied", reason="via Discord")
    assert (frozen(home, job) / "resume.pdf").read_bytes() == RESUME


def test_the_backfill_command_freezes_applied_jobs_once(env, monkeypatch):  # noqa: F811
    from auto_app.cli import app

    home, ids = env
    s = service(home)
    publish(home, ids["Backend Engineer"])
    submit(s, ids["Backend Engineer"])  # frozen at submission
    store_job = ids["Data Engineer"]
    from tests.imported_jobs import make_store
    store = make_store(home)
    store.set_job_state(store_job, decision="applied")  # applied before freezing existed
    store.set_job_state(ids["ML Engineer"], decision="applied")  # no package published
    store.engine.dispose()
    publish(home, store_job)
    monkeypatch.setenv("AUTO_APP_HOME", str(home))
    result = CliRunner().invoke(app, ["handoff", "freeze-submitted"])
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out == {"frozen": [store_job], "already": [ids["Backend Engineer"]], "no_package": [ids["ML Engineer"]]}
    assert json.loads((frozen(home, store_job) / "frozen.json").read_text())["backfilled"] is True

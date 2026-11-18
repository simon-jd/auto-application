"""Handoff: the user's change requests outside a claim (request_change), a job's progress in one read (status), and
the kick file that starts the package runner (docs/superpowers/specs/2026-11-18-phone-control-design.md)."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import update

from auto_app.core.db import tables as T
from auto_app.handoff.kick import KICK_FILE
from auto_app.handoff.service import HandoffError
from tests.handoff.test_questions import application, queued
from tests.handoff.test_service import (  # noqa: F401  (env is a fixture)
    NOW,
    env,
    service,
)
from tests.imported_jobs import make_store

CHANGE = "简历多写一点 Iceberg 的经历"


@pytest.fixture
def tmp_path(tmp_path):
    """The runtime is tmp_path/runtime, so its sibling apply-kit/ belongs to this test alone."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    return runtime


def kick_file(home):
    return home / "data" / KICK_FILE


def publish(home, job, at="2026-10-12T11:00:00+00:00", message=False):
    jobs = home.parent / "apply-kit" / "jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    (jobs / "index.json").write_text(json.dumps({"jobs": [
        {"job_id": job, "company": "Acme", "title": "Backend Engineer", "published_at": at,
         "has_message": message}]}))


# ---- request_change -------------------------------------------------------------------------------------------

def test_request_parks_an_unclaimed_wanted_job_and_creates_its_application(env):  # noqa: F811
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    assert application(home, job) is None  # never claimed: no application yet
    result = s.request_change(job, CHANGE, "grok-bot", now=NOW)
    assert result == {"job_id": job, "status": "needs_answers", "question_set_id": result["question_set_id"],
                      "changes": 1}
    assert job not in queued(s)
    assert application(home, job)["status"] == "needs_answers"
    pending = s.pending_questions()["items"]
    assert [(p["job_id"], p["agent"]) for p in pending] == [(job, "grok-bot")]
    assert pending[0]["questions"] == [{"text": CHANGE, "required": None, "limit": None, "page": None,
                                        "kind": "change"}]
    assert kick_file(home).is_file()


def test_second_request_joins_the_pending_set_and_moves_submitted_at(env):  # noqa: F811
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    first = s.request_change(job, CHANGE, "grok-bot", now=NOW)
    second = s.request_change(job, "求职信去掉第二段", "grok-bot", now=NOW + timedelta(hours=1))
    assert second["question_set_id"] == first["question_set_id"] and second["changes"] == 2
    pending = s.pending_questions()["items"]
    assert len(pending) == 1
    assert [q["text"] for q in pending[0]["questions"]] == [CHANGE, "求职信去掉第二段"]
    assert pending[0]["submitted_at"].startswith("2026-10-12T13:00")  # a package from before this text can't pass


def test_request_joins_open_questions_the_agent_handed_back(env):  # noqa: F811
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    token = s.claim(job, "grok-bot", now=NOW)["token"]
    s.submit_questions(token, [{"text": "Why us?"}], now=NOW)
    s.request_change(job, CHANGE, "grok-bot", now=NOW + timedelta(minutes=5))
    [item] = s.pending_questions()["items"]
    assert [(q["text"], q["kind"]) for q in item["questions"]] == [("Why us?", "question"), (CHANGE, "change")]


def test_request_refused_while_claimed_or_held(env):  # noqa: F811
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    token = s.claim(job, "grok-bot", now=NOW)["token"]
    with pytest.raises(HandoffError, match="questions"):
        s.request_change(job, CHANGE, "grok-bot", now=NOW + timedelta(minutes=1))
    s.hold(token, now=NOW + timedelta(minutes=2))
    with pytest.raises(HandoffError, match="questions"):
        s.request_change(job, CHANGE, "grok-bot", now=NOW + timedelta(hours=2))
    assert application(home, job)["status"] == "ready"  # the held form is untouched


def test_request_refused_for_parked_unwanted_or_too_long(env):  # noqa: F811
    home, ids = env
    s = service(home)
    blocked = ids["Backend Engineer"]
    token = s.claim(blocked, "grok-bot", now=NOW)["token"]
    s.record_attempt(token, "blocked", "the site wants a login", now=NOW)
    with pytest.raises(HandoffError, match="blocked"):
        s.request_change(blocked, CHANGE, "grok-bot", now=NOW)  # a login is not fixed by new materials
    store = make_store(home)
    store.set_job_state(ids["Data Engineer"], decision="not_applied")
    store.engine.dispose()
    with pytest.raises(HandoffError, match="want_to_apply"):
        s.request_change(ids["Data Engineer"], CHANGE, "grok-bot", now=NOW)
    with pytest.raises(HandoffError, match="1000"):
        s.request_change(ids["ML Engineer"], "x" * 1001, "grok-bot", now=NOW)
    with pytest.raises(HandoffError):
        s.request_change(ids["ML Engineer"], "  ", "grok-bot", now=NOW)
    assert not kick_file(home).exists()


def test_answers_ready_after_request_returns_job_to_queue(env):  # noqa: F811
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    s.request_change(job, CHANGE, "grok-bot", now=NOW)
    s.answers_ready(job, now=NOW + timedelta(hours=1))
    assert job in queued(s, NOW + timedelta(hours=1))
    assert s.pending_questions()["items"] == []


# ---- status ---------------------------------------------------------------------------------------------------

def write_runner(home, value):
    root = home.parent / "apply-kit"
    root.mkdir(parents=True, exist_ok=True)
    (root / "runner.json").write_text(json.dumps(value))


def test_status_before_the_package_is_made(env):  # noqa: F811
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    status = s.status(job, now=NOW)
    assert status["decision"] == "want_to_apply" and status["title"] == "Backend Engineer"
    assert status["application"] is None and status["claim"] is None and status["pending"] is None
    assert status["package"] == {"published": False, "published_at": None, "has_message": False}
    assert status["runner"] is None
    assert "not made yet" in status["summary"]


def test_status_with_a_running_runner_and_a_pending_change(env):  # noqa: F811
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    write_runner(home, {"running": True, "round": 1, "started_at": "2026-10-12T12:05:00+00:00", "items": 1})
    s.request_change(job, CHANGE, "grok-bot", now=NOW)
    status = s.status(job, now=NOW + timedelta(minutes=10))
    assert status["pending"] == {"questions": 0, "changes": 1, "submitted_at": "2026-10-12T12:00:00+00:00"}
    assert status["runner"]["running"] is True
    assert "waiting for Claude" in status["summary"] and "running" in status["summary"]


def test_status_while_claimed_held_and_once_published(env):  # noqa: F811
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    publish(home, job)
    assert "published" in s.status(job, now=NOW)["summary"]
    assert s.status(job, now=NOW)["package"]["published_at"] == "2026-10-12T11:00:00+00:00"
    token = s.claim(job, "grok-bot", now=NOW)["token"]
    claimed = s.status(job, now=NOW + timedelta(minutes=1))
    assert claimed["claim"]["agent"] == "grok-bot" and claimed["claim"]["held"] is False
    assert "being filled" in claimed["summary"]
    assert "token" not in json.dumps(claimed)  # never the token, nor its hash
    s.hold(token, now=NOW + timedelta(minutes=2))
    held = s.status(job, now=NOW + timedelta(minutes=3))
    assert held["claim"]["held"] is True and "waiting for your go" in held["summary"]


def test_status_of_applied_and_unknown_jobs(env):  # noqa: F811
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    token = s.claim(job, "grok-bot", now=NOW)["token"]
    s.record_attempt(token, "submitted", now=NOW)
    assert "applied" in s.status(job, now=NOW)["summary"]
    with pytest.raises(HandoffError, match="not found"):
        s.status(999999, now=NOW)


def test_status_ignores_a_broken_runner_file(env):  # noqa: F811
    home, ids = env
    root = home.parent / "apply-kit"
    root.mkdir(parents=True, exist_ok=True)
    (root / "runner.json").write_text("not json")
    assert service(home).status(ids["Backend Engineer"], now=NOW)["runner"] is None


# ---- the kick ---------------------------------------------------------------------------------------------------

def test_want_and_questions_kick_the_runner(env):  # noqa: F811
    from auto_app.agent.service import AgentService

    home, ids = env
    db = home / "data" / "auto_app.db"
    store = make_store(home)
    with store.engine.begin() as conn:
        conn.execute(update(T.jobs).where(T.jobs.c.job_id == ids["ML Engineer"]).values(decision=None))
    store.engine.dispose()
    AgentService(db).record_application_status(ids["ML Engineer"], "want_to_apply", reason="test")
    assert kick_file(home).is_file()
    kick_file(home).unlink()
    s = service(home)
    token = s.claim(ids["Backend Engineer"], "grok-bot", now=NOW)["token"]
    s.submit_questions(token, [{"text": "Why us?"}], now=NOW)
    assert kick_file(home).is_file()


def test_kick_never_raises(tmp_path):
    from auto_app.handoff.kick import kick

    (tmp_path / "data").write_text("a file where the folder should be")
    kick(tmp_path)  # no exception


# ---- review fixes (2026-11-18) ----------------------------------------------------------------------------------

def test_answers_ready_refuses_when_changes_arrived_after_the_answered_round(env):  # noqa: F811
    """A request merged while the runner was building from the first one must not be marked answered with it."""
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    s.request_change(job, CHANGE, "grok-bot", now=NOW)
    seen = s.pending_questions()["items"][0]["submitted_at"]  # what the runner's todo saw
    s.request_change(job, "求职信去掉第二段", "grok-bot", now=NOW + timedelta(minutes=10))
    with pytest.raises(HandoffError, match="changed"):
        s.answers_ready(job, now=NOW + timedelta(hours=1), asked_at=seen)
    assert job not in queued(s, NOW + timedelta(hours=1))
    [item] = s.pending_questions()["items"]
    assert len(item["questions"]) == 2  # nothing marked answered
    s.answers_ready(job, now=NOW + timedelta(hours=2), asked_at=item["submitted_at"])
    assert job in queued(s, NOW + timedelta(hours=2))


def test_answers_ready_refuses_an_unreadable_asked_at(env):  # noqa: F811
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    s.request_change(job, CHANGE, "grok-bot", now=NOW)
    with pytest.raises(HandoffError, match="asked_at"):
        s.answers_ready(job, now=NOW, asked_at="yesterday")


def test_status_names_what_keeps_a_wanted_job_out_of_the_queue(env):  # noqa: F811
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    store = make_store(home)
    with store.engine.begin() as conn:
        conn.execute(T.job_screenings.insert().values(
            job_id=job, rules_version="t", facts_hash="h", outcome="excluded",
            reasons=["start_before_graduation:2026-12"], signals={}, screened_at=NOW))
    store.engine.dispose()
    status = s.status(job, now=NOW)
    assert status["queue_block"] == "screened_out"
    assert "not in the queue" in status["summary"] and "screened_out" in status["summary"]
    assert "starts on the next kick" not in status["summary"]
    assert s.status(ids["Data Engineer"], now=NOW)["queue_block"] is None


def test_request_moves_a_job_parked_for_the_user_into_needs_answers(env):  # noqa: F811
    """#206 (2026-11-18): the agent stopped on [USER TO CONFIRM] items (needs_user); the user's answers, sent with
    request, go to Claude, and answers-ready returns the job once the revised package is published."""
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    token = s.claim(job, "grok-bot", now=NOW)["token"]
    s.record_attempt(token, "needs_user", "answers.md has USER TO CONFIRM items", now=NOW)
    parked = s.status(job, now=NOW)
    assert parked["queue_block"] == "waiting_for_you" and "`request`" in parked["summary"]
    s.request_change(job, "出差：可以，最多 50%", "grok-bot", now=NOW + timedelta(minutes=5))
    s.request_change(job, "创业经历：没有", "grok-bot", now=NOW + timedelta(minutes=6))
    assert application(home, job)["status"] == "needs_answers"
    [item] = s.pending_questions()["items"]
    assert [q["text"] for q in item["questions"]] == ["出差：可以，最多 50%", "创业经历：没有"]
    assert "waiting for Claude" in s.status(job, now=NOW + timedelta(minutes=7))["summary"]
    s.answers_ready(job, now=NOW + timedelta(hours=1), asked_at=item["submitted_at"])
    assert job in queued(s, NOW + timedelta(hours=1))

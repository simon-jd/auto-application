"""Handoff: open questions the agent hands back for answers, the owner's "answers ready", and holding a filled form."""

import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from auto_app.core.db import tables as T
from tests.handoff.test_service import (  # noqa: F401  (env is a fixture)
    NOW,
    env,
    service,
)
from tests.imported_jobs import make_store

QUESTIONS = [{"text": "Why do you want to work here?", "required": True, "limit": "300 words", "page": 1},
             {"text": "Tell us one thing that's not on your resume.", "required": False}]


def application(home, job_id):
    store = make_store(home)
    try:
        with store.engine.connect() as conn:
            return conn.execute(select(T.applications).where(T.applications.c.job_id == job_id)
                                .order_by(T.applications.c.application_id.desc())).mappings().first()
    finally:
        store.engine.dispose()


def queued(s, now=NOW):
    return [i["job_id"] for i in s.queue(now=now)["items"]]


# ---- handing questions back -------------------------------------------------------------------------------------

def test_questions_park_the_job_until_the_owner_marks_answers_ready(env):  # noqa: F811
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    token = s.claim(job, "agent-a", now=NOW)["token"]
    result = s.submit_questions(token, QUESTIONS, now=NOW + timedelta(minutes=2))
    assert result == {"job_id": job, "status": "needs_answers", "questions": 2,
                      "question_set_id": result["question_set_id"]}
    assert job not in queued(s)
    assert s.queue(now=NOW)["blocked"]["by_reason"]["waiting_for_answers"] == 1
    assert application(home, job)["status"] == "needs_answers"
    pending = s.pending_questions()["items"]
    assert [(p["job_id"], p["agent"], p["questions"]) for p in pending] == [(job, "agent-a", [
        {"text": "Why do you want to work here?", "required": True, "limit": "300 words", "page": 1,
         "kind": "question"},
        {"text": "Tell us one thing that's not on your resume.", "required": False, "limit": None, "page": None,
         "kind": "question"}])]
    assert pending[0]["title"] == "Backend Engineer" and pending[0]["submitted_at"].startswith("2026-10-12T12:02")

    ready = s.answers_ready(job, now=NOW + timedelta(hours=3))
    assert ready["status"] == "queued" and ready["job_id"] == job
    assert job in queued(s, NOW + timedelta(hours=3))
    assert s.pending_questions()["items"] == []
    assert application(home, job)["status"] == "queued"


def test_submitting_questions_uses_up_the_claim_and_leaves_a_trace(env):  # noqa: F811
    from auto_app.handoff.service import HandoffError

    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    token = s.claim(job, "agent-a", now=NOW)["token"]
    s.submit_questions(token, QUESTIONS, now=NOW)
    for later in (lambda: s.packet(token, now=NOW), lambda: s.record_attempt(token, "submitted", now=NOW),
                  lambda: s.submit_questions(token, QUESTIONS, now=NOW)):
        with pytest.raises(HandoffError, match="claim token"):
            later()
    store = make_store(home)
    try:
        with store.engine.connect() as conn:
            attempts = conn.execute(select(T.attempts)).mappings().all()
    finally:
        store.engine.dispose()
    assert [(a["applier"], a["outcome"]) for a in attempts] == [("agent-a", "needs_input")]
    assert "2 open questions" in attempts[0]["reason"]


def test_questions_need_a_live_claim(env):  # noqa: F811
    from auto_app.handoff.service import HandoffError

    home, ids = env
    s = service(home)
    token = s.claim(ids["Backend Engineer"], "agent-a", now=NOW)["token"]
    with pytest.raises(HandoffError, match="expired"):
        s.submit_questions(token, QUESTIONS, now=NOW + timedelta(minutes=31))
    with pytest.raises(HandoffError, match="claim token"):
        s.submit_questions("x" * 32, QUESTIONS, now=NOW)


@pytest.mark.parametrize("questions,message", [
    ([], "1 to 60"),
    ("Why us?", "1 to 60"),
    ([{"text": ""}], "text"),
    ([{"text": "   "}], "text"),
    ([{"text": "x" * 4001}], "text"),
    ([{"text": "Why us?\x07"}], "control"),
    ([{"text": "Why us?", "answer": "Because"}], "unknown"),
    ([{"text": "Why us?", "required": "yes"}], "required"),
    ([{"text": "Why us?", "limit": 300}], "limit"),
    ([{"text": "Why us?", "page": 0}], "page"),
    ([{"text": "Why us?", "page": True}], "page"),
    ([{"text": "Shorter, please", "kind": "comment"}], "kind"),
    ([{"text": f"Question {n}?"} for n in range(61)], "1 to 60"),
    (["Why us?"], "object"),
])
def test_malformed_questions_are_refused_and_keep_the_claim(env, questions, message):  # noqa: F811
    from auto_app.handoff.service import HandoffError

    home, ids = env
    s = service(home)
    token = s.claim(ids["Backend Engineer"], "agent-a", now=NOW)["token"]
    with pytest.raises(HandoffError, match=message):
        s.submit_questions(token, questions, now=NOW)
    assert s.packet(token, now=NOW)["job"]["job_id"] == ids["Backend Engineer"]  # the claim is still usable


def test_question_text_keeps_its_line_breaks_and_is_trimmed(env):  # noqa: F811
    home, ids = env
    s = service(home)
    token = s.claim(ids["Backend Engineer"], "agent-a", now=NOW)["token"]
    s.submit_questions(token, [{"text": "  Describe a project.\n(Max 2 paragraphs)\t "}], now=NOW)
    assert s.pending_questions()["items"][0]["questions"][0]["text"] == "Describe a project.\n(Max 2 paragraphs)"


def test_answers_ready_needs_a_job_that_is_waiting_for_answers(env):  # noqa: F811
    from auto_app.handoff.service import HandoffError

    home, ids = env
    s = service(home)
    with pytest.raises(HandoffError, match="not waiting for answers"):
        s.answers_ready(ids["Backend Engineer"], now=NOW)
    token = s.claim(ids["Backend Engineer"], "agent-a", now=NOW)["token"]
    s.submit_questions(token, QUESTIONS, now=NOW)
    store = make_store(home)
    store.set_job_state(ids["Backend Engineer"], decision="not_applied")
    store.engine.dispose()
    with pytest.raises(HandoffError, match="no longer want_to_apply"):
        s.answers_ready(ids["Backend Engineer"], now=NOW)


def test_the_packet_shows_the_latest_questions_and_whether_they_were_answered(env):  # noqa: F811
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    first = s.claim(job, "agent-a", now=NOW)["token"]
    assert s.packet(first, now=NOW)["open_questions"] is None
    s.submit_questions(first, QUESTIONS, now=NOW)
    s.answers_ready(job, now=NOW + timedelta(hours=1))
    second = s.claim(job, "agent-a", now=NOW + timedelta(hours=1))["token"]
    shown = s.packet(second, now=NOW + timedelta(hours=1))["open_questions"]
    assert [q["text"] for q in shown["questions"]] == [q["text"] for q in QUESTIONS]
    assert shown["submitted_at"].startswith("2026-10-12T12:00") and shown["answered_at"].startswith("2026-10-12T13:00")


def test_a_second_round_of_questions_is_listed_on_its_own(env):  # noqa: F811
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    s.submit_questions(s.claim(job, "agent-a", now=NOW)["token"], QUESTIONS, now=NOW)
    s.answers_ready(job, now=NOW + timedelta(hours=1))
    token = s.claim(job, "agent-a", now=NOW + timedelta(hours=2))["token"]
    s.submit_questions(token, [{"text": "A question on page 2?", "page": 2}], now=NOW + timedelta(hours=2))
    pending = s.pending_questions()["items"]
    assert len(pending) == 1 and [q["text"] for q in pending[0]["questions"]] == ["A question on page 2?"]


# ---- holding a filled form for the user's go -------------------------------------------------------------------

def test_a_hold_keeps_the_claim_past_its_lease_and_the_job_out_of_the_queue(env):  # noqa: F811
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    token = s.claim(job, "agent-a", now=NOW)["token"]
    held = s.hold(token, now=NOW + timedelta(minutes=20))
    assert held["status"] == "ready" and held["lease_until"].startswith("2026-10-13T12:20")  # 24 hours by default
    assert application(home, job)["status"] == "ready"
    two_hours = NOW + timedelta(hours=2)
    assert job not in queued(s, two_hours)
    result = s.record_attempt(token, "submitted", "user said submit", now=two_hours)
    assert result["status"] == "submitted"


def test_hold_hours_come_from_handoff_json(env):  # noqa: F811
    home, ids = env
    (home / "config").mkdir(exist_ok=True)
    (home / "config" / "handoff.json").write_text(json.dumps({"hold_hours": 72}))
    s = service(home)
    token = s.claim(ids["Backend Engineer"], "agent-a", now=NOW)["token"]
    assert s.hold(token, now=NOW)["lease_until"].startswith("2026-10-15T12:00")


def test_a_hold_needs_a_live_claim(env):  # noqa: F811
    from auto_app.handoff.service import HandoffError

    home, ids = env
    s = service(home)
    token = s.claim(ids["Backend Engineer"], "agent-a", now=NOW)["token"]
    with pytest.raises(HandoffError, match="expired"):
        s.hold(token, now=NOW + timedelta(minutes=31))


def test_the_same_agent_can_pick_up_its_held_job_with_a_new_token(env):  # noqa: F811
    from auto_app.handoff.service import HandoffError

    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    old = s.claim(job, "agent-a", now=NOW)["token"]
    s.hold(old, now=NOW)
    with pytest.raises(HandoffError, match="claimed"):
        s.claim(job, "agent-b", now=NOW + timedelta(hours=1))
    again = s.claim(job, "agent-a", now=NOW + timedelta(hours=1))
    assert again["token"] != old and again["resumed"] is True
    assert again["lease_until"].startswith("2026-10-13T12:00")  # the hold's end, not a fresh 30-minute lease
    assert application(home, job)["status"] == "ready"
    with pytest.raises(HandoffError, match="claim token"):
        s.record_attempt(old, "submitted", now=NOW + timedelta(hours=1))  # the old token no longer works
    assert s.record_attempt(again["token"], "submitted", now=NOW + timedelta(hours=1))["status"] == "submitted"


def test_an_ordinary_live_claim_is_not_picked_up_again_by_its_agent(env):  # noqa: F811
    from auto_app.handoff.service import HandoffError

    home, ids = env
    s = service(home)
    s.claim(ids["Backend Engineer"], "agent-a", now=NOW)
    with pytest.raises(HandoffError, match="claimed"):
        s.claim(ids["Backend Engineer"], "agent-a", now=NOW + timedelta(minutes=5))


def test_an_expired_hold_returns_the_job_to_the_queue(env):  # noqa: F811
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    s.hold(s.claim(job, "agent-a", now=NOW)["token"], now=NOW)
    later = NOW + timedelta(hours=25)
    assert job in queued(s, later)
    fresh = s.claim(job, "agent-b", now=later)
    assert fresh.get("resumed") is False and fresh["lease_until"].startswith("2026-10-13T13:30")


def test_the_users_change_requests_go_back_the_same_way(env):  # noqa: F811
    """At the pre-submit check the user asks for a change; the agent hands it back verbatim instead of editing."""
    home, ids = env
    s, job = service(home), ids["Backend Engineer"]
    token = s.claim(job, "agent-a", now=NOW)["token"]
    s.hold(token, now=NOW)
    asked = [{"kind": "change", "text": "Q3: drop the half-sentence about FDSE; end at 'where I want to grow.'"},
             {"kind": "question", "text": "A new question on page 2?", "page": 2}]
    assert s.submit_questions(token, asked, now=NOW + timedelta(hours=1))["status"] == "needs_answers"
    items = s.pending_questions()["items"][0]["questions"]
    assert [(q["kind"], q["text"][:3]) for q in items] == [("change", "Q3:"), ("question", "A n")]
    assert job not in queued(s, NOW + timedelta(hours=1))  # the held form waits for the revised package

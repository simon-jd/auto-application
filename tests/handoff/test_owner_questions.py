"""Questions Claude asks the owner (posted to Discord, answered there, then handled by Claude)."""

from datetime import timedelta

import pytest

from auto_app.handoff.asks import AskError, OwnerQuestions
from auto_app.handoff.kick import KICK_FILE
from tests.handoff.test_service import (  # noqa: F401  (env is a fixture)
    NOW,
    env,
)
from tests.imported_jobs import make_store

ASK = {"job_id": None, "question": "这是通用岗位，要投吗？投的话走哪个方向？",
       "context": "JD 写着 \"hires across all teams\"；和 #30271 冲突（30 天内只看一份申请）。",
       "options": ["投，infra/distributed_systems", "投，sde"], "recommended": 0}


def asks(home):
    return OwnerQuestions(home / "data" / "auto_app.db")


def decision(home, job_id):
    store = make_store(home)
    try:
        return store.job(job_id)["decision"]
    finally:
        store.engine.dispose()


def test_a_question_waits_to_be_posted_with_its_job(env):  # noqa: F811
    home, ids = env
    q = asks(home)
    job = ids["Backend Engineer"]
    sent = q.ask({**ASK, "job_id": job}, "claude-code", now=NOW)
    assert sent == {"question_id": sent["question_id"], "status": "open", "duplicate": False}
    [item] = q.to_post()
    assert item["question_id"] == sent["question_id"] and item["job_id"] == job
    assert item["title"] == "Backend Engineer" and item["company"] and item["link"].startswith("https://")
    assert item["options"] == ["投，infra/distributed_systems", "投，sde"] and item["recommended"] == 0
    q.mark_posted(sent["question_id"], "1572734903118860360", now=NOW)
    assert q.to_post() == []


def test_the_same_open_question_is_not_asked_twice(env):  # noqa: F811
    home, ids = env
    q = asks(home)
    first = q.ask({**ASK, "job_id": ids["Backend Engineer"]}, "claude-code", now=NOW)
    again = q.ask({**ASK, "job_id": ids["Backend Engineer"]}, "claude-code", now=NOW + timedelta(hours=1))
    assert again == {"question_id": first["question_id"], "status": "open", "duplicate": True}
    other = q.ask({**ASK, "job_id": ids["Data Engineer"]}, "claude-code", now=NOW)
    assert other["question_id"] != first["question_id"]


@pytest.mark.parametrize("payload", [
    {"question": ""}, {"question": "x" * 1001}, {"context": "x" * 3001},
    {"options": ["a"] * 5}, {"options": [""]}, {"options": ["x" * 81]}, {"recommended": 2},
    {"recommended": 0, "options": []}, {"job_id": 0}, {"unknown": 1},
])
def test_bad_questions_are_refused(env, payload):  # noqa: F811
    home, _ = env
    with pytest.raises(AskError):
        asks(home).ask({**ASK, **payload}, "claude-code", now=NOW)


def test_an_answer_goes_to_claude_and_kicks_the_runner(env):  # noqa: F811
    home, ids = env
    q = asks(home)
    question_id = q.ask({**ASK, "job_id": ids["Backend Engineer"]}, "claude-code", now=NOW)["question_id"]
    q.mark_posted(question_id, "42", now=NOW)
    answered = q.answer(question_id, "投，sde", now=NOW + timedelta(minutes=5))
    assert answered["outcome"] == "answered" and (home / "data" / KICK_FILE).is_file()
    [item] = q.answered()
    assert (item["question_id"], item["answer"], item["job_id"]) == (question_id, "投，sde", ids["Backend Engineer"])
    assert item["question"] == ASK["question"]
    with pytest.raises(AskError, match="already"):
        q.answer(question_id, "again")
    q.done(question_id, "材料已发布，进入投递队列", now=NOW + timedelta(minutes=30))
    assert q.answered() == []
    [report] = q.to_report()
    assert report["message_id"] == "42" and report["handled_note"] == "材料已发布，进入投递队列"
    q.mark_reported(question_id, now=NOW + timedelta(minutes=31))
    assert q.to_report() == []


def test_skip_records_not_applied_and_needs_no_claude(env):  # noqa: F811
    home, ids = env
    q = asks(home)
    job = ids["Backend Engineer"]
    question_id = q.ask({**ASK, "job_id": job}, "claude-code", now=NOW)["question_id"]
    q.mark_posted(question_id, "43", now=NOW)
    assert q.skip(question_id, now=NOW)["outcome"] == "skipped"
    assert decision(home, job) == "not_applied"
    assert q.answered() == []
    assert [r["question_id"] for r in q.to_report()] == [question_id]


def test_skip_needs_a_job_and_done_needs_an_answer(env):  # noqa: F811
    home, _ = env
    q = asks(home)
    question_id = q.ask(ASK, "claude-code", now=NOW)["question_id"]  # not about a job
    with pytest.raises(AskError, match="job"):
        q.skip(question_id)
    with pytest.raises(AskError, match="answer"):
        q.done(question_id, "nothing yet")
    with pytest.raises(AskError, match="not found"):
        q.answer(999999, "x")
    for bad in ("", "x" * 1001):
        with pytest.raises(AskError):
            q.answer(question_id, bad)

"""jobq: the user's change requests outside a claim (request) and one job's progress (status). Both work through
sudo, which is how grokbot reaches jobq."""

import pytest

from tests.jobq.test_cli import home, run  # noqa: F401  (home is a fixture)


def want(capsys, job):
    assert run(capsys, "want", str(job), "--reason", "the user picked it (test)")[0] == 0


def test_request_parks_the_job_and_status_says_so(home, capsys, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SUDO_USER", "grokbot")
    _, ids = home
    job = ids["ML Engineer"]
    want(capsys, job)
    code, out = run(capsys, "request", str(job), "--text", "简历多写一点 Iceberg")
    assert code == 0 and out["status"] == "needs_answers" and out["changes"] == 1
    code, out = run(capsys, "answers-needed")
    assert [(i["job_id"], i["agent"], i["questions"][0]["kind"]) for i in out["items"]] == [(job, "grok-bot", "change")]
    code, out = run(capsys, "status", str(job))
    assert code == 0 and out["pending"]["changes"] == 1 and "waiting for Claude" in out["summary"]
    code, out = run(capsys, "queue")
    assert job not in [i["job_id"] for i in out["items"]]


@pytest.mark.parametrize("text", ["two\nlines", "x" * 1001, "  "])
def test_bad_request_text_is_refused_by_the_parser(home, capsys, text):  # noqa: F811
    _, ids = home
    with pytest.raises(SystemExit) as exit_:
        run(capsys, "request", str(ids["ML Engineer"]), "--text", text)
    assert exit_.value.code == 2


def test_request_for_an_unwanted_job_fails_and_status_of_a_missing_job_too(home, capsys):  # noqa: F811
    _, ids = home
    code, out = run(capsys, "request", str(ids["ML Engineer"]), "--text", "more Iceberg")
    assert code == 1 and "want_to_apply" in out["error"]
    code, out = run(capsys, "status", "999999")
    assert code == 1 and "not found" in out["error"]


def test_answers_ready_passes_asked_at(home, capsys, monkeypatch):  # noqa: F811
    monkeypatch.delenv("SUDO_USER", raising=False)
    _, ids = home
    job = ids["ML Engineer"]
    want(capsys, job)
    run(capsys, "request", str(job), "--text", "more Iceberg")
    code, out = run(capsys, "answers-needed")
    seen = out["items"][0]["submitted_at"]
    code, out = run(capsys, "answers-ready", str(job), "--asked-at", "2000-01-01T00:00:00+00:00")
    assert code == 1 and "changed" in out["error"]
    code, out = run(capsys, "answers-ready", str(job), "--asked-at", seen)
    assert code == 0 and out["status"] == "queued"


def test_cheatsheet_requests_through_jobq_and_done_is_the_owners_step(home, capsys, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SUDO_USER", "grokbot")
    code, out = run(capsys, "cheatsheet", "--text", "最早入职时间：2027 年 6 月")
    assert code == 0 and out["status"] == "pending"
    request_id = out["request_id"]
    code, out = run(capsys, "cheatsheet-requests")
    assert code == 0 and [r["request_id"] for r in out["pending"]] == [request_id]
    assert out["pending"][0]["agent"] == "grok-bot"
    code, out = run(capsys, "cheatsheet-done", str(request_id), "--outcome", "applied", "--note", "added")
    assert code == 1 and "owner" in out["error"]  # Grok cannot mark its own request done
    monkeypatch.delenv("SUDO_USER")
    code, out = run(capsys, "cheatsheet-done", str(request_id), "--outcome", "applied", "--note", "§6: added")
    assert code == 0 and out["outcome"] == "applied"


def test_ask_answered_and_done_are_the_owners_steps(home, capsys, monkeypatch):  # noqa: F811
    import io
    import json as _json

    from auto_app.handoff.asks import OwnerQuestions

    _, ids = home
    job = ids["ML Engineer"]
    payload = _json.dumps({"job_id": job, "question": "要投吗？", "context": "JD: hiring immediately",
                           "options": ["投", "先放着"], "recommended": 0})
    monkeypatch.setenv("SUDO_USER", "grokbot")
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    code, out = run(capsys, "ask")
    assert code == 1 and "owner" in out["error"]  # Grok does not ask the owner through this channel
    monkeypatch.delenv("SUDO_USER")
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    code, out = run(capsys, "ask")
    assert code == 0 and out["status"] == "open"
    question_id = out["question_id"]
    db = home[0] / "data" / "auto_app.db"
    OwnerQuestions(db).answer(question_id, "投")  # what the Discord button does
    code, out = run(capsys, "answered")
    assert code == 0 and [(i["question_id"], i["answer"]) for i in out["items"]] == [(question_id, "投")]
    code, out = run(capsys, "question-done", str(question_id), "--note", "材料已发布")
    assert code == 0 and out["status"] == "done"
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert run(capsys, "ask")[0] == 2

"""jobq: hand open questions back (questions), list them (answers-needed), mark them answered (answers-ready, the
owner's step: refused through sudo, which is how grokbot reaches jobq) and hold a filled form (hold)."""

import io
import json

import pytest

from tests.jobq.test_cli import home, run  # noqa: F401  (home is a fixture)

QUESTIONS = [{"text": "Why us?", "required": True, "limit": "200 words", "page": 1}]


def stdin(monkeypatch, text):
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def claim(capsys, job):
    run(capsys, "want", str(job), "--reason", "the user picked it (test)")  # only wanted jobs are queued
    code, out = run(capsys, "claim", str(job))
    assert code == 0
    return out["token"]


def test_questions_read_the_token_then_the_json_from_stdin(home, capsys, monkeypatch):  # noqa: F811
    _, ids = home
    job = ids["ML Engineer"]
    token = claim(capsys, job)
    stdin(monkeypatch, token + "\n" + json.dumps(QUESTIONS))
    code, out = run(capsys, "questions")
    assert code == 0 and out["status"] == "needs_answers" and out["questions"] == 1
    code, out = run(capsys, "answers-needed")
    assert code == 0 and [(i["job_id"], i["agent"]) for i in out["items"]] == [(job, "grok-bot")]
    assert out["items"][0]["questions"][0]["limit"] == "200 words"
    code, out = run(capsys, "queue")
    assert job not in [i["job_id"] for i in out["items"]]


def test_with_the_token_as_an_argument_stdin_is_only_the_json(home, capsys, monkeypatch):  # noqa: F811
    _, ids = home
    token = claim(capsys, ids["ML Engineer"])
    stdin(monkeypatch, json.dumps(QUESTIONS))
    assert run(capsys, "questions", token)[0] == 0


@pytest.mark.parametrize("body,message", [
    ("", "no questions"),
    ("not json", "not valid JSON"),
    ('{"text": "Why us?"}', "1 to 60"),
    ("[" + "x" * 70000 + "]", "too long"),
])
def test_bad_question_input_is_refused_and_the_claim_kept(home, capsys, monkeypatch, body, message):  # noqa: F811
    _, ids = home
    token = claim(capsys, ids["ML Engineer"])
    stdin(monkeypatch, token + "\n" + body)
    code, out = run(capsys, "questions")
    assert code in (1, 2) and message in out["error"]
    stdin(monkeypatch, token + "\n")
    assert run(capsys, "packet")[0] == 0  # the claim still works


def test_answers_ready_puts_the_job_back_in_the_queue(home, capsys, monkeypatch):  # noqa: F811
    _, ids = home
    job = ids["ML Engineer"]
    token = claim(capsys, job)
    stdin(monkeypatch, token + "\n" + json.dumps(QUESTIONS))
    run(capsys, "questions")
    monkeypatch.delenv("SUDO_USER", raising=False)
    code, out = run(capsys, "answers-ready", str(job))
    assert code == 0 and out["status"] == "queued"
    assert job in [i["job_id"] for i in run(capsys, "queue")[1]["items"]]
    assert run(capsys, "answers-needed")[1]["items"] == []


def test_answers_ready_is_refused_through_sudo(home, capsys, monkeypatch):  # noqa: F811
    _, ids = home
    job = ids["ML Engineer"]
    token = claim(capsys, job)
    stdin(monkeypatch, token + "\n" + json.dumps(QUESTIONS))
    run(capsys, "questions")
    monkeypatch.setenv("SUDO_USER", "grokbot")
    code, out = run(capsys, "answers-ready", str(job))
    assert code == 1 and "owner" in out["error"]
    assert len(run(capsys, "answers-needed")[1]["items"]) == 1  # nothing changed


def test_hold_then_pick_up_the_held_job_again(home, capsys, monkeypatch):  # noqa: F811
    _, ids = home
    job = ids["ML Engineer"]
    token = claim(capsys, job)
    stdin(monkeypatch, token + "\n")
    code, out = run(capsys, "hold")
    assert code == 0 and out["status"] == "ready"
    code, again = run(capsys, "claim", str(job))  # jobq's agent is always grok-bot, so this is its own hold
    assert code == 0 and again["resumed"] is True and again["token"] != token
    stdin(monkeypatch, again["token"] + "\n")
    assert run(capsys, "record", "submitted", "--note", "user said submit")[0] == 0

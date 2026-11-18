"""Cheat-sheet requests: the user's general answers, passed on by an agent, for Claude to add to apply-kit/answers.md.

The agent only hands the words over; Claude edits the cheat sheet and marks each request done (the owner's step).
"""

from datetime import timedelta

import pytest

from auto_app.handoff.kick import KICK_FILE
from auto_app.handoff.service import HandoffError
from tests.handoff.test_service import (  # noqa: F401  (env is a fixture)
    NOW,
    env,
    service,
)

TEXT = "最早入职时间：2027 年 6 月"


def test_a_request_waits_for_claude_and_kicks_the_runner(env):  # noqa: F811
    home, _ = env
    s = service(home)
    sent = s.request_cheatsheet(TEXT, "grok-bot", now=NOW)
    assert sent == {"request_id": sent["request_id"], "status": "pending"}
    assert (home / "data" / KICK_FILE).is_file()
    listed = s.cheatsheet_requests(now=NOW)
    assert listed["pending"] == [{"request_id": sent["request_id"], "text": TEXT, "agent": "grok-bot",
                                  "submitted_at": "2026-10-12T12:00:00+00:00"}]
    assert listed["recent"] == []


def test_done_records_the_outcome_and_the_note(env):  # noqa: F811
    home, _ = env
    s = service(home)
    first = s.request_cheatsheet(TEXT, "grok-bot", now=NOW)["request_id"]
    second = s.request_cheatsheet("#206 出差可以 50%", "grok-bot", now=NOW + timedelta(minutes=1))["request_id"]
    done = s.cheatsheet_done(first, "applied", "§6 时间与地点：加了 Earliest start date 一行", now=NOW + timedelta(hours=1))
    assert done == {"request_id": first, "outcome": "applied", "done_at": "2026-10-12T13:00:00+00:00"}
    s.cheatsheet_done(second, "declined", "只针对 #206：用 jobq request 206", now=NOW + timedelta(hours=1))
    listed = s.cheatsheet_requests(now=NOW + timedelta(hours=2))
    assert listed["pending"] == []
    assert [(r["request_id"], r["outcome"]) for r in listed["recent"]] == [(second, "declined"), (first, "applied")]
    assert listed["recent"][1]["note"].startswith("§6")
    with pytest.raises(HandoffError, match="already"):
        s.cheatsheet_done(first, "applied", "again")


@pytest.mark.parametrize("text", ["", "   ", "x" * 1001, "bad\x00text"])
def test_bad_request_text_is_refused(env, text):  # noqa: F811
    home, _ = env
    with pytest.raises(HandoffError):
        service(home).request_cheatsheet(text, "grok-bot", now=NOW)
    assert not (home / "data" / KICK_FILE).exists()


@pytest.mark.parametrize("outcome,note", [("maybe", "n"), ("applied", ""), ("declined", "x" * 1001)])
def test_bad_done_input_is_refused(env, outcome, note):  # noqa: F811
    home, _ = env
    s = service(home)
    request_id = s.request_cheatsheet(TEXT, "grok-bot", now=NOW)["request_id"]
    with pytest.raises(HandoffError):
        s.cheatsheet_done(request_id, outcome, note)
    with pytest.raises(HandoffError, match="not found"):
        s.cheatsheet_done(999999, "applied", "n")


def test_recent_lists_at_most_twenty_done_requests(env):  # noqa: F811
    home, _ = env
    s = service(home)
    for i in range(25):
        request_id = s.request_cheatsheet(f"fact {i}", "grok-bot", now=NOW + timedelta(minutes=i))["request_id"]
        s.cheatsheet_done(request_id, "applied", "ok", now=NOW + timedelta(hours=1, minutes=i))
    recent = s.cheatsheet_requests(now=NOW + timedelta(hours=3))["recent"]
    assert len(recent) == 20 and recent[0]["text"] == "fact 24"

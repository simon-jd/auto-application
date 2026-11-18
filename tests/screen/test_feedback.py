"""The user's "shouldn't recommend" reasons from Discord, and the screening-rule proposals made from them."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect

from auto_app.agent.control import ControlError, ControlService
from auto_app.screen.feedback import FeedbackError, ScreeningFeedback
from auto_app.screen.rules import ScreeningRules
from auto_app.screen.store import screen_jobs
from tests.imported_jobs import ingest, job_id_of, make_store, record

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def jobs(tmp_path):
    store = make_store(tmp_path)
    ingest(store, [record("201", "Software Engineer"), record("202", "Field Service Associate"),
                   record("203", "Platform Engineer")])
    screen_jobs(store, ScreeningRules())
    ids = {t: job_id_of(store, t) for t in ("Software Engineer", "Field Service Associate", "Platform Engineer")}
    db = store.db_path
    store.engine.dispose()
    return db, ids


def test_the_table_exists_after_migration(jobs):
    db, _ = jobs
    store = make_store(db.parent.parent)
    try:
        columns = {c["name"] for c in inspect(store.engine).get_columns("screening_feedback")}
    finally:
        store.engine.dispose()
    assert columns == {"feedback_id", "job_id", "reason", "title", "company", "signals", "created_at", "proposal_id",
                       "resolved_at", "resolution", "resolution_note", "posted_at"}


def test_record_keeps_a_snapshot_of_the_job(jobs):
    db, ids = jobs
    feedback = ScreeningFeedback(db)
    saved = feedback.record(ids["Field Service Associate"], "  这是现场维修，不是软件岗  ", now=NOW)
    [item] = feedback.pending(now=NOW)
    assert item["feedback_id"] == saved["feedback_id"] and item["job_id"] == ids["Field Service Associate"]
    assert item["reason"] == "这是现场维修，不是软件岗"
    assert item["title"] == "Field Service Associate" and item["company"]
    assert item["signals"]["outcome"] == "kept" and "role" in item["signals"]
    assert item["created_at"].startswith("2026-11-18T12:00")


@pytest.mark.parametrize("reason", ["", "   ", "x" * 301, "two\x00parts"])
def test_record_refuses_bad_reasons(jobs, reason):
    db, ids = jobs
    with pytest.raises(FeedbackError):
        ScreeningFeedback(db).record(ids["Software Engineer"], reason, now=NOW)


def test_record_refuses_an_unknown_job(jobs):
    db, _ = jobs
    with pytest.raises(FeedbackError, match="not found"):
        ScreeningFeedback(db).record(999999, "no", now=NOW)


def test_due_after_three_reasons_or_three_days(jobs):
    db, ids = jobs
    feedback = ScreeningFeedback(db)
    assert feedback.due(now=NOW) is False
    feedback.record(ids["Software Engineer"], "a", now=NOW)
    feedback.record(ids["Platform Engineer"], "b", now=NOW)
    assert feedback.due(now=NOW) is False
    assert feedback.due(now=NOW + timedelta(days=3)) is True  # the oldest waited three days
    feedback.record(ids["Field Service Associate"], "c", now=NOW)
    assert feedback.due(now=NOW) is True


def test_a_proposal_carries_its_note_and_preview_and_holds_the_reasons(jobs):
    db, ids = jobs
    feedback, control = ScreeningFeedback(db), ControlService(db)
    first = feedback.record(ids["Field Service Associate"], "现场技术员", now=NOW)["feedback_id"]
    proposal = control.propose_screening_change({"extra_non_cs_terms": ["field service"]}, note="排除 field service")
    assert proposal["note"] == "排除 field service"
    saved = control.screening_proposal(proposal["change_id"])
    assert saved["note"] == "排除 field service"
    assert saved["preview"]["newly_excluded"] == 1 and saved["diff"]["extra_non_cs_terms"][1] == ["field service"]
    assert feedback.attach([first], proposal["change_id"]) == 1
    assert feedback.pending(now=NOW) == []  # held by an open proposal
    assert feedback.open_proposals() == [proposal["change_id"]]
    assert [i["reason"] for i in feedback.attached(proposal["change_id"])] == ["现场技术员"]
    with pytest.raises(FeedbackError, match="pending"):
        feedback.attach([first], proposal["change_id"])  # already held


def test_resolve_after_apply_and_discard(jobs):
    db, ids = jobs
    feedback, control = ScreeningFeedback(db), ControlService(db)
    one = feedback.record(ids["Software Engineer"], "a", now=NOW)["feedback_id"]
    two = feedback.record(ids["Platform Engineer"], "b", now=NOW)["feedback_id"]
    applied = control.propose_screening_change({"extra_non_cs_terms": ["technician"]})
    feedback.attach([one], applied["change_id"])
    control.apply_screening_change(applied["change_id"], applied["base_revision"])
    assert feedback.resolve(applied["change_id"], "applied") == 1
    discarded = control.propose_screening_change({"extra_non_cs_terms": ["technician", "field"]})
    feedback.attach([two], discarded["change_id"])
    assert control.discard_screening_change(discarded["change_id"]) == {"change_id": discarded["change_id"],
                                                                        "discarded": True}
    assert control.screening_proposal(discarded["change_id"]) is None
    assert feedback.resolve(discarded["change_id"], "discarded") == 1
    assert feedback.pending(now=NOW) == [] and feedback.open_proposals() == []
    with pytest.raises(ControlError):
        control.discard_screening_change(discarded["change_id"])
    with pytest.raises(FeedbackError):
        feedback.resolve(discarded["change_id"], "maybe")


def test_an_expired_or_missing_proposal_releases_its_reasons(jobs):
    db, ids = jobs
    feedback, control = ScreeningFeedback(db), ControlService(db)
    one = feedback.record(ids["Software Engineer"], "a", now=NOW)["feedback_id"]
    proposal = control.propose_screening_change({"extra_non_cs_terms": ["technician"]})
    feedback.attach([one], proposal["change_id"])
    path = control.proposals / f"{proposal['change_id']}.json"
    saved = json.loads(path.read_text())
    saved["created_at"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    path.write_text(json.dumps(saved))
    assert control.screening_proposal(proposal["change_id"]) is None
    assert [i["feedback_id"] for i in feedback.pending(now=NOW)] == [one]
    assert feedback.open_proposals() == []


def test_close_without_a_rule_change_is_posted_once(jobs):
    db, ids = jobs
    feedback = ScreeningFeedback(db)
    one = feedback.record(ids["Software Engineer"], "方向不对", now=NOW)["feedback_id"]
    assert feedback.close([one], "关键词分不出方向，留给以后的 Claude 筛选器", now=NOW) == 1
    assert feedback.pending(now=NOW) == []
    [closed] = feedback.unposted_closures()
    assert closed["reason"] == "方向不对" and closed["resolution_note"].startswith("关键词")
    feedback.mark_posted([one], now=NOW)
    assert feedback.unposted_closures() == []
    with pytest.raises(FeedbackError, match="pending"):
        feedback.close([one], "again", now=NOW)

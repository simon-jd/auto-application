"""Expiry maintenance must not turn a committed user decision into a failure."""

import pytest
from sqlalchemy import event, select, update

from auto_app.core.config import JobTTL
from auto_app.core.db.job_ttl import expire_jobs
from auto_app.core.db import tables as T
from auto_app.select.review import Review
from tests.core.test_job_ttl import db, seen_days_ago
from tests.process.test_link import add, job_of, run, wd
from tests.select.test_review import env


def preference_file(db, text):
    db.paths.preferences.parent.mkdir(exist_ok=True)
    db.paths.preferences.write_text(text)


@pytest.mark.parametrize("text", ['{broken', '{"exclude_countries": "Canada"}'])
def test_bad_preferences_do_not_break_saved_decision_or_undo(db, text):
    add(db, "nvidia", "JR1", url=wd("JR1"))
    add(db, "nvidia", "JR2", url=wd("JR2"), title="Another Role")
    run(db)
    target, expired = (job_of(db, f"nvidia/{ref}") for ref in ("JR1", "JR2"))
    seen_days_ago(db, "nvidia/JR2", 40)
    preference_file(db, text)
    review = Review(db, [], ttl=JobTTL())

    review.decide(target, "want_to_apply", "Interested")
    assert db.job(target)["decision"] == "want_to_apply"
    assert len(db.reviews(target)) == 1
    assert db.job(expired) is not None
    assert review.undo() == f"decision on #{target}"
    assert db.job(target)["decision"] is None
    assert db.reviews(target) == []
    assert db.job(expired) is not None


@pytest.mark.parametrize("dry_run", [False, True])
def test_invalid_preferences_defer_deletion_and_report_warning(db, dry_run, caplog):
    add(db, "nvidia", "JR1", url=wd("JR1"))
    run(db)
    jid = job_of(db, "nvidia/JR1")
    seen_days_ago(db, "nvidia/JR1", 40)
    preference_file(db, '{broken')

    report = expire_jobs(db, JobTTL(), dry_run=dry_run)
    assert report.deleted == []
    assert report.archived == []
    assert db.job(jid) is not None
    assert report.warnings and "preferences" in report.warnings[0]
    assert "preferences" in caplog.text


def test_preferences_are_not_needed_when_no_job_can_be_deleted(db):
    add(db, "nvidia", "JR1", url=wd("JR1"))
    run(db)
    jid = job_of(db, "nvidia/JR1")
    preference_file(db, '{broken')
    report = expire_jobs(db, JobTTL())
    assert report.deleted == report.archived == []
    assert db.job(jid) is not None


def test_malformed_preferences_preserve_implied_labels_and_undo(env):
    db, review, jobs, _ = env
    # An unrelated expired unjudged job forces the maintenance preference check.
    add(db, "nvidia", "JR99", url=wd("JR99"), title="Unjudged Role")
    run(db)
    seen_days_ago(db, "nvidia/JR99", 40)
    preference_file(db, '{broken')
    review.decide(jobs["sales"], "want_to_apply")
    labels = db.feedback_of(jobs["sales"])
    assert set(labels) == {"ml", "research"}
    assert all(row["label"] and row["origin"] == "implied" for row in labels.values())
    assert review.undo() == f"decision on #{jobs['sales']}"
    assert db.feedback_of(jobs["sales"]) == {}
    assert db.reviews(jobs["sales"]) == []


def test_expiry_does_not_read_location_json_for_current_jobs(db):
    add(db, "nvidia", "JR1", url=wd("JR1"))
    run(db)
    preference_file(db, '{"exclude_countries": ["Canada"]}')
    statements = []
    def record(conn, cursor, statement, parameters, context, many):
        statements.append((statement.lower(), parameters))
    event.listen(db.engine, "before_cursor_execute", record)
    try:
        expire_jobs(db, JobTTL())
    finally:
        event.remove(db.engine, "before_cursor_execute", record)
    # A regular decision must not scan every saved posting's location document.
    assert not any("json_extract" in sql and "$.locations" in str(params) for sql, params in statements)


def test_structured_canada_only_country_evidence_archives_instead_of_deleting(db):
    add(db, "nvidia", "JR1", url=wd("JR1"), locations=["Remote"])
    run(db)
    jid = job_of(db, "nvidia/JR1")
    with db.engine.begin() as conn:
        parsed = conn.execute(select(T.postings.c.parsed)).scalar_one()
        parsed["extras"] = {**parsed.get("extras", {}), "country": "CA"}
        conn.execute(update(T.postings).values(parsed=parsed))
    seen_days_ago(db, "nvidia/JR1", 40)
    preference_file(db, '{"exclude_countries": ["Canada"]}')

    report = expire_jobs(db, JobTTL())
    assert report.deleted == []
    assert [item.job_id for item in report.archived] == [jid]
    assert db.job(jid)["archive"]["countries"] == ["CA"]
    assert db.jobs(archived=True, include_excluded=False) == []

"""Source observations must stay discoverable through parsing and automatic TTL runs."""

import logging
from datetime import datetime, timezone

import pytest
from sqlalchemy import update

from auto_app.collect.store import PostingRef
from auto_app.core.config import AppConfig, JobTTL
from auto_app.core.db import tables as T
from auto_app.core.db.job_ttl import apply_job_ttl
from auto_app.core.db.store import DbStore
from auto_app.ingest.models import IngestRecord, ParsedFeed
from auto_app.ingest.service import IngestService
from auto_app.process.parse import parse_postings
from auto_app.query.service import QueryService
from auto_app.settings import Paths
from tests.helpers import test_registry
from tests.process.test_link import add, run, wd


OLD = datetime(2024, 1, 2, 12, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
URL = "https://nvidia.wd5.myworkdayjobs.com/site/job/US-CA/Engineer_R42"


@pytest.fixture
def store(tmp_path):
    db = DbStore(tmp_path / "retention.db", Paths(home=tmp_path))
    yield db
    db.engine.dispose()


def old_email(store, *, complete=False, source="mail"):
    record = IngestRecord(
        external_id="message-1", url=URL, title="Backend Engineer" if complete else None,
        description="Build distributed systems." if complete else "", company="NVIDIA",
        metadata={"observed_at": OLD.isoformat()},
    )
    IngestService(store).ingest(source, ParsedFeed([record]), kind="email", origin="old.eml", revision="message-id")
    return store.jobs()[0]["job_id"]


def test_old_pending_email_survives_dry_and_real_ttl_without_refreshing_observation(store):
    jid = old_email(store)
    dry = apply_job_ttl(store, JobTTL(), now=NOW, dry_run=True)
    assert dry.deleted == [] and dry.archived == []
    real = apply_job_ttl(store, JobTTL(), now=NOW)
    assert real.deleted == [] and real.archived == []
    job = store.job(jid)
    assert job["archived_at"] is None and job["expires_at"] is None
    assert job["last_seen_at"] == OLD.isoformat()
    with QueryService(store.db_path) as query:
        assert query.jobs()["total"] == 1
        entry = query.job(jid)["ingestion"]["entries"][0]
        assert entry["last_seen_at"] == OLD.isoformat()
        assert [c["event"] for c in entry["changes"]] == ["created"]


def test_missing_repository_row_stays_pending_instead_of_becoming_confirmed_closed(store, monkeypatch):
    import auto_app.ingest.service as module

    monkeypatch.setattr(module, "utcnow", lambda: OLD)
    service = IngestService(store)
    service.ingest("repo", ParsedFeed([IngestRecord("row", URL, title="Backend Engineer")]),
                   kind="repository", origin="feed.json", complete_snapshot=True)
    jid = store.jobs()[0]["job_id"]
    monkeypatch.setattr(module, "utcnow", lambda: NOW)
    service.ingest("repo", ParsedFeed(), kind="repository", origin="feed.json", complete_snapshot=True)
    report = apply_job_ttl(store, JobTTL(), now=NOW)
    assert report.deleted == [] and report.archived == []
    assert store.job(jid)["expires_at"] is None
    with QueryService(store.db_path) as query:
        assert query.job(jid)["ingestion"]["entries"][0]["status"] == "missing"


def test_manual_decision_allows_pending_import_to_archive_with_history(store):
    jid = old_email(store)
    store.mark_job(jid, "not_applied")
    report = apply_job_ttl(store, JobTTL(), now=NOW)
    assert report.deleted == [] and [j.job_id for j in report.archived] == [jid]
    with QueryService(store.db_path) as query:
        detail = query.job(jid)
        assert detail["state"] == "archived" and detail["decision"] == "not_applied"
        assert detail["ingestion"]["entries"][0]["payload"]["metadata"]["observed_at"] == OLD.isoformat()
        assert detail["ingestion"]["entries"][0]["changes"][0]["event"] == "created"


def test_complete_unjudged_import_archives_instead_of_disappearing(store):
    jid = old_email(store, complete=True)
    report = apply_job_ttl(store, JobTTL(), now=NOW)
    assert report.deleted == []
    assert [job.job_id for job in report.archived] == [jid]
    with QueryService(store.db_path) as query:
        archived = query.jobs(archived=True)["items"]
        assert [job["job_id"] for job in archived] == [jid]
        archived_job = query.job(jid)
        # The description survives on the posting; the import record keeps its fingerprint.
        assert archived_job["parsed"]["description_text"] == "Build distributed systems."
        assert archived_job["ingestion"]["entries"][0]["payload"]["description_chars"] == 26


def test_complete_linked_source_removes_pending_hold_but_preserves_import_history(store):
    jid = old_email(store)
    add(store, "nvidia", "R42", title="Backend Engineer", url=URL)
    run(store)
    with store.engine.begin() as conn:
        conn.execute(update(T.postings).values(last_seen_at=OLD))
    assert len(store.jobs()) == 1
    report = apply_job_ttl(store, JobTTL(), now=NOW)
    assert [job.job_id for job in report.archived] == [jid] and report.deleted == []


def test_archiving_preserves_country_exclusion_from_nonprimary_raw_location(store):
    config = store.db_path.parent / "config"
    config.mkdir()
    (config / "preferences.json").write_text('{"exclude_countries": ["Canada"]}')
    service = IngestService(store)
    for source, location, description in (
        ("us-source", "Remote", "Build distributed systems. " * 30),  # a US place would keep the job
        ("ca-source", "Remote in Canada", ""),
    ):
        record = IngestRecord(
            external_id="one", url=URL, title="Backend Engineer", company="NVIDIA",
            locations=[location], description=description,
            metadata={"observed_at": OLD.isoformat()},
        )
        service.ingest(source, ParsedFeed([record]), kind="email", origin="old.eml")
    [job] = store.jobs()
    jid = job["job_id"]
    store.mark_job(jid, "not_applied")
    with QueryService(store.db_path) as query:
        assert query.jobs()["total"] == 0
        assert query.job(jid)["screening"]["matched_locations"] == ["Remote in Canada"]

    report = apply_job_ttl(store, JobTTL(), now=NOW)
    assert [job.job_id for job in report.archived] == [jid]
    assert store.get_parsed(PostingRef("ca-source", f"one#archived{jid}")) is None
    with QueryService(store.db_path) as query:
        assert query.jobs(archived=True)["total"] == 0
        audit = query.jobs(archived=True, include_excluded=True)
        assert audit["total"] == 1
        assert audit["items"][0]["screening"]["matched_locations"] == ["Remote in Canada"]
        detail = query.job(jid)
        assert detail["screening"]["reasons"] == ["excluded_country:Canada"]
        assert set(detail["archive"]["raw_locations"]) == {"Remote", "Remote in Canada"}


def test_nonimported_unjudged_job_still_expires_by_deletion(store):
    add(store, "nvidia", "R999", url=wd("R999"))
    run(store)
    jid = store.jobs()[0]["job_id"]
    with store.engine.begin() as conn:
        conn.execute(update(T.postings).values(last_seen_at=OLD))
    report = apply_job_ttl(store, JobTTL(), now=NOW)
    assert [job.job_id for job in report.deleted] == [jid] and report.archived == []
    assert store.job(jid) is None


@pytest.mark.parametrize("reparse", [False, True])
@pytest.mark.parametrize("source", ["mail", "nvidia"])
def test_general_parse_keeps_imported_records_without_plugin_lookup_or_reset(store, caplog, source, reparse):
    old_email(store, source=source)
    ref = PostingRef(source, "message-1")
    before = store.get_posting(ref), store.get_parsed(ref), store.get_normalized(ref)
    with caplog.at_level(logging.WARNING):
        stats = parse_postings(store, AppConfig(), test_registry(), reparse=reparse)
    assert stats[source].skipped == 1
    assert stats[source].parsed == stats[source].failed == 0
    assert stats[source].filled["title"] == 0  # a display placeholder is not an observed title
    assert stats[source].filled["apply_url"] == 1
    assert "no plugin" not in caplog.text
    assert (store.get_posting(ref), store.get_parsed(ref), store.get_normalized(ref)) == before

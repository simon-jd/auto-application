"""Import evidence is visible through the lightweight, read-only query interface."""

import json
import sqlite3

import pytest

from auto_app.core.db.store import DbStore
from auto_app.query.service import QueryError, QueryService
from auto_app.settings import Paths
from tests.query.test_service import STAMP, add_job


@pytest.fixture
def path(tmp_path):
    db_path = tmp_path / "query-imports.db"
    store = DbStore(db_path, Paths(home=tmp_path))
    store.engine.dispose()
    with sqlite3.connect(db_path) as conn:
        add_job(conn, 1, "[Title pending]", "Acme", "email")
        add_job(conn, 2, "Backend Engineer", "Acme", "repo")
        add_job(conn, 3, "Legacy Role", "Elsewhere", "linkedin")
        add_job(conn, 4, "Old Role", "OldCo", "old-repo", archived_at=STAMP)
        conn.execute("""INSERT INTO postings (posting_id, job_id, source, source_job_id, kind, state, purged,
                        first_seen_at, last_seen_at, title, company)
                        VALUES (20, 2, 'email', 'second-mail', 'api', 'linked', 0, ?, ?, '[Title pending]', 'Acme')""",
                     (STAMP, STAMP))
        records = [
            (1, "email", "first-mail", "email", "first.eml", "unknown", True, False),
            (2, "repo", "row-1", "repository", "https://example.com/list.json", "active", False, True),
            (20, "email", "second-mail", "email", "second.eml", "unknown", True, False),
            (4, "old-repo", "old-row", "repository", "old.json", "missing", True, False),
            (None, "repo", "inactive-row", "repository", "https://example.com/list.json", "inactive", True, False),
        ]
        for pid, source, external, kind, origin, status, pending, reassess in records:
            metadata = {"enrichment_status": "pending" if pending else "complete", "title_observed": not pending,
                        "source_kind": kind, "external_id": external, "origin": origin, "revision": "r2"}
            if pid:
                conn.execute("UPDATE postings SET parsed=? WHERE posting_id=?", (json.dumps({
                    "title": "[Title pending]" if pending else "Backend Engineer",
                    "description_text": "" if pending else "Build distributed systems.",
                    "extras": {"ingestion": metadata, "unrelated": "NOT EXPOSED"},
                }), pid))
            payload = {"external_id": external, "title": None if pending else "Backend Engineer",
                       "metadata": {"upstream": {"terms": ["Summer 2027"]}}, "description": "Stored source evidence"}
            conn.execute("""INSERT INTO ingest_entries (source,external_id,posting_id,kind,origin,revision,
                            content_hash,payload,status,first_seen_at,last_seen_at,last_imported_at,needs_reassessment)
                            VALUES (?,?,?,?,?,'r2','hash-2',?,?,?,?,?,?)""",
                         (source, external, pid, kind, origin, json.dumps(payload), status, STAMP, STAMP, STAMP, reassess))
            conn.execute("""INSERT INTO ingest_changes (source,external_id,posting_id,event,revision,content_hash,
                            payload,observed_at,created_at) VALUES (?,?,?,'created','r1','hash-1',?,?,?)""",
                         (source, external, pid, json.dumps({"title": "Original source title"}), STAMP, STAMP))
            if reassess:
                conn.execute("""INSERT INTO ingest_changes (source,external_id,posting_id,event,revision,content_hash,
                                payload,observed_at,created_at) VALUES (?,?,?,'updated','r2','hash-2',?,?,?)""",
                             (source, external, pid, json.dumps(payload), STAMP, STAMP))
    return db_path


def test_context_counts_import_status_and_only_active_pending_jobs(path):
    with QueryService(path) as query:
        context = query.context()
    assert context["capabilities"].get("ingestion") is True
    assert context["ingestion"] == {
        "entries": 5, "by_status": {"active": 1, "unknown": 2, "missing": 1, "inactive": 1},
        "pending_jobs": 1, "needs_reassessment_jobs": 1,
    }
    assert context["counts"]["total"] == 4
    assert context["counts"]["active"] == 3
    assert context["counts"]["postings"] == 5


def test_list_adds_compact_page_scoped_provenance_without_payloads(path):
    with QueryService(path) as query:
        page = query.jobs(source="repo", limit=1)
    [job] = page["items"]
    ingestion = job.get("ingestion")
    assert ingestion is not None, "job queries must expose source evidence state"
    assert ingestion["pending_enrichment"] is False  # complete linked evidence satisfies the email lead
    assert ingestion["needs_reassessment"] is True
    assert [entry["source"] for entry in ingestion["entries"]] == ["email", "repo"]
    repository = ingestion["entries"][1]
    assert repository["kind"] == "repository"
    assert repository["origin"] == "https://example.com/list.json"
    assert repository["status"] == "active" and repository["revision"] == "r2"
    assert repository["last_seen_at"] == "2026-10-09T10:00:00+00:00"
    assert "Stored source evidence" not in json.dumps(page)
    assert "NOT EXPOSED" not in json.dumps(page)
    assert all("payload" not in entry and "changes" not in entry for entry in ingestion["entries"])


def test_detail_returns_saved_record_metadata_and_each_entries_history(path):
    with QueryService(path) as query:
        detail = query.job(2)
        pending = query.job(1)
    assert "ingestion" in detail
    repository = next(x for x in detail["ingestion"]["entries"] if x["source"] == "repo")
    assert repository["payload"]["metadata"]["upstream"]["terms"] == ["Summer 2027"]
    assert repository["parsed_ingestion"]["enrichment_status"] == "complete"
    assert [change["event"] for change in repository["changes"]] == ["created", "updated"]
    assert repository["changes"][0]["payload"]["title"] == "Original source title"
    assert repository["changes"][1]["content_hash"] == "hash-2"
    assert repository["changes"][0]["observed_at"] == "2026-10-09T10:00:00+00:00"
    assert pending["ingestion"]["pending_enrichment"] is True
    assert pending["ingestion"]["entries"][0]["parsed_ingestion"]["title_observed"] is False
    assert pending["ingestion"]["entries"][0]["payload"]["title"] is None
    assert "NOT EXPOSED" not in json.dumps(detail)


@pytest.mark.parametrize("tables", [("ingest_changes", "ingest_entries"), ("ingest_changes",)])
def test_previous_schema_without_both_tables_is_queryable_without_migration(path, tables):
    with sqlite3.connect(path) as conn:
        for table in tables:
            conn.execute(f"DROP TABLE {table}")
        before = list(conn.iterdump())
    with QueryService(path) as query:
        context = query.context()
        assert context["capabilities"].get("ingestion") is False
        assert "ingestion" not in context
        assert query.jobs()["total"] == 3
        assert "ingestion" not in query.job(2)
    with sqlite3.connect(path) as conn:
        assert list(conn.iterdump()) == before


def test_import_queries_are_read_only_and_do_not_read_off_page_payloads(path):
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE ingest_entries SET payload='malformed' WHERE posting_id=1")
        before = list(conn.iterdump())
    with QueryService(path) as query:
        query.context()
        query.jobs()
        assert "ingestion" in query.job(2)
        with pytest.raises(QueryError, match="JSON"):
            query.job(1)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            query._conn.execute("DELETE FROM ingest_changes")
    with sqlite3.connect(path) as conn:
        assert list(conn.iterdump()) == before

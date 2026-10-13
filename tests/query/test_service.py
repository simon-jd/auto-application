"""Read-only queries against the real, migrated schema in disposable databases."""

import json
import sqlite3

import pytest


STAMP = "2026-10-09 10:00:00.000000"


def queries(path):
    from auto_app.query.service import QueryService

    return QueryService(path)


def add_job(conn, job_id, title, company, source, **values):
    fields = {"job_id": job_id, "company_norm": company.lower(), "title_norm": title.lower(),
              "first_seen_at": STAMP, "last_seen_at": STAMP, **values}
    conn.execute(f"INSERT INTO jobs ({', '.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                 list(fields.values()))
    if source:
        conn.execute("""INSERT INTO postings (posting_id, job_id, source, source_job_id, kind, state, purged,
                        first_seen_at, last_seen_at, title, company, parsed)
                        VALUES (?, ?, ?, ?, 'api', 'linked', 0, ?, ?, ?, ?, ?)""",
                     (job_id, job_id, source, f"ref-{job_id}", STAMP, STAMP, title, company,
                      json.dumps({"title": title, "company": company, "description_text": f"JD for {title}"})))
        conn.execute("UPDATE jobs SET primary_posting_id=? WHERE job_id=?", (job_id, job_id))


@pytest.fixture
def db_path(tmp_path):
    from auto_app.core.db.store import DbStore
    from auto_app.settings import Paths

    path = tmp_path / "query database.db"
    store = DbStore(path, Paths(home=tmp_path))
    store.engine.dispose()
    with sqlite3.connect(path) as conn:
        add_job(conn, 1, "Backend Engineer", "Acme", "linkedin")
        add_job(conn, 2, "Platform Engineer", "Acme", "ashby", step2="recommended")
        add_job(conn, 3, "Senior Backend Engineer", "Beta", "linkedin", step2="un_recommended")
        add_job(conn, 4, "ML Infrastructure", "Acme", "greenhouse", step2="recommended",
                decision="want_to_apply", decided_at=STAMP, expires_at="2020-01-01 00:00:00.000000")
        add_job(conn, 5, "Orphan Engineer", "Solo", None)
        archive = {"title": "Archived Engineer", "company": "OldCo", "locations": ["boston, ma"],
                   "posted_at": "2026-09-01T00:00:00+00:00", "apply_url": "https://example.com/old",
                   "postings": [{"source": "nvidia", "source_job_id": "old-6", "title": "Archived Engineer"}]}
        add_job(conn, 6, "Archived Engineer", "OldCo", None, archived_at=STAMP, step2="recommended",
                archive=json.dumps(archive))
        conn.executemany("INSERT INTO job_locations (job_id,location_norm) VALUES (?,?)",
                         [(4, "seattle, wa"), (4, "boston, ma")])
        conn.execute("INSERT INTO source_health (source,status,data) VALUES ('linkedin','blocked',?)",
                     (json.dumps({"last_ok_at": "2026-10-08T10:00:00+00:00", "last_block_reason": "login"}),))
        conn.execute("INSERT INTO batches (batch_id,started_at,finished_at,interrupted,data) VALUES (?, ?, ?, 0, ?)",
                     ("b1", STAMP, STAMP, '{}'))
        conn.execute("""INSERT INTO judgments (job_id,filter_version,filter,status,recommend,fit,dealbreaker,
                        reason,answer,trace,created_at) VALUES (4,'v1','backend','ok',1,1,0,'Backend fit',
                        'RAW ANSWER',X'736563726574',?)""", (STAMP,))
        conn.execute("""INSERT INTO reviews (job_id,decision,reason,filters,job_text,created_at)
                        VALUES (4,'want_to_apply','Interesting team',?,'Training copy',?)""",
                     (json.dumps([{"filter": "backend", "recommend": True}]), STAMP))
        conn.execute("""INSERT INTO feedback (job_id,filter,filter_version,verdict,correct,label,origin,
                        reason,job_text,created_at) VALUES (4,'backend','v1',1,1,1,'explicit',
                        'Relevant work','Training copy',?)""", (STAMP,))
    yield path


def test_missing_database_does_not_create_a_file_or_parent(tmp_path):
    from auto_app.query.service import QueryError, QueryService

    path = tmp_path / "missing directory" / "missing.db"
    with pytest.raises(QueryError, match="database"):
        QueryService(path)
    assert not path.parent.exists()


def test_missing_schema_is_reported_without_migration(tmp_path):
    from auto_app.query.service import QueryError

    path = tmp_path / "empty.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE marker (value TEXT)")
    before = path.read_bytes()
    with pytest.raises(QueryError, match="schema"):
        queries(path)
    assert path.read_bytes() == before


def test_queries_never_change_business_data_and_connection_rejects_writes(db_path):
    with sqlite3.connect(db_path) as conn:
        before = list(conn.iterdump())
    with queries(db_path) as service:
        service.context()
        service.jobs()
        service.job(4)
        for sql in ("DELETE FROM jobs", "CREATE TABLE unexpected (id INTEGER)"):
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                service._conn.execute(sql)
    with sqlite3.connect(db_path) as conn:
        assert list(conn.iterdump()) == before


def test_context_reports_counts_source_health_and_unimplemented_capabilities(db_path):
    with queries(db_path) as service:
        result = service.context()
    assert result["schema_version"]
    assert result["database"] == str(db_path.resolve())
    assert result["counts"] == {
        "total": 6, "active": 5, "archived": 1, "postings": 4,
        "by_state": {"not_evaluated": 2, "recommended": 1, "un_recommended": 1,
                     "want_to_apply": 1, "applied": 0, "not_applied": 0, "archived": 1},
    }
    linkedin = next(x for x in result["sources"] if x["source"] == "linkedin")
    assert linkedin["posting_count"] == 2
    assert linkedin["status"] == "blocked"
    assert linkedin["last_ok_at"] == "2026-10-08T10:00:00+00:00"
    assert linkedin["last_block_reason"] == "login"
    assert result["latest_batch"]["batch_id"] == "b1"
    assert result["latest_batch"]["interrupted"] is False
    assert result["capabilities"]["read_only"] is True
    for key in ("opportunity_qualification", "evidence", "tasks", "resume_providers"):
        assert result["capabilities"][key] is False
    json.dumps(result)


def test_pagination_visits_all_original_ids_and_ignores_new_insertions(db_path):
    with queries(db_path) as service:
        page = service.jobs(limit=2)
        assert [x["job_id"] for x in page["items"]] == [5, 4]
        assert page["total"] == 5 and page["returned"] == 2 and page["snapshot_max_id"] == 6
        ids = [x["job_id"] for x in page["items"]]
        with sqlite3.connect(db_path) as writer:
            add_job(writer, 7, "New Engineer", "Acme", "ashby")
        while page["next_cursor"]:
            page = service.jobs(limit=2, cursor=page["next_cursor"])
            ids.extend(x["job_id"] for x in page["items"])
            assert page["total"] == 5 and page["snapshot_max_id"] == 6
        assert ids == [5, 4, 3, 2, 1]
        assert page["returned"] == 1
        assert service.jobs()["total"] == 6


@pytest.mark.parametrize(("filters", "ids"), [
    ({"query": "BACKEND"}, [3, 1]),
    ({"query": "%"}, []),
    ({"company": "ACM", "source": "ashby", "state": "recommended"}, [2]),
    ({"state": "want_to_apply"}, [4]),
    ({"state": "recommended"}, [2]),
    ({"source": "linkedin"}, [3, 1]),
    ({"archived": True, "company": "Old", "source": "nvidia", "query": "Archived"}, [6]),
    ({"archived": None}, [6, 5, 4, 3, 2, 1]),
])
def test_filters_are_applied_before_pagination(db_path, filters, ids):
    with queries(db_path) as service:
        page = service.jobs(**filters, limit=1)
        assert page["total"] == len(ids)
        found = [x["job_id"] for x in page["items"]]
        while page["next_cursor"]:
            page = service.jobs(**filters, limit=1, cursor=page["next_cursor"])
            found.extend(x["job_id"] for x in page["items"])
    assert found == ids


@pytest.mark.parametrize("changed", [
    {"query": "engineer"}, {"state": "recommended"}, {"company": "Acme"},
    {"source": "ashby"}, {"archived": True},
])
def test_cursor_cannot_be_reused_with_different_filters(db_path, changed):
    from auto_app.query.service import QueryError

    with queries(db_path) as service:
        cursor = service.jobs(limit=1)["next_cursor"]
        with pytest.raises(QueryError, match="cursor"):
            service.jobs(cursor=cursor, **changed)


@pytest.mark.parametrize("arguments", [
    {"limit": 0}, {"limit": 201}, {"limit": True}, {"state": "possible"},
    {"cursor": "not-a-cursor"}, {"cursor": "e30="}, {"archived": "false"},
])
def test_invalid_query_parameters_are_actionable_errors(db_path, arguments):
    from auto_app.query.service import QueryError

    with queries(db_path) as service, pytest.raises(QueryError):
        service.jobs(**arguments)


def test_detail_contains_parsed_jd_postings_verdict_reasons_and_user_feedback(db_path):
    with queries(db_path) as service:
        detail = service.job(4)
        assert service.job(9999) is None
    assert detail["state"] == "want_to_apply"
    assert detail["parsed"]["description_text"] == "JD for ML Infrastructure"
    assert detail["locations"] == ["boston, ma", "seattle, wa"]
    assert detail["sources"] == ["greenhouse"]
    assert detail["postings"] == 1
    assert detail["posting_details"][0]["source_job_id"] == "ref-4"
    assert detail["judgments"][0]["reason"] == "Backend fit"
    assert detail["judgments"][0]["recommend"] is True
    assert detail["judgments"][0]["dealbreaker"] is False
    assert detail["reviews"][0]["reason"] == "Interesting team"
    assert detail["reviews"][0]["filters"] == [{"filter": "backend", "recommend": True}]
    assert detail["feedback"][0]["label"] is True
    assert detail["first_seen_at"] == "2026-10-09T10:00:00+00:00"
    assert detail["judgments"][0]["created_at"] == "2026-10-09T10:00:00+00:00"
    assert not {"trace", "answer", "accounts"} & detail["judgments"][0].keys()
    assert "RAW ANSWER" not in json.dumps(detail)


def test_archive_fallback_and_known_job_without_primary_posting_are_preserved(db_path):
    with queries(db_path) as service:
        archived = service.job(6)
        orphan = service.job(5)
    assert archived["state"] == "archived"
    assert archived["title"] == "Archived Engineer" and archived["company"] == "OldCo"
    assert archived["locations"] == ["boston, ma"] and archived["sources"] == ["nvidia"]
    assert archived["canonical_url"] == "https://example.com/old"
    assert archived["posting_details"][0]["source_job_id"] == "old-6"
    assert archived["postings"] == 1 and archived["parsed"] is None
    assert orphan["job_id"] == 5 and orphan["title"] == "orphan engineer"
    assert orphan["company"] == "solo" and orphan["parsed"] is None


def test_unchosen_large_or_malformed_json_does_not_break_page_or_point_lookup(db_path):
    # List projection must run after the SQL page/filter; detail must not scan unrelated jobs.
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE postings SET parsed='not json' WHERE posting_id=1")
        conn.execute("UPDATE jobs SET warnings='not json' WHERE job_id=1")
    with queries(db_path) as service:
        assert service.jobs(limit=1)["items"][0]["job_id"] == 5
        assert service.jobs(company="Solo")["total"] == 1
        assert service.job(4)["job_id"] == 4


def test_invalid_stored_json_is_reported_without_changing_it(db_path):
    from auto_app.query.service import QueryError

    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE postings SET parsed='not json' WHERE posting_id=4")
    with queries(db_path) as service, pytest.raises(QueryError, match="JSON"):
        service.job(4)

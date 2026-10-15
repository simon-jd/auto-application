# tests/screening_db.py
"""A migrated database with three jobs and two screening verdicts, for query and MCP tests."""

import json
import sqlite3

import pytest

STAMP = "2026-10-09 10:00:00.000000"


@pytest.fixture
def screening_db(tmp_path):
    from auto_app.core.db.store import DbStore
    from auto_app.settings import Paths

    path = tmp_path / "data" / "auto_app.db"
    DbStore(path, Paths(home=tmp_path)).engine.dispose()
    with sqlite3.connect(path) as conn:
        for job_id, title in ((1, "Software Engineer, New Grad"), (2, "Account Executive"), (3, "Data Analyst")):
            conn.execute("""INSERT INTO jobs (job_id, company_norm, title_norm, first_seen_at, last_seen_at)
                            VALUES (?, 'acme', ?, ?, ?)""", (job_id, title.lower(), STAMP, STAMP))
            conn.execute("""INSERT INTO postings (posting_id, job_id, source, source_job_id, kind, state, purged,
                            first_seen_at, last_seen_at, title, company, parsed)
                            VALUES (?, ?, 'simplify-newgrad', ?, 'api', 'linked', 0, ?, ?, ?, 'Acme', ?)""",
                         (job_id, job_id, str(job_id), STAMP, STAMP, title, json.dumps({"title": title})))
            conn.execute("UPDATE jobs SET primary_posting_id=? WHERE job_id=?", (job_id, job_id))
        for job_id, outcome, reasons, sponsorship in ((1, "kept", [], "offers"),
                                                       (2, "excluded", ["not_cs_role"], "unknown")):
            conn.execute("""INSERT INTO job_screenings (job_id, rules_version, facts_hash, outcome, reasons, signals,
                            screened_at) VALUES (?, 'v1', 'h', ?, ?, ?, ?)""",
                         (job_id, outcome, json.dumps(reasons), json.dumps({"sponsorship": sponsorship}), STAMP))
    return path

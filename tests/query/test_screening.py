# tests/query/test_screening.py
"""Rule-excluded jobs are hidden by default, explained on audit, and filterable by sponsorship."""

import json
import sqlite3

import pytest

from tests.screening_db import screening_db  # noqa: F401 — the shared fixture


def service(path):
    from auto_app.query.service import QueryService

    return QueryService(path)


def ids(page):
    return [item["job_id"] for item in page["items"]]


def test_rule_excluded_jobs_are_hidden_by_default_and_explained_on_audit(screening_db):  # noqa: F811
    with service(screening_db) as query:
        assert ids(query.jobs()) == [3, 1]
        assert ids(query.jobs(include_excluded=True)) == [3, 2, 1]
        excluded = query.job(2)["screening"]
        assert excluded["excluded"] is True and excluded["reasons"] == ["not_cs_role"]
        assert excluded["rules_version"] == "v1" and excluded["signals"] == {"sponsorship": "unknown"}
        assert query.job(3)["screening"] == {"excluded": False, "reasons": [], "matched_locations": []}


def test_sponsorship_filter_and_context_counts(screening_db):  # noqa: F811
    with service(screening_db) as query:
        assert ids(query.jobs(sponsorship="offers")) == [1]
        with pytest.raises(ValueError, match="sponsorship must be one of"):
            query.jobs(sponsorship="maybe")
        context = query.context()
    assert context["capabilities"]["rules_screening"] is True
    assert context["screening"] == {"kept": 1, "excluded": 1, "unscreened": 1,
                                    "excluded_by_reason": {"not_cs_role": 1}, "kept_by_sponsorship": {"offers": 1}}


def test_databases_without_the_screening_table_still_work(screening_db):  # noqa: F811
    with sqlite3.connect(screening_db) as conn:
        conn.execute("DROP TABLE job_screenings")
    with service(screening_db) as query:
        assert ids(query.jobs()) == [3, 2, 1]
        assert query.context()["capabilities"]["rules_screening"] is False
        with pytest.raises(ValueError, match="screening"):
            query.jobs(sponsorship="offers")


def test_query_cli_passes_the_sponsorship_filter(screening_db, capsys):  # noqa: F811
    from auto_app.query.cli import main

    assert main(["--db", str(screening_db), "jobs", "--sponsorship", "offers"]) == 0
    assert ids(json.loads(capsys.readouterr().out)) == [1]


def test_http_adapter_filters_sponsorship(screening_db):
    from fastapi.testclient import TestClient
    from auto_app.query.api import create_app
    with TestClient(create_app(screening_db), base_url="http://127.0.0.1:8766") as client:
        response = client.get("/api/jobs", params={"sponsorship": "offers"})
        assert response.status_code == 200 and ids(response.json()) == [1]
        assert client.get("/api/jobs", params={"sponsorship": "invalid"}).status_code == 422


def test_cursor_is_bound_to_screening_filters(screening_db):
    with service(screening_db) as query:
        page = query.jobs(limit=1, include_excluded=True)
        with pytest.raises(ValueError, match="cursor"):
            query.jobs(limit=1, cursor=page["next_cursor"], include_excluded=True, sponsorship="offers")
        with pytest.raises(ValueError, match="cursor"):
            query.jobs(limit=1, cursor=page["next_cursor"], include_excluded=False)


def test_stdlib_query_does_not_change_database(screening_db):
    import subprocess
    import sys
    from pathlib import Path
    before = screening_db.read_bytes()
    script = Path(__file__).resolve().parents[2] / "scripts" / "job_view.py"
    result = subprocess.run([sys.executable, "-S", str(script), "--db", str(screening_db),
                              "jobs", "--sponsorship", "offers"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert ids(json.loads(result.stdout)) == [1]
    assert screening_db.read_bytes() == before


@pytest.mark.parametrize("decision", ["want_to_apply", "applied", "not_applied"])
def test_rule_exclusions_never_hide_jobs_you_decided_on(screening_db, decision):
    with sqlite3.connect(screening_db) as conn:
        conn.execute("UPDATE jobs SET decision=? WHERE job_id=2", (decision,))
    with service(screening_db) as query:
        assert ids(query.jobs()) == [3, 2, 1]
        assert ids(query.jobs(state=decision)) == [2]

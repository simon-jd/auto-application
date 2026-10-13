import json
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import insert

from auto_app.core.db import tables as T
from auto_app.core.db.store import DbStore
from auto_app.settings import Paths


ROOT = Path(__file__).resolve().parents[2]


def database(tmp_path):
    path = tmp_path / "data" / "auto_app.db"
    store = DbStore(path, Paths(home=tmp_path))
    with store.engine.begin() as conn:
        conn.execute(insert(T.jobs), [
            {"job_id": 1, "company_norm": "acme", "title_norm": "backend engineer", "step2": "recommended"},
            {"job_id": 2, "company_norm": "acme", "title_norm": "data engineer", "step2": None},
        ])
    store.engine.dispose()
    return path


def invoke(tmp_path, *args, script=False):
    entry = [str(ROOT / "scripts" / "job_view.py")] if script else ["-m", "auto_app.query.cli"]
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "AUTO_APP_HOME": str(tmp_path)}
    return subprocess.run([sys.executable, *entry, *args], cwd=tmp_path, env=env,
                          text=True, capture_output=True, timeout=15)


def test_script_returns_context_and_respects_home(tmp_path):
    database(tmp_path)
    result = invoke(tmp_path, "context", "--json", script=True)
    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)
    assert "counts" in context and "sources" in context
    assert result.stderr == ""


def test_cli_paginates_and_gets_detail(tmp_path):
    path = database(tmp_path)
    first = invoke(tmp_path, "--db", str(path), "jobs", "--limit", "1")
    assert first.returncode == 0, first.stderr
    page = json.loads(first.stdout)
    assert [row["job_id"] for row in page["items"]] == [2]
    assert page["total"] == 2
    second = invoke(tmp_path, "--db", str(path), "jobs", "--limit", "1", "--cursor", page["next_cursor"])
    assert second.returncode == 0, second.stderr
    assert [row["job_id"] for row in json.loads(second.stdout)["items"]] == [1]
    detail = invoke(tmp_path, "--db", str(path), "job", "1")
    assert detail.returncode == 0, detail.stderr
    assert json.loads(detail.stdout)["job_id"] == 1


def test_missing_database_is_actionable_and_does_not_create_files(tmp_path):
    missing = tmp_path / "missing" / "db.sqlite"
    result = invoke(tmp_path, "--db", str(missing), "context")
    assert result.returncode != 0
    assert result.stdout == ""
    assert "database" in result.stderr.lower()
    assert not missing.parent.exists()


def test_missing_job_is_not_a_successful_empty_response(tmp_path):
    path = database(tmp_path)
    result = invoke(tmp_path, "--db", str(path), "job", "99")
    assert result.returncode != 0
    assert result.stdout == ""
    assert "99" in result.stderr and "not found" in result.stderr.lower()


def test_query_import_is_light_and_does_not_load_env_file(tmp_path):
    # A fresh interpreter catches accidental imports through auto_app.cli/DbStore.
    script = """
import sys
import auto_app.query.cli
assert not any(m in sys.modules for m in ('playwright', 'textual', 'anthropic', 'auto_app.core.config', 'auto_app.cli'))
"""
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr

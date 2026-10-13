"""Exercise the real MCP protocol against disposable databases, never live data."""

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import time

from mcp import Client, ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
import pytest


ROOT = Path(__file__).resolve().parents[2]
STAMP = "2026-10-09 10:00:00.000000"


@pytest.fixture
def mcp_db(tmp_path):
    from auto_app.core.db.store import DbStore
    from auto_app.settings import Paths

    path = tmp_path / "data" / "mcp.db"
    store = DbStore(path, Paths(home=tmp_path))
    store.engine.dispose()
    with sqlite3.connect(path) as conn:
        for job_id, title, company, source in [
            (1, "Backend Engineer", "Acme", "ashby"),
            (2, "Data Engineer", "Beta", "greenhouse"),
        ]:
            conn.execute("""INSERT INTO jobs (job_id, company_norm, title_norm, step2,
                first_seen_at, last_seen_at) VALUES (?, ?, ?, 'recommended', ?, ?)""",
                         (job_id, company.lower(), title.lower(), STAMP, STAMP))
            conn.execute("""INSERT INTO postings (posting_id, job_id, source, source_job_id,
                kind, state, purged, first_seen_at, last_seen_at, title, company, parsed)
                VALUES (?, ?, ?, ?, 'api', 'linked', 0, ?, ?, ?, ?, ?)""",
                         (job_id, job_id, source, str(job_id), STAMP, STAMP, title, company,
                          json.dumps({"title": title, "company": company,
                                      "description_text": "Imported job description"})))
            conn.execute("UPDATE jobs SET primary_posting_id=? WHERE job_id=?", (job_id, job_id))
    return path


async def test_tool_schema_annotations_and_json_queries(mcp_db):
    from auto_app.agent.mcp_server import create_mcp_server

    async with Client(create_mcp_server(mcp_db)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert set(tools) == {"get_job_context", "search_jobs", "get_job", "record_application_status"}
        for name, tool in tools.items():
            assert tool.annotations.read_only_hint is (name != "record_application_status")
            assert tool.annotations.open_world_hint is False
            assert tool.annotations.destructive_hint is False
            assert tool.annotations.idempotent_hint is True
            assert tool.output_schema["type"] == "object"
        status_schema = tools["record_application_status"].input_schema
        assert status_schema["required"] == ["job_id", "status"]
        assert status_schema["properties"]["status"]["enum"] == ["want_to_apply", "applied", "not_applied"]

        context = await client.call_tool("get_job_context", {})
        assert not context.is_error
        assert context.structured_content["counts"]["total"] == 2
        page = await client.call_tool("search_jobs", {"query": "Backend", "company": "Acme",
                                                      "source": "ashby", "state": "recommended", "limit": 1})
        assert not page.is_error
        assert [job["job_id"] for job in page.structured_content["items"]] == [1]


def _environment():
    return {**os.environ, "PYTHONPATH": str(ROOT / "src")}


@asynccontextmanager
async def _session(transport, path, tmp_path):
    args = ["-m", "auto_app.agent.cli", "serve", "--db", str(path), "--transport", transport]
    if transport == "stdio":
        params = StdioServerParameters(command=sys.executable, args=args, env=_environment(), cwd=tmp_path)
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=10) as session:
                yield session
        return

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    log_path = tmp_path / "mcp-server.log"
    with log_path.open("w") as log:
        process = subprocess.Popen([sys.executable, *args, "--port", str(port)],
                                   cwd=tmp_path, env=_environment(), stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None, log_path.read_text()
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                except OSError:
                    assert time.monotonic() < deadline, log_path.read_text()
                    await asyncio.sleep(0.025)
                else:
                    writer.close()
                    await writer.wait_closed()
                    break
            async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=10) as session:
                    yield session
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                pytest.fail("MCP server did not shut down after SIGTERM")
        assert process.returncode in (0, -signal.SIGTERM), log_path.read_text()


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_real_client_initializes_reads_records_and_reports_errors(mcp_db, tmp_path, transport):
    async with _session(transport, mcp_db, tmp_path) as session:
        initialized = await session.initialize()
        assert initialized.server_info.name == "auto-app-agent"
        tools = await session.list_tools()
        assert len(tools.tools) == 4
        context = await session.call_tool("get_job_context", {})
        assert not context.is_error and context.structured_content["counts"]["total"] == 2
        result = await session.call_tool("get_job", {"job_id": 1})
        assert not result.is_error and result.structured_content["job_id"] == 1
        recorded = await session.call_tool("record_application_status",
                                          {"job_id": 1, "status": "applied", "reason": "Applied manually"})
        assert not recorded.is_error
        assert recorded.structured_content["status"] == "applied"
        assert recorded.structured_content["changed"] is True
        repeated = await session.call_tool("record_application_status",
                                          {"job_id": 1, "status": "applied", "reason": "Applied manually"})
        assert not repeated.is_error and repeated.structured_content["changed"] is False
        updated = await session.call_tool("get_job", {"job_id": 1})
        assert updated.structured_content["decision"] == "applied"
        page = await session.call_tool("search_jobs", {"state": "applied"})
        assert [job["job_id"] for job in page.structured_content["items"]] == [1]
        for name, arguments, expected in [
            ("get_job", {"job_id": 999}, "not found"),
            ("get_job", {}, "job_id"),
            ("record_application_status", {"job_id": 999, "status": "applied"}, "missing"),
            ("record_application_status", {"job_id": 1, "status": "submit"}, "status"),
        ]:
            failure = await session.call_tool(name, arguments)
            assert failure.is_error
            assert expected in " ".join(item.text for item in failure.content).lower()
    with sqlite3.connect(mcp_db) as conn:
        assert conn.execute("SELECT decision FROM jobs WHERE job_id=1").fetchone() == ("applied",)
        assert conn.execute("SELECT COUNT(*) FROM reviews WHERE job_id=1").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM applications").fetchone() == (0,)


def test_cli_rejects_public_bind_and_missing_database_without_creating_it(tmp_path):
    missing = tmp_path / "absent" / "db.sqlite"
    for extra, expected in [( ["--host", "0.0.0.0"], "loopback"), ([], "database")]:
        result = subprocess.run([sys.executable, "-m", "auto_app.agent.cli", "serve", "--db", str(missing),
                                 "--transport", "streamable-http", *extra],
                                cwd=tmp_path, env=_environment(), capture_output=True, text=True, timeout=10)
        assert result.returncode != 0
        assert result.stdout == ""
        assert expected in result.stderr.lower()
        assert not missing.parent.exists()


def test_import_does_not_load_secrets_or_application_runtime(tmp_path):
    (tmp_path / ".env").write_text("MCP_IMPORT_SENTINEL=must-not-load\n")
    script = """
import os
import sys
import auto_app.agent.cli
import auto_app.agent.mcp_server
assert 'MCP_IMPORT_SENTINEL' not in os.environ
assert not any(m in sys.modules for m in (
    'auto_app.cli', 'auto_app.settings', 'auto_app.core.config', 'auto_app.core.db.store',
    'playwright', 'anthropic', 'claude_agent_sdk', 'textual'))
"""
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=_environment(),
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr

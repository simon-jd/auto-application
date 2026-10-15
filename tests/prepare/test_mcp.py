"""Preparation tools are opt-in and never mutate application decisions."""

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
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
URL = "https://jobs.ashbyhq.com/example/83eec8f4-c37d-43c9-860b-27223eee3643/application"


@pytest.fixture
def preparation_db(tmp_path):
    from auto_app.core.db.store import DbStore
    from auto_app.settings import Paths

    path = tmp_path / "jobs.db"
    store = DbStore(path, Paths(home=tmp_path))
    store.engine.dispose()
    return path


async def test_opt_in_tools_enqueue_idempotently_and_return_persisted_result(preparation_db, tmp_path):
    from auto_app.agent.mcp_server import create_mcp_server

    root = tmp_path / "preparation"
    async with Client(create_mcp_server(preparation_db, preparation_dir=root)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert set(tools) == {"get_job_context", "search_jobs", "get_job", "record_application_status",
                              "record_application_event", "prepare_application", "get_application_run"}
        prepare = tools["prepare_application"]
        assert prepare.annotations.read_only_hint is False
        assert prepare.annotations.open_world_hint is True
        assert prepare.annotations.idempotent_hint is True
        assert prepare.annotations.destructive_hint is False
        assert tools["get_application_run"].annotations.read_only_hint is True
        assert tools["get_application_run"].annotations.open_world_hint is False
        assert set(prepare.input_schema["properties"]) == {"job_id", "application_url"}
        context = (await client.call_tool("get_job_context", {})).structured_content
        assert context["capabilities"]["preparation"]["automated_submission"] is False
        assert context["capabilities"]["preparation"]["worker"]["available"] is False
        result = await client.call_tool("prepare_application", {"application_url": URL})
        assert not result.is_error
        run = result.structured_content
        assert run["status"] == "queued"
        repeated = await client.call_tool("prepare_application", {"application_url": URL})
        assert repeated.structured_content["run_id"] == run["run_id"]
        override = await client.call_tool("prepare_application", {"application_url": URL, "candidate": {"name": "real"}})
        assert override.structured_content["run_id"] == run["run_id"]
        assert override.structured_content["candidate"]["candidate_id"] == "fictional-sales-v1"

    # A fresh server reads the ledger, not process memory.
    async with Client(create_mcp_server(preparation_db, preparation_dir=root)) as client:
        fetched = await client.call_tool("get_application_run", {"run_id": run["run_id"]})
        assert not fetched.is_error
        assert fetched.structured_content["run_id"] == run["run_id"]
        for name, args in [
            ("prepare_application", {}),
            ("prepare_application", {"job_id": 1, "application_url": URL}),
            ("prepare_application", {"job_id": 999}),
            ("prepare_application", {"application_url": "http://127.0.0.1/"}),
            ("get_application_run", {"run_id": "../../outside"}),
        ]:
            assert (await client.call_tool(name, args)).is_error
    with sqlite3.connect(preparation_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0


@asynccontextmanager
async def _session(transport, db, root):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    args = ["-m", "auto_app.agent.cli", "serve", "--db", str(db),
            "--preparation-dir", str(root), "--transport", transport]
    if transport == "stdio":
        params = StdioServerParameters(command=sys.executable, args=args, env=env, cwd=root.parent)
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer, read_timeout_seconds=10) as session:
                await session.initialize()
                yield session
        return
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with (root.parent / "preparation-mcp.log").open("w") as log:
        process = subprocess.Popen([sys.executable, *args, "--port", str(port)],
                                   env=env, cwd=root.parent, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", port)
                except OSError:
                    assert time.monotonic() < deadline
                    await asyncio.sleep(0.025)
                else:
                    writer.close()
                    await writer.wait_closed()
                    break
            async with streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (reader, writer):
                async with ClientSession(reader, writer, read_timeout_seconds=10) as session:
                    await session.initialize()
                    yield session
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.mark.parametrize("transport", ["stdio", "streamable-http"])
async def test_preparation_over_real_mcp_transport(preparation_db, tmp_path, transport):
    async with _session(transport, preparation_db, tmp_path / "runs") as session:
        assert {tool.name for tool in (await session.list_tools()).tools} == {
            "get_job_context", "search_jobs", "get_job", "record_application_status", "record_application_event",
            "prepare_application", "get_application_run",
        }
        queued = await session.call_tool("prepare_application", {"application_url": URL})
        assert not queued.is_error
        run = queued.structured_content
        assert run["status"] == "queued"
        fetched = await session.call_tool("get_application_run", {"run_id": run["run_id"]})
        assert fetched.structured_content["run_id"] == run["run_id"]

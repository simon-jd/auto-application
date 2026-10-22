"""Dot compatibility probes run against disposable MCP services, never production."""
import importlib.util
from pathlib import Path

from mcp import Client
import pytest

from auto_app.agent.mcp_server import create_mcp_server
from tests.agent.test_mcp import mcp_db


def checker():
    path = Path(__file__).resolve().parents[2] / "scripts/check_agent_mcp.py"
    spec = importlib.util.spec_from_file_location("check_agent_mcp", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("controls,handoff", [(False, False), (True, False), (True, True)])
async def test_context_describes_this_servers_tools(mcp_db, controls, handoff):
    async with Client(create_mcp_server(mcp_db, control_tools=controls, handoff_tools=handoff)) as client:
        result = (await client.call_tool("get_job_context", {})).structured_content
        interface = result["capabilities"]["agent_interface"]
        assert interface["control_tools"] is controls
        assert interface["handoff_tools"] is handoff
        assert interface["preparation_tools"] is False
        assert result["capabilities"]["automated_submission"] is False


@pytest.mark.parametrize("controls", [False, True])
async def test_probe_accepts_additive_tools_without_calling_them(mcp_db, controls):
    server = create_mcp_server(mcp_db, control_tools=controls)
    @server.tool()
    def future_write() -> dict:
        raise AssertionError("A compatibility probe must never call extra tools")

    before = mcp_db.read_bytes()
    async with Client(server) as client:
        report = await checker().inspect_session(client, "auto-app-agent")
    assert report["writes_performed"] is False
    assert report["search_total"] == 2
    assert "future_write" in report["tools"]
    assert mcp_db.read_bytes() == before


async def test_probe_rejects_missing_required_tool(mcp_db):
    server = create_mcp_server(mcp_db)
    server.remove_tool("get_job")
    async with Client(server) as client:
        with pytest.raises(RuntimeError, match="missing required tools.*get_job"):
            await checker().inspect_session(client, "auto-app-agent")

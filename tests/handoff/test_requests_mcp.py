"""The change request and status steps over MCP, for ChatGPT Dots once handoff tools are on."""

from mcp import Client

from auto_app.agent.mcp_server import create_mcp_server
from tests.handoff.test_questions_mcp import wanted_job


async def test_request_and_status_over_mcp(tmp_path):
    db, job = wanted_job(tmp_path)
    async with Client(create_mcp_server(db, handoff_tools=True)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"request_material_change", "get_application_status"} <= names
        empty = await client.call_tool("request_material_change", {"job_id": job, "text": " ", "agent": "dot"})
        assert empty.is_error
        sent = await client.call_tool("request_material_change",
                                      {"job_id": job, "text": "more Iceberg", "agent": "dot"})
        assert sent.structured_content["status"] == "needs_answers"
        status = (await client.call_tool("get_application_status", {"job_id": job})).structured_content
        assert status["pending"]["changes"] == 1


async def test_without_handoff_tools_request_and_status_are_absent(tmp_path):
    db, _ = wanted_job(tmp_path)
    async with Client(create_mcp_server(db)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert not {"request_material_change", "get_application_status"} & names


async def test_cheatsheet_requests_over_mcp(tmp_path):
    db, _ = wanted_job(tmp_path)
    async with Client(create_mcp_server(db, handoff_tools=True)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"request_cheatsheet_change", "get_cheatsheet_requests"} <= names
        assert not {n for n in names if "cheatsheet_done" in n}  # the owner's step stays off MCP
        sent = await client.call_tool("request_cheatsheet_change", {"text": "可以搬家", "agent": "dot"})
        assert sent.structured_content["status"] == "pending"
        listed = (await client.call_tool("get_cheatsheet_requests", {})).structured_content
        assert listed["pending"][0]["agent"] == "dot"

# tests/agent/test_mcp_screening.py
"""search_jobs exposes the sponsorship signal to chat clients."""

from mcp import Client

from tests.screening_db import screening_db  # noqa: F401 — the shared fixture


async def test_search_jobs_filters_by_sponsorship(screening_db):  # noqa: F811
    from auto_app.agent.mcp_server import create_mcp_server

    async with Client(create_mcp_server(screening_db)) as client:
        page = await client.call_tool("search_jobs", {"sponsorship": "offers"})
        assert not page.is_error
        assert [job["job_id"] for job in page.structured_content["items"]] == [1]
        context = await client.call_tool("get_job_context", {})
        assert context.structured_content["screening"]["excluded"] == 1

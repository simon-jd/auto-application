#!/usr/bin/env python3
"""Read-only smoke check against a running MCP HTTP server; never records decisions."""

import argparse
import asyncio
import json
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def inspect_server(url: str) -> dict:
    async with streamable_http_client(url) as (reader, writer):
        async with ClientSession(reader, writer, read_timeout_seconds=30) as session:
            initialized = await session.initialize()
            return await inspect_session(session, initialized.server_info.name)


async def inspect_session(session, server_name: str) -> dict:
    """Probe only fixed read calls; tolerate optional and future tool groups."""
    tools = {tool.name: tool for tool in (await session.list_tools()).tools}
    expected = {"get_job_context", "search_jobs", "get_job", "record_application_status"}
    missing = expected - set(tools)
    if missing:
        raise RuntimeError(f"missing required tools: {sorted(missing)}")

    async def call(name: str, arguments: dict) -> dict:
        result = await session.call_tool(name, arguments)
        if result.is_error or result.structured_content is None:
            raise RuntimeError(f"{name} failed: {result.content}")
        return result.structured_content

    context = await call("get_job_context", {})
    page = await call("search_jobs", {"limit": 5})
    sample = None
    if page["items"]:
        job_id = page["items"][0]["job_id"]
        detail = await call("get_job", {"job_id": job_id})
        if detail["job_id"] != job_id:
            raise RuntimeError("detail did not match the requested ID")
        sample = {key: detail.get(key) for key in ("job_id", "title", "company", "decision")}
    return {
        "server": server_name,
        "tools": sorted(tools),
        "context": context,
        "sample": sample,
        "search_total": page["total"],
        "sample_job_ids": [item["job_id"] for item in page["items"]],
        "writes_performed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8767/mcp")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = asyncio.run(inspect_server(args.url))
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(encoded + "\n")
    print(encoded)


if __name__ == "__main__":
    main()

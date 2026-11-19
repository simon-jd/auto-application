"""The held list and the apply-kit reads over MCP, for an agent such as ChatGPT Dots once handoff tools are on.
Reading a package needs no claim token."""

import base64
import hashlib
import json

import pytest
from mcp import Client

from auto_app.agent.mcp_server import create_mcp_server
from tests.agent.test_mcp import _session
from tests.handoff.test_questions_mcp import wanted_job

RESUME = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3 fictional resume\x00\n%%EOF\n"
ANSWERS = "## Why us?\nA fictional answer. é\n"
NEW_TOOLS = {"get_held_applications", "get_application_file", "get_apply_guide"}


@pytest.fixture
def tmp_path(tmp_path):
    """The runtime is tmp_path/runtime, so its sibling apply-kit/ belongs to this test alone."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    return runtime


@pytest.fixture
def kit(tmp_path):
    db, job = wanted_job(tmp_path)
    root = tmp_path.parent / "apply-kit"
    package = root / "jobs" / str(job)
    package.mkdir(parents=True)
    (package / "job.json").write_text(json.dumps({"job_id": job, "title": "Software Engineer"}))
    (package / "resume.pdf").write_bytes(RESUME)
    (package / "answers.md").write_text(ANSWERS, encoding="utf-8")
    (package / "jd.txt").write_bytes(b"\xff not UTF-8\n")
    (root / "answers.md").write_text("# Cheat sheet (fictional)\n")
    (root / "README.md").write_text("# Apply kit (fictional)\n")
    return db, job


async def test_held_and_kit_round_trip_over_mcp(kit):
    db, job = kit
    async with Client(create_mcp_server(db, handoff_tools=True)) as client:
        assert NEW_TOOLS <= {t.name for t in (await client.list_tools()).tools}
        assert (await client.call_tool("get_held_applications", {})).structured_content == {"items": []}
        token = (await client.call_tool("claim_application", {"job_id": job, "agent": "dot"})).structured_content["token"]
        await client.call_tool("hold_application", {"token": token})
        held = (await client.call_tool("get_held_applications", {})).structured_content["items"]
        assert [(i["job_id"], i["agent"], i["title"]) for i in held] == [(job, "dot", "Software Engineer")]

        pdf = (await client.call_tool("get_application_file", {"job_id": job, "name": "resume.pdf"})).structured_content
        assert base64.b64decode(pdf["base64"]) == RESUME and "text" not in pdf
        assert (pdf["job_id"], pdf["name"], pdf["bytes"], pdf["sha256"]) == \
            (job, "resume.pdf", len(RESUME), hashlib.sha256(RESUME).hexdigest())
        text = (await client.call_tool("get_application_file", {"job_id": job, "name": "answers.md"})).structured_content
        assert text["text"] == ANSWERS and "base64" not in text and text["bytes"] == len(ANSWERS.encode())
        manifest = (await client.call_tool("get_application_file", {"job_id": job, "name": "job.json"})).structured_content
        assert json.loads(manifest["text"])["job_id"] == job

        guide = (await client.call_tool("get_apply_guide", {})).structured_content
        assert guide == {"name": "answers.md", "text": "# Cheat sheet (fictional)\n"}
        readme = (await client.call_tool("get_apply_guide", {"which": "readme"})).structured_content
        assert readme == {"name": "README.md", "text": "# Apply kit (fictional)\n"}


async def test_kit_errors_over_mcp(kit):
    db, job = kit
    async with Client(create_mcp_server(db, handoff_tools=True)) as client:
        missing = await client.call_tool("get_application_file", {"job_id": job, "name": "cover_letter.pdf"})
        assert missing.is_error and "not published yet" in missing.content[0].text
        for arguments in ({"job_id": job, "name": "../profile.yaml"}, {"job_id": 0, "name": "job.json"},
                          {"job_id": str(job), "name": "job.json"}):
            assert (await client.call_tool("get_application_file", arguments)).is_error
        broken = await client.call_tool("get_application_file", {"job_id": job, "name": "jd.txt"})
        assert broken.is_error and "UTF-8" in broken.content[0].text
        assert (await client.call_tool("get_apply_guide", {"which": "profile"})).is_error


async def test_without_handoff_tools_the_reads_are_absent(kit):
    db, _ = kit
    async with Client(create_mcp_server(db)) as client:
        assert not NEW_TOOLS & {t.name for t in (await client.list_tools()).tools}


async def test_loopback_http_handoff_through_the_cli(kit, tmp_path, monkeypatch, capsys):
    from auto_app.jobq.cli import main as jobq

    db, job = kit
    async with _session("streamable-http", db, tmp_path, serve_args=(
        "--handoff-tools", "--allow-unauthenticated-loopback-handoff",
    )) as session:
        await session.initialize()
        tools = {tool.name: tool for tool in (await session.list_tools()).tools}
        assert not any("answers_ready" in name or "answers-ready" in name for name in tools)
        assert tools["claim_application"].annotations.read_only_hint is False
        assert tools["submit_open_questions"].annotations.read_only_hint is False

        async def call(tool_name, **arguments):
            result = await session.call_tool(tool_name, arguments)
            assert not result.is_error, tool_name
            return result.structured_content

        queue = await call("get_application_queue")
        assert [item["job_id"] for item in queue["items"]] == [job]
        claim = await call("claim_application", job_id=job, agent="dot")
        token = claim["token"]
        packet = await call("get_application_packet", token=token)
        assert packet["job"]["job_id"] == job
        pdf = await call("get_application_file", job_id=job, name="resume.pdf")
        assert base64.b64decode(pdf["base64"]) == RESUME
        assert pdf["sha256"] == hashlib.sha256(RESUME).hexdigest()
        assert (await call("hold_application", token=token))["status"] == "ready"
        held = await call("get_held_applications")
        assert [(item["job_id"], item["agent"]) for item in held["items"]] == [(job, "dot")]
        assert (await call("release_application", token=token))["released"] is True
        assert await call("get_held_applications") == {"items": []}

    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    assert jobq(["held"]) == 0
    assert json.loads(capsys.readouterr().out) == {"items": []}


async def test_documents_over_mcp(tmp_path):
    import base64

    from mcp import Client

    from auto_app.agent.mcp_server import create_mcp_server
    from tests.handoff.test_questions_mcp import wanted_job

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    db, _ = wanted_job(runtime)
    folder = runtime.parent / "apply-kit" / "documents"
    folder.mkdir(parents=True)
    (folder / "neu-unofficial-transcript.pdf").write_bytes(b"%PDF-1.7 fictional\n")
    async with Client(create_mcp_server(db, handoff_tools=True)) as client:
        listed = (await client.call_tool("list_documents", {})).structured_content
        assert [d["name"] for d in listed["documents"]] == ["neu-unofficial-transcript.pdf"]
        one = (await client.call_tool("get_document", {"name": "neu-unofficial-transcript.pdf"})).structured_content
        assert base64.b64decode(one["base64"]) == b"%PDF-1.7 fictional\n"
        bad = await client.call_tool("get_document", {"name": "../answers.md"})
        assert bad.is_error

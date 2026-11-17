"""The same question and hold steps over MCP, so an agent such as ChatGPT Dots can use them once handoff tools are on.
Marking answers ready stays the owner's step: there is no MCP tool for it."""

from mcp import Client

from auto_app.agent.mcp_server import create_mcp_server
from auto_app.agent.service import AgentService
from auto_app.handoff.service import HandoffService
from auto_app.screen.rules import ScreeningRules
from auto_app.screen.store import screen_jobs
from tests.imported_jobs import ingest, job_id_of, make_store, record


def wanted_job(tmp_path):
    store = make_store(tmp_path)
    ingest(store, [record("787", "Software Engineer")])
    screen_jobs(store, ScreeningRules())
    job = job_id_of(store, "Software Engineer")
    db = store.db_path
    store.engine.dispose()
    AgentService(db).record_application_status(job, "want_to_apply")
    return db, job


async def test_questions_and_hold_round_trip_over_mcp(tmp_path):
    db, job = wanted_job(tmp_path)
    async with Client(create_mcp_server(db, handoff_tools=True)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"submit_open_questions", "hold_application"} <= names
        assert not {n for n in names if "answers_ready" in n or "requeue" in n}  # the owner's steps stay off MCP
        token = (await client.call_tool("claim_application", {"job_id": job, "agent": "dot"})).structured_content["token"]
        bad = await client.call_tool("submit_open_questions", {"token": token, "questions": [{"text": ""}]})
        assert bad.is_error
        sent = await client.call_tool("submit_open_questions", {"token": token, "questions": [
            {"text": "Why us?", "required": True, "limit": "200 words", "page": 1}]})
        assert not sent.is_error and sent.structured_content["status"] == "needs_answers"
        HandoffService(db).answers_ready(job)
        again = await client.call_tool("claim_application", {"job_id": job, "agent": "dot"})
        token = again.structured_content["token"]
        packet = (await client.call_tool("get_application_packet", {"token": token})).structured_content
        assert packet["open_questions"]["questions"][0]["text"] == "Why us?"
        held = await client.call_tool("hold_application", {"token": token})
        assert not held.is_error and held.structured_content["status"] == "ready"
        resumed = await client.call_tool("claim_application", {"job_id": job, "agent": "dot"})
        assert resumed.structured_content["resumed"] is True
        done = await client.call_tool("record_application_attempt",
                                      {"token": resumed.structured_content["token"], "outcome": "submitted"})
        assert not done.is_error


async def test_without_handoff_tools_the_new_steps_are_absent(tmp_path):
    db, _ = wanted_job(tmp_path)
    async with Client(create_mcp_server(db)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert not {"submit_open_questions", "hold_application"} & names

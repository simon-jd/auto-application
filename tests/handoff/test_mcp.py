"""Handoff is explicitly enabled and shares service guards with CLI."""

import json
from mcp import Client

from auto_app.agent.mcp_server import create_mcp_server
from auto_app.agent.service import AgentService
from auto_app.screen.rules import ScreeningRules
from auto_app.screen.store import screen_jobs
from tests.imported_jobs import make_store, ingest, record, job_id_of


async def test_handoff_tools_and_submission_timeline_round_trip(tmp_path):
    store = make_store(tmp_path)
    ingest(store, [record("787", "Software Engineer")])
    screen_jobs(store, ScreeningRules())
    job = job_id_of(store, "Software Engineer")
    db = store.db_path
    store.engine.dispose()
    AgentService(db).record_application_status(job, "want_to_apply")
    async with Client(create_mcp_server(db)) as client:
        assert "claim_application" not in {t.name for t in (await client.list_tools()).tools}
    async with Client(create_mcp_server(db, handoff_tools=True, control_tools=True)) as client:
        queue = await client.call_tool("get_application_queue", {})
        assert queue.structured_content["items"][0]["job_id"] == job
        claim = await client.call_tool("claim_application", {"job_id": job, "agent": "fictional-test"})
        token = claim.structured_content["token"]
        account = await client.call_tool("record_site_account", {"site": "example.com", "email": "x@example.com",
                                              "password": "fictional-secret-must-not-save"})
        assert account.is_error
        # The service must never accept the unknown secret-bearing field by silently dropping it.
        result = await client.call_tool("record_application_attempt", {"token": token, "outcome": "submitted"})
        assert not result.is_error
        detail = (await client.call_tool("get_job", {"job_id": job})).structured_content
        assert detail["state"] == "applied"
        assert [(e["kind"], e["source"]) for e in detail["events"]] == [("applied", "agent")]
        repeat = await client.call_tool("record_application_attempt", {"token": token, "outcome": "submitted"})
        assert repeat.is_error
        event = await client.call_tool("record_application_event", {"job_id": job, "kind": "interview"})
        assert not event.is_error and event.structured_content["events"][-1]["kind"] == "interview"
        assert not (await client.call_tool("list_unmatched_updates", {})).is_error


def test_network_handoff_flag_requires_authentication(tmp_path, capsys):
    from auto_app.agent.cli import main

    result = main(["serve", "--db", str(tmp_path / "missing.db"), "--handoff-tools"])
    assert result == 2 and "requires" in capsys.readouterr().err

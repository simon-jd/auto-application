# tests/agent/test_control.py
"""Control tools: status, pause, previewed rule changes applied only with the matching revision, feedback."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from mcp import Client
from sqlalchemy import insert, update

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow
from auto_app.screen.rules import ScreeningRules
from auto_app.screen.store import screen_jobs
from tests.imported_jobs import ingest, job_id_of, make_store, record


@pytest.fixture
def db(tmp_path):
    store = make_store(tmp_path)
    # "Account Executive" is excluded only as non-CS ("Product Manager" would also hit the senior rule via "manager").
    ingest(store, [record("101", "Software Engineer, New Grad"), record("102", "Account Executive")])
    screen_jobs(store, ScreeningRules())
    store.engine.dispose()
    return tmp_path / "data" / "auto_app.db"


def service(db):
    from auto_app.agent.control import ControlService

    return ControlService(db)


def test_pause_resume_and_status(db):
    control = service(db)
    paused = control.pause("2099-01-01T00:00:00+00:00", "vacation")
    assert paused["paused"] is True and control.pipeline_status()["control"]["reason"] == "vacation"
    assert control.resume()["paused"] is False
    status = control.pipeline_status()
    assert set(status) == {"ingest", "worker", "control"} and "checkpoint" not in json.dumps(status)


def test_rule_changes_are_previewed_then_applied_only_at_the_same_revision(db):
    control = service(db)
    before = control.screening_rules()
    proposal = control.propose_screening_change({"exclude_non_cs": False})
    assert proposal["diff"] == {"exclude_non_cs": [True, False]}
    assert proposal["preview"]["newly_kept"] == 1 and proposal["preview"]["examples"]["newly_kept"][0]["title"] == "Account Executive"
    with pytest.raises(ValueError, match="revision"):
        control.apply_screening_change(proposal["change_id"], "not-the-revision")
    applied = control.apply_screening_change(proposal["change_id"], before["revision"])
    assert applied["revision"] != before["revision"]
    assert control.screening_rules()["rules"]["exclude_non_cs"] is False
    with pytest.raises(ValueError, match="change"):
        control.apply_screening_change(proposal["change_id"], applied["revision"])  # used once


def test_invalid_rule_changes_are_rejected_before_any_preview(db):
    with pytest.raises(ValueError, match="unknown keys"):
        service(db).propose_screening_change({"exclude_everything": True})


def test_filter_feedback_needs_a_verdict_of_that_filter(db):
    control = service(db)
    store = make_store(db.parent.parent)
    job = job_id_of(store, "Software Engineer, New Grad")
    with store.engine.begin() as conn:
        conn.execute(insert(T.filter_versions).values(filter_version="v1", filter="ng", command="x", created_at=utcnow()))
        conn.execute(insert(T.judgments).values(job_id=job, filter_version="v1", filter="ng", status="ok",
                                                recommend=True, reason="fits", created_at=utcnow()))
    store.engine.dispose()
    result = control.record_filter_feedback(job, "ng", False, "not CS enough")
    assert result["label"] is False
    with pytest.raises(ValueError, match="didn't judge"):
        control.record_filter_feedback(job, "other", True, None)


async def test_control_tools_are_registered_only_when_asked(db):
    from auto_app.agent.mcp_server import create_mcp_server

    async with Client(create_mcp_server(db)) as client:
        assert "pause_pipeline" not in {t.name for t in (await client.list_tools()).tools}
    async with Client(create_mcp_server(db, control_tools=True)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"get_pipeline_status", "pause_pipeline", "resume_pipeline", "get_screening_rules",
                "propose_screening_change", "apply_screening_change", "record_filter_feedback", "get_digest"} <= names
        status = await client.call_tool("get_pipeline_status", {})
        assert not status.is_error


def test_only_one_concurrent_proposal_can_replace_a_revision(db):
    control = service(db)
    proposals = [control.propose_screening_change({"exclude_non_cs": False}),
                 control.propose_screening_change({"exclude_senior": False})]

    def apply(proposal):
        try:
            return control.apply_screening_change(proposal["change_id"], proposal["base_revision"])
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, proposals))
    assert sum(result is not None for result in results) == 1
    rules = control.screening_rules()["rules"]
    assert (rules["exclude_non_cs"], rules["exclude_senior"]) in ((False, True), (True, False))


def test_status_does_not_expose_mailbox_or_arbitrary_error_payloads(db):
    from auto_app.ingest.scheduler import _write_state
    from auto_app.worker import loop

    secret = "fictional-sensitive-value"
    _write_state(db, {"status": "partial", "checkpoint": secret, "last_report": {"sources": {
        "mail": {"status": "error", "error": secret, "checkpoint": secret}}}})
    _write_state(db, {"status": "partial", "alerts": {secret: "now"}, "error": secret,
                      "last_report": {"steps": {"enrich": {"status": "error", "error": secret}}}}, loop.STATE)
    result = service(db).pipeline_status()
    assert secret not in json.dumps(result)
    assert result["ingest"]["sources"]["mail"]["status"] == "error"


def test_digest_is_read_only_and_requires_worker_cutoff(db):
    control = service(db)
    before = db.read_bytes()
    assert control.digest() == {"jobs": [], "more": 0}
    assert db.read_bytes() == before


async def test_mcp_digest_uses_the_same_explicit_country_preferences_as_search(tmp_path):
    from auto_app.agent.mcp_server import create_mcp_server
    from auto_app.ingest.scheduler import _write_state
    from auto_app.worker import loop

    store = make_store(tmp_path)
    ingest(store, [record("201", "Software Engineer Boston"),
                   record("202", "Software Engineer Toronto", locations=["Toronto, ON, Canada"]),
                   record("203", "Software Engineer Multiple", locations=["Boston, MA", "Remote in Canada"])])
    screen_jobs(store, ScreeningRules())
    now = utcnow()
    with store.engine.begin() as conn:
        conn.execute(update(T.jobs).values(first_seen_at=now - timedelta(hours=3)))
    store.engine.dispose()
    db = tmp_path / "data" / "auto_app.db"
    _write_state(db, {"notify_since": (now - timedelta(hours=4)).isoformat()}, loop.STATE)
    preferences = tmp_path / "custom-preferences.json"
    preferences.write_text(json.dumps({"exclude_countries": ["Canada"]}))
    async with Client(create_mcp_server(db, preferences_path=preferences, control_tools=True)) as client:
        search = await client.call_tool("search_jobs", {})
        digest = await client.call_tool("get_digest", {})
        assert not search.is_error and not digest.is_error
        # Toronto alone is hidden; Boston plus remote Canada is still open in the US.
        visible = {"Software Engineer Boston", "Software Engineer Multiple"}
        assert {job["title"] for job in search.structured_content["items"]} == visible
        assert {job["title"] for job in digest.structured_content["jobs"]} == visible
        # Explicit configuration is required even if it disappears after server construction.
        preferences.unlink()
        assert (await client.call_tool("get_digest", {})).is_error


def test_invalid_pause_and_proposal_ids_are_generic_errors(db):
    control = service(db)
    with pytest.raises(ValueError) as exc:
        control.pause("fictional-sensitive-value")
    assert "fictional-sensitive-value" not in str(exc.value)
    for bad in ("../outside", "x" * 1000, "é"):
        with pytest.raises(ValueError, match="change"):
            control.apply_screening_change(bad, "default")


def test_status_reports_an_invalid_control_file_and_resume_repairs_it(db):
    from auto_app.worker.control import CONTROL, read_control

    db.with_name(db.name + CONTROL).write_text("broken")
    control = service(db).pipeline_status()["control"]
    assert control["paused"] is True and "invalid" in control["error"]
    service(db).resume()
    assert read_control(db)["paused"] is False and "error" not in service(db).pipeline_status()["control"]


def test_proposals_expire_after_a_day_and_old_ones_are_pruned(db):
    control = service(db)
    revision = control.screening_rules()["revision"]
    stale = control.propose_screening_change({"exclude_non_cs": False})
    stale_path = control.proposals / f"{stale['change_id']}.json"
    saved = json.loads(stale_path.read_text())
    stale_path.write_text(json.dumps({**saved, "created_at": (utcnow() - timedelta(hours=25)).isoformat()}))
    with pytest.raises(ValueError, match="expired"):
        control.apply_screening_change(stale["change_id"], revision)
    assert not stale_path.exists()
    old = control.propose_screening_change({"exclude_senior": False})
    old_path = control.proposals / f"{old['change_id']}.json"
    old_path.write_text(json.dumps({**json.loads(old_path.read_text()),
                                    "created_at": (utcnow() - timedelta(days=3)).isoformat()}))
    fresh = control.propose_screening_change({"exclude_internships": False})
    assert not old_path.exists() and (control.proposals / f"{fresh['change_id']}.json").exists()
    assert control.apply_screening_change(fresh["change_id"], revision)["rules"]["exclude_internships"] is False

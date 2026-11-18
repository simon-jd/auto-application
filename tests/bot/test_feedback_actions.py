"""Discord: "Shouldn't recommend" with a reason, and the screening proposals made from the reasons (Apply / Discard)."""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from auto_app.agent.control import ControlService
from auto_app.bot.actions import (
    closure_message,
    custom_id,
    parse_custom_id,
    parse_proposal_id,
    proposal_action,
    proposal_custom_id,
    proposal_message,
    record_shouldnt,
)
from auto_app.bot.app import ProposalButton
from auto_app.ingest.scheduler import _lock
from auto_app.screen.feedback import ScreeningFeedback
from auto_app.screen.rules import ScreeningRules
from auto_app.screen.store import screen_jobs
from tests.imported_jobs import ingest, job_id_of, make_store, record

OWNER = 1234567890


@pytest.fixture
def db(tmp_path):
    store = make_store(tmp_path)
    ingest(store, [record("301", "Field Service Associate"), record("302", "Software Engineer")])
    screen_jobs(store, ScreeningRules())
    ids = (job_id_of(store, "Field Service Associate"), job_id_of(store, "Software Engineer"))
    store.engine.dispose()
    return tmp_path / "data" / "auto_app.db", ids


def decision(path, job):
    store = make_store(path.parent.parent)
    try:
        return store.job(job)["decision"]
    finally:
        store.engine.dispose()


def test_bad_and_proposal_custom_ids():
    assert parse_custom_id(custom_id(41, "bad")) == (41, "bad")
    change = "a" * 32
    assert parse_proposal_id(proposal_custom_id(change, "apply")) == (change, "apply")
    for bad in ("screen:" + "g" * 32 + ":apply", "screen:abc:apply", "screen:" + change + ":delete"):
        assert parse_proposal_id(bad) is None
    with pytest.raises(ValueError):
        proposal_custom_id("nope", "apply")


def test_shouldnt_records_the_decision_and_the_reason_for_the_owner_only(db):
    path, (job, _) = db
    assert "only" in record_shouldnt(path, 999, OWNER, job, "现场服务").lower()
    assert decision(path, job) is None
    assert "reason" in record_shouldnt(path, OWNER, OWNER, job, "  ").lower()
    reply = record_shouldnt(path, OWNER, OWNER, job, "现场服务，不是软件岗")
    assert f"#{job}" in reply and decision(path, job) == "not_applied"
    [item] = ScreeningFeedback(path).pending()
    assert item["job_id"] == job and item["reason"] == "现场服务，不是软件岗"


def proposal_for(path, job, reason="现场服务"):
    feedback, control = ScreeningFeedback(path), ControlService(path)
    one = feedback.record(job, reason)["feedback_id"]
    proposal = control.propose_screening_change({"extra_non_cs_terms": ["field service"]}, note="排除 field service")
    feedback.attach([one], proposal["change_id"])
    return feedback, control, proposal


def test_proposal_message_shows_the_change_the_counts_and_the_reasons(db):
    path, (job, _) = db
    _, _, proposal = proposal_for(path, job)
    text = proposal_message(path, proposal["change_id"])
    assert "排除 field service" in text and "extra_non_cs_terms" in text and "field service" in text
    assert "1" in text and "Field Service Associate" in text and "现场服务" in text
    assert proposal_message(path, "b" * 32) is None


def test_apply_changes_the_rules_and_resolves_the_reasons(db):
    path, (job, _) = db
    feedback, control, proposal = proposal_for(path, job)
    denied = proposal_action(path, 999, OWNER, proposal["change_id"], "apply")
    assert "only" in denied.message.lower()
    assert not denied.remove_buttons
    result = proposal_action(path, OWNER, OWNER, proposal["change_id"], "apply")
    assert result.message.startswith("Applied.")
    assert result.remove_buttons
    assert "field service" in control.screening_rules()["rules"]["extra_non_cs_terms"]
    assert feedback.open_proposals() == [] and feedback.pending() == []


@pytest.mark.parametrize("action", ["apply", "discard"])
async def test_expired_proposal_returns_its_reasons_and_removes_buttons(db, monkeypatch, action):
    from auto_app.agent import control as control_module

    path, (job, _) = db
    feedback, control, proposal = proposal_for(path, job)
    change_id = proposal["change_id"]
    file = control.proposals / f"{change_id}.json"
    created_at = datetime.fromisoformat(json.loads(file.read_text())["created_at"])

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return (created_at + timedelta(hours=24)).astimezone(tz)

    monkeypatch.setattr(control_module, "datetime", Clock)
    interaction = await click_proposal(path, change_id, action)
    assert "expired" in interaction.followup.send.await_args.args[0].lower()
    assert [i["job_id"] for i in feedback.pending()] == [job]
    assert not file.exists()
    assert "field service" not in control.screening_rules()["rules"].get("extra_non_cs_terms", [])
    interaction.message.edit.assert_awaited_once_with(view=None)
    reply_text(interaction)


def test_discard_removes_the_proposal_and_resolves_the_reasons(db):
    path, (job, _) = db
    feedback, control, proposal = proposal_for(path, job)
    result = proposal_action(path, OWNER, OWNER, proposal["change_id"], "discard")
    assert result.message.startswith("Discarded.")
    assert result.remove_buttons
    assert control.screening_proposal(proposal["change_id"]) is None
    assert feedback.pending() == [] and feedback.open_proposals() == []
    repeated = proposal_action(path, OWNER, OWNER, proposal["change_id"], "discard")
    assert "expired" in repeated.message.lower()
    assert repeated.remove_buttons


async def click_proposal(path, change_id, action, *, user_id=OWNER, guild_id=456):
    settings = SimpleNamespace(db_path=path, owner_id=OWNER, guild_id=456)
    interaction = SimpleNamespace(
        client=SimpleNamespace(settings=settings), guild_id=guild_id, user=SimpleNamespace(id=user_id),
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()), message=SimpleNamespace(edit=AsyncMock()),
    )
    button_id = proposal_custom_id(change_id, action)
    match = re.fullmatch(r"screen:(?P<change>[a-f0-9]{32}):(?P<action>apply|discard)", button_id)
    button = await ProposalButton.from_custom_id(interaction, None, match)
    assert button.item.custom_id == button_id
    await button.callback(interaction)
    return interaction


def reply_text(interaction):
    reply = interaction.followup.send.await_args
    assert reply.kwargs["ephemeral"] is True
    assert reply.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    return reply.args[0].lower()


@pytest.mark.parametrize("action", ["apply", "discard"])
async def test_proposal_buttons_can_retry_after_lock_conflict(db, action):
    path, (job, _) = db
    feedback, control, proposal = proposal_for(path, job)
    change_id = proposal["change_id"]
    with _lock(control.rules_path.with_suffix(".lock"), "test screening change"):
        interaction = await click_proposal(path, change_id, action)
        assert "in progress" in interaction.followup.send.await_args.args[0].lower()
        interaction.message.edit.assert_not_awaited()
    assert control.screening_proposal(change_id) is not None
    assert [i["job_id"] for i in feedback.attached(change_id)] == [job]

    retry = await click_proposal(path, change_id, action)
    assert reply_text(retry).startswith("applied." if action == "apply" else "discarded.")
    retry.message.edit.assert_awaited_once_with(view=None)
    assert feedback.pending() == [] and feedback.open_proposals() == []

    repeated = await click_proposal(path, change_id, action)
    assert "expired or is gone" in reply_text(repeated)
    repeated.message.edit.assert_awaited_once_with(view=None)
    assert feedback.pending() == []


async def test_revision_conflict_leaves_discard_available(db):
    path, (job, _) = db
    feedback, control, proposal = proposal_for(path, job)
    change_id = proposal["change_id"]
    newer = control.propose_screening_change({"exclude_senior": False})
    control.apply_screening_change(newer["change_id"], newer["base_revision"])

    interaction = await click_proposal(path, change_id, "apply")
    text = interaction.followup.send.await_args.args[0].lower()
    assert "revision mismatch" in text and "propose again" in text
    interaction.message.edit.assert_not_awaited()
    assert "field service" not in control.screening_rules()["rules"].get("extra_non_cs_terms", [])
    assert [i["job_id"] for i in feedback.attached(change_id)] == [job]

    discarded = await click_proposal(path, change_id, "discard")
    assert reply_text(discarded).startswith("discarded.")
    discarded.message.edit.assert_awaited_once_with(view=None)
    assert feedback.pending() == [] and feedback.open_proposals() == []


@pytest.mark.parametrize("action", ["apply", "discard"])
async def test_proposal_buttons_stay_available_if_the_file_cannot_be_read(db, monkeypatch, action):
    path, (job, _) = db
    feedback, control, proposal = proposal_for(path, job)
    change_id = proposal["change_id"]
    saved_proposal = control.proposals / f"{change_id}.json"
    read_text = Path.read_text

    def fail_read(file, *args, **kwargs):
        if file == saved_proposal:
            raise PermissionError
        return read_text(file, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", fail_read)
        interaction = await click_proposal(path, change_id, action)
        assert "retry" in interaction.followup.send.await_args.args[0].lower()
        interaction.message.edit.assert_not_awaited()
    assert saved_proposal.is_file()
    assert [i["job_id"] for i in feedback.attached(change_id)] == [job]


async def test_expired_proposal_releases_only_its_own_reasons(db, monkeypatch):
    path, (expired_job, other_job) = db
    feedback, control, expired = proposal_for(path, expired_job)
    _, _, other = proposal_for(path, other_job)
    expired_id = expired["change_id"]
    expired_file = control.proposals / f"{expired_id}.json"
    other_file = control.proposals / f"{other['change_id']}.json"
    saved = json.loads(expired_file.read_text())
    saved["created_at"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    expired_file.write_text(json.dumps(saved))
    read_text = Path.read_text

    def fail_other_read(file, *args, **kwargs):
        if file == other_file:
            raise PermissionError
        return read_text(file, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", fail_other_read)
        interaction = await click_proposal(path, expired_id, "discard")
    assert "expired" in reply_text(interaction)
    interaction.message.edit.assert_awaited_once_with(view=None)
    assert feedback.attached(expired_id) == []
    assert [item["job_id"] for item in feedback.pending()] == [expired_job]
    assert [item["job_id"] for item in feedback.attached(other["change_id"])] == [other_job]


@pytest.mark.parametrize(("user_id", "guild_id"), [(999, 456), (OWNER, 999)])
async def test_proposal_button_requires_both_owner_and_configured_guild(db, user_id, guild_id):
    path, (job, _) = db
    feedback, control, proposal = proposal_for(path, job)
    change_id = proposal["change_id"]
    interaction = await click_proposal(path, change_id, "apply", user_id=user_id, guild_id=guild_id)
    interaction.response.send_message.assert_awaited_once()
    denied = interaction.response.send_message.await_args
    assert denied.args == ("Owner only in the configured server.",)
    assert denied.kwargs["ephemeral"] is True
    assert denied.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    interaction.response.defer.assert_not_awaited()
    interaction.followup.send.assert_not_awaited()
    interaction.message.edit.assert_not_awaited()
    assert [i["job_id"] for i in feedback.attached(change_id)] == [job]
    assert "field service" not in control.screening_rules()["rules"].get("extra_non_cs_terms", [])


def test_closure_message_lists_each_reason_with_the_note():
    text = closure_message([{"job_id": 7, "title": "Platform Engineer", "reason": "方向不对",
                             "resolution_note": "关键词分不出方向"}])
    assert "#7" in text and "方向不对" in text and "关键词分不出方向" in text

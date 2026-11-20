"""Bot guards and delivery, with Discord transport replaced by a local channel."""

import json
import re
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, update

from auto_app.agent.service import AgentService
from auto_app.bot.actions import BUTTONS, BotSettings
from auto_app.bot.app import JobBot, JobButton, digest_ready
from auto_app.core.db import tables as T
from auto_app.ingest.scheduler import _lock, _path
from auto_app.worker import loop
from auto_app.worker.control import write_control
from tests.imported_jobs import ingest, job_id_of, make_store, record

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def test_digest_waits_for_successful_completed_screening_and_unpaused_control():
    state = {"status": "ok", "running": False, "notify_since": "2026-10-12T00:00:00+00:00",
             "last_report": {"steps": {"screen": {"status": "ok"}}}}
    assert digest_ready(state, {"paused": False}, NOW)
    assert digest_ready({**state, "running": True}, {"paused": False}, NOW)
    assert not digest_ready({**state, "status": "running"}, {"paused": False}, NOW)
    # An enrichment outage leaves the round partial, but its screening verdicts are current.
    enrich_outage = {**state, "status": "partial",
                     "last_report": {"steps": {"screen": {"status": "ok"}, "enrich": {"status": "error"}}}}
    assert digest_ready(enrich_outage, {"paused": False}, NOW)
    assert not digest_ready({**state, "status": "partial", "last_report": {"steps": {
        "screen": {"status": "ok"}, "rescreen": {"status": "error"}}}}, {"paused": False}, NOW)
    assert not digest_ready({**state, "status": "error", "last_report": {"status": "error"}}, {"paused": False}, NOW)
    assert not digest_ready({**state, "status": "waiting"}, {"paused": False}, NOW)
    assert not digest_ready({**state, "last_report": {"steps": {"screen": {"status": "error"}}}},
                            {"paused": False}, NOW)
    assert not digest_ready(state, {"paused": True, "paused_until": None}, NOW)


def test_idle_worker_daemon_lock_does_not_block_digest(tmp_path):
    db_path = tmp_path / "auto_app.db"
    db_path.with_name(db_path.name + loop.STATE).write_text(json.dumps({
        "status": "ok", "notify_since": "2026-10-12T00:00:00+00:00",
        "last_report": {"steps": {"screen": {"status": "ok"}}}}))
    with _lock(_path(db_path, loop.LOCK), "worker"):
        state = loop.status(db_path)
        assert state["running"] is True and state["status"] == "ok"
        assert digest_ready(state, {"paused": False}, NOW)


@pytest.mark.asyncio
async def test_post_digest_marks_only_accepted_discord_messages(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    ingest(store, [record(str(100 + i), f"Engineer {i}") for i in range(7)])
    with store.engine.begin() as conn:
        conn.execute(update(T.jobs).values(first_seen_at=NOW))
        for i in range(7):
            job_id = job_id_of(store, f"Engineer {i}")
            conn.execute(insert(T.job_screenings).values(
                job_id=job_id, rules_version="t", facts_hash="h", outcome="kept", reasons=[],
                signals={"sponsorship": "offers", "has_description": True}, screened_at=NOW))
    store.engine.dispose()
    db_path = tmp_path / "data" / "auto_app.db"
    db_path.with_name(db_path.name + ".worker-state.json").write_text(json.dumps({
        "status": "ok", "notify_since": "2026-10-12T00:00:00+00:00",
        "last_report": {"steps": {"screen": {"status": "ok"}}}}))

    class Channel:
        guild = SimpleNamespace(id=456)

        def __init__(self):
            self.sent = []
            self.unavailable = True

        async def send(self, **kwargs):
            if self.sent and self.unavailable:
                raise RuntimeError("Discord unavailable")
            self.sent.append(kwargs)

    channel = Channel()
    bot = JobBot(BotSettings("local.test.token", 123, 456, 789, db_path))
    bot.digest_at = None  # this test is about rechecks, not the morning schedule
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: channel)
    with pytest.raises(RuntimeError, match="Discord unavailable"):
        await bot.post_digest()
    assert len(channel.sent) == 1
    assert "embed" in channel.sent[0]
    job_id = int(channel.sent[0]["embed"].footer.text.split()[-1])
    reopened = make_store(tmp_path)
    assert [job["job_id"] for job in reopened.jobs() if job["notified_at"]] == [job_id]
    reopened.engine.dispose()
    write_control(db_path, paused=True)
    await bot.post_digest()
    assert len(channel.sent) == 1
    write_control(db_path, paused=False)
    channel.guild = SimpleNamespace(id=999)
    with pytest.raises(ValueError, match="configured server"):
        await bot.post_digest()
    channel.guild = SimpleNamespace(id=456)
    channel.unavailable = False
    await bot.post_digest()
    delivered = {int(message["embed"].footer.text.split()[-1]) for message in channel.sent}
    assert len(channel.sent) == len(delivered) == 7
    await bot.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("intervention", ["completed_screen", "manual_decision"])
async def test_later_digest_message_uses_current_eligibility(tmp_path, monkeypatch, intervention):
    store = make_store(tmp_path)
    ingest(store, [record(str(100 + i), f"Engineer {i}") for i in range(7)])
    ids = [job_id_of(store, f"Engineer {i}") for i in range(7)]
    with store.engine.begin() as conn:
        conn.execute(update(T.jobs).values(first_seen_at=NOW))
        for job_id in ids:
            conn.execute(insert(T.job_screenings).values(
                job_id=job_id, rules_version="t", facts_hash="h", outcome="kept", reasons=[],
                signals={"sponsorship": "offers", "has_description": True}, screened_at=NOW))
    store.engine.dispose()
    db_path = tmp_path / "data" / "auto_app.db"
    state_path = db_path.with_name(db_path.name + loop.STATE)
    state = {"status": "ok", "notify_since": "2026-10-12T00:00:00+00:00",
             "last_run_at": "2026-10-12T12:00:00+00:00",
             "last_report": {"steps": {"screen": {"status": "ok"}}}}
    state_path.write_text(json.dumps(state))

    class Channel:
        guild = SimpleNamespace(id=456)

        def __init__(self):
            self.sent = []
            self.handled = None

        async def send(self, **kwargs):
            self.sent.append(kwargs)
            if len(self.sent) != 1:
                return
            first_id = int(kwargs["embed"].footer.text.split()[-1])
            remaining = [job_id for job_id in ids if job_id != first_id]
            if intervention == "completed_screen":
                state_path.write_text(json.dumps({**state, "status": "running"}))
                updated = make_store(tmp_path)
                with updated.engine.begin() as conn:
                    conn.execute(update(T.job_screenings).where(T.job_screenings.c.job_id.in_(remaining))
                                 .values(outcome="excluded", reasons=["new rule"]))
                updated.engine.dispose()
                state_path.write_text(json.dumps({**state, "last_run_at": "2026-10-12T12:15:00+00:00"}))
            else:
                self.handled = remaining[0]
                AgentService(db_path).record_application_status(self.handled, "not_applied", reason="manual")

    channel = Channel()
    bot = JobBot(BotSettings("local.test.token", 123, 456, 789, db_path))
    bot.digest_at = None  # this test is about rechecks, not the morning schedule
    monkeypatch.setattr(bot, "get_channel", lambda _: channel)
    await bot.post_digest()
    delivered = []
    for message in channel.sent:
        job_id = int(message["embed"].footer.text.split()[-1])
        delivered.append(job_id)
        assert [button.custom_id for button in message["view"].children] == [
            f"job:{job_id}:{action}" for action in BUTTONS
        ]
    checked = make_store(tmp_path)
    notified = {job["job_id"] for job in checked.jobs() if job["notified_at"]}
    checked.engine.dispose()
    if intervention == "completed_screen":
        assert len(channel.sent) == 1
        assert set(delivered) == notified and len(notified) == 1
    else:
        assert len(channel.sent) == 6
        assert channel.handled not in delivered and channel.handled not in notified
        assert set(delivered) == notified and len(notified) == 6
    await bot.close()


@pytest.mark.asyncio
async def test_persistent_button_rebuilds_after_restart_and_rejects_unauthorized_users(tmp_path):
    bot = JobBot(BotSettings("local.test.token", 123, 456, 789, tmp_path / "missing.db"))

    class Response:
        def __init__(self):
            self.sent = []

        async def send_message(self, message, **kwargs):
            self.sent.append(message)

    response = Response()
    interaction = SimpleNamespace(client=bot, guild_id=999, user=SimpleNamespace(id=123), response=response)
    match = re.fullmatch(r"job:(?P<job>[1-9][0-9]{0,11}):(?P<action>want|skip|applied)",
                         "job:41:want")
    button = await JobButton.from_custom_id(interaction, None, match)
    assert button.item.custom_id == "job:41:want"
    await button.callback(interaction)
    assert response.sent == ["Owner only in the configured server."]
    response.sent.clear()
    interaction.guild_id = 456
    interaction.user.id = 999
    await button.callback(interaction)
    assert response.sent == ["Owner only in the configured server."]
    await bot.close()


@pytest.mark.asyncio
async def test_post_digest_holds_back_a_newly_added_feed(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    ingest(store, [record("101", "Engineer 0")], source="board-greenhouse-acme")
    with store.engine.begin() as conn:
        conn.execute(update(T.jobs).values(first_seen_at=NOW))
        conn.execute(insert(T.job_screenings).values(
            job_id=job_id_of(store, "Engineer 0"), rules_version="t", facts_hash="h", outcome="kept", reasons=[],
            signals={"sponsorship": "offers", "has_description": True}, screened_at=NOW))
    store.engine.dispose()
    db_path = tmp_path / "data" / "auto_app.db"
    state_path = db_path.with_name(db_path.name + loop.STATE)
    state = {"status": "ok", "notify_since": "2026-10-12T00:00:00+00:00",
             "last_report": {"steps": {"screen": {"status": "ok"}}},
             "source_since": {"board-greenhouse-acme": "2026-10-12T13:00:00+00:00"}}  # added after the job
    state_path.write_text(json.dumps(state))

    class Channel:
        guild = SimpleNamespace(id=456)
        sent = []

        async def send(self, **kwargs):
            self.sent.append(kwargs)

    bot = JobBot(BotSettings("local.test.token", 123, 456, 789, db_path))
    bot.digest_at = None  # this test is about new feeds, not the morning schedule
    monkeypatch.setattr(bot, "get_channel", lambda _: Channel())
    await bot.post_digest()
    assert Channel.sent == []
    state_path.write_text(json.dumps({**state, "source_since": {"board-greenhouse-acme": None}}))
    await bot.post_digest()
    assert len(Channel.sent) == 1
    await bot.close()


@pytest.mark.asyncio
async def test_bad_button_opens_the_reason_modal_for_the_owner_only(tmp_path):
    from auto_app.bot.app import ShouldntModal

    bot = JobBot(BotSettings("local.test.token", 123, 456, 789, tmp_path / "missing.db"))

    class Response:
        def __init__(self):
            self.sent, self.modals = [], []

        async def send_message(self, message, **kwargs):
            self.sent.append(message)

        async def send_modal(self, modal):
            self.modals.append(modal)

    response = Response()
    interaction = SimpleNamespace(client=bot, guild_id=456, user=SimpleNamespace(id=999), response=response)
    match = re.fullmatch(r"job:(?P<job>[1-9][0-9]{0,11}):(?P<action>want|skip|applied|bad)", "job:41:bad")
    button = await JobButton.from_custom_id(interaction, None, match)
    await button.callback(interaction)
    assert response.sent == ["Owner only in the configured server."] and response.modals == []
    interaction.user.id = 123
    await button.callback(interaction)
    [modal] = response.modals
    assert isinstance(modal, ShouldntModal) and modal.job_id == 41
    assert modal.reason.max_length == 300 and modal.reason.required
    await bot.close()


@pytest.mark.asyncio
async def test_screening_updates_are_posted_once(tmp_path, monkeypatch):
    from auto_app.agent.control import ControlService
    from auto_app.screen.feedback import ScreeningFeedback
    from auto_app.screen.rules import ScreeningRules
    from auto_app.screen.store import screen_jobs

    store = make_store(tmp_path)
    ingest(store, [record("401", "Field Service Associate"), record("402", "Platform Engineer")])
    screen_jobs(store, ScreeningRules())
    field, platform = job_id_of(store, "Field Service Associate"), job_id_of(store, "Platform Engineer")
    store.engine.dispose()
    db_path = tmp_path / "data" / "auto_app.db"
    feedback = ScreeningFeedback(db_path)
    one = feedback.record(field, "现场服务")["feedback_id"]
    two = feedback.record(platform, "方向不对")["feedback_id"]
    proposal = ControlService(db_path).propose_screening_change({"extra_non_cs_terms": ["field service"]}, note="n")
    feedback.attach([one], proposal["change_id"])
    feedback.close([two], "关键词分不出方向")

    class Channel:
        guild = SimpleNamespace(id=456)

        def __init__(self):
            self.sent = []

        async def send(self, **kwargs):
            self.sent.append(kwargs)

    channel = Channel()
    bot = JobBot(BotSettings("local.test.token", 123, 456, 789, db_path))
    monkeypatch.setattr(bot, "get_channel", lambda _: channel)
    await bot.post_screening_updates()
    assert len(channel.sent) == 2
    ids = [item.custom_id for item in channel.sent[0]["view"].children]
    assert ids == [f"screen:{proposal['change_id']}:apply", f"screen:{proposal['change_id']}:discard"]
    assert "方向不对" in channel.sent[1]["content"]
    await bot.post_screening_updates()
    assert len(channel.sent) == 2  # each proposal and each closure once
    await bot.close()


@pytest.mark.asyncio
async def test_the_digest_sends_fifty_a_day_from_eight_am_pacific_oldest_first(tmp_path, monkeypatch):
    """The owner's choice on 2026-11-19: one batch of 50 every morning at 8:00 Pacific, oldest collected first."""
    from datetime import time, timedelta

    from auto_app.bot import app
    from auto_app.bot.app import DIGEST_AT, DIGEST_PER_DAY, DIGEST_ZONE

    assert (DIGEST_PER_DAY, DIGEST_AT, DIGEST_ZONE) == (50, time(8, 0), "America/Los_Angeles")
    store = make_store(tmp_path)
    ingest(store, [record(str(300 + i), f"Engineer {i}") for i in range(55)])
    order = [job_id_of(store, f"Engineer {i}") for i in range(55)]
    with store.engine.begin() as conn:
        for i, job_id in enumerate(order):  # Engineer 0 was collected first; the newest is a sponsor
            conn.execute(update(T.jobs).where(T.jobs.c.job_id == job_id).values(first_seen_at=NOW + timedelta(minutes=i)))
            conn.execute(insert(T.job_screenings).values(
                job_id=job_id, rules_version="t", facts_hash="h", outcome="kept", reasons=[],
                signals={"sponsorship": "offers" if i == 54 else "unknown", "has_description": True}, screened_at=NOW))
    store.engine.dispose()
    db_path = tmp_path / "data" / "auto_app.db"
    db_path.with_name(db_path.name + ".worker-state.json").write_text(json.dumps({
        "status": "ok", "notify_since": "2026-10-12T00:00:00+00:00",
        "last_report": {"steps": {"screen": {"status": "ok"}}}}))

    class Channel:
        guild = SimpleNamespace(id=456)

        def __init__(self):
            self.sent = []

        async def send(self, **kwargs):
            self.sent.append(kwargs)

    clock = {"now": datetime(2026, 11, 20, 15, 59, tzinfo=UTC)}  # 07:59 Pacific
    monkeypatch.setattr(app, "utcnow", lambda: clock["now"])
    channel = Channel()
    bot = JobBot(BotSettings("local.test.token", 123, 456, 789, db_path))
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: channel)
    def sent():
        return [int(m["embed"].footer.text.split()[-1]) for m in channel.sent]

    await bot.post_digest()
    assert sent() == []  # not before 8:00
    clock["now"] = datetime(2026, 11, 20, 16, 0, tzinfo=UTC)
    await bot.post_digest()
    assert sent() == order[:50]  # the day's 50 in one check, oldest collected first
    assert channel.sent[0]["content"] == "**今日职位 50 个**（按收录时间从旧到新，另有 5 个在排队）"
    assert all(m["content"] is None for m in channel.sent[1:])
    clock["now"] = datetime(2026, 11, 21, 7, 0, tzinfo=UTC)  # 23:00 Pacific, same day
    await bot.post_digest()
    assert len(sent()) == 50
    clock["now"] = datetime(2026, 11, 21, 15, 59, tzinfo=UTC)  # 07:59 the next day
    await bot.post_digest()
    assert len(sent()) == 50
    clock["now"] = datetime(2026, 11, 21, 16, 5, tzinfo=UTC)
    await bot.post_digest()
    assert sent() == order  # the last 5
    await bot.close()

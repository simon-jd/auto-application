"""External outcome notifications survive empty job digests and partial delivery."""

from sqlalchemy import insert

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow
from tests.imported_jobs import make_store, ingest, record, job_id_of
from tests.worker.test_digest import FakeDiscord
import pytest


def outcomes(tmp_path, count=1):
    store = make_store(tmp_path)
    ingest(store, [record("719", "Software Engineer")])
    job = job_id_of(store, "Software Engineer")
    now = utcnow()
    with store.engine.begin() as conn:
        app = conn.execute(insert(T.applications).values(job_id=job, platform="other", status="needs_input",
                   chain=[], chain_pos=0, created_at=now, updated_at=now)).inserted_primary_key[0]
        for _ in range(count):
            conn.execute(insert(T.attempts).values(application_id=app, applier="external-agent", **{"pass": "submit"},
                         outcome="needs_input", reason="A fictional verification question " * 20,
                         started_at=now, ended_at=now))
    return store


def test_updates_are_chunked_and_cursor_tracks_only_accepted_batches(tmp_path):
    from auto_app.worker.digest import send_handoff_updates

    store, state = outcomes(tmp_path, 15), {}
    discord = FakeDiscord(fail_on=1)
    report = send_handoff_updates(store, discord, state)
    assert 0 < report["sent"] < 15 and report["error"]
    cursor = state["handoff_cursor"]
    assert cursor == report["sent"]
    successful = FakeDiscord()
    report = send_handoff_updates(store, successful, state)
    assert report["sent"] == 15 - cursor and state["handoff_cursor"] == 15
    assert all(len(content.encode("utf-16-le")) // 2 <= 2000 for content, _ in discord.posts + successful.posts)
    assert send_handoff_updates(store, successful, state)["sent"] == 0
    store.engine.dispose()


@pytest.mark.asyncio
async def test_bot_outcomes_do_not_require_a_job_digest_and_keep_their_own_cursor(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from auto_app.bot.actions import BotSettings
    from auto_app.bot.app import JobBot
    from auto_app.worker.control import write_control

    store = outcomes(tmp_path)
    db = store.db_path
    store.engine.dispose()
    class Channel:
        guild = SimpleNamespace(id=456)
        sent = []
        async def send(self, **kwargs):
            self.sent.append(kwargs)
    channel = Channel()
    bot = JobBot(BotSettings("fictional.token", 123, 456, 789, db))
    monkeypatch.setattr(bot, "get_channel", lambda _: channel)
    write_control(db, paused=True)
    await bot.post_handoff_updates()
    assert not channel.sent
    write_control(db, paused=False)
    await bot.post_handoff_updates()
    await bot.post_handoff_updates()
    assert len(channel.sent) == 1 and "needs you" in channel.sent[0]["content"]
    assert json.loads(db.with_name(db.name + ".bot-state.json").read_text())["handoff_cursor"] == 1
    assert not db.with_name(db.name + ".worker-state.json").exists()
    await bot.close()

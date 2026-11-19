"""Discord: the weekly board proposals, with Add / Skip for the owner."""

import json
from types import SimpleNamespace

import pytest

from auto_app.bot.actions import BotSettings, board_proposal_action, board_proposal_embed, parse_board_id
from auto_app.bot.app import BOT_STATE, JobBot
from auto_app.ingest.scheduler import _read_state
from auto_app.worker.board_proposals import BoardProposals
from tests.worker.test_board_proposals import NOW, home  # noqa: F401  (home is a fixture)

OWNER = 123


def test_ids_and_embed(home):  # noqa: F811
    assert parse_board_id("bp:5:add") == (5, "add") and parse_board_id("bp:5:drop") is None
    [beta, _] = BoardProposals(home / "data" / "auto_app.db").generate(now=NOW)
    embed = board_proposal_embed(beta)
    assert "Beta" in embed["title"] and embed["url"] == "https://jobs.lever.co/beta"
    assert "想投 1" in embed["description"] and "每 6 小时" in embed["description"]


def test_only_the_owner_adds_and_a_board_is_decided_once(home):  # noqa: F811
    db = home / "data" / "auto_app.db"
    [beta, acme] = BoardProposals(db).generate(now=NOW)
    assert "only" in board_proposal_action(db, 999, OWNER, acme["proposal_id"], "add").message.lower()
    result = board_proposal_action(db, OWNER, OWNER, acme["proposal_id"], "add")
    assert result.remove_buttons and "board-greenhouse-acme" in result.message
    assert "board-greenhouse-acme" in (home / "config" / "feeds.yaml").read_text()
    assert not board_proposal_action(db, OWNER, OWNER, acme["proposal_id"], "skip").remove_buttons
    assert board_proposal_action(db, OWNER, OWNER, beta["proposal_id"], "skip").remove_buttons


@pytest.mark.asyncio
async def test_the_bot_generates_weekly_and_posts_each_proposal_once(home, monkeypatch):  # noqa: F811
    db = home / "data" / "auto_app.db"

    class Channel:
        id, guild = 999, SimpleNamespace(id=456)

        def __init__(self):
            self.sent = []

        async def send(self, **kwargs):
            self.sent.append(kwargs)
            return SimpleNamespace(id=2000 + len(self.sent))

    channel = Channel()
    bot = JobBot(BotSettings("local.test.token", OWNER, 456, 789, db, 999))
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: channel)
    await bot.post_board_proposals()
    assert [m["embeds"][0].url for m in channel.sent] == ["https://jobs.lever.co/beta", "https://job-boards.greenhouse.io/acme"]
    assert [c.custom_id for c in channel.sent[0]["view"].children][0].startswith("bp:")
    assert _read_state(db, BOT_STATE).get("board_proposals_at")
    await bot.post_board_proposals()
    assert len(channel.sent) == 2  # not again within the week, and each posted once
    await bot.close()

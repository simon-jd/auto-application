"""Discord: Claude's questions for the owner, in their own channel, answered with buttons or a reply box."""

import os
from types import SimpleNamespace

import pytest

from auto_app.bot.actions import BotSettings, ask_action, ask_custom_id, ask_embed, parse_ask_id
from auto_app.bot.app import AskButton, AskReplyModal, JobBot
from auto_app.handoff.asks import OwnerQuestions
from tests.imported_jobs import ingest, job_id_of, make_store, record

OWNER = 123
ENV_LINES = ["AUTO_APP_DISCORD_BOT_TOKEN=local.test.token", f"AUTO_APP_DISCORD_OWNER_ID={OWNER}",
             "AUTO_APP_DISCORD_GUILD_ID=456", "AUTO_APP_DISCORD_CHANNEL_ID=789"]


@pytest.fixture
def db(tmp_path):
    store = make_store(tmp_path)
    ingest(store, [record("501", "Software Engineer, General")])
    store.set_job_state(job_id_of(store, "Software Engineer, General"), decision="want_to_apply")
    job = job_id_of(store, "Software Engineer, General")
    store.engine.dispose()
    return tmp_path / "data" / "auto_app.db", job


def ask(path, job, **extra):
    return OwnerQuestions(path).ask({"job_id": job, "question": "通用岗位，要投吗？",
                                     "context": "JD 写着 hires across all teams", "options": ["投，infra", "投，sde"],
                                     "recommended": 0, **extra}, "claude-code")["question_id"]


def write_env(tmp_path, extra=()):
    path = tmp_path / "bot.env"
    path.write_text("\n".join([*ENV_LINES, *extra]) + "\n")
    os.chmod(path, 0o600)
    return path


def test_the_questions_channel_is_optional(tmp_path):
    assert BotSettings.from_file(write_env(tmp_path), tmp_path / "x.db").questions_channel_id is None
    settings = BotSettings.from_file(write_env(tmp_path, ["AUTO_APP_DISCORD_QUESTIONS_CHANNEL_ID=999"]), tmp_path / "x.db")
    assert settings.questions_channel_id == 999 and settings.channel_id == 789
    with pytest.raises(ValueError):
        BotSettings.from_file(write_env(tmp_path, ["AUTO_APP_DISCORD_QUESTIONS_CHANNEL_ID=abc"]), tmp_path / "x.db")


def test_ask_ids():
    assert parse_ask_id(ask_custom_id(7, "opt2")) == (7, "opt2")
    assert parse_ask_id(ask_custom_id(7, "skip")) == (7, "skip")
    for bad in ("ask:7:opt4", "ask:0:reply", "ask:7:delete", "job:7:want"):
        assert parse_ask_id(bad) is None
    with pytest.raises(ValueError):
        ask_custom_id(7, "opt9")


def test_the_embed_carries_the_job_question_context_and_recommendation(db):
    path, job = db
    item = OwnerQuestions(path).get(ask(path, job))
    embed = ask_embed(item)
    assert embed["title"].startswith(f"#{job} ") and "Software Engineer, General" in embed["title"]
    assert "通用岗位，要投吗？" in embed["description"] and "hires across all teams" in embed["description"]
    assert "投，infra" in embed["description"]  # the recommendation
    assert embed["url"].startswith("https://")


def test_owner_answers_with_an_option_or_their_own_words(db):
    path, job = db
    first, second = ask(path, job), ask(path, job, question="另一个问题")
    assert "only" in ask_action(path, 999, OWNER, first, "opt1").message.lower()
    result = ask_action(path, OWNER, OWNER, first, "opt1")
    assert result.remove_buttons and "投，sde" in result.message
    assert [i["answer"] for i in OwnerQuestions(path).answered()] == ["投，sde"]
    assert not ask_action(path, OWNER, OWNER, first, "opt0").remove_buttons  # already answered
    ask_action(path, OWNER, OWNER, second, "reply", "投，但只投 infra 方向")
    assert [i["answer"] for i in OwnerQuestions(path).answered()] == ["投，sde", "投，但只投 infra 方向"]


def test_skip_marks_the_job_not_applied(db):
    path, job = db
    question = ask(path, job)
    result = ask_action(path, OWNER, OWNER, question, "skip")
    assert result.remove_buttons
    store = make_store(path.parent.parent)
    assert store.job(job)["decision"] == "not_applied"
    store.engine.dispose()


class Channel:
    def __init__(self, channel_id):
        self.id, self.guild, self.sent = channel_id, SimpleNamespace(id=456), []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(id=1000 + len(self.sent))


@pytest.mark.asyncio
async def test_questions_go_to_their_channel_once_and_results_follow(db, monkeypatch):
    path, job = db
    question = ask(path, job)
    channels = {789: Channel(789), 999: Channel(999)}
    bot = JobBot(BotSettings("local.test.token", OWNER, 456, 789, path, 999))
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: channels[channel_id])
    await bot.post_owner_questions()
    [sent] = channels[999].sent
    assert channels[789].sent == []
    assert sent["embeds"][0].title.startswith(f"#{job} ")
    ids = [item.custom_id for item in sent["view"].children]
    assert ids == [f"ask:{question}:opt0", f"ask:{question}:opt1", f"ask:{question}:reply", f"ask:{question}:skip"]
    await bot.post_owner_questions()
    assert len(channels[999].sent) == 1  # posted once
    OwnerQuestions(path).answer(question, "投，infra")
    OwnerQuestions(path).done(question, "材料已发布，进入投递队列，Grok 下一轮会填表")
    await bot.post_owner_questions()
    [_, result] = channels[999].sent
    assert "材料已发布" in result["content"] and result["reference"].message_id == 1001
    await bot.post_owner_questions()
    assert len(channels[999].sent) == 2
    await bot.close()


@pytest.mark.asyncio
async def test_without_a_questions_channel_they_go_to_the_main_channel(db, monkeypatch):
    path, job = db
    ask(path, job)
    channel = Channel(789)
    bot = JobBot(BotSettings("local.test.token", OWNER, 456, 789, path))
    monkeypatch.setattr(bot, "get_channel", lambda channel_id: channel)
    await bot.post_owner_questions()
    assert len(channel.sent) == 1
    await bot.close()


@pytest.mark.asyncio
async def test_buttons_are_for_the_owner_and_reply_opens_the_box(tmp_path):
    import re

    bot = JobBot(BotSettings("local.test.token", OWNER, 456, 789, tmp_path / "missing.db"))

    class Response:
        def __init__(self):
            self.sent, self.modals = [], []

        async def send_message(self, message, **kwargs):
            self.sent.append(message)

        async def send_modal(self, modal):
            self.modals.append(modal)

    response = Response()
    interaction = SimpleNamespace(client=bot, guild_id=456, user=SimpleNamespace(id=999), response=response)
    match = re.fullmatch(AskButton.__discord_ui_compiled_template__, "ask:5:reply")
    button = await AskButton.from_custom_id(interaction, None, match)
    await button.callback(interaction)
    assert response.sent == ["Owner only in the configured server."] and response.modals == []
    interaction.user.id = OWNER
    await button.callback(interaction)
    [modal] = response.modals
    assert isinstance(modal, AskReplyModal) and modal.question_id == 5 and modal.reply.max_length == 1000
    await bot.close()

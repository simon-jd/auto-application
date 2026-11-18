"""Discord buttons, slash commands, and a five-minute digest loop."""

import asyncio
import logging
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import tasks

from auto_app.bot.actions import (
    BUTTONS,
    ask_action,
    ask_custom_id,
    ask_embed,
    LABELS,
    BotSettings,
    closure_message,
    custom_id,
    job_summary,
    jobs_summary,
    proposal_action,
    proposal_custom_id,
    proposal_message,
    record_action,
    record_shouldnt,
)
from auto_app.core.models import utcnow
from auto_app.worker.control import is_paused

log = logging.getLogger(__name__)
STYLES = {"want": discord.ButtonStyle.success, "skip": discord.ButtonStyle.secondary,
          "applied": discord.ButtonStyle.primary, "bad": discord.ButtonStyle.danger}
BOT_STATE = ".bot-state.json"
POSTED_KEEP = 200  # proposal ids remembered as posted


def digest_ready(state: dict, control: dict, now: datetime) -> bool:
    """A finished round whose screening succeeded may release queued jobs.

    The round as a whole may be partial — an employer API outage fails only enrichment, and the
    verdicts are still current — but a failed screen or rescreen, a round in progress, a crashed
    round or a pause holds the digest back.
    """
    steps = (state.get("last_report") or {}).get("steps") or {}
    screened = (steps.get("screen", {}).get("status") == "ok"
                and steps.get("rescreen", {"status": "ok"}).get("status") == "ok")
    return (bool(state.get("notify_since")) and state.get("status") in ("ok", "partial") and screened
            and not is_paused(control, now))


class ShouldntModal(discord.ui.Modal, title="Why shouldn't it be recommended?"):
    """The reason for "Shouldn't recommend": it feeds the daily screening-rule summary."""

    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, min_length=1, max_length=300,
                                  required=True, placeholder="e.g. field service work, not software")

    def __init__(self, job_id: int):
        super().__init__(timeout=600)
        self.job_id = job_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        settings = interaction.client.settings
        if not _owner(interaction, settings):
            await interaction.response.send_message("Owner only in the configured server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await asyncio.to_thread(record_shouldnt, settings.db_path, interaction.user.id, settings.owner_id,
                                         self.job_id, str(self.reason.value))
        await interaction.followup.send(result, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


class ProposalButton(discord.ui.DynamicItem[discord.ui.Button],
                     template=r"screen:(?P<change>[a-f0-9]{32}):(?P<action>apply|discard)"):
    def __init__(self, change_id: str, action: str):
        super().__init__(discord.ui.Button(
            label="Apply" if action == "apply" else "Discard",
            style=discord.ButtonStyle.success if action == "apply" else discord.ButtonStyle.secondary,
            custom_id=proposal_custom_id(change_id, action)))
        self.change_id, self.action = change_id, action

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["change"], match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        settings = interaction.client.settings
        if not _owner(interaction, settings):
            await interaction.response.send_message("Owner only in the configured server.", ephemeral=True,
                                                    allowed_mentions=discord.AllowedMentions.none())
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await asyncio.to_thread(proposal_action, settings.db_path, interaction.user.id, settings.owner_id,
                                         self.change_id, self.action)
        await interaction.followup.send(result.message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        message = getattr(interaction, "message", None)
        if result.remove_buttons and message is not None:
            try:
                await message.edit(view=None)
            except discord.HTTPException:
                pass


class AskReplyModal(discord.ui.Modal, title="你的回复"):
    """The owner's own words for one of Claude's questions."""

    reply = discord.ui.TextInput(label="回复", style=discord.TextStyle.paragraph, min_length=1, max_length=1000,
                                 required=True)

    def __init__(self, question_id: int):
        super().__init__(timeout=900)
        self.question_id = question_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        settings = interaction.client.settings
        if not _owner(interaction, settings):
            await interaction.response.send_message("Owner only in the configured server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await asyncio.to_thread(ask_action, settings.db_path, interaction.user.id, settings.owner_id,
                                         self.question_id, "reply", str(self.reply.value))
        await interaction.followup.send(result.message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        await _close_controls(interaction, result.remove_buttons)


class AskButton(discord.ui.DynamicItem[discord.ui.Button],
                template=r"ask:(?P<question>[1-9][0-9]{0,11}):(?P<action>opt[0-3]|reply|skip)"):
    def __init__(self, question_id: int, action: str, label: str | None = None, row: int | None = None):
        style = (discord.ButtonStyle.secondary if action == "skip" else
                 discord.ButtonStyle.primary if action == "reply" else discord.ButtonStyle.success)
        default = {"reply": "其他回复…", "skip": "不投"}.get(action, action)
        super().__init__(discord.ui.Button(label=(label or default)[:80], style=style,
                                           custom_id=ask_custom_id(question_id, action), row=row))
        self.question_id, self.action = question_id, action

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["question"]), match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        settings = interaction.client.settings
        if not _owner(interaction, settings):
            await interaction.response.send_message("Owner only in the configured server.", ephemeral=True)
            return
        if self.action == "reply":  # a modal must be the interaction's first response
            await interaction.response.send_modal(AskReplyModal(self.question_id))
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await asyncio.to_thread(ask_action, settings.db_path, interaction.user.id, settings.owner_id,
                                         self.question_id, self.action)
        await interaction.followup.send(result.message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        await _close_controls(interaction, result.remove_buttons)


async def _close_controls(interaction, close: bool) -> None:
    message = getattr(interaction, "message", None)
    if close and message is not None:
        try:
            await message.edit(view=None)
        except discord.HTTPException:
            pass


class JobButton(discord.ui.DynamicItem[discord.ui.Button],
                template=r"job:(?P<job>[1-9][0-9]{0,11}):(?P<action>want|skip|applied|bad)"):
    def __init__(self, job_id: int, action: str, row: int | None = None):
        super().__init__(discord.ui.Button(label=LABELS[action], style=STYLES[action],
                                           custom_id=custom_id(job_id, action), row=row))
        self.job_id, self.action = job_id, action

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["job"]), match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        settings = interaction.client.settings
        if not _owner(interaction, settings):
            await interaction.response.send_message("Owner only in the configured server.", ephemeral=True)
            return
        if self.action == "bad":  # the reason first; a modal must be the interaction's first response
            await interaction.response.send_modal(ShouldntModal(self.job_id))
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await asyncio.to_thread(record_action, settings.db_path, interaction.user.id, settings.owner_id,
                                         self.job_id, self.action)
        await interaction.followup.send(result, ephemeral=True)


class JobBot(discord.Client):
    def __init__(self, settings: BotSettings):
        super().__init__(intents=discord.Intents.default())
        self.settings = settings
        self._handoff_lock = asyncio.Lock()
        self.tree = app_commands.CommandTree(self)
        self.target_guild = discord.Object(id=settings.guild_id)
        _commands(self)

    async def setup_hook(self) -> None:
        self.add_dynamic_items(JobButton, ProposalButton, AskButton)
        await self.tree.sync(guild=self.target_guild)
        self.digest.start()

    @tasks.loop(minutes=5)
    async def digest(self) -> None:
        for deliver in (self.post_digest, self.post_handoff_updates, self.post_screening_updates,
                        self.post_owner_questions):
            try:
                await deliver()
            except Exception:  # noqa: BLE001 - keep retrying; never log raw remote errors
                log.warning("Discord delivery failed; pending messages will be retried")

    async def post_handoff_updates(self) -> None:
        """Attempt outcomes do not depend on a new-job digest or a successful screening round."""
        from auto_app.core.db.store import DbStore
        from auto_app.ingest.scheduler import _read_state, _write_state
        from auto_app.settings import Paths
        from auto_app.worker.control import read_control
        from auto_app.worker.digest import handoff_batches

        db = self.settings.db_path
        async with self._handoff_lock:
            if is_paused(await asyncio.to_thread(read_control, db), utcnow()):
                return
            def plan():
                state = _read_state(db, ".bot-state.json")
                home = db.parent.parent if db.parent.name == "data" else db.parent
                store = DbStore(db, Paths(home=home), migrate=False)
                try:
                    return state, handoff_batches(store, state.get("handoff_cursor", 0))
                finally:
                    store.engine.dispose()
            state, batches = await asyncio.to_thread(plan)
            if not batches:
                return
            channel = self.get_channel(self.settings.channel_id) or await self.fetch_channel(self.settings.channel_id)
            if getattr(getattr(channel, "guild", None), "id", None) != self.settings.guild_id:
                raise ValueError("Digest channel does not belong to the configured server")
            for content, cursor, _ in batches:
                if is_paused(await asyncio.to_thread(read_control, db), utcnow()):
                    return
                await channel.send(content=content, allowed_mentions=discord.AllowedMentions.none())
                state["handoff_cursor"] = cursor
                await asyncio.to_thread(_write_state, db, state, ".bot-state.json")

    async def post_owner_questions(self) -> None:
        """Claude's questions for the owner, each once, in the questions channel (or the main one), with buttons;
        and, under each question, what Claude did with the answer. Not held back by a pause: the owner asked for
        these to reach them."""
        from auto_app.handoff.asks import OwnerQuestions

        questions = OwnerQuestions(self.settings.db_path)
        new, done = await asyncio.to_thread(lambda: (questions.to_post(), questions.to_report()))
        if not new and not done:
            return
        channel_id = self.settings.questions_channel_id or self.settings.channel_id
        channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        if getattr(getattr(channel, "guild", None), "id", None) != self.settings.guild_id:
            raise ValueError("Questions channel does not belong to the configured server")
        for item in new:
            view = discord.ui.View(timeout=None)
            for index, option in enumerate(item["options"]):
                view.add_item(AskButton(item["question_id"], f"opt{index}", option, row=0))
            view.add_item(AskButton(item["question_id"], "reply", row=1))
            if item.get("job_id") is not None:
                view.add_item(AskButton(item["question_id"], "skip", row=1))
            message = await channel.send(embeds=[discord.Embed.from_dict(ask_embed(item))], view=view,
                                         allowed_mentions=discord.AllowedMentions.none())
            await asyncio.to_thread(questions.mark_posted, item["question_id"], str(message.id))
        for item in done:
            label = f"#{item['job_id']} " if item.get("job_id") is not None else ""
            await channel.send(content=f"{label}已处理：{item['handled_note']}"[:1900],
                               reference=discord.MessageReference(message_id=int(item["message_id"]),
                                                                  channel_id=channel.id, fail_if_not_exists=False),
                               allowed_mentions=discord.AllowedMentions.none())
            await asyncio.to_thread(questions.mark_reported, item["question_id"])

    async def post_screening_updates(self) -> None:
        """Screening proposals made from the owner's reasons (with Apply / Discard), and reasons no rule fits."""
        from auto_app.ingest.scheduler import _read_state, _write_state
        from auto_app.screen.feedback import ScreeningFeedback
        from auto_app.worker.control import read_control

        db = self.settings.db_path
        if is_paused(await asyncio.to_thread(read_control, db), utcnow()):
            return

        def plan():
            state = _read_state(db, BOT_STATE)
            posted = set(state.get("posted_proposals") or [])
            feedback = ScreeningFeedback(db)
            proposals = [(change, proposal_message(db, change)) for change in feedback.open_proposals()
                         if change not in posted]
            return state, [(c, m) for c, m in proposals if m], feedback.unposted_closures()

        state, proposals, closures = await asyncio.to_thread(plan)
        if not proposals and not closures:
            return
        channel = self.get_channel(self.settings.channel_id) or await self.fetch_channel(self.settings.channel_id)
        if getattr(getattr(channel, "guild", None), "id", None) != self.settings.guild_id:
            raise ValueError("Digest channel does not belong to the configured server")
        for change, message in proposals:
            view = discord.ui.View(timeout=None)
            view.add_item(ProposalButton(change, "apply"))
            view.add_item(ProposalButton(change, "discard"))
            await channel.send(content=message[:1900], view=view, allowed_mentions=discord.AllowedMentions.none())
            state["posted_proposals"] = [*(state.get("posted_proposals") or []), change][-POSTED_KEEP:]
            await asyncio.to_thread(_write_state, db, state, BOT_STATE)
        if closures:
            await channel.send(content=closure_message(closures)[:1900],
                               allowed_mentions=discord.AllowedMentions.none())
            await asyncio.to_thread(ScreeningFeedback(db).mark_posted, [c["feedback_id"] for c in closures])

    async def post_digest(self) -> None:
        from auto_app.core.db.store import DbStore
        from auto_app.settings import Paths
        from auto_app.worker import loop
        from auto_app.worker.control import read_control
        from auto_app.worker.digest import embed, mark_notified, pending

        db = self.settings.db_path
        home = db.parent.parent if db.parent.name == "data" else db.parent

        def generation(state: dict) -> tuple[str | None, str | None]:
            return state.get("last_run_at"), state.get("last_finished_at")

        def select_jobs(job_id: int | None = None, expected_generation: tuple[str | None, str | None] | None = None):
            state = loop.status(db)
            round_generation = generation(state)
            now = utcnow()
            if not digest_ready(state, read_control(db), now):
                return None
            if expected_generation is not None and round_generation != expected_generation:
                return None
            try:
                since = datetime.fromisoformat(state["notify_since"])
            except (TypeError, ValueError):
                return None
            store = DbStore(db, Paths(home=home), migrate=False)
            try:
                jobs, more = pending(store, limit=30 if job_id is None else 1, since=since, now=now,
                                     source_since=state.get("source_since"), job_id=job_id)
            finally:
                store.engine.dispose()
            # Catch a round that finished while the database snapshot was being read.
            latest = loop.status(db)
            if generation(latest) != round_generation or not digest_ready(latest, read_control(db), utcnow()):
                return None
            return jobs, len(jobs) + more, round_generation

        def mark(job):
            store = DbStore(db, Paths(home=home), migrate=False)
            try:
                mark_notified(store, [job], utcnow())
            finally:
                store.engine.dispose()

        plan = await asyncio.to_thread(select_jobs)
        if plan is None or not plan[0]:
            return
        planned_jobs, total, planned_generation = plan
        channel = self.get_channel(self.settings.channel_id) or await self.fetch_channel(self.settings.channel_id)
        if getattr(getattr(channel, "guild", None), "id", None) != self.settings.guild_id:
            raise ValueError("Digest channel does not belong to the configured server")
        header: str | None = f"**{total} new job(s)** — visa sponsorship first"
        for candidate in planned_jobs:
            refreshed = await asyncio.to_thread(select_jobs, candidate["job_id"], planned_generation)
            if refreshed is None:
                return
            current_jobs, _, _ = refreshed
            if not current_jobs:
                continue
            [job] = current_jobs
            view = discord.ui.View(timeout=None)
            for action in BUTTONS:
                view.add_item(JobButton(job["job_id"], action, row=0))
            await channel.send(content=header, embed=discord.Embed.from_dict(embed(job)), view=view,
                               allowed_mentions=discord.AllowedMentions.none())
            await asyncio.to_thread(mark, job)
            header = None


def _owner(interaction: discord.Interaction, settings: BotSettings) -> bool:
    return interaction.guild_id == settings.guild_id and interaction.user.id == settings.owner_id


def _line(job: dict) -> str:
    return (f"#{job['job_id']} {job.get('title') or 'Untitled'} — {job.get('company') or '?'} "
            f"· {job.get('sponsorship') or 'unknown'} · {job.get('state') or 'unknown'}")


def _commands(bot: JobBot) -> None:
    guild = bot.target_guild

    async def authorized(interaction: discord.Interaction) -> bool:
        if not _owner(interaction, interaction.client.settings):
            await interaction.response.send_message("Owner only in the configured server.", ephemeral=True)
            return False
        await interaction.response.defer(ephemeral=True, thinking=True)
        return True

    @bot.tree.command(name="jobs", description="Open jobs, optionally filtered by visa, company, or keyword",
                      guild=guild)
    @app_commands.choices(sponsorship=[app_commands.Choice(name=value, value=value)
                                       for value in ("offers", "unknown", "no")])
    async def jobs(interaction: discord.Interaction, sponsorship: app_commands.Choice[str] | None = None,
                   company: str | None = None, query: str | None = None) -> None:
        if not await authorized(interaction):
            return
        rows = await asyncio.to_thread(jobs_summary, interaction.client.settings.db_path,
                                       sponsorship=sponsorship.value if sponsorship else None,
                                       company=company, query=query)
        result = "\n".join(_line(row) for row in rows) or "No matching open jobs."
        await interaction.followup.send(result[:1900], ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    @bot.tree.command(name="job", description="Show one job by number", guild=guild)
    async def job(interaction: discord.Interaction, job_id: int) -> None:
        if not await authorized(interaction):
            return
        try:
            row = await asyncio.to_thread(job_summary, interaction.client.settings.db_path, job_id)
        except ValueError:
            row = None
        result = (_line(row) + (f"\n{row['url']}" if row.get("url") else "")) if row else "Job not found."
        await interaction.followup.send(result[:1900], ephemeral=True,
                                        allowed_mentions=discord.AllowedMentions.none())

    @bot.tree.command(name="status", description="Feed and worker status", guild=guild)
    async def status(interaction: discord.Interaction) -> None:
        if not await authorized(interaction):
            return
        from auto_app.agent.control import ControlService

        state = await asyncio.to_thread(ControlService(interaction.client.settings.db_path).pipeline_status)
        result = (f"ingest: {state['ingest']['status']} (next {state['ingest']['next_run_at']})\n"
                  f"worker: {state['worker']['status']} (next {state['worker']['next_run_at']}, "
                  f"failures {state['worker']['consecutive_failures']})\n"
                  f"paused: {state['control']['paused']} {state['control'].get('paused_until') or ''}")
        await interaction.followup.send(result, ephemeral=True)

    @bot.tree.command(name="pause", description="Pause the worker for N hours (0 = until resumed)", guild=guild)
    async def pause(interaction: discord.Interaction, hours: app_commands.Range[int, 0, 720] = 0) -> None:
        if not await authorized(interaction):
            return
        from auto_app.agent.control import ControlService

        until = (utcnow() + timedelta(hours=hours)).isoformat() if hours else None
        result = await asyncio.to_thread(ControlService(interaction.client.settings.db_path).pause,
                                         until, "via Discord")
        await interaction.followup.send(f"Paused until {result['paused_until'] or 'resumed'}.", ephemeral=True)

    @bot.tree.command(name="resume", description="Resume the worker", guild=guild)
    async def resume(interaction: discord.Interaction) -> None:
        if not await authorized(interaction):
            return
        from auto_app.agent.control import ControlService

        await asyncio.to_thread(ControlService(interaction.client.settings.db_path).resume)
        await interaction.followup.send("Resumed.", ephemeral=True)


def run(settings: BotSettings) -> None:
    JobBot(settings).run(settings.token, log_handler=None)

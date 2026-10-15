"""Discord buttons, slash commands, and a five-minute digest loop."""

import asyncio
import logging
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import tasks

from auto_app.bot.actions import (
    ACTIONS,
    LABELS,
    BotSettings,
    custom_id,
    job_summary,
    jobs_summary,
    record_action,
)
from auto_app.core.models import utcnow
from auto_app.worker.control import is_paused

log = logging.getLogger(__name__)
STYLES = {"want": discord.ButtonStyle.success, "skip": discord.ButtonStyle.secondary,
          "applied": discord.ButtonStyle.primary}


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


class JobButton(discord.ui.DynamicItem[discord.ui.Button],
                template=r"job:(?P<job>[1-9][0-9]{0,11}):(?P<action>want|skip|applied)"):
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
        self.add_dynamic_items(JobButton)
        await self.tree.sync(guild=self.target_guild)
        self.digest.start()

    @tasks.loop(minutes=5)
    async def digest(self) -> None:
        for deliver in (self.post_digest, self.post_handoff_updates):
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

    async def post_digest(self) -> None:
        from auto_app.core.db.store import DbStore
        from auto_app.settings import Paths
        from auto_app.worker import loop
        from auto_app.worker.control import read_control
        from auto_app.worker.digest import batches, embed, mark_notified, pending

        now = utcnow()

        def generation(state: dict) -> tuple[str | None, str | None]:
            return state.get("last_run_at"), state.get("last_finished_at")

        def plan():
            state = loop.status(self.settings.db_path)
            control = read_control(self.settings.db_path)
            if not digest_ready(state, control, now):
                return [], None
            try:
                since = datetime.fromisoformat(state["notify_since"])
            except (TypeError, ValueError):
                return [], None
            db_path = self.settings.db_path
            home = db_path.parent.parent if db_path.parent.name == "data" else db_path.parent
            store = DbStore(db_path, Paths(home=home), migrate=False)
            try:
                planned, _ = batches(store, limit=30, since=since, now=now,
                                     source_since=state.get("source_since"))
                return planned, generation(state)
            finally:
                store.engine.dispose()

        def revalidate(batch: list[dict], expected_generation: tuple[str | None, str | None]):
            state = loop.status(self.settings.db_path)
            control = read_control(self.settings.db_path)
            current = utcnow()
            if not digest_ready(state, control, current) or generation(state) != expected_generation:
                return None
            try:
                since = datetime.fromisoformat(state["notify_since"])
            except (TypeError, ValueError):
                return None
            db_path = self.settings.db_path
            home = db_path.parent.parent if db_path.parent.name == "data" else db_path.parent
            store = DbStore(db_path, Paths(home=home), migrate=False)
            try:
                jobs, more = pending(store, limit=30, since=since, now=current,
                                     source_since=state.get("source_since"))
            finally:
                store.engine.dispose()
            # Catch a round that finished while the database snapshot was being read.
            latest = loop.status(self.settings.db_path)
            if (generation(latest) != expected_generation
                    or not digest_ready(latest, read_control(self.settings.db_path), utcnow())):
                return None
            eligible = {job["job_id"]: job for job in jobs}
            return [eligible[job["job_id"]] for job in batch if job["job_id"] in eligible], len(jobs) + more

        def mark(batch):
            db_path = self.settings.db_path
            home = db_path.parent.parent if db_path.parent.name == "data" else db_path.parent
            store = DbStore(db_path, Paths(home=home), migrate=False)
            try:
                mark_notified(store, batch, now)
            finally:
                store.engine.dispose()

        plan_batches, planned_generation = await asyncio.to_thread(plan)
        if not plan_batches:
            return
        channel = self.get_channel(self.settings.channel_id) or await self.fetch_channel(self.settings.channel_id)
        if getattr(getattr(channel, "guild", None), "id", None) != self.settings.guild_id:
            raise ValueError("Digest channel does not belong to the configured server")
        sent = 0
        for batch in plan_batches:
            refreshed = await asyncio.to_thread(revalidate, batch, planned_generation)
            if refreshed is None:
                return
            current_batch, remaining = refreshed
            if not current_batch:
                continue
            view = discord.ui.View(timeout=None)
            for row, job in enumerate(current_batch):
                for action in ACTIONS:
                    view.add_item(JobButton(job["job_id"], action, row=row))
            await channel.send(content=f"**{remaining} new job(s)** — visa sponsorship first" if sent == 0 else None,
                               embeds=[discord.Embed.from_dict(embed(job)) for job in current_batch], view=view,
                               allowed_mentions=discord.AllowedMentions.none())
            await asyncio.to_thread(mark, current_batch)
            sent += len(current_batch)


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

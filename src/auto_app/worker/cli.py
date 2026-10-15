# src/auto_app/worker/cli.py
"""`auto-app worker`: screen, enrich and tell you on Discord — on the Linux host (docs/linux-worker.md)."""

import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import typer

from auto_app.settings import get_paths
from auto_app.core.discord import redact_webhook_secrets
from auto_app.worker.round import STEPS, WorkerOptions

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="The Linux worker: screen jobs, fetch missing descriptions, tell you on Discord.")
DbOption = Annotated[Path | None, typer.Option("--db", help="Database file; defaults to AUTO_APP_HOME/data/auto_app.db.")]
StepsOption = Annotated[str, typer.Option("--steps", help=f"Comma-separated steps: {', '.join(STEPS)}.")]
EnrichOption = Annotated[int, typer.Option("--enrich-limit", min=0, max=200, help="Most employer API requests per round.")]
SinceOption = Annotated[str | None, typer.Option(
    "--notify-since", help="Announce jobs first seen since this ISO time (default: the first round).")]
DiskOption = Annotated[float, typer.Option("--min-free-gb", min=0, help="Alert below this much free database disk space (GiB).")]


def _json(report: dict) -> None:
    typer.echo(json.dumps(report, ensure_ascii=False))


def _options(steps: str, enrich_limit: int, min_free_gb: float = 2.0) -> WorkerOptions:
    chosen = tuple(s.strip() for s in steps.split(",") if s.strip())
    unknown = sorted(set(chosen) - set(STEPS))
    if not chosen:
        raise typer.BadParameter("choose at least one step", param_hint="--steps")
    if unknown:
        raise typer.BadParameter(f"unknown steps {unknown}; choose from {', '.join(STEPS)}", param_hint="--steps")
    return WorkerOptions(screening_path=get_paths().home / "config" / "screening.json", enrich_limit=enrich_limit,
                         steps=chosen, min_free_gb=min_free_gb)


def _since(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("--notify-since must be an ISO date/time") from None
    return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).isoformat()


def _round_fn(db_path: Path, opts: WorkerOptions, notify_since: str | None):
    def run(state: dict) -> dict:
        from auto_app.core.db.store import DbStore
        from auto_app.core.discord import WEBHOOK_ENV, Discord, DiscordError
        from auto_app.core.models import utcnow
        from auto_app.enrich.fetch import Fetcher
        from auto_app.ingest import scheduler
        from auto_app.worker.digest import send_alerts
        from auto_app.worker import loop
        from auto_app.worker.round import run_round

        if notify_since:
            state["notify_since"] = notify_since
            scheduler._write_state(db_path, state, loop.STATE)
        store = DbStore(db_path, get_paths())
        fetcher = Fetcher() if "enrich" in opts.steps else None
        discord, discord_error = None, None
        if {"notify", "enrich", "summary", "health"}.intersection(opts.steps) and os.environ.get(WEBHOOK_ENV):
            try:
                discord = Discord(os.environ[WEBHOOK_ENV])
            except DiscordError as exc:
                discord_error = str(exc)
        try:
            now = utcnow()
            report = run_round(store, opts, fetcher=fetcher, discord=discord, state=state, now=now,
                               save_state=lambda: scheduler._write_state(db_path, state, loop.STATE))
            if discord_error:
                report["discord_error"] = discord_error
                report["status"] = "partial"
                error_step = "notify" if "notify" in opts.steps else "alerts"
                report["steps"][error_step] = {"status": "error", "error": discord_error}
            report["alerts"] = send_alerts(discord, state, report, scheduler.status(db_path), now)
            return report
        finally:
            if fetcher is not None:
                fetcher.close()
            if discord is not None:
                discord.close()
            store.engine.dispose()
    return run


def _problem_alert(message: str) -> None:
    """Tell Discord about a problem found outside a round (rounds alert through send_alerts)."""
    from auto_app.core.discord import WEBHOOK_ENV, DiscordError
    import auto_app.core.discord as discord_module

    if not os.environ.get(WEBHOOK_ENV):
        return
    try:
        discord = discord_module.Discord(os.environ[WEBHOOK_ENV])
        try:
            discord.post(f"⚠️ {message}"[:2000])
        finally:
            discord.close()
    except DiscordError:
        pass  # the state file and `worker status` still report it


def _watch(db: Path | None, opts: WorkerOptions, *, interval: int, once: bool, notify_since: str | None) -> dict:
    from auto_app.worker import loop

    db_path = db or get_paths().db
    stopped = False

    def request_stop(*_):
        nonlocal stopped
        stopped = True

    previous = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        return loop.watch(db_path, _round_fn(db_path, opts, _since(notify_since)), interval_seconds=interval,
                          once=once, should_stop=lambda: stopped,
                          sleep=lambda seconds: time.sleep(min(seconds, 0.25)), emit=_json,
                          on_problem=_problem_alert)
    finally:
        signal.signal(signal.SIGTERM, previous)


@app.command("run")
def run(db: DbOption = None, steps: StepsOption = ",".join(STEPS), enrich_limit: EnrichOption = 200,
        notify_since: SinceOption = None, min_free_gb: DiskOption = 2.0) -> None:
    """One round now: screen → enrich → screen again → Discord. Prints the round's JSON report."""
    try:
        result = _watch(db, _options(steps, enrich_limit, min_free_gb), interval=900, once=True, notify_since=notify_since)
    except (OSError, ValueError) as exc:
        _json({"status": "error", "error": redact_webhook_secrets(str(exc))})
        raise typer.Exit(1)
    if result["status"] == "control_invalid":  # no round ran, so nothing was emitted
        _json({"status": "control_invalid", "error": result.get("control_error")})
    if result["status"] != "ok":
        raise typer.Exit(1)


@app.command("watch")
def watch(db: DbOption = None, steps: StepsOption = ",".join(STEPS), enrich_limit: EnrichOption = 200,
          notify_since: SinceOption = None,
          min_free_gb: DiskOption = 2.0,
          interval: Annotated[int, typer.Option("--interval", min=60, help="Seconds between rounds.")] = 900) -> None:
    """Rounds forever (systemd runs this): one now, then one every --interval seconds."""
    try:
        _watch(db, _options(steps, enrich_limit, min_free_gb), interval=interval, once=False, notify_since=notify_since)
    except (OSError, ValueError) as exc:
        _json({"status": "error", "error": redact_webhook_secrets(str(exc))})
        raise typer.Exit(1)


@app.command("status")
def status(db: DbOption = None) -> None:
    """Last/next round, its report, failures and whether a worker is running — without opening the database."""
    from auto_app.worker import loop

    report = loop.status(db or get_paths().db)
    _json(report)
    if report["status"] == "error" and "error" in report:
        raise typer.Exit(1)


@app.command("boards")
def boards(db: DbOption = None,
           top: Annotated[int, typer.Option("--top", min=1, max=200, help="How many boards to list.")] = 30,
           min_jobs: Annotated[int, typer.Option("--min-jobs", min=1, help="Skip boards with fewer kept jobs.")] = 2,
           yaml_out: Annotated[bool, typer.Option("--yaml", help="Print feeds.yaml entries instead of JSON.")] = False) -> None:
    """Employer boards worth adding as feeds, ranked by kept jobs that sponsor visas. Read-only."""
    from auto_app.core.db.store import DbStore
    from auto_app.ingest.runner import load_feed_config
    from auto_app.worker.boards import as_yaml, suggest_boards

    paths = get_paths()
    feeds = paths.home / "config" / "feeds.yaml"
    configured = ({d["location"] for d in load_feed_config(feeds).values() if d.get("location")}
                  if feeds.exists() else set())
    store = DbStore(db or paths.db, paths, migrate=False)
    try:
        found = suggest_boards(store, configured=configured, top=top, min_jobs=min_jobs)
    finally:
        store.engine.dispose()
    typer.echo(as_yaml(found) if yaml_out else json.dumps(found, ensure_ascii=False, indent=2))

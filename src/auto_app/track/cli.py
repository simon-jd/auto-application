"""Application timelines and mail review; machine-readable JSON output."""

import json
from datetime import datetime, timezone
from typing import Annotated

import typer

from auto_app.settings import get_paths

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Application timeline and mail updates to review.")


def _store():
    from auto_app.core.db.store import DbStore

    paths = get_paths()
    return DbStore(paths.db, paths)


def _run(operation):
    store = None
    try:
        store = _store()
        typer.echo(json.dumps(operation(store), ensure_ascii=False, indent=2, default=str))
    except ValueError as exc:
        typer.echo(json.dumps({"status": "error", "error": str(exc)}))
        raise typer.Exit(1)
    finally:
        if store is not None:
            store.engine.dispose()


@app.command("add")
def add(job: int, kind: str,
        at: Annotated[str | None, typer.Option("--at", help="ISO date/time; naive times use UTC; default now.")] = None,
        note: Annotated[str | None, typer.Option("--note")] = None) -> None:
    """Add an event: applied, assessment, interview, rejected, offer or withdrawn."""
    from auto_app.track.events import events_for, record_event

    def operation(store):
        try:
            when = datetime.fromisoformat(at) if at is not None else None
        except ValueError:
            raise ValueError("at must be an ISO date/time with timezone") from None
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        record_event(store, job, kind, occurred_at=when, note=note)
        return events_for(store, job)
    _run(operation)


@app.command("list")
def list_(job: int) -> None:
    """One job's timeline."""
    from auto_app.track.events import events_for

    _run(lambda store: events_for(store, job))


@app.command("unmatched")
def unmatched_() -> None:
    """Mail updates waiting for your review."""
    from auto_app.track.updates import unmatched

    _run(unmatched)


@app.command("assign")
def assign_(update: int, job: int) -> None:
    """Attach a mail update to a saved job and record its event."""
    from auto_app.track.updates import assign

    _run(lambda store: assign(store, update, job))


@app.command("dismiss")
def dismiss_(update: int) -> None:
    """Dismiss a mail update that is not about an application."""
    from auto_app.track.updates import dismiss

    _run(lambda store: dismiss(store, update))

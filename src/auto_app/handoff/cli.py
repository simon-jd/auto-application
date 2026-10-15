"""`auto-app handoff`: the interface your own application agent uses (docs/agent-handoff.md). JSON out."""

import json
from pathlib import Path
from typing import Annotated

import typer

from auto_app.settings import get_paths

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="Queue, claims, packets and outcomes for your own application agent.")


def _service():
    from auto_app.handoff.service import HandoffService

    return HandoffService(get_paths().db)


def _out(operation):
    from auto_app.handoff.service import HandoffError

    try:
        typer.echo(json.dumps(operation(), ensure_ascii=False, indent=2, default=str))
    except (HandoffError, ValueError) as exc:
        typer.echo(json.dumps({"status": "error", "error": str(exc)}))
        raise typer.Exit(1)


@app.command("queue")
def queue(limit: Annotated[int, typer.Option("--limit", min=1, max=100)] = 20) -> None:
    """Jobs your agent may apply to now, oldest decision first, after every guardrail."""
    _out(lambda: _service().queue(limit))


@app.command("claim")
def claim(job: int, agent: Annotated[str, typer.Option("--agent", help="A short name for your agent.")]) -> None:
    """Claim one job; the token is valid for the configured lease."""
    _out(lambda: _service().claim(job, agent))


@app.command("packet")
def packet(token: str) -> None:
    """Everything needed to apply to the claimed job — never account passwords."""
    _out(lambda: _service().packet(token))


@app.command("record")
def record(token: str, outcome: str, note: Annotated[str | None, typer.Option("--note")] = None,
           evidence: Annotated[Path | None, typer.Option("--evidence", help="A file under $AUTO_APP_HOME/evidence/.")] = None) -> None:
    """Record the outcome: submitted, needs_user, blocked, failed or skipped."""
    _out(lambda: _service().record_attempt(token, outcome, note, str(evidence) if evidence else None))


@app.command("release")
def release(token: str) -> None:
    """Give a claim back without an outcome."""
    _out(lambda: _service().release(token))


@app.command("requeue")
def requeue(job: int) -> None:
    """Put a job that waited for you (needs_user / blocked) back into the queue."""
    _out(lambda: _service().requeue(job))


@app.command("account")
def account(site: str, email: str, existing: Annotated[bool, typer.Option("--existing", help="You created it, not the agent.")] = False,
            note: Annotated[str | None, typer.Option("--note")] = None) -> None:
    """Note that a site account exists (never its password)."""
    _out(lambda: _service().record_account(site, email, not existing, note))


@app.command("resume")
def resume(job: int, path: Path, note: Annotated[str | None, typer.Option("--note")] = None) -> None:
    """Register a resume file (under $AUTO_APP_HOME/resumes/) made for one job."""
    _out(lambda: _service().register_resume(job, str(path), note))

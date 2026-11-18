"""`auto-app screen-feedback`: the daily summary of the user's "shouldn't recommend" reasons (deploy/screening-feedback.sh).

It lists the pending reasons with the current rules, previews a change without saving it, saves a rule-change
proposal for some of them (the Discord bot shows it with Apply / Discard), or closes the ones no rule can express. It never applies a change. Output is one JSON
object; an error is {"status": "error", "error": "..."} with exit status 1.
"""

import json
from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="The user's 'shouldn't recommend' reasons from Discord, turned into screening-rule proposals.")
DbOpt = Annotated[Path | None, typer.Option("--db", help="Database file (default: the runtime's).")]
IdsOpt = Annotated[str, typer.Option("--ids", help="Feedback ids, comma-separated, e.g. 3,5,8.")]


def _db(db: Path | None) -> Path:
    from auto_app.settings import get_paths

    return db or get_paths().db


def _emit(value: dict) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, default=str))


def _fail(message: str) -> None:
    _emit({"status": "error", "error": message})
    raise typer.Exit(1)


def _parse_ids(text: str) -> list[int]:
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if not parts or not all(p.isascii() and p.isdigit() for p in parts):
        _fail("--ids must be feedback ids separated by commas, e.g. 3,5,8")
    return [int(p) for p in parts]


@app.command("due")
def due(db: DbOpt = None) -> None:
    """Whether the summary should run: 3 or more pending reasons, or the oldest waited 3 days."""
    from auto_app.screen.feedback import ScreeningFeedback

    feedback = ScreeningFeedback(_db(db))
    _emit({"due": feedback.due(), "pending": len(feedback.pending())})


@app.command("list")
def list_(db: DbOpt = None) -> None:
    """The pending reasons (each with the job's title, company and screening verdict), and the current rules."""
    from auto_app.screen.feedback import ScreeningFeedback

    feedback = ScreeningFeedback(_db(db))
    _emit({"items": feedback.pending(), "rules": feedback.control.screening_rules()})


@app.command("preview")
def preview(changes: Annotated[str, typer.Option("--changes", help="Rule changes as a JSON object, as for propose.")],
            db: DbOpt = None) -> None:
    """What a change would do (kept-job counts and examples), saving nothing and holding no reasons: try narrower
    words here, then propose once."""
    from auto_app.agent.control import ControlError, ControlService

    try:
        parsed = json.loads(changes)
    except ValueError:
        _fail("--changes is not valid JSON")
    control = ControlService(_db(db))
    try:
        proposal = control.propose_screening_change(parsed)
    except (ControlError, ValueError) as exc:
        _fail(str(exc))
    control.discard_screening_change(proposal["change_id"])  # a preview leaves nothing that could be applied
    _emit({"diff": proposal["diff"], "preview": proposal["preview"]})


@app.command("propose")
def propose(changes: Annotated[str, typer.Option("--changes", help='Rule changes as a JSON object, e.g. '
                                                 '\'{"extra_non_cs_terms": [...]}\' (the whole new list).')],
            ids: IdsOpt,
            note: Annotated[str | None, typer.Option("--note", help="For the user: what this answers, and why.")]
            = None,
            db: DbOpt = None) -> None:
    """Save a proposal with its preview for these reasons; the user applies or discards it on Discord."""
    from auto_app.agent.control import ControlError
    from auto_app.screen.feedback import FeedbackError, ScreeningFeedback

    wanted = _parse_ids(ids)
    try:
        parsed = json.loads(changes)
    except ValueError:
        _fail("--changes is not valid JSON")
    feedback = ScreeningFeedback(_db(db))
    try:
        proposal = feedback.control.propose_screening_change(parsed, note=note)
    except (ControlError, ValueError) as exc:
        _fail(str(exc))
    try:
        attached = feedback.attach(wanted, proposal["change_id"])
    except FeedbackError as exc:
        feedback.control.discard_screening_change(proposal["change_id"])  # no orphan proposal
        _fail(str(exc))
    _emit({**proposal, "attached": attached})


@app.command("close")
def close(ids: IdsOpt,
          note: Annotated[str, typer.Option("--note", help="For the user: why no rule change fits these reasons.")],
          db: DbOpt = None) -> None:
    """Close reasons that no screening rule can express (posted to Discord with the note)."""
    from auto_app.screen.feedback import FeedbackError, ScreeningFeedback

    wanted = _parse_ids(ids)
    try:
        _emit({"closed": ScreeningFeedback(_db(db)).close(wanted, note)})
    except FeedbackError as exc:
        _fail(str(exc))

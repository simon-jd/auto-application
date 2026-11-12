"""Commands for importing repository feeds and saved recommended-job emails."""

import json
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import httpx
from sqlalchemy.exc import SQLAlchemyError
import typer
import yaml

from auto_app.core.db.store import DbStore
from auto_app.ingest.runner import load_feed_config, resolve_location, run_config, run_email, run_repository, validate_source
from auto_app.ingest.scheduler import import_lock
from auto_app.ingest.repository import validate_repository_format
from auto_app.settings import get_paths

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="Import repository feeds and recommended-job emails, or run them on a schedule.")
DbOption = Annotated[Path | None, typer.Option("--db", help="Database file; defaults to AUTO_APP_HOME/data/auto_app.db.")]
SourceOption = Annotated[str, typer.Option("--source", help="Stable source name for repeat imports.")]
ERRORS = (OSError, ValueError, httpx.HTTPError, yaml.YAMLError, SQLAlchemyError)


def _fail(exc: Exception, *, code: int) -> None:
    report = {"status": "error", "error": str(exc)}
    typer.echo(json.dumps(report, ensure_ascii=False))
    typer.echo(str(exc), err=True)
    raise typer.Exit(code)


def _emit(report: dict) -> None:
    typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
    if report.get("status") != "ok":
        typer.echo(f"Import finished with {report['status']} results; see JSON errors and issues.", err=True)
        for message in _diagnostics(report):
            typer.echo(message, err=True)
        raise typer.Exit(1)


def _diagnostics(report: dict):
    children = list(report.get("sources", {}).values()) or report.get("files", [])
    if children:
        for child in children:
            yield from _diagnostics(child)
        return
    label = report.get("source") or report.get("path") or "input"
    if report.get("error"):
        yield f"{label}: {report['error']}"
    for issue in report.get("issues", []):
        yield f"{label}: {issue.get('reason', str(issue))}"


def _execute(db: Path | None, operation) -> None:
    store = None
    try:
        paths = get_paths()
        with import_lock(db or paths.db):
            store = DbStore(db or paths.db, paths)
            report = operation(store)
    except ERRORS as exc:
        _fail(exc, code=1)
    finally:
        if store is not None:
            store.engine.dispose()
    _emit(report)


@app.command("repository")
def repository(location: str, source: SourceOption, db: DbOption = None,
               format: Annotated[str, typer.Option("--format", help="simplify, speedyapply, applyguy, ricsign, jobright, or an employer board: greenhouse-board, lever-board, ashby-board, workday-board, eightfold-board, icims-jibe-board, oracle-board, smartrecruiters-board.")] = "simplify") -> None:
    """Import a supported repository JSON or Markdown snapshot."""
    try:
        validate_source(source)
        validate_repository_format(format)
        location = resolve_location(location)
        if not urlparse(location).scheme and not Path(location).is_file():
            raise FileNotFoundError(f"feed file not found: {location}")
    except ERRORS as exc:
        _fail(exc, code=2)
    _execute(db, lambda store: run_repository(store, source, location, format=format))


@app.command("email")
def email(files: Annotated[list[Path], typer.Argument(help="Saved .eml files.")],
          source: SourceOption, db: DbOption = None) -> None:
    """Import individual saved messages without connecting to a mailbox."""
    try:
        validate_source(source)
        if not any(path.expanduser().is_file() for path in files):
            raise FileNotFoundError("no saved email files found: " + ", ".join(str(path) for path in files))
    except ERRORS as exc:
        _fail(exc, code=2)
    _execute(db, lambda store: run_email(store, source, files))


@app.command("run")
def run(config: Annotated[Path, typer.Option("--config", help="YAML file containing feed sources.")],
        db: DbOption = None) -> None:
    """Import enabled sources from a feed configuration, reporting each result."""
    try:
        load_feed_config(config)
    except ERRORS as exc:
        _fail(exc, code=2)
    _execute(db, lambda store: run_config(store, config))


from auto_app.ingest.schedule_cli import register as register_schedule

register_schedule(app)

from auto_app.ingest.auth_cli import register as register_auth

register_auth(app)

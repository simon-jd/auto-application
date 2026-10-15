"""Operations CLI; backup verifies every copy before pruning."""

import json
import os
from pathlib import Path
from typing import Annotated

import typer

from auto_app.settings import get_paths

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Backups, restore checks and failure alerts.")


@app.command("backup")
def backup_(dest: Annotated[Path | None, typer.Option("--dest")] = None,
            keep_daily: Annotated[int, typer.Option("--keep-daily", min=1)] = 14,
            keep_weekly: Annotated[int, typer.Option("--keep-weekly", min=0)] = 8) -> None:
    """Back up jobs and an existing preparation ledger, verify, then prune."""
    from auto_app.ops.backup import backup, prune, verify

    paths = get_paths()
    target = dest or paths.home / "backups"
    entry = backup(paths.db, target)
    entry["verify"] = verify(Path(entry["path"]))
    preparation = paths.home / "preparation/preparation.sqlite3"
    if preparation.exists():
        saved = backup(preparation, target)
        saved["verify"] = verify(Path(saved["path"]))
        entry["preparation"] = saved
    entry["pruned"] = prune(target, keep_daily=keep_daily, keep_weekly=keep_weekly)
    typer.echo(json.dumps(entry, indent=2))


@app.command("verify")
def verify_(path: Path) -> None:
    """Read a backup and report integrity, count and compatible schema."""
    from auto_app.ops.backup import verify

    typer.echo(json.dumps(verify(path), indent=2))


@app.command("prune")
def prune_(dest: Annotated[Path | None, typer.Option("--dest")] = None,
           keep_daily: Annotated[int, typer.Option("--keep-daily", min=1)] = 14,
           keep_weekly: Annotated[int, typer.Option("--keep-weekly", min=0)] = 8) -> None:
    """Apply retention to recognized backup files only."""
    from auto_app.ops.backup import prune

    typer.echo(json.dumps(prune(dest or get_paths().home / "backups", keep_daily=keep_daily,
                                keep_weekly=keep_weekly), indent=2))


@app.command("alert")
def alert(unit: Annotated[str, typer.Option("--unit")]) -> None:
    """Send a minimal service-failure alert to Discord."""
    from auto_app.ops.alert import send_failure

    raise typer.Exit(send_failure(unit, os.environ))

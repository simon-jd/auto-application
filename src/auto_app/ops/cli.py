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
    """Back up jobs, an existing preparation ledger and the apply-kit folder next to the runtime, verify, then
    prune."""
    from auto_app.ops.backup import backup, backup_tree, prune, verify, verify_tree

    paths = get_paths()
    target = dest or paths.home / "backups"
    entry = backup(paths.db, target)
    entry["verify"] = verify(Path(entry["path"]))
    preparation = paths.home / "preparation/preparation.sqlite3"
    if preparation.exists():
        saved = backup(preparation, target)
        saved["verify"] = verify(Path(saved["path"]))
        entry["preparation"] = saved
    kit = paths.home.parent / "apply-kit"  # published packages, frozen submitted copies, the cheat sheet
    if kit.is_dir() and not kit.is_symlink():
        saved = backup_tree(kit, target)
        saved["verify"] = verify_tree(Path(saved["path"]))
        entry["apply_kit"] = saved
    entry["pruned"] = prune(target, keep_daily=keep_daily, keep_weekly=keep_weekly)
    typer.echo(json.dumps(entry, indent=2))


@app.command("verify")
def verify_(path: Path) -> None:
    """Read a backup and report integrity, count and compatible schema (or, for an apply-kit archive, its files)."""
    from auto_app.ops.backup import verify, verify_tree

    typer.echo(json.dumps(verify_tree(path) if path.name.endswith(".tar.zst") else verify(path), indent=2))


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

"""Feed worker CLI and a generated user systemd unit; no installation side effects."""

import json
from pathlib import Path
import shutil
import signal
import time
from typing import Annotated

import typer

from auto_app.ingest import scheduler
from auto_app.service import _quote
from auto_app.settings import PROJECT_ROOT, get_paths


def build_systemd_unit(project: Path, home: Path, uv: str, *, config: Path | None = None,
                       db: Path | None = None, interval_seconds: int = 1800) -> str:
    """Generate paths for a target host without following local host symlinks."""
    scheduler.validate_interval(interval_seconds)
    for value in (project, home, uv, config, db):
        if value is not None and any(ord(char) < 32 or ord(char) == 127 for char in str(value)):
            raise ValueError("service paths must not contain control characters")
    project, home = Path(project).expanduser().absolute(), Path(home).expanduser().absolute()
    uv = str(Path(uv).expanduser().absolute())
    config = Path(config).expanduser().absolute() if config is not None else home / "config" / "feeds.yaml"
    db = Path(db).expanduser().absolute() if db is not None else home / "data" / "auto_app.db"
    directory = str(project).rstrip("/").replace("%", "%%") + "/"
    return f"""[Unit]
Description=Auto-app repository and recommended-job mail ingestion
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
WorkingDirectory={directory}
Environment={_quote(f'AUTO_APP_HOME={home}')}
ExecStart=:{_quote(uv)} run --project {_quote(str(project))} auto-app ingest watch --config {_quote(str(config))} --db {_quote(str(db))} --interval {interval_seconds:g}
Restart=on-failure
RestartSec=30
KillMode=control-group
TimeoutStopSec=300

[Install]
WantedBy=default.target
"""


DbOption = Annotated[Path | None, typer.Option("--db", help="Database file; defaults to AUTO_APP_HOME/data/auto_app.db.")]
ConfigOption = Annotated[Path | None, typer.Option("--config", help="Feed YAML; defaults to AUTO_APP_HOME/config/feeds.yaml.")]
IntervalOption = Annotated[int, typer.Option("--interval", min=1, help="Seconds between completed rounds (default: 30 minutes).")]


def _json(report: dict) -> None:
    typer.echo(json.dumps(report, ensure_ascii=False))


def register(app: typer.Typer) -> None:
    @app.command("watch")
    def watch(config: ConfigOption = None, db: DbOption = None, interval: IntervalOption = 1800,
              once: Annotated[bool, typer.Option("--once", help="Run one immediate round and exit.")] = False) -> None:
        """Continuously import enabled feeds without browser or model startup."""
        paths, stopped = get_paths(), False

        def request_stop(*_):
            nonlocal stopped
            # Signal handlers can reenter: Event.set() takes a non-reentrant lock.
            stopped = True

        previous = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, request_stop)
        try:
            result = scheduler.watch(config or paths.home / "config" / "feeds.yaml", db or paths.db,
                                     interval_seconds=interval, once=once, should_stop=lambda: stopped,
                                     sleep=lambda seconds: time.sleep(min(seconds, 0.25)), emit=_json)
        except (OSError, ValueError) as exc:
            _json({"status": "error", "error": str(exc)})
            typer.echo(str(exc), err=True)
            raise typer.Exit(1)
        finally:
            signal.signal(signal.SIGTERM, previous)
        if once and result["status"] != "ok":
            raise typer.Exit(1)

    @app.command("status")
    def status(db: DbOption = None) -> None:
        """Read last/next run, failures and process state without opening the DB."""
        report = scheduler.status(db or get_paths().db)
        _json(report)
        if report["status"] == "error" and "error" in report:
            raise typer.Exit(1)

    @app.command("systemd")
    def systemd(project: Annotated[Path | None, typer.Option("--project", help="Project checkout path on Linux.")] = None,
                home: Annotated[Path | None, typer.Option("--home", help="Runtime home path on Linux.")] = None,
                config: ConfigOption = None, db: DbOption = None,
                uv: Annotated[str | None, typer.Option("--uv", help="Absolute uv executable path on Linux.")] = None,
                interval: IntervalOption = 1800) -> None:
        """Print a user service for systemctl --user; does not install or start it."""
        executable = uv or shutil.which("uv")
        if not executable:
            typer.echo("uv was not found; pass its Linux path with --uv.", err=True)
            raise typer.Exit(2)
        try:
            unit = build_systemd_unit(project or PROJECT_ROOT, home or get_paths().home, executable,
                                      config=config, db=db, interval_seconds=interval)
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(2)
        typer.echo(unit, nl=False)

"""Run the Discord bot with a private credentials file."""

from pathlib import Path
from typing import Annotated

import typer

from auto_app.settings import get_paths

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Discord digests and owner controls.")


@app.command("run")
def run(db: Annotated[Path | None, typer.Option("--db", help="Database file.")] = None,
        credentials: Annotated[Path | None, typer.Option("--credentials", help="Private bot.env file (mode 0600).")]
        = None) -> None:
    """Run the bot until stopped."""
    from auto_app.bot.actions import BotSettings

    paths = get_paths()
    try:
        settings = BotSettings.from_file(credentials or paths.home / "credentials" / "bot.env", db or paths.db)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    import discord

    from auto_app.bot.app import run as run_bot

    try:
        run_bot(settings)
    except discord.LoginFailure:
        # A rejected token is configuration, not an outage: exit 2 so systemd stops retrying and alerts.
        typer.echo("Discord rejected the bot token; check AUTO_APP_DISCORD_BOT_TOKEN in bot.env", err=True)
        raise typer.Exit(2) from None

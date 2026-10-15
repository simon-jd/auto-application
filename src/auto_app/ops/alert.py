"""OnFailure alerts contain only a unit name, timestamp, and local log command."""

from collections.abc import Mapping
from datetime import datetime, timezone
import re

from auto_app.core.discord import WEBHOOK_ENV, Discord, DiscordError

UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._-]{0,119}\.(?:service|timer)")


def send_failure(unit: str, environ: Mapping[str, str], discord_factory=Discord) -> int:
    if not isinstance(unit, str) or not UNIT.fullmatch(unit) or not environ.get(WEBHOOK_ENV):
        return 1
    try:
        discord = discord_factory(environ[WEBHOOK_ENV])
        try:
            discord.post(f"🚨 {unit} failed at {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC — "
                         f"`journalctl --user -u {unit} -n 50` on the host")
        finally:
            discord.close()
    except DiscordError:
        return 1
    return 0

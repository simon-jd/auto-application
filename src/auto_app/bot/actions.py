"""Pure bot configuration and database actions, separate from Discord transport."""

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

ACTIONS = {"want": "want_to_apply", "skip": "not_applied", "applied": "applied"}
LABELS = {"want": "Want to apply", "skip": "Skip", "applied": "Applied"}
CUSTOM_ID = re.compile(r"job:([1-9][0-9]{0,11}):(want|skip|applied)")
TOKEN = re.compile(r"[A-Za-z0-9_.-]+")
ENV = ("AUTO_APP_DISCORD_BOT_TOKEN", "AUTO_APP_DISCORD_OWNER_ID", "AUTO_APP_DISCORD_GUILD_ID",
       "AUTO_APP_DISCORD_CHANNEL_ID")


def custom_id(job_id: int, action: str) -> str:
    if type(job_id) is not int or not 0 < job_id < 10**12 or action not in ACTIONS:
        raise ValueError("Invalid job button")
    return f"job:{job_id}:{action}"


def parse_custom_id(value: str) -> tuple[int, str] | None:
    match = CUSTOM_ID.fullmatch(value) if isinstance(value, str) else None
    return (int(match[1]), match[2]) if match else None


@dataclass(frozen=True)
class BotSettings:
    token: str = field(repr=False)
    owner_id: int
    guild_id: int
    channel_id: int
    db_path: Path

    @classmethod
    def _from_values(cls, values: dict[str, str], db_path: Path) -> "BotSettings":
        for key in ENV:
            if not values.get(key):
                raise ValueError(f"{key} is missing from bot credentials")
        if not TOKEN.fullmatch(values[ENV[0]]):
            raise ValueError("AUTO_APP_DISCORD_BOT_TOKEN has invalid format")
        ids = []
        for key in ENV[1:]:
            value = values[key]
            if not value.isascii() or not value.isdecimal() or int(value) <= 0:
                raise ValueError(f"{key} must be a positive Discord numeric id")
            ids.append(int(value))
        return cls(values[ENV[0]], *ids, Path(db_path))

    @classmethod
    def from_env(cls, db_path: Path) -> "BotSettings":
        """Support tests and explicit callers; production startup requires from_file."""
        return cls._from_values({key: os.environ.get(key, "").strip() for key in ENV}, db_path)

    @classmethod
    def from_file(cls, path: Path, db_path: Path) -> "BotSettings":
        path = Path(path).expanduser()
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            raise ValueError("bot credential file is missing or unreadable") from None
        try:
            mode = os.fstat(fd).st_mode
            if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o600:
                raise ValueError("bot credential file must be a regular file with mode 0600")
            raw = os.read(fd, 8193)
        except OSError:
            raise ValueError("bot credential file is unreadable") from None
        finally:
            os.close(fd)
        if len(raw) > 8192:
            raise ValueError("bot credential file is too large")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("bot credential file is not valid UTF-8") from None
        values = {}
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not sep or key not in ENV or key in values or not value:
                raise ValueError("bot credential file contains an invalid entry")
            values[key] = value
        return cls._from_values(values, db_path)


def record_action(db_path: Path, user_id: int, owner_id: int, job_id: int, action: str) -> str:
    """Write one owner's manual decision through the shared AgentService."""
    if user_id != owner_id:
        return "Only the owner of this job search can record decisions."
    if action not in ACTIONS:
        return "Unknown action."
    from auto_app.agent.service import AgentError, AgentService

    try:
        result = AgentService(db_path).record_application_status(job_id, ACTIONS[action], reason="via Discord")
    except (AgentError, ValueError):
        return "Not recorded: this job cannot be changed. Check its current status."
    return f"Job #{job_id}: {ACTIONS[action]}" + ("" if result["changed"] else " (already)")


def jobs_summary(db_path: Path, *, sponsorship: str | None = None, query: str | None = None,
                 company: str | None = None, limit: int = 10) -> list[dict]:
    """Find open, visible jobs with the same country and screening rules as chat."""
    from auto_app.query.service import QueryService

    wanted, cursor = [], None
    limit = max(1, min(limit, 10))
    with QueryService(db_path) as service:
        while len(wanted) < limit:
            page = service.jobs(query=query, company=company, sponsorship=sponsorship, limit=100, cursor=cursor)
            for job in page["items"]:
                if job["state"] in ("not_evaluated", "recommended"):
                    wanted.append(_brief(job))
                    if len(wanted) == limit:
                        break
            cursor = page["next_cursor"]
            if not cursor:
                break
    return wanted


def job_summary(db_path: Path, job_id: int) -> dict | None:
    from auto_app.query.service import QueryService

    with QueryService(db_path) as service:
        job = service.job(job_id)
    return _brief(job) if job else None


def _brief(job: dict) -> dict:
    return {"job_id": job["job_id"], "title": job.get("title"), "company": job.get("company"),
            "state": job.get("state"), "url": job.get("canonical_url"),
            "sponsorship": ((job.get("screening") or {}).get("signals") or {}).get("sponsorship", "unknown")}

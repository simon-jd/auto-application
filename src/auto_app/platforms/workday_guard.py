"""One cool-down for every request to Workday, shared by the feed worker and JD enrichment.

Workday's tenants (*.myworkdayjobs.com) sit behind one service, and it blocks the address, not one site: on
2026-11-18 the feed worker drew HTTP 429 from all five Workday boards every round, and on 2026-11-19 117 of
enrichment's JD requests were refused with HTTP 403, from dozens of tenants, between good reads. So a 403 or 429
from any Workday host starts a cool-down for both: 1, 2, 4, 8, then 12 hours (or Retry-After, when longer),
growing with each strike in a row.
A strike while already cooling down only lengthens the cool-down to a longer Retry-After; it does not count again.
A good Workday read clears it, unless the read started before the strike (it may have raced past the block).

The state is a small JSON file next to the database, written atomically, so the two processes share it without
taking the database's write lock. A missing or unreadable file means no cool-down.
"""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKOFF = (timedelta(hours=1), timedelta(hours=2), timedelta(hours=4), timedelta(hours=8), timedelta(hours=12))
BLOCKED = (403, 429)
FILE_NAME = ".workday-guard.json"


def _moment(value) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
    return moment if moment is None or moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def retry_after_seconds(headers) -> int | None:
    """Retry-After in whole seconds, when the header gives a number (an HTTP date is ignored)."""
    value = (headers or {}).get("Retry-After", "") or ""
    return int(value.strip()) if value.strip().isdigit() else None


class WorkdayGuard:
    def __init__(self, path: Path):
        self.path = Path(path)

    @classmethod
    def beside(cls, db_path) -> "WorkdayGuard":
        return cls(Path(db_path).parent / FILE_NAME)

    def state(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def cooling_until(self, now: datetime) -> datetime | None:
        """The end of the cool-down, while one is on; None otherwise."""
        until = _moment(self.state().get("until"))
        return until if until is not None and until > now else None

    def strike(self, now: datetime, *, status: int, by: str, retry_after: int | None = None) -> datetime:
        """Record a 403/429 from Workday; return the end of the cool-down."""
        state = self.state()
        current = self.cooling_until(now)
        strikes = state.get("strikes") if isinstance(state.get("strikes"), int) else 0
        if current is None:
            strikes += 1
        strikes = max(1, strikes)
        until = now + BACKOFF[min(strikes, len(BACKOFF)) - 1] if current is None else current
        if retry_after:
            until = max(until, now + timedelta(seconds=retry_after))
        self._write({"strikes": strikes, "until": until.isoformat(), "status": status, "by": by,
                     "at": (now if current is None else _moment(state.get("at")) or now).isoformat()})
        return until

    def clear(self, *, started: datetime) -> None:
        """A good Workday read that started at `started` ends the cool-down and the strikes."""
        state = self.state()
        if not state:
            return
        struck = _moment(state.get("at"))
        if struck is not None and struck >= started:
            return
        self.path.unlink(missing_ok=True)

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".workday-guard.", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle)
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

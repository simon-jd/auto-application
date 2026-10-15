"""Server-side guardrails for agent applications ($AUTO_APP_HOME/config/handoff.json)."""

import json
from dataclasses import dataclass, fields
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class Guard:
    daily_submissions: int = 20  # per local calendar day, across every company
    company_window_days: int = 30
    company_limit: int = 3  # submissions to one company within the window
    lease_minutes: int = 30  # how long a claim stays valid
    timezone: str | None = None  # IANA zone for the daily limit's day; None = the host's local zone


def load_guard(path: Path) -> Guard:
    if not Path(path).exists():
        return Guard()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read {Path(path).name}: {exc}") from exc
    if not isinstance(data, dict) or set(data) - {f.name for f in fields(Guard)}:
        raise ValueError(f"{Path(path).name}: expected an object with only {[f.name for f in fields(Guard)]}")
    for key, value in data.items():
        if key == "timezone":
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError, TypeError):
                raise ValueError(f"{Path(path).name}: timezone must be an IANA zone such as America/New_York") from None
            continue
        if type(value) is not int or not 1 <= value <= 1000:
            raise ValueError(f"{Path(path).name}: {key} must be an integer between 1 and 1000")
    return Guard(**data)

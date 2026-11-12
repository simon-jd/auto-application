"""Server-side guardrails for agent applications ($AUTO_APP_HOME/config/handoff.json)."""

import json
from dataclasses import dataclass, fields
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MAX_GROUPS = 500  # company groups in one file
MAX_GROUP_NAMES = 50  # other names in one group
MAX_NAME_LENGTH = 200


@dataclass(frozen=True)
class Guard:
    daily_submissions: int = 20  # per local calendar day, across every company
    company_window_days: int = 30
    company_limit: int = 3  # submissions to one company within the window
    lease_minutes: int = 30  # how long a claim stays valid
    timezone: str | None = None  # IANA zone for the daily limit's day; None = the host's local zone
    # (other name, group name) pairs, both normalized like a job's company_norm: names that share the company limit
    company_groups: tuple[tuple[str, str], ...] = ()

    def company_keys(self) -> dict[str, str]:
        """Each grouped name → its group's name. A name in no group is its own company."""
        return dict(self.company_groups)


def load_guard(path: Path) -> Guard:
    if not Path(path).exists():
        return Guard()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Cannot read {Path(path).name}: {exc}") from exc
    if not isinstance(data, dict) or set(data) - {f.name for f in fields(Guard)}:
        raise ValueError(f"{Path(path).name}: expected an object with only {[f.name for f in fields(Guard)]}")
    values = {}
    for key, value in data.items():
        if key == "timezone":
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError, TypeError):
                raise ValueError(f"{Path(path).name}: timezone must be an IANA zone such as America/New_York") from None
        elif key == "company_groups":
            value = _company_groups(value, Path(path).name)
        elif type(value) is not int or not 1 <= value <= 1000:
            raise ValueError(f"{Path(path).name}: {key} must be an integer between 1 and 1000")
        values[key] = value
    return Guard(**values)


def _company_groups(value, where: str) -> tuple[tuple[str, str], ...]:
    """{"group name": ["other name", …]} → sorted (other, group) pairs, each name normalized like company_norm.

    A name may sit in only one group, and a group's own name may not be listed inside another group: either would
    make "which company is this" depend on the order the file was read in.
    """
    from auto_app.process.normalize import company_basic

    shape = f'{where}: company_groups must map a company name to a list of its other names, e.g. ' \
            f'{{"amazon": ["amazon web services"]}}'
    if not isinstance(value, dict) or len(value) > MAX_GROUPS:
        raise ValueError(shape + f" (at most {MAX_GROUPS} groups)")
    owner, groups = {}, set()
    for group, names in value.items():
        if not isinstance(group, str) or not isinstance(names, list) or not names or len(names) > MAX_GROUP_NAMES \
                or any(not isinstance(n, str) or len(n) > MAX_NAME_LENGTH for n in [group, *names]):
            raise ValueError(shape + f" (1 to {MAX_GROUP_NAMES} names each, at most {MAX_NAME_LENGTH} characters)")
        key = company_basic(group)
        if not key or key in groups:
            raise ValueError(f"{where}: company_groups has an empty or repeated group {group!r}")
        groups.add(key)
        for name in names:
            other = company_basic(name)
            if not other:
                raise ValueError(f"{where}: company_groups[{group!r}] has an empty name")
            if other != key and owner.setdefault(other, key) != key:
                raise ValueError(f"{where}: {other!r} is in two company groups ({owner[other]!r} and {key!r})")
    nested = sorted(set(owner) & groups)
    if nested:
        raise ValueError(f"{where}: {nested[0]!r} is a group and also a name inside another group; merge the two")
    return tuple(sorted((other, key) for other, key in owner.items() if other != key))

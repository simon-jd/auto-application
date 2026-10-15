"""Independent, atomic pause requests; the worker never writes this control file."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile

from auto_app.ingest.scheduler import _path, _read_state

CONTROL = ".worker-control.json"
DEFAULT = {"paused": False, "paused_until": None, "reason": None, "updated_at": None}


def _time(value: str) -> datetime:
    try:
        when = datetime.fromisoformat(value)
        return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        raise ValueError("worker control contains an invalid timestamp") from None


def read_control(db_path: Path) -> dict:
    if not _path(db_path, CONTROL).exists():
        return dict(DEFAULT)
    state = _read_state(Path(db_path), CONTROL)
    if "paused" not in state:
        raise ValueError("worker control must explicitly set paused")
    result = {key: state.get(key, value) for key, value in DEFAULT.items()}
    if type(result["paused"]) is not bool:
        raise ValueError("worker control paused must be a boolean")
    for key in ("paused_until", "updated_at"):
        if result[key] is not None:
            _time(result[key])
    if result["reason"] is not None and not isinstance(result["reason"], str):
        raise ValueError("worker control reason must be text")
    return result


def write_control(db_path: Path, *, paused: bool, paused_until: datetime | None = None,
                  reason: str | None = None, now: datetime | None = None) -> dict:
    if type(paused) is not bool or (reason is not None and not isinstance(reason, str)):
        raise ValueError("invalid worker control request")
    now = now or datetime.now(timezone.utc)
    value = {"paused": paused,
             "paused_until": _time(paused_until.isoformat()).isoformat() if paused and paused_until else None,
             "reason": (reason or "")[:200] or None if paused else None,
             "updated_at": _time(now.isoformat()).isoformat()}
    path = _path(db_path, CONTROL)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return value


def is_paused(control: dict, now: datetime) -> bool:
    if not control.get("paused"):
        return False
    until = control.get("paused_until")
    return until is None or _time(until) > _time(now.isoformat())

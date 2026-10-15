"""Application events, written in the caller's transaction when one is active."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, insert, select

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow

KINDS = ("applied", "assessment", "interview", "rejected", "offer", "withdrawn")
SOURCES = ("manual", "agent", "email", "system")
TERMINAL = ("rejected", "offer", "withdrawn")
REPEATABLE = ("assessment", "interview")  # one application can have several in a day
SAME_WITHIN = timedelta(hours=24)


def valid_id(value, name="job_id"):
    if type(value) is not int or not 0 < value <= 9223372036854775807:
        raise ValueError(f"{name} must be a positive integer")
    return value


def valid_date(value, name="occurred_at"):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    try:
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ValueError(f"{name} is outside the supported date range") from None


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@contextmanager
def write_transaction(store):
    # BEGIN IMMEDIATE serializes the duplicate check and insert across SQLite writers.
    if store._tx is not None:
        with store._conn() as conn:
            yield conn
    else:
        with store.transaction(immediate=True):
            with store._conn() as conn:
                yield conn


def require_job(conn, job_id):
    valid_id(job_id)
    if conn.execute(select(T.jobs.c.job_id).where(T.jobs.c.job_id == job_id)).scalar() is None:
        raise ValueError(f"unknown job #{job_id}")


def record_event(store, job_id: int, kind: str, *, occurred_at: datetime | None = None,
                 source: str = "manual", note: str | None = None, mail_update_id: int | None = None) -> int:
    valid_id(job_id)
    if kind not in KINDS:
        raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
    if source not in SOURCES:
        raise ValueError(f"source must be one of: {', '.join(SOURCES)}")
    if note is not None and not isinstance(note, str):
        raise ValueError("note must be a string")
    occurred_at = valid_date(occurred_at if occurred_at is not None else utcnow())
    if mail_update_id is not None:
        valid_id(mail_update_id, "mail_update_id")
    e = T.application_events
    with write_transaction(store) as conn:
        require_job(conn, job_id)
        if mail_update_id is not None:
            row = conn.execute(select(T.mail_updates).where(T.mail_updates.c.update_id == mail_update_id)).mappings().first()
            if row is None or row["job_id"] != job_id or row["kind"] != kind or source != "email":
                raise ValueError("mail_update_id must refer to this job's matching email update")
            existing = conn.execute(select(e.c.event_id).where(e.c.mail_update_id == mail_update_id)).scalar()
            if existing is not None:
                return existing
            if kind in REPEATABLE:
                # A distinct message is a distinct round (a morning phone screen, an afternoon
                # onsite invite); its message identity, checked above, is the only duplicate.
                return _insert(conn, job_id, kind, occurred_at, source, note, mail_update_id)
        try:
            lower, upper = occurred_at - SAME_WITHIN, occurred_at + SAME_WITHIN
        except OverflowError:
            raise ValueError("occurred_at is outside the supported date range") from None
        existing = conn.execute(select(e.c.event_id).where(
            e.c.job_id == job_id, e.c.kind == kind, e.c.source == source,
            e.c.occurred_at >= lower, e.c.occurred_at <= upper).order_by(e.c.event_id)).scalar()
        if existing is not None:
            return existing
        return _insert(conn, job_id, kind, occurred_at, source, note, mail_update_id)


def _insert(conn, job_id, kind, occurred_at, source, note, mail_update_id):
    return conn.execute(insert(T.application_events).values(job_id=job_id, kind=kind, occurred_at=occurred_at,
        source=source, note=(note or "").strip()[:500] or None, mail_update_id=mail_update_id,
        created_at=utcnow())).inserted_primary_key[0]


def events_for(store, job_id: int) -> list[dict]:
    valid_id(job_id)
    e = T.application_events
    with store._conn() as conn:
        return [{**dict(row), "occurred_at": _aware(row["occurred_at"]).isoformat()}
                for row in conn.execute(select(e.c.event_id, e.c.kind, e.c.occurred_at, e.c.source, e.c.note)
                    .where(e.c.job_id == job_id).order_by(e.c.occurred_at, e.c.event_id)).mappings()]


def weekly_summary(store, since: datetime) -> dict:
    since = valid_date(since, "since")
    e = T.application_events
    with store._conn() as conn:
        by_kind = dict(conn.execute(select(e.c.kind, func.count()).where(e.c.occurred_at >= since)
                                    .group_by(e.c.kind)).all())
        applied_ids = set(conn.execute(select(e.c.job_id).where(e.c.kind == "applied")).scalars())
        # Older CLI/inbox writes have no timeline event. Count their durable
        # evidence without inserting made-up events or assigning missing dates.
        legacy = {job_id: when for job_id, when in conn.execute(
            select(T.jobs.c.job_id, T.jobs.c.decided_at).where(T.jobs.c.decision == "applied"))
            if job_id not in applied_ids}
        for job_id, submitted_at in conn.execute(select(T.applications.c.job_id, func.min(T.applications.c.submitted_at))
                .where(T.applications.c.status == "submitted").group_by(T.applications.c.job_id)):
            if job_id not in applied_ids:
                # A recorded submission date is stronger than decided_at, which
                # older repeated marks could overwrite long after submission.
                if submitted_at is not None or job_id not in legacy:
                    legacy[job_id] = submitted_at
        recent_legacy = sum(when is not None and _aware(when) >= since for when in legacy.values())
        if recent_legacy:
            by_kind["applied"] = by_kind.get("applied", 0) + recent_legacy
        applied = len(applied_ids) + len(legacy)
        latest = {}
        for job_id, kind in conn.execute(select(e.c.job_id, e.c.kind).order_by(e.c.occurred_at, e.c.event_id)):
            latest[job_id] = kind
    pipeline = {}
    for kind in latest.values():
        if kind not in ("applied", *TERMINAL):
            pipeline[kind] = pipeline.get(kind, 0) + 1
    return {"by_kind": by_kind, "applied_total": applied, "active_pipeline": pipeline}

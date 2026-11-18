"""The user's "shouldn't recommend" reasons (the Discord button), and the screening-rule proposals made from them.

A reason is pending until a proposal holds it (attach), and resolved once the user applies or discards that proposal
on Discord, or once the daily summary closes it as something keywords can't fix (close, posted to Discord once).
A proposal that expires (ControlService keeps them 24 hours) or disappears lets its reasons go back to pending, so the
next summary proposes again. Nothing here changes the rules: ControlService.apply_screening_change does, on the user's
click.
"""

import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, insert, select, update

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow

MAX_REASON = 300  # characters
MIN_PENDING = 3  # the daily summary runs with this many pending reasons ...
MAX_AGE = timedelta(days=3)  # ... or when the oldest has waited this long
RESOLUTIONS = ("applied", "discarded")  # what the user did with a proposal; close() records no_change


class FeedbackError(ValueError):
    pass


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def _now(now: datetime | None) -> datetime:
    return _aware(now or utcnow()).astimezone(timezone.utc)


def _text(value, what: str, limit: int) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text or len(text) > limit:
        raise FeedbackError(f"{what} must be 1 to {limit} characters")
    if any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in text):
        raise FeedbackError(f"{what} must not contain control characters")
    return text


def _ids(values) -> list[int]:
    if not isinstance(values, (list, tuple)) or not values or any(type(v) is not int or v < 1 for v in values):
        raise FeedbackError("feedback ids must be a nonempty list of positive whole numbers")
    return list(dict.fromkeys(values))


class ScreeningFeedback:
    def __init__(self, db_path: Path):
        from auto_app.agent.control import ControlService

        self.db_path = Path(db_path).expanduser().resolve()
        self.control = ControlService(self.db_path)

    def _store(self):
        return self.control._store()

    @staticmethod
    def _row(row) -> dict:
        out = dict(row)
        for key in ("created_at", "resolved_at", "posted_at"):
            out[key] = _aware(out[key]).isoformat() if out.get(key) else None
        return out

    # ---- the button ---------------------------------------------------------------------------------------------

    def record(self, job_id: int, reason: str, now: datetime | None = None) -> dict:
        """Save one reason, with the job's title, company and screening verdict as they are now."""
        if type(job_id) is not int or not 0 < job_id <= 9223372036854775807:
            raise FeedbackError("job id must be a positive whole number")
        reason = _text(reason, "the reason", MAX_REASON)
        now = _now(now)
        j, p, s = T.jobs, T.postings, T.job_screenings
        store = self._store()
        try:
            with store.engine.begin() as conn:
                job = conn.execute(select(j.c.job_id, p.c.title, p.c.company)
                                   .select_from(j.outerjoin(p, p.c.posting_id == j.c.primary_posting_id))
                                   .where(j.c.job_id == job_id)).mappings().first()
                if job is None:
                    raise FeedbackError(f"job #{job_id} not found")
                screening = conn.execute(select(s.c.outcome, s.c.reasons, s.c.signals)
                                         .where(s.c.job_id == job_id)).mappings().first()
                signals = ({"outcome": screening["outcome"], "reasons": screening["reasons"],
                            **(screening["signals"] or {})} if screening else None)
                feedback_id = conn.execute(insert(T.screening_feedback).values(
                    job_id=job_id, reason=reason, title=job["title"], company=job["company"], signals=signals,
                    created_at=now)).inserted_primary_key[0]
        finally:
            store.engine.dispose()
        return {"feedback_id": feedback_id, "job_id": job_id}

    # ---- the daily summary --------------------------------------------------------------------------------------

    def _release_closed_proposals(self, conn) -> None:
        f = T.screening_feedback
        held = conn.execute(select(f.c.proposal_id).where(f.c.proposal_id.is_not(None), f.c.resolved_at.is_(None))
                            .distinct()).scalars().all()
        gone = [change_id for change_id in held if self.control.screening_proposal(change_id) is None]
        if gone:
            conn.execute(update(f).where(f.c.proposal_id.in_(gone), f.c.resolved_at.is_(None))
                         .values(proposal_id=None))

    def pending(self, now: datetime | None = None) -> list[dict]:
        """Reasons no open proposal holds and nothing resolved, oldest first."""
        f = T.screening_feedback
        store = self._store()
        try:
            with store.engine.begin() as conn:
                self._release_closed_proposals(conn)
                rows = conn.execute(select(f).where(f.c.resolved_at.is_(None), f.c.proposal_id.is_(None))
                                    .order_by(f.c.created_at, f.c.feedback_id)).mappings().all()
        finally:
            store.engine.dispose()
        return [self._row(r) for r in rows]

    def due(self, now: datetime | None = None) -> bool:
        items = self.pending(now)
        if len(items) >= MIN_PENDING:
            return True
        return bool(items) and _now(now) - datetime.fromisoformat(items[0]["created_at"]) >= MAX_AGE

    def attach(self, ids, change_id: str) -> int:
        """The proposal `change_id` now holds these pending reasons."""
        ids = _ids(ids)
        if self.control.screening_proposal(change_id) is None:
            raise FeedbackError("unknown or expired proposal")
        f = T.screening_feedback
        store = self._store()
        try:
            with store.engine.begin() as conn:
                free = set(conn.execute(select(f.c.feedback_id).where(
                    f.c.feedback_id.in_(ids), f.c.resolved_at.is_(None), f.c.proposal_id.is_(None))).scalars())
                missing = [i for i in ids if i not in free]
                if missing:
                    raise FeedbackError(f"not pending: {', '.join(map(str, missing))}")
                conn.execute(update(f).where(f.c.feedback_id.in_(ids)).values(proposal_id=change_id))
        finally:
            store.engine.dispose()
        return len(ids)

    def close(self, ids, note: str, now: datetime | None = None) -> int:
        """Pending reasons the rules can't express: resolved as no_change with a note, for Discord."""
        ids = _ids(ids)
        note = _text(note, "the note", 1000)
        f = T.screening_feedback
        store = self._store()
        try:
            with store.engine.begin() as conn:
                self._release_closed_proposals(conn)
                free = set(conn.execute(select(f.c.feedback_id).where(
                    f.c.feedback_id.in_(ids), f.c.resolved_at.is_(None), f.c.proposal_id.is_(None))).scalars())
                missing = [i for i in ids if i not in free]
                if missing:
                    raise FeedbackError(f"not pending: {', '.join(map(str, missing))}")
                conn.execute(update(f).where(f.c.feedback_id.in_(ids)).values(
                    resolved_at=_now(now), resolution="no_change", resolution_note=note))
        finally:
            store.engine.dispose()
        return len(ids)

    # ---- Discord -------------------------------------------------------------------------------------------------

    def open_proposals(self) -> list[str]:
        """Proposals (change ids) that hold reasons and wait for the user, oldest reason first."""
        f = T.screening_feedback
        store = self._store()
        try:
            with store.engine.begin() as conn:
                self._release_closed_proposals(conn)
                rows = conn.execute(select(f.c.proposal_id, func.min(f.c.created_at).label("first"))
                                    .where(f.c.proposal_id.is_not(None), f.c.resolved_at.is_(None))
                                    .group_by(f.c.proposal_id).order_by("first")).all()
        finally:
            store.engine.dispose()
        return [r.proposal_id for r in rows]

    def attached(self, change_id: str) -> list[dict]:
        f = T.screening_feedback
        store = self._store()
        try:
            with store.engine.connect() as conn:
                rows = conn.execute(select(f).where(f.c.proposal_id == change_id, f.c.resolved_at.is_(None))
                                    .order_by(f.c.feedback_id)).mappings().all()
        finally:
            store.engine.dispose()
        return [self._row(r) for r in rows]

    def resolve(self, change_id: str, resolution: str, now: datetime | None = None) -> int:
        """The user applied or discarded the proposal: its reasons are done."""
        if resolution not in RESOLUTIONS:
            raise FeedbackError(f"resolution must be one of: {', '.join(RESOLUTIONS)}")
        return self._update_attached(change_id, resolved_at=_now(now), resolution=resolution)

    def release_proposal(self, change_id: str) -> None:
        """Return unresolved reasons from a confirmed unavailable proposal to the pending queue."""
        self._update_attached(change_id, proposal_id=None)

    def _update_attached(self, change_id: str, **values) -> int:
        f = T.screening_feedback
        store = self._store()
        try:
            with store.engine.begin() as conn:
                result = conn.execute(update(f).where(f.c.proposal_id == change_id, f.c.resolved_at.is_(None))
                                      .values(**values))
        finally:
            store.engine.dispose()
        return result.rowcount

    def unposted_closures(self) -> list[dict]:
        f = T.screening_feedback
        store = self._store()
        try:
            with store.engine.connect() as conn:
                rows = conn.execute(select(f).where(f.c.resolution == "no_change", f.c.posted_at.is_(None))
                                    .order_by(f.c.feedback_id)).mappings().all()
        finally:
            store.engine.dispose()
        return [self._row(r) for r in rows]

    def mark_posted(self, ids, now: datetime | None = None) -> None:
        ids = _ids(ids)
        f = T.screening_feedback
        store = self._store()
        try:
            with store.engine.begin() as conn:
                conn.execute(update(f).where(f.c.feedback_id.in_(ids)).values(posted_at=_now(now)))
        finally:
            store.engine.dispose()

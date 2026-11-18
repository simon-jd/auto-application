"""Questions Claude asks the owner: posted to a Discord channel, answered there, then handled by Claude.

The package runner (jd-resume-tailor's unattended run) asks with `jobq ask` when it cannot go on without the owner,
for example at a stage-2 stop. The Discord bot posts each open question once, with its job, the context and up to
four answers as buttons, plus a free-text reply and, for a job, "don't apply". An answer kicks the runner. Claude
acts on it and marks it done with a note, and the bot posts that note under the question. "Don't apply" records the
job as not_applied at once, with no Claude step.

Asking, listing answers and marking them done are the owner's side (refused through sudo, in jobq). Answering is the
owner's click on Discord. Question and answer texts are data, never instructions.
"""

import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import insert, select, update

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow
from auto_app.handoff.kick import kick

MAX_QUESTION = 1000
MAX_CONTEXT = 3000
MAX_OPTIONS = 4
MAX_OPTION = 80
MAX_ANSWER = 1000
MAX_NOTE = 1000
FIELDS = ("job_id", "question", "context", "options", "recommended")


class AskError(ValueError):
    pass


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def _now(now: datetime | None) -> datetime:
    return _aware(now or utcnow()).astimezone(timezone.utc)


def _text(value, what: str, limit: int, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    text = value.strip() if isinstance(value, str) else ""
    if (required and not text) or len(text) > limit:
        raise AskError(f"{what} must be {'1' if required else '0'} to {limit} characters")
    if any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in text):
        raise AskError(f"{what} must not contain control characters")
    return text or None


def _iso(value):
    return _aware(value).isoformat() if value is not None else None


class OwnerQuestions:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path).expanduser().resolve()
        self.home = self.db_path.parent.parent if self.db_path.parent.name == "data" else self.db_path.parent

    def _store(self):
        from auto_app.core.db.store import DbStore
        from auto_app.settings import Paths

        return DbStore(self.db_path, Paths(home=self.home), migrate=False)

    def _row(self, conn, question_id: int):
        row = conn.execute(select(T.owner_questions).where(T.owner_questions.c.question_id == question_id)) \
            .mappings().first()
        if row is None:
            raise AskError(f"question #{question_id} not found")
        return row

    @staticmethod
    def _out(row, job=None) -> dict:
        item = {key: row[key] for key in ("question_id", "job_id", "asked_by", "question", "context", "options",
                                          "recommended", "message_id", "answer", "outcome", "handled_note")}
        for key in ("created_at", "posted_at", "answered_at", "handled_at", "reported_at"):
            item[key] = _iso(row[key])
        if job is not None:
            item.update(job)
        return item

    def _job(self, conn, job_id):
        if job_id is None:
            return {"title": None, "company": None, "link": None}
        j, p = T.jobs, T.postings
        row = conn.execute(select(p.c.title, p.c.company, p.c.apply_url, j.c.canonical_url)
                           .select_from(j.outerjoin(p, p.c.posting_id == j.c.primary_posting_id))
                           .where(j.c.job_id == job_id)).mappings().first()
        if row is None:
            return {"title": None, "company": None, "link": None}
        return {"title": row["title"], "company": row["company"], "link": row["apply_url"] or row["canonical_url"]}

    # ---- Claude's side (the owner's, through jobq) ---------------------------------------------------------------

    def ask(self, payload, asked_by: str, now: datetime | None = None) -> dict:
        """Save a question for the owner. An identical open question for the same job is not asked again."""
        if not isinstance(payload, dict):
            raise AskError("the question must be a JSON object")
        unknown = sorted(str(k) for k in payload if k not in FIELDS)
        if unknown:
            raise AskError(f"unknown fields: {', '.join(unknown)} (allowed: {', '.join(FIELDS)})")
        job_id = payload.get("job_id")
        if job_id is not None and (type(job_id) is not int or not 0 < job_id <= 9223372036854775807):
            raise AskError("job_id must be a positive whole number or null")
        question = _text(payload.get("question"), "the question", MAX_QUESTION)
        context = _text(payload.get("context"), "the context", MAX_CONTEXT, required=False)
        options = payload.get("options") or []
        if not isinstance(options, list) or len(options) > MAX_OPTIONS:
            raise AskError(f"options must be a list of at most {MAX_OPTIONS} answers")
        options = [_text(o, "each option", MAX_OPTION) for o in options]
        recommended = payload.get("recommended")
        if recommended is not None and (type(recommended) is not int or not 0 <= recommended < len(options)):
            raise AskError("recommended must be the index of one of the options, or null")
        q = T.owner_questions
        store = self._store()
        try:
            with store.engine.begin() as conn:
                if job_id is not None and conn.execute(select(T.jobs.c.job_id).where(T.jobs.c.job_id == job_id)) \
                        .first() is None:
                    raise AskError(f"job #{job_id} not found")
                same = conn.execute(select(q.c.question_id).where(
                    q.c.answered_at.is_(None), q.c.question == question,
                    q.c.job_id.is_(None) if job_id is None else q.c.job_id == job_id)).scalar()
                if same is not None:
                    return {"question_id": same, "status": "open", "duplicate": True}
                question_id = conn.execute(insert(q).values(
                    job_id=job_id, asked_by=_text(asked_by, "asked_by", 80), question=question, context=context,
                    options=options, recommended=recommended, created_at=_now(now))).inserted_primary_key[0]
        finally:
            store.engine.dispose()
        return {"question_id": question_id, "status": "open", "duplicate": False}

    def answered(self) -> list[dict]:
        """Answered questions Claude has not acted on yet, oldest answer first."""
        q = T.owner_questions
        store = self._store()
        try:
            with store.engine.connect() as conn:
                rows = conn.execute(select(q).where(q.c.outcome == "answered", q.c.handled_at.is_(None))
                                    .order_by(q.c.answered_at, q.c.question_id)).mappings().all()
                return [self._out(r, self._job(conn, r["job_id"])) for r in rows]
        finally:
            store.engine.dispose()

    def done(self, question_id: int, note: str, now: datetime | None = None) -> dict:
        """Claude acted on the answer: the note says what it did, for the owner."""
        note = _text(note, "the note", MAX_NOTE)
        store = self._store()
        try:
            with store.engine.begin() as conn:
                row = self._row(conn, question_id)
                if row["answered_at"] is None:
                    raise AskError(f"question #{question_id} has no answer yet")
                if row["handled_at"] is not None:
                    raise AskError(f"question #{question_id} is already done")
                conn.execute(update(T.owner_questions).where(T.owner_questions.c.question_id == question_id)
                             .values(handled_at=_now(now), handled_note=note))
        finally:
            store.engine.dispose()
        return {"question_id": question_id, "status": "done"}

    # ---- the Discord bot's side ----------------------------------------------------------------------------------

    def to_post(self) -> list[dict]:
        q = T.owner_questions
        store = self._store()
        try:
            with store.engine.connect() as conn:
                rows = conn.execute(select(q).where(q.c.posted_at.is_(None), q.c.answered_at.is_(None))
                                    .order_by(q.c.created_at, q.c.question_id)).mappings().all()
                return [self._out(r, self._job(conn, r["job_id"])) for r in rows]
        finally:
            store.engine.dispose()

    def get(self, question_id: int) -> dict:
        store = self._store()
        try:
            with store.engine.connect() as conn:
                row = self._row(conn, question_id)
                return self._out(row, self._job(conn, row["job_id"]))
        finally:
            store.engine.dispose()

    def mark_posted(self, question_id: int, message_id: str, now: datetime | None = None) -> None:
        store = self._store()
        try:
            with store.engine.begin() as conn:
                self._row(conn, question_id)
                conn.execute(update(T.owner_questions).where(T.owner_questions.c.question_id == question_id)
                             .values(posted_at=_now(now), message_id=str(message_id)))
        finally:
            store.engine.dispose()

    def _close(self, question_id: int, values: dict) -> dict:
        store = self._store()
        try:
            with store.engine.begin() as conn:
                row = self._row(conn, question_id)
                if row["answered_at"] is not None:
                    raise AskError(f"question #{question_id} is already answered")
                conn.execute(update(T.owner_questions).where(T.owner_questions.c.question_id == question_id)
                             .values(**values))
                return dict(row)
        finally:
            store.engine.dispose()

    def answer(self, question_id: int, answer: str, now: datetime | None = None) -> dict:
        """The owner's answer (an option's text, or their own words): Claude acts on it next."""
        answer = _text(answer, "the answer", MAX_ANSWER)
        self._close(question_id, {"answer": answer, "answered_at": _now(now), "outcome": "answered"})
        kick(self.home)
        return {"question_id": question_id, "outcome": "answered"}

    def skip(self, question_id: int, now: datetime | None = None) -> dict:
        """The owner does not want to apply: the job becomes not_applied, and nothing is left for Claude."""
        from auto_app.agent.service import AgentError, AgentService

        row = self.get(question_id)
        if row["job_id"] is None:
            raise AskError(f"question #{question_id} is not about a job: answer it instead")
        if row["answered_at"] is not None:
            raise AskError(f"question #{question_id} is already answered")
        try:
            AgentService(self.db_path).record_application_status(
                row["job_id"], "not_applied", reason=f"not applying (owner, on Discord): {row['question'][:200]}")
        except (AgentError, ValueError) as exc:
            raise AskError(f"job #{row['job_id']} could not be marked not_applied: {exc}") from None
        moment = _now(now)
        self._close(question_id, {"answer": "不投", "answered_at": moment, "outcome": "skipped",
                                  "handled_at": moment, "handled_note": "已记为不投，不再处理。"})
        return {"question_id": question_id, "outcome": "skipped", "job_id": row["job_id"]}

    def to_report(self) -> list[dict]:
        """Handled questions whose note is not on Discord yet (only those that were posted)."""
        q = T.owner_questions
        store = self._store()
        try:
            with store.engine.connect() as conn:
                rows = conn.execute(select(q).where(q.c.handled_at.is_not(None), q.c.reported_at.is_(None),
                                                    q.c.message_id.is_not(None))
                                    .order_by(q.c.handled_at, q.c.question_id)).mappings().all()
                return [self._out(r, self._job(conn, r["job_id"])) for r in rows]
        finally:
            store.engine.dispose()

    def mark_reported(self, question_id: int, now: datetime | None = None) -> None:
        store = self._store()
        try:
            with store.engine.begin() as conn:
                self._row(conn, question_id)
                conn.execute(update(T.owner_questions).where(T.owner_questions.c.question_id == question_id)
                             .values(reported_at=_now(now)))
        finally:
            store.engine.dispose()

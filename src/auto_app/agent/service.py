"""Job queries and explicit manual decisions, without application execution.

Connections belong to individual calls so an adapter can dispatch tools on any
thread. Read tools keep QueryService's schema checks and preference filtering.
Writes reuse the store's decisions, review records, and feedback semantics in a
single transaction; they never run migrations or a library-wide TTL refresh.
"""

from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Literal

from auto_app.query.service import QueryService


ApplicationStatus = Literal["want_to_apply", "applied", "not_applied"]
APPLICATION_STATUSES = ("want_to_apply", "applied", "not_applied")


class AgentError(ValueError):
    """An explicit user decision cannot be recorded as requested."""


def apply_manual_decision_in_transaction(store, job_id: int, status: ApplicationStatus, reason: str | None = None,
                                         *, event_source: str = "manual", occurred_at: datetime | None = None) -> bool:
    """Apply the shared decision/review/feedback semantics using an existing DbStore transaction.

    The caller owns commit/rollback. No migration, independent connection, or TTL refresh occurs.
    """
    from auto_app.select.review import Review

    if store._tx is None:
        raise RuntimeError("an active DbStore transaction is required")
    if type(job_id) is not int or not 0 < job_id <= 9223372036854775807:
        raise AgentError("job_id must be a positive integer")
    if not isinstance(status, str) or status not in APPLICATION_STATUSES:
        raise AgentError(f"status must be one of: {', '.join(APPLICATION_STATUSES)}")
    if reason is not None and not isinstance(reason, str):
        raise AgentError("reason must be a string or null")

    changed = False
    job = store.job(job_id)
    if job is None:
        raise AgentError(f"job #{job_id} is missing")
    if job.get("archived_at"):
        raise AgentError(f"job #{job_id} is archived; its state cannot change")
    if job.get("duplicate_of") is not None:
        raise AgentError(f"job #{job_id} is a duplicate of #{job['duplicate_of']}")
    if job.get("decision") == "applied" and status != "applied":
        raise AgentError(f"job #{job_id} is already applied; this tool cannot undo that decision")

    reviews = store.reviews(job_id)
    latest_review = reviews[-1] if reviews else None
    same_status = job.get("decision") == status
    same_review = latest_review and latest_review["decision"] == status and latest_review["reason"] == reason
    if not (same_status and same_review):
        # With no local filter folders, snapshot the latest recorded
        # successful verdict for each filter rather than load agents.
        latest = {row["filter"]: row for row in store.judgment_rows(job_id=job_id)
                  if row["status"] != "error"}
        filters = [SimpleNamespace(name=name, version=row["filter_version"])
                   for name, row in latest.items()]
        review = Review(store, filters)
        detail = review.detail(job_id, job=job)
        if not same_status:
            store.mark_job(job_id, status, event_source=event_source, occurred_at=occurred_at, note=reason)
        verdicts = [{"filter": v.filter, "version": v.version, "recommend": v.recommend}
                    for v in detail.verdicts]
        store.add_review(job_id, status, reason, verdicts, detail.text_for_labels)
        if status == "want_to_apply":
            for verdict in detail.verdicts:
                if verdict.recommend is False and verdict.label is None:
                    review._label(job_id, verdict, True, "implied: you chose to apply",
                                  "implied", detail.text_for_labels)
        changed = True
    return changed



class AgentService:
    def __init__(self, db_path: Path, preferences_path: Path | None = None):
        self.db_path = Path(db_path).expanduser().resolve()
        self.preferences_path = preferences_path

    def context(self) -> dict:
        with QueryService(self.db_path, self.preferences_path) as query:
            result = query.context()
        result["capabilities"].update(read_only=False, manual_application_tracking=True, automated_submission=False)
        return result

    def search_jobs(self, **filters) -> dict:
        with QueryService(self.db_path, self.preferences_path) as query:
            return query.jobs(**filters)

    def get_job(self, job_id: int) -> dict | None:
        with QueryService(self.db_path, self.preferences_path) as query:
            return query.job(job_id)

    def record_application_status(self, job_id: int, status: ApplicationStatus, reason: str | None = None,
                                  *, event_source: str = "manual") -> dict:
        """Record the user's intent or their report of a manual submission.

        An identical latest decision is a no-op. A new reason for the same
        status adds an audit record without changing its original decided_at.
        Applied jobs require a separate undo mechanism, absent from this API.
        """
        if type(job_id) is not int or not 0 < job_id <= 9223372036854775807:
            raise AgentError("job_id must be a positive integer")
        if not isinstance(status, str) or status not in APPLICATION_STATUSES:
            raise AgentError(f"status must be one of: {', '.join(APPLICATION_STATUSES)}")
        if reason is not None and not isinstance(reason, str):
            raise AgentError("reason must be a string or null")

        # Validate the existing schema and preferences before opening a writer.
        # These imports are intentionally absent from the read-only tool path.
        with QueryService(self.db_path, self.preferences_path):
            pass
        from auto_app.core.db.store import DbStore
        from auto_app.settings import Paths

        home = self.db_path.parent.parent if self.db_path.parent.name == "data" else self.db_path.parent
        store = DbStore(self.db_path, Paths(home=home), migrate=False)
        changed = False
        try:
            with store.transaction(immediate=True):
                changed = apply_manual_decision_in_transaction(store, job_id, status, reason,
                                                               event_source=event_source)
        finally:
            store.engine.dispose()
        if changed and status == "want_to_apply":
            from auto_app.handoff.kick import kick

            kick(home)  # the package runner starts on it (deploy/jd-apply-queue.path)
        if changed and status == "applied":
            from auto_app.handoff.archive import freeze_package

            freeze_package(home.parent / "apply-kit", job_id)  # the package that went out (handoff/archive.py)
        return {"success": True, "changed": changed, "status": status, "job": self.get_job(job_id)}

    def record_event(self, job_id: int, kind: str, occurred_at: str | None = None,
                     note: str | None = None) -> dict:
        from auto_app.core.db.store import DbStore
        from auto_app.settings import Paths
        from auto_app.track.events import record_event, events_for

        when = None
        if occurred_at is not None:
            try:
                when = datetime.fromisoformat(occurred_at)
                when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                raise AgentError("occurred_at must be an ISO date/time") from None
        with QueryService(self.db_path, self.preferences_path):
            pass
        home = self.db_path.parent.parent if self.db_path.parent.name == "data" else self.db_path.parent
        store = DbStore(self.db_path, Paths(home=home), migrate=False)
        try:
            with store.transaction(immediate=True):
                event_id = record_event(store, job_id, kind, occurred_at=when, source="manual", note=note)
                events = events_for(store, job_id)
            return {"event_id": event_id, "events": events}
        except ValueError as exc:
            raise AgentError(str(exc)) from None
        finally:
            store.engine.dispose()

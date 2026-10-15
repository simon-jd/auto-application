# src/auto_app/agent/control.py
"""Pipeline control for chat tools and the Discord bot (docs/remote-mcp.md, "Control tools").

Writes are narrow and checked here, not in prompts: pausing writes the worker's control file; rule changes
are proposed with a preview, then applied only with the same change id and the unchanged file revision.
"""

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

INGEST_FIELDS = ("status", "running", "last_run_at", "last_finished_at", "next_run_at", "consecutive_failures")
PROPOSAL_TTL = timedelta(hours=24)  # a preview the user hasn't confirmed by then is shown again


class ControlError(ValueError):
    pass


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class ControlService:
    def __init__(self, db_path: Path, preferences_path: Path | None = None):
        self.db_path = Path(db_path).expanduser().resolve()
        self.preferences_path = preferences_path
        self.home = self.db_path.parent.parent if self.db_path.parent.name == "data" else self.db_path.parent
        self.rules_path = self.home / "config" / "screening.json"
        self.proposals = self.home / "config" / ".screening-proposals"

    @contextmanager
    def _rules_lock(self):
        from auto_app.ingest.scheduler import _lock, ImportLocked

        try:
            with _lock(self.rules_path.with_suffix(".lock"), "screening configuration change"):
                yield
        except ImportLocked:
            raise ControlError("another screening change is in progress; retry") from None

    def _store(self):
        from auto_app.core.db.store import DbStore
        from auto_app.settings import Paths
        from auto_app.query.service import QueryService

        with QueryService(self.db_path, self.preferences_path) as query:
            paths = Paths(home=self.home, preferences_file=query.preferences_path)
        return DbStore(self.db_path, paths, migrate=False)

    # ---- application mail review ------------------------------------------------------------

    def unmatched_updates(self) -> list[dict]:
        from auto_app.track.updates import unmatched

        store = self._store()
        try:
            return unmatched(store)
        finally:
            store.engine.dispose()

    def assign_update(self, update_id: int, job_id: int) -> dict:
        from auto_app.track.updates import assign

        store = self._store()
        try:
            return assign(store, update_id, job_id)
        finally:
            store.engine.dispose()

    def dismiss_update(self, update_id: int) -> dict:
        from auto_app.track.updates import dismiss

        store = self._store()
        try:
            return dismiss(store, update_id)
        finally:
            store.engine.dispose()

    # ---- status and pause -------------------------------------------------------------------

    def pipeline_status(self) -> dict:
        from auto_app.ingest import scheduler
        from auto_app.worker import loop
        from auto_app.worker.control import read_control
        from auto_app.worker.control import is_paused

        ingest = scheduler.status(self.db_path)
        sources = ((ingest.get("last_report") or {}).get("sources") or {})
        def health(value):
            allowed = {"ok", "error", "partial", "running", "waiting", "paused", "not_started", "busy",
                       "interrupted", "disabled", "skipped", "not_modified"}
            return {"status": value.get("status") if value.get("status") in allowed else "unknown",
                    "error": "Operation failed; inspect local service logs" if value.get("error") else None}

        def state_fields(value):
            return {**{k: value.get(k) for k in INGEST_FIELDS if k != "status"}, **health(value)}

        worker = loop.status(self.db_path)
        worker_public = state_fields(worker)
        worker_public["steps"] = {name: health(step) for name, step in
                                  ((worker.get("last_report") or {}).get("steps") or {}).items()
                                  if name in {"screen", "enrich", "rescreen", "notify", "summary", "health", "handoff"}}
        try:
            control = read_control(self.db_path)
            control["paused"] = is_paused(control, datetime.now(timezone.utc))
        except (OSError, ValueError):
            # The worker treats an unreadable control file as paused; resume() or pause() rewrites it.
            control = {"paused": True, "paused_until": None, "reason": None, "updated_at": None,
                       "error": "the worker control file is invalid; resume or pause rewrites it"}
        return {"ingest": {**state_fields(ingest), "sources": {name: health(r) for name, r in sources.items()}},
                "worker": worker_public, "control": control}

    def pause(self, until: str | None = None, reason: str | None = None) -> dict:
        from auto_app.worker.control import write_control

        when = None
        if until:
            try:
                when = datetime.fromisoformat(until)
            except (TypeError, ValueError):
                raise ControlError("until must be an ISO date/time") from None
            when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
            if when <= datetime.now(timezone.utc):
                raise ControlError("until must be in the future")
        return write_control(self.db_path, paused=True, paused_until=when, reason=(reason or "")[:200] or None)

    def resume(self) -> dict:
        from auto_app.worker.control import write_control

        return write_control(self.db_path, paused=False)

    # ---- screening rules ---------------------------------------------------------------------

    def _revision(self) -> str:
        return (hashlib.sha256(self.rules_path.read_bytes()).hexdigest()[:16]
                if self.rules_path.exists() else "default")

    def _screening_rules(self) -> dict:
        from auto_app.screen.rules import load_rules

        return {"rules": asdict(load_rules(self.rules_path)), "revision": self._revision()}

    def screening_rules(self) -> dict:
        with self._rules_lock():
            return self._screening_rules()

    def propose_screening_change(self, changes: dict) -> dict:
        with self._rules_lock():
            return self._propose_screening_change(changes)

    def _propose_screening_change(self, changes: dict) -> dict:
        from auto_app.screen.rules import rules_from_dict, screen
        from auto_app.screen.store import load_facts

        if not isinstance(changes, dict) or not changes:
            raise ControlError("changes must be a nonempty object")
        current = self._screening_rules()
        merged = {**current["rules"], **changes}
        proposed = rules_from_dict({k: list(v) if isinstance(v, tuple) else v for k, v in merged.items()},
                                   where="the proposed screening rules")
        before = rules_from_dict({k: list(v) if isinstance(v, tuple) else v for k, v in current["rules"].items()})
        store = self._store()
        try:
            facts = load_facts(store)
        finally:
            store.engine.dispose()
        newly_excluded, newly_kept, kept_before, kept_after = [], [], 0, 0
        for fact in facts:
            old, new = screen(fact, before).outcome, screen(fact, proposed).outcome
            kept_before += old == "kept"
            kept_after += new == "kept"
            if old != new:
                (newly_kept if new == "kept" else newly_excluded).append(
                    {"job_id": fact.job_id, "title": " / ".join(fact.titles)})
        self._prune_proposals()
        change_id = uuid4().hex
        _atomic_json(self.proposals / f"{change_id}.json",
            {"base_revision": current["revision"], "rules": asdict(proposed),
             "created_at": datetime.now(timezone.utc).isoformat()})
        diff = {key: [current["rules"][key], value] for key, value in asdict(proposed).items()
                if current["rules"][key] != value}
        return {"change_id": change_id, "base_revision": current["revision"], "diff": diff,
                "preview": {"kept_before": kept_before, "kept_after": kept_after,
                            "newly_excluded": len(newly_excluded), "newly_kept": len(newly_kept),
                            "examples": {"newly_excluded": newly_excluded[:10], "newly_kept": newly_kept[:10]}}}

    @staticmethod
    def _expired(proposal: dict) -> bool:
        try:
            created = datetime.fromisoformat(proposal["created_at"])
        except (KeyError, TypeError, ValueError):
            return True
        created = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - created > PROPOSAL_TTL

    def _prune_proposals(self) -> None:
        """Drop expired or unreadable proposals so stale previews neither pile up nor stay applicable."""
        if not self.proposals.is_dir():
            return
        for path in self.proposals.glob("*.json"):
            try:
                stale = self._expired(json.loads(path.read_text()))
            except (OSError, ValueError):
                stale = True
            if stale:
                path.unlink(missing_ok=True)

    def apply_screening_change(self, change_id: str, expected_revision: str) -> dict:
        with self._rules_lock():
            return self._apply_screening_change(change_id, expected_revision)

    def _apply_screening_change(self, change_id: str, expected_revision: str) -> dict:
        from auto_app.screen.rules import rules_from_dict

        if not isinstance(change_id, str) or not re.fullmatch(r"[a-f0-9]{32}", change_id):
            raise ControlError("unknown change id")
        path = self.proposals / f"{change_id}.json"
        if not path.exists():
            raise ControlError("unknown or already applied change id")
        try:
            proposal = json.loads(path.read_text())
            rules = asdict(rules_from_dict(proposal["rules"]))
            base_revision = proposal["base_revision"]
        except (ValueError, KeyError, TypeError, OSError):
            raise ControlError("invalid saved screening change; propose again") from None
        if self._expired(proposal):
            path.unlink(missing_ok=True)
            raise ControlError("this screening change expired (proposals last 24 hours); propose again")
        if expected_revision != base_revision or self._revision() != base_revision:
            raise ControlError("the rules file changed since this proposal (revision mismatch); propose again")
        _atomic_json(self.rules_path, rules)
        path.unlink()
        return {"revision": self._revision(), "rules": rules,
                "note": "the worker applies the new rules at its next round"}

    # ---- feedback and digest -----------------------------------------------------------------

    def record_filter_feedback(self, job_id: int, filter: str, should_recommend: bool,
                               reason: str | None = None) -> dict:
        from auto_app.select.review import Review

        if type(job_id) is not int or not 0 < job_id <= 9223372036854775807 or type(should_recommend) is not bool:
            raise ControlError("job id and feedback label are invalid")
        if not isinstance(filter, str) or not filter or (reason is not None and not isinstance(reason, str)):
            raise ControlError("filter and reason must be text")
        store = self._store()
        try:
            with store.transaction(immediate=True):
                latest = {row["filter"]: row for row in store.judgment_rows(job_id=job_id) if row["status"] != "error"}
                if filter not in latest:
                    raise ControlError(f"{filter} didn't judge job #{job_id}")
                review = Review(store, [SimpleNamespace(name=name, version=row["filter_version"])
                                        for name, row in latest.items()])
                review.label(job_id, filter, should_recommend, reason)
            return {"job_id": job_id, "filter": filter, "label": bool(should_recommend)}
        finally:
            store.engine.dispose()

    def digest(self, limit: int = 20) -> dict:
        from auto_app.worker import loop
        from auto_app.worker.digest import pending

        state = loop.status(self.db_path)
        since = state.get("notify_since")
        if not since:
            return {"jobs": [], "more": 0}
        store = self._store()
        try:
            jobs, more = pending(store, max(1, min(limit, 50)), datetime.fromisoformat(since),
                                 source_since=state.get("source_since"))
        finally:
            store.engine.dispose()
        keep = ("job_id", "title", "company", "locations", "posted_at", "sponsorship", "recommended", "reason",
                "title_only", "link")
        return {"jobs": [{k: j.get(k) for k in keep} for j in jobs], "more": more}

"""Small task-level interface: fixed fictional candidate, Ashby targets, no browser."""

from pathlib import Path
from urllib.parse import urlsplit

from auto_app.agent.service import AgentService

from .candidate import CANDIDATE_ID, fictional_candidate
from .store import RunStore
from .urls import normalize_ashby_url


class PreparationError(ValueError):
    """A preparation request cannot be accepted within the test-only contract."""


class PreparationService:
    def __init__(self, job_db: Path, preparation_dir: Path, preferences_path: Path | None = None):
        self.job_db = Path(job_db).expanduser().resolve()
        self.preparation_dir = Path(preparation_dir).expanduser().resolve()
        self.preferences_path = preferences_path
        if (self.preparation_dir / "preparation.sqlite3").resolve() == self.job_db:
            raise PreparationError("Preparation ledger must be separate from the job library")
        self.store = RunStore(self.preparation_dir)

    def capabilities(self) -> dict:
        from .worker import worker_status

        return {"enabled": True, "mode": "fictional_preparation_only", "candidate_id": CANDIDATE_ID,
                "supported_ats": ["ashby"], "automated_submission": False, "live_browser_handoff": False,
                "simplify": {"available": False, "interface_reserved": True},
                "worker": worker_status(self.preparation_dir)}

    def prepare_application(self, job_id: int | None = None, application_url: str | None = None) -> dict:
        if (job_id is None) == (application_url is None):
            raise PreparationError("Provide exactly one of job_id or application_url")
        if job_id is not None:
            target = self._saved_target(job_id)
        else:
            apply_url = normalize_ashby_url(application_url)
            target = {"job_id": None, "title": "Ashby preparation (title not verified)",
                      "company": urlsplit(apply_url).path.split("/")[1], "apply_url": apply_url}
        return self.store.enqueue(target, fictional_candidate())

    def get_application_run(self, run_id: str) -> dict:
        return self.store.get(run_id)

    def _saved_target(self, job_id: int) -> dict:
        if type(job_id) is not int or not 0 < job_id <= 9223372036854775807:
            raise PreparationError("job_id must be a positive integer")
        job = AgentService(self.job_db, self.preferences_path).get_job(job_id)
        if job is None:
            raise PreparationError(f"job #{job_id} is missing")
        if job.get("archived_at"):
            raise PreparationError(f"job #{job_id} is archived")
        if job.get("duplicate_of") is not None:
            raise PreparationError(f"job #{job_id} is a duplicate")
        if job.get("decision") == "applied":
            raise PreparationError(f"job #{job_id} is already applied")
        if (job.get("screening") or {}).get("excluded"):
            raise PreparationError(f"job #{job_id} is excluded by preferences")
        parsed = job.get("parsed") or {}
        postings = job.get("posting_details") or []
        primary = next((p for p in postings if p.get("posting_id") == job.get("primary_posting_id")), {})
        # Prefer the canonical/primary evidence; secondary postings only supply
        # a fallback when they agree on one supported target.
        for url in (job.get("canonical_url"), parsed.get("apply_url"), primary.get("apply_url"),
                    primary.get("source_url"), parsed.get("url")):
            try:
                apply_url = normalize_ashby_url(url)
                break
            except ValueError:
                continue
        else:
            alternatives = set()
            for posting in postings:
                for field in ("apply_url", "source_url"):
                    try:
                        alternatives.add(normalize_ashby_url(posting.get(field)))
                    except ValueError:
                        pass
            if len(alternatives) != 1:
                raise PreparationError(f"job #{job_id} needs one verified Ashby application URL")
            apply_url = alternatives.pop()
        return {"job_id": job_id, "title": job.get("title") or "", "company": job.get("company") or "",
                "apply_url": apply_url}

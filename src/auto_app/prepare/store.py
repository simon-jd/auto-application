"""Durable idempotent preparation ledger, separate from the read-only job library."""

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from uuid import UUID, uuid4

from .candidate import canonical_json, validate_candidate
from .urls import normalize_ashby_url


TERMINAL_STATUSES = ("ready_for_review", "needs_input", "action_needed", "failed", "interrupted")
STATUSES = ("queued", "running", *TERMINAL_STATUSES)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_id(value: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError) as exc:
        raise ValueError("Invalid preparation run_id") from exc
    return value


class RunStore:
    """Each operation owns its connection; enqueue and claim are serialized in SQLite."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "preparation.sqlite3"
        # Do not switch journal modes per connection: that can race first-time
        # concurrent constructors. SQLite serializes this idempotent DDL itself.
        with self._connect() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS preparation_runs (
                run_id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL CHECK(status IN ('queued','running','ready_for_review',
                    'needs_input','action_needed','failed','interrupted')),
                job_id INTEGER, title TEXT, company TEXT, apply_url TEXT NOT NULL,
                candidate TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt BETWEEN 0 AND 2), result TEXT)""")

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _decode(row) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        result.pop("idempotency_key", None)
        result["candidate"] = json.loads(result["candidate"])
        result["result"] = json.loads(result["result"]) if result["result"] else None
        return result

    def enqueue(self, target: dict, candidate: dict) -> dict:
        validate_candidate(candidate)
        apply_url = normalize_ashby_url(target.get("apply_url"))
        job_id = target.get("job_id")
        if job_id is not None and (type(job_id) is not int or not 0 < job_id <= 9223372036854775807):
            raise ValueError("job_id must be a positive integer")
        for key in ("title", "company"):
            if not isinstance(target.get(key), str):
                raise ValueError(f"target {key} must be a string")
        key = sha256(canonical_json([apply_url, candidate["package_hash"]]).encode()).hexdigest()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM preparation_runs WHERE idempotency_key=?", (key,)).fetchone()
            if existing:
                return self._decode(existing)
            run_id, now = str(uuid4()), _now()
            artifact_dir = self.root / "runs" / run_id
            artifact_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            resume = artifact_dir / "fictional-sales-resume.pdf"
            content = Path(candidate["resume_path"]).read_bytes()
            if sha256(content).hexdigest() != candidate["resume_sha256"]:
                raise ValueError("Fictional candidate resume changed while enqueueing")
            # Snapshot is published before its ledger row, so no worker can
            # claim a run with a partially written attachment.
            with resume.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            resume.chmod(0o400)
            snap = json.loads(canonical_json(candidate))
            snap["resume_path"] = str(resume)
            snap["profile"]["cv"] = str(resume)
            conn.execute("""INSERT INTO preparation_runs
                (run_id,idempotency_key,status,job_id,title,company,apply_url,candidate,created_at,updated_at)
                VALUES (?,?,'queued',?,?,?,?,?,?,?)""",
                         (run_id, key, job_id, target["title"], target["company"], apply_url,
                          canonical_json(snap), now, now))
            return self._decode(conn.execute("SELECT * FROM preparation_runs WHERE run_id=?", (run_id,)).fetchone())

    def get(self, run_id: str) -> dict:
        run_id = _run_id(run_id)
        with self._connect() as conn:
            run = self._decode(conn.execute("SELECT * FROM preparation_runs WHERE run_id=?", (run_id,)).fetchone())
        if run is None:
            raise ValueError("Preparation run is missing")
        return run

    def claim(self) -> dict | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("""SELECT run_id FROM preparation_runs WHERE status='queued' AND attempt<2
                                  ORDER BY created_at,run_id LIMIT 1""").fetchone()
            if row is None:
                return None
            conn.execute("""UPDATE preparation_runs SET status='running',attempt=attempt+1,result=NULL,updated_at=?
                            WHERE run_id=?""", (_now(), row["run_id"]))
            return self._decode(conn.execute("SELECT * FROM preparation_runs WHERE run_id=?", (row["run_id"],)).fetchone())

    def finish(self, run_id: str, result: dict) -> dict:
        run_id = _run_id(run_id)
        if not isinstance(result, dict) or result.get("status") not in TERMINAL_STATUSES:
            raise ValueError("Invalid terminal preparation status")
        encoded = canonical_json(result)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute("""UPDATE preparation_runs SET status=?,result=?,updated_at=?
                                      WHERE run_id=? AND status='running'""",
                                   (result["status"], encoded, _now(), run_id)).rowcount
            if not changed:
                raise ValueError("Only a running preparation run can finish")
            return self._decode(conn.execute("SELECT * FROM preparation_runs WHERE run_id=?", (run_id,)).fetchone())

    def recover_interrupted(self) -> int:
        """Called by the worker only while holding its exclusive process lock.

        A crashed preparation may retry once because submission is structurally
        unavailable. Second interruption is terminal and never silently retried.
        """
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT run_id,attempt FROM preparation_runs WHERE status='running'").fetchall()
            for row in rows:
                status = "queued" if row["attempt"] < 2 else "interrupted"
                result = {"status": "interrupted", "reason": "Worker interrupted before a durable result",
                          "stopped_before_submit": True, "retry_queued": status == "queued"}
                conn.execute("UPDATE preparation_runs SET status=?,result=?,updated_at=? WHERE run_id=?",
                             (status, canonical_json(result), _now(), row["run_id"]))
            return len(rows)

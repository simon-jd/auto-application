"""Weekly board proposals: unconnected Greenhouse, Ashby and Lever boards offered to the owner on Discord.

Once a week the Discord bot picks up to LIMIT boards that have kept jobs but no feed. They are ranked by the owner's
applied and wanted jobs at the company, then by kept jobs. Boards are left out when they were proposed before (added
or skipped), look like data-annotation, translation or staffing vendors (by name, or on the VENDOR_BOARDS list),
or belong to companies the owner skipped twice and never wanted. "Add" appends a feed to the runtime's feeds.yaml with `every_hours: EVERY_HOURS`, after a
dated backup and a validation of the new file as the feed worker would load it. The new file replaces the old one
atomically, and the feed worker reads it at its next round. Nothing is added without the owner's click, and no
model is involved.
"""

import json
import os
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import yaml
from sqlalchemy import insert, select, update

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow

LIMIT = 10  # proposals per week
WEEK = timedelta(days=7)
EVERY_HOURS = 6
PLATFORMS = {"greenhouse": "greenhouse-board", "ashby": "ashby-board", "lever": "lever-board"}
BOARD_PAGES = {"greenhouse": "https://job-boards.greenhouse.io/{board}", "ashby": "https://jobs.ashbyhq.com/{board}",
               "lever": "https://jobs.lever.co/{board}"}
APPLY_HOSTS = {"greenhouse": "greenhouse.io", "ashby": "ashbyhq.com", "lever": "lever.co"}
VENDOR = re.compile(r"\b(?:annotation|data\s+label\w*|translation|localization|staffing|outsourc\w*|"
                    r"talent\s+solutions|recruit\w*)\b", re.I)
# Vendors whose names say nothing about it: AI-training, data and language work the owner left out of the long-tail
# boards on 2026-11-18, yet proposed in the first week (2026-11-19). Matched by board or by company name.
VENDOR_BOARDS = {("lever", "tsmg"), ("greenhouse", "innodatainc"), ("lever", "rws"), ("lever", "weloglobal"),
                 ("greenhouse", "agency"), ("greenhouse", "prolificacademicltd")}
VENDOR_COMPANIES = re.compile(r"(?:tsmg|innodata|rws|welo(?:\s+global)?|invisible\s+technologies|prolific)"
                              r"(?:\s*,?\s*(?:inc|ltd|llc|plc|holdings?|group)\.?)*", re.I)
EXAMPLES = 3


class BoardProposalError(ValueError):
    pass


def _aware(value):
    if value is None:
        return None
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _same(location) -> str:
    return unquote(str(location)).strip().lower().rstrip("/")


def _vendor(board: dict) -> bool:
    company = (board.get("company") or "").strip()
    return bool(VENDOR.search(company) or VENDOR_COMPANIES.fullmatch(company)
                or (board["platform"], str(board["board"]).lower()) in VENDOR_BOARDS)


class BoardProposals:
    def __init__(self, db_path: Path, config_path: Path | None = None):
        self.db_path = Path(db_path).expanduser().resolve()
        self.home = self.db_path.parent.parent if self.db_path.parent.name == "data" else self.db_path.parent
        self.config_path = Path(config_path) if config_path else self.home / "config" / "feeds.yaml"

    def _store(self):
        from auto_app.core.db.store import DbStore
        from auto_app.settings import Paths

        return DbStore(self.db_path, Paths(home=self.home), migrate=False)

    @staticmethod
    def due(last: str | None, now: datetime) -> bool:
        """Whether a week has passed since the last generation (none yet: due)."""
        return last is None or _aware(now) - _aware(last) >= WEEK

    def _sources(self) -> dict:
        data = yaml.safe_load(self.config_path.read_text()) if self.config_path.exists() else None
        return (data or {}).get("sources") or {}

    @staticmethod
    def _row(row) -> dict:
        item = dict(row)
        for key in ("created_at", "posted_at", "decided_at"):
            item[key] = _aware(item[key]).isoformat() if item.get(key) else None
        item["board_page"] = BOARD_PAGES[item["platform"]].format(board=item["board"])
        return item

    # ---- choosing ------------------------------------------------------------------------------------------------

    def generate(self, now: datetime | None = None, limit: int = LIMIT) -> list[dict]:
        """Save and return this week's proposals (at most `limit`); an empty list when nothing new qualifies."""
        from auto_app.worker.boards import suggest_boards

        now = _aware(now or utcnow())
        configured = {_same(v.get("location")) for v in self._sources().values() if isinstance(v, dict)}
        store = self._store()
        try:
            boards = suggest_boards(store, configured=configured, top=5000, min_jobs=1)
            with store.engine.connect() as conn:
                proposed = {_same(r.location) for r in conn.execute(select(T.board_proposals.c.location))}
                decisions: dict[str, Counter] = defaultdict(Counter)
                j, p = T.jobs, T.postings
                for company, decision in conn.execute(
                        select(p.c.company, j.c.decision).select_from(j.join(p, p.c.posting_id == j.c.primary_posting_id))
                        .where(j.c.decision.is_not(None))):
                    decisions[(company or "").lower()][decision] += 1
            candidates = []
            for board in boards:
                if board["platform"] not in PLATFORMS or board.get("configured") or _same(board["location"]) in configured \
                        or _same(board["location"]) in proposed or _vendor(board):
                    continue
                seen = decisions.get((board.get("company") or "").lower(), Counter())
                if seen["not_applied"] >= 2 and not seen["applied"] and not seen["want_to_apply"]:
                    continue
                candidates.append({**board, "applied": seen["applied"], "wanted": seen["want_to_apply"],
                                   "skipped": seen["not_applied"]})
            candidates.sort(key=lambda b: (-(b["applied"] + b["wanted"]), -b["kept_jobs"], b["board"]))
            made = []
            with store.engine.begin() as conn:
                for board in candidates[:limit]:
                    proposal_id = conn.execute(insert(T.board_proposals).values(
                        platform=board["platform"], board=board["board"], company=board.get("company"),
                        location=board["location"], kept_jobs=board["kept_jobs"], applied=board["applied"],
                        wanted=board["wanted"], skipped=board["skipped"],
                        examples=self._examples(conn, board["platform"], board["board"]),
                        created_at=now)).inserted_primary_key[0]
                    made.append(self._row(conn.execute(select(T.board_proposals).where(
                        T.board_proposals.c.proposal_id == proposal_id)).mappings().one()))
            return made
        finally:
            store.engine.dispose()

    @staticmethod
    def _examples(conn, platform: str, board: str) -> list[str]:
        p, j, s = T.postings, T.jobs, T.job_screenings
        pattern = f"%{APPLY_HOSTS[platform]}%/{board}/%"
        titles = conn.execute(select(p.c.title).select_from(p.join(j, j.c.job_id == p.c.job_id)
                                                            .join(s, s.c.job_id == j.c.job_id))
                              .where(s.c.outcome == "kept", j.c.archived_at.is_(None), p.c.apply_url.like(pattern))
                              .distinct().limit(EXAMPLES)).scalars().all()
        return [t for t in titles if t]

    # ---- Discord -------------------------------------------------------------------------------------------------

    def to_post(self) -> list[dict]:
        bp = T.board_proposals
        store = self._store()
        try:
            with store.engine.connect() as conn:
                return [self._row(r) for r in conn.execute(select(bp).where(bp.c.posted_at.is_(None),
                                                                            bp.c.decision.is_(None))
                                                           .order_by(bp.c.proposal_id)).mappings()]
        finally:
            store.engine.dispose()

    def mark_posted(self, proposal_id: int, message_id: str, now: datetime | None = None) -> None:
        store = self._store()
        try:
            with store.engine.begin() as conn:
                conn.execute(update(T.board_proposals).where(T.board_proposals.c.proposal_id == proposal_id)
                             .values(posted_at=_aware(now or utcnow()), message_id=str(message_id)))
        finally:
            store.engine.dispose()

    def decide(self, proposal_id: int, decision: str, now: datetime | None = None) -> dict:
        """The owner's click: "add" appends the feed (every_hours 6), "skip" never proposes the board again."""
        if decision not in ("add", "skip"):
            raise BoardProposalError("decision must be add or skip")
        now = _aware(now or utcnow())
        bp = T.board_proposals
        store = self._store()
        try:
            with store.engine.connect() as conn:
                row = conn.execute(select(bp).where(bp.c.proposal_id == proposal_id)).mappings().first()
            if row is None:
                raise BoardProposalError(f"board proposal #{proposal_id} not found")
            if row["decision"] is not None:
                raise BoardProposalError(f"board proposal #{proposal_id} is already {row['decision']}")
            feed = self._append_feed(dict(row), now) if decision == "add" else None
            with store.engine.begin() as conn:
                conn.execute(update(bp).where(bp.c.proposal_id == proposal_id).values(
                    decision="added" if feed else "skipped", decided_at=now, feed=feed))
        finally:
            store.engine.dispose()
        return {"proposal_id": proposal_id, "decision": "added" if feed else "skipped", "feed": feed}

    # ---- feeds.yaml ----------------------------------------------------------------------------------------------

    def _append_feed(self, row: dict, now: datetime) -> str:
        from auto_app.ingest.runner import load_feed_config

        path = self.config_path
        original = path.read_text()
        sources = self._sources()
        if _same(row["location"]) in {_same(v.get("location")) for v in sources.values() if isinstance(v, dict)}:
            raise BoardProposalError(f"{row['company'] or row['board']} is already in feeds.yaml")
        slug = re.sub(r"[^a-z0-9_-]+", "-", row["board"].lower()).strip("-") or "board"
        base = name = f"board-{row['platform']}-{slug}"
        number = 2
        while name in sources:
            name, number = f"{base}-{number}", number + 1
        entry = (f"  {name}:\n    kind: repository\n    format: {PLATFORMS[row['platform']]}\n"
                 f"    company: {json.dumps(row['company'] or row['board'], ensure_ascii=False)}\n"
                 f"    location: {row['location']}\n    every_hours: {EVERY_HOURS}\n")
        text = original + ("" if original.endswith("\n") else "\n") + \
            f"  # added from the weekly board proposal #{row['proposal_id']} on {now:%Y-%m-%d}\n" + entry
        fd, temporary = tempfile.mkstemp(prefix=".feeds.", suffix=".yaml", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(text)
            try:
                os.chmod(temporary, path.stat().st_mode & 0o777)
                loaded = load_feed_config(Path(temporary))
                new_sources = yaml.safe_load(text)["sources"]
            except (ValueError, OSError, yaml.YAMLError) as exc:
                raise BoardProposalError(f"the new feeds.yaml did not validate, nothing changed: {exc}") from None
            if name not in loaded or any(new_sources.get(k) != v for k, v in sources.items()):
                raise BoardProposalError("the new entry did not land under sources, nothing changed")
            backup = path.with_name(f"{path.name}.bak-before-board-proposals-{now:%Y%m%d}")
            if not backup.exists():
                shutil.copy2(path, backup)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return name

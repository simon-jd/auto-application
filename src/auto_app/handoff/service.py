"""Queue, claims, packets and outcomes for your own application agent (docs/agent-handoff.md).

Guardrails live here, not in an agent's instructions: one live claim per job; claims expire; a daily
submission limit; a per-company cooldown; no job that is applied, archived, rule-excluded or a repost of
one you applied to. A submitted outcome marks the job applied through the same service as chat tools.
"""

import hashlib
import ipaddress
import json
import re
import secrets
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import yaml
from sqlalchemy import delete, func, insert, inspect, select, update

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow
from auto_app.handoff.evidence import closed, feed_states, gone_links
from auto_app.handoff.guard import load_guard
from auto_app.handoff.archive import freeze_package, frozen_at
from auto_app.handoff.kick import kick

OUTCOMES = {  # agent outcome → (applications.status, attempts.outcome, job decision or None)
    "submitted": ("submitted", "submitted", "applied"),
    "needs_user": ("needs_input", "needs_input", None),
    "blocked": ("action_needed", "action_needed", None),
    "failed": ("failed", "failed", None),
    "skipped": ("dropped", "unsupported", "not_applied"),
}
PARKED = ("needs_input", "action_needed")  # out of the queue until you requeue
NEEDS_ANSWERS = "needs_answers"  # out of the queue until the owner marks the answers published (answers_ready)
HELD = "ready"  # the form is filled and waits for the user's go; its claim lasts hold_hours (hold)
HOLD_NOTE = "form filled; waiting for the user's go"  # the note hold sets; release clears it
MAX_QUESTIONS = 60
MAX_QUESTION_TEXT = 4000
MAX_QUESTIONS_BYTES = 64 * 1024
QUESTION_FIELDS = ("text", "required", "limit", "page", "kind")
# "question": an open question on the form that the package does not answer; "change": the user's own words asking
# for a change to the materials (an answer, the resume or the cover letter), passed on verbatim at the pre-submit check
QUESTION_KINDS = ("question", "change")
APPLIER = "external-agent"  # the applier chain; an attempt records the agent that actually ran
FAILED_ATTEMPT_LIMIT = 3  # consecutive failures before a job is parked instead of returned to the queue
# The published application packages (jd-resume-tailor's apply_kit.py writes them) sit beside the runtime:
# job-search/apply-kit next to job-search/runtime. Reading them needs no claim token.
APPLY_KIT_DIR = "apply-kit"
KIT_FILES = ("job.json", "fields.json", "jd.txt", "resume.pdf", "cover_letter.pdf", "answers.md", "message.txt")
KIT_GUIDES = {"cheatsheet": "answers.md", "readme": "README.md"}  # apply-kit/answers.md is the agents' cheat sheet
MAX_KIT_BYTES = 10 * 1024 * 1024
DOCUMENTS_DIR = "documents"  # apply-kit/documents/: the user's own files a form may ask for (a transcript, say)
DOCUMENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
RUNNER_FILE = "runner.json"  # the package runner's state (deploy/jd-apply-queue.sh writes it)
MAX_REQUEST_TEXT = 1000  # a change request's characters (request_change), and a cheat-sheet request's
CHEATSHEET_OUTCOMES = ("applied", "declined")
RECENT_CHEATSHEET = 20  # done cheat-sheet requests listed, newest first


class HandoffError(ValueError):
    pass


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _plain(value, depth=0):
    if depth > 12:
        return False
    if isinstance(value, dict):
        return all(isinstance(k, str) and _plain(v, depth + 1) for k, v in value.items())
    if isinstance(value, list):
        return all(_plain(v, depth + 1) for v in value)
    return value is None or isinstance(value, (str, int, float, bool))


def _note(value):
    if value is not None and (not isinstance(value, str) or len(value) > 2000):
        raise HandoffError('note must be a string of at most 2000 characters or null')
    if isinstance(value, str) and HandoffService.SECRET_KEYS.search(value):
        raise HandoffError('passwords and other secrets must never be included in notes')


def _job_id(value):
    if type(value) is not int or not 0 < value <= 9223372036854775807:
        raise HandoffError("job_id must be a positive integer")


def _safe_link(value):
    if not isinstance(value, str) or any(ord(c) < 33 or ord(c) == 127 for c in value) or "\\" in value:
        return False
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower()
        if parsed.scheme != 'https' or not host or parsed.username or parsed.password:
            return False
        if parsed.port not in (None, 443) or '.' not in host or host.endswith(('.localhost', '.local')):
            return False
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return len(host) <= 253 and bool(re.fullmatch(
                r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', host))
        return False
    except ValueError:
        return False


def _control_free(text: str) -> bool:
    """No control characters apart from line breaks and tabs, and no Unicode line or paragraph separators."""
    return not any((unicodedata.category(c) == "Cc" and c not in "\n\t") or c in "\u2028\u2029" for c in text)


def _questions(value) -> list[dict]:
    """The open questions an agent hands back, each {text, required, limit, page}; anything else is refused."""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_QUESTIONS:
        raise HandoffError(f"questions must be a list of 1 to {MAX_QUESTIONS} objects")
    cleaned = []
    for item in value:
        if not isinstance(item, dict):
            raise HandoffError("each question must be an object with text, and optionally required, limit, page "
                               "and kind")
        unknown = sorted(str(k) for k in item if k not in QUESTION_FIELDS)
        if unknown:
            raise HandoffError(f"unknown question fields: {', '.join(unknown)} (allowed: {', '.join(QUESTION_FIELDS)})")
        text = item.get("text")
        text = text.strip() if isinstance(text, str) else ""
        if not text or len(text) > MAX_QUESTION_TEXT:
            raise HandoffError(f"question text must be 1 to {MAX_QUESTION_TEXT} characters")
        if not _control_free(text):
            raise HandoffError("question text must not contain control characters (line breaks and tabs are fine)")
        required = item.get("required")
        if required is not None and type(required) is not bool:
            raise HandoffError("required must be true, false or null")
        limit = item.get("limit")
        if limit is not None and (not isinstance(limit, str) or not limit.strip() or len(limit) > 200
                                  or "\n" in limit or not _control_free(limit)):
            raise HandoffError('limit must be one short line such as "300 words", or null')
        page = item.get("page")
        if page is not None and (type(page) is not int or not 1 <= page <= 100):
            raise HandoffError("page must be a whole number from 1 to 100, or null")
        kind = item.get("kind", "question")
        if kind not in QUESTION_KINDS:
            raise HandoffError(f"kind must be one of: {', '.join(QUESTION_KINDS)}")
        cleaned.append({"text": text, "required": required, "limit": limit.strip() if limit else None, "page": page,
                        "kind": kind})
    if len(json.dumps(cleaned, ensure_ascii=False).encode()) > MAX_QUESTIONS_BYTES:
        raise HandoffError("questions are too long (at most 64 KiB in all)")
    return cleaned


class HandoffService:
    def __init__(self, db_path: Path, preferences_path: Path | None = None):
        self.db_path = Path(db_path).expanduser().resolve()
        self.preferences_path = preferences_path
        self.home = self.db_path.parent.parent if self.db_path.parent.name == "data" else self.db_path.parent

    def _store(self):
        from auto_app.core.db.store import DbStore
        from auto_app.settings import Paths
        from auto_app.query.service import QueryService

        with QueryService(self.db_path, self.preferences_path):
            pass
        return DbStore(self.db_path, Paths(home=self.home), migrate=False)

    def guard(self):
        return load_guard(self.home / "config" / "handoff.json")

    # ---- eligibility ---------------------------------------------------------------------------

    def _applied_events(self, conn):
        """P3's explicit applied events are submission evidence, when that migration is present."""
        table = getattr(T, 'application_events', None)
        if table is None or not inspect(conn).has_table(table.name):
            return []
        return conn.execute(select(table.c.job_id, table.c.occurred_at)
                            .where(table.c.kind == 'applied')).all()

    def _usage(self, conn, now, *, exclude_claim_job=None, guard=None):
        """Count each job once, including reported manual submissions and live reservations."""
        guard = guard or self.guard()
        # The daily limit follows your calendar day, not UTC's (00:00 UTC is mid-evening in the US).
        local = now.astimezone(ZoneInfo(guard.timezone) if guard.timezone else None)
        day = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        window = now - timedelta(days=guard.company_window_days)
        j = T.jobs
        jobs = {r['job_id']: r for r in conn.execute(
            select(j.c.job_id, j.c.company_norm, j.c.decision, j.c.decided_at)).mappings()}
        submissions = {}

        def add(job_id, at):
            at = _aware(at)
            if at is not None and at <= now:
                submissions[job_id] = min(submissions.get(job_id, at), at)
        for row in conn.execute(select(T.applications.c.job_id, T.applications.c.submitted_at)
                                .where(T.applications.c.submitted_at.is_not(None))):
            add(row.job_id, row.submitted_at)
        for row in conn.execute(select(T.reviews.c.job_id, T.reviews.c.created_at)
                                .where(T.reviews.c.decision == 'applied')):
            add(row.job_id, row.created_at)
        for row in self._applied_events(conn):
            add(row.job_id, row.occurred_at)
        for job_id, job in jobs.items():
            if job['decision'] == 'applied':
                add(job_id, job['decided_at'])
        daily = {job_id for job_id, at in submissions.items() if at >= day}
        groups = guard.company_keys()  # names listed together in handoff.json share one company limit

        def company_of(job_id):
            name = (jobs.get(job_id) or {}).get('company_norm') or ''
            return groups.get(name, name)
        company = {}
        for job_id, at in submissions.items():
            if at >= window:
                company.setdefault(company_of(job_id), set()).add(job_id)
        submitted_today = len(daily)
        for row in conn.execute(select(T.handoff_claims).where(T.handoff_claims.c.lease_until > now)).mappings():
            job_id = row['job_id']
            if job_id == exclude_claim_job or job_id in submissions:
                continue
            daily.add(job_id)
            company.setdefault(company_of(job_id), set()).add(job_id)
        return len(daily), {name: len(ids) for name, ids in company.items()}, submitted_today

    def _candidates(self, conn, now: datetime, *, ignore_claim_job=None, only_job=None):
        """Evaluate saved evidence and every guardrail on the caller's transaction snapshot.

        Evidence is read only for the jobs being judged (every wanted job, or `only_job`), so a claim
        or packet holding the write lock does not scan the whole posting history.
        """
        from auto_app.enrich.targets import target_for
        from auto_app.preferences import load_preferences, preferences_for_database

        guard = self.guard()
        preference_path = self.preferences_path or preferences_for_database(self.db_path)
        preferences = load_preferences(Path(preference_path), required=self.preferences_path is not None)
        daily_count, company_counts, submitted_today = self._usage(conn, now, exclude_claim_job=ignore_claim_job, guard=guard)
        j, p = T.jobs, T.postings
        judged = [j.c.archived_at.is_(None), j.c.decision == 'want_to_apply']
        if only_job is not None:
            judged.append(j.c.job_id == only_job)
        wanted = select(j.c.job_id).where(*judged)
        rows = conn.execute(select(j, p.c.title, p.c.company, p.c.apply_url)
                            .select_from(j.outerjoin(p, p.c.posting_id == j.c.primary_posting_id))
                            .where(*judged)
                            .order_by(j.c.decided_at, j.c.job_id)).mappings().all()
        applied_history = set(conn.execute(select(T.reviews.c.job_id)
                                           .where(T.reviews.c.decision == "applied",
                                                  T.reviews.c.job_id.in_(wanted))).scalars())
        applied_history.update(row.job_id for row in self._applied_events(conn))
        applied_jobs = set(conn.execute(select(j.c.job_id).where(j.c.decision == 'applied')).scalars())
        screenings = {r['job_id']: r for r in conn.execute(
            select(T.job_screenings).where(T.job_screenings.c.job_id.in_(wanted))).mappings()}
        claims = {r['job_id']: r for r in conn.execute(
            select(T.handoff_claims).where(T.handoff_claims.c.job_id.in_(wanted))).mappings()}
        apps = {}
        for row in conn.execute(select(T.applications).where(T.applications.c.job_id.in_(wanted))
                                .order_by(T.applications.c.application_id)).mappings():
            apps.setdefault(row['job_id'], []).append(row)
        locations, countries = {}, {}
        for job_id, location in conn.execute(select(T.job_locations.c.job_id, T.job_locations.c.location_norm)
                                             .where(T.job_locations.c.job_id.in_(wanted))):
            locations.setdefault(job_id, []).append(location)
        for job_id, parsed in conn.execute(select(p.c.job_id, p.c.parsed).where(p.c.job_id.in_(wanted))):
            raw = (parsed or {}).get('locations') or []
            if not isinstance(raw, list) or any(not isinstance(v, str) for v in raw):
                raise HandoffError('invalid saved location evidence')
            locations.setdefault(job_id, []).extend(raw)
            countries.setdefault(job_id, []).append(((parsed or {}).get('extras') or {}).get('country'))
        if preferences.exclude_countries:
            for job_id, country in conn.execute(select(p.c.job_id,
                    func.json_extract(T.ingest_entries.c.payload, '$.metadata.upstream.country'))
                    .join(p, p.c.posting_id == T.ingest_entries.c.posting_id).where(p.c.job_id.in_(wanted))):
                countries.setdefault(job_id, []).append(country)
        source_states = feed_states(conn, wanted)
        gone = gone_links(conn)
        groups = guard.company_keys()
        items, blocked = [], {}
        for row in rows:
            job_id = row['job_id']
            screen = screenings.get(job_id) or {}
            app_rows = apps.get(job_id, [])
            claim = claims.get(job_id)
            link = row['apply_url'] or row['canonical_url']
            warnings = row['warnings'] or []
            if not isinstance(warnings, list) or any(not isinstance(w, dict) for w in warnings):
                raise HandoffError('invalid saved repost evidence')
            archive = row['archive'] or {}
            locs = [*locations.get(job_id, []), *(archive.get('locations') or []), *(archive.get('raw_locations') or [])]
            reason = None
            if row['duplicate_of'] is not None:
                reason = 'duplicate'
            elif preferences.evaluate(locs, countries=[*countries.get(job_id, []),
                                                       *(archive.get('countries') or [])])['excluded']:
                reason = 'excluded_country'
            elif screen.get('outcome') == 'excluded':
                reason = 'screened_out'
            elif any(w.get('kind') == 'repost' and ('applied' in str(w.get('text') or '').lower()
                     or (type(w.get('job_id')) is int and w['job_id'] in applied_jobs)) for w in warnings):
                reason = 'already_applied_repost'
            elif job_id in applied_history or any(a['status'] == 'submitted' or a['submitted_at'] is not None for a in app_rows):
                reason = 'already_submitted'
            elif app_rows and app_rows[-1]['status'] == NEEDS_ANSWERS:
                reason = 'waiting_for_answers'
            elif app_rows and app_rows[-1]['status'] in PARKED:
                reason = 'waiting_for_you'
            elif claim and job_id != ignore_claim_job and _aware(claim['lease_until']) > now:
                reason = 'claimed'
            elif closed(source_states.get(job_id)):
                reason = 'source_inactive'
            elif link in gone:
                reason = 'link_gone'
            elif not _safe_link(link):
                reason = 'invalid_apply_url'
            elif company_counts.get(groups.get(row['company_norm'] or '', row['company_norm'] or ''), 0) \
                    >= guard.company_limit:
                reason = 'company_cooldown'
            if reason:
                blocked[reason] = blocked.get(reason, 0) + 1
                continue
            target = target_for(link)
            items.append({'job_id': job_id, 'title': row['title'], 'company': row['company'], 'apply_url': link,
                          'platform': target.platform if target else 'other',
                          'sponsorship': (screen.get('signals') or {}).get('sponsorship', 'unknown'),
                          'decided_at': _aware(row['decided_at']).isoformat() if row['decided_at'] else None})
        return items, blocked, daily_count >= guard.daily_submissions, submitted_today

    # ---- queue and claims ----------------------------------------------------------------------

    def queue(self, limit: int = 20, now: datetime | None = None) -> dict:
        now = _aware(now or utcnow()).astimezone(timezone.utc)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise HandoffError("limit must be an integer between 1 and 100")
        store = self._store()
        try:
            with store.engine.connect() as conn:
                items, blocked, daily, today = self._candidates(conn, now)
        finally:
            store.engine.dispose()
        return {"items": [] if daily else items[:limit], "total": 0 if daily else len(items),
                "blocked": {"daily_limit": daily, "by_reason": blocked}, "submitted_today": today}

    def claim(self, job_id: int, agent: str, now: datetime | None = None) -> dict:
        from auto_app.enrich.targets import target_for

        now = _aware(now or utcnow()).astimezone(timezone.utc)
        _job_id(job_id)
        if not isinstance(agent, str) or not agent.strip() or len(agent) > 80:
            raise HandoffError("agent must be a short name")
        agent = agent.strip()
        store = self._store()
        try:
            with store.transaction(immediate=True):
                conn = store._tx
                # The agent that held a filled form (hold) may pick it up again, e.g. from a new chat once the user
                # says "submit": it gets a fresh token, and the hold keeps its end. Anyone else still sees it claimed.
                held = self._own_hold(conn, job_id, agent, now)
                items, blocked, daily, _ = self._candidates(conn, now, only_job=job_id,
                                                            ignore_claim_job=job_id if held else None)
                if daily:
                    raise HandoffError("the daily submission limit is reached; try tomorrow")
                item = next((i for i in items if i["job_id"] == job_id), None)
                if item is None:
                    state = conn.execute(select(T.handoff_claims.c.lease_until)
                                         .where(T.handoff_claims.c.job_id == job_id)).scalar()
                    if state is not None and _aware(state) > now:
                        raise HandoffError(f"job #{job_id} is claimed until {_aware(state).isoformat()}")
                    raise HandoffError(f"job #{job_id} is not in the queue (applied, parked, company cooldown "
                                       f"or not want_to_apply)")
                token = secrets.token_urlsafe(24)
                while token.startswith("-"):  # a command line would read it as an option, not as `packet TOKEN`
                    token = secrets.token_urlsafe(24)
                lease = now + timedelta(minutes=self.guard().lease_minutes)
                if held:
                    lease = max(lease, _aware(held["lease_until"]))
                conn.execute(delete(T.handoff_claims).where(T.handoff_claims.c.job_id == job_id))
                conn.execute(insert(T.handoff_claims).values(job_id=job_id, agent=agent,
                                                             token_hash=_hash(token), claimed_at=now,
                                                             lease_until=lease))
                app = self._latest_application(conn, job_id)
                app_id = app["application_id"] if app is not None else None
                if app_id is None:
                    target = target_for(item["apply_url"])
                    app_id = conn.execute(insert(T.applications).values(
                        job_id=job_id, platform=target.platform if target else "other",
                        apply_url=item["apply_url"], status="queued", chain=[APPLIER], chain_pos=0,
                        applier=APPLIER, created_at=now, updated_at=now)).inserted_primary_key[0]
                else:
                    # A fresh claim after an expired hold must not keep the hold's note; any other note stays.
                    note = app["note"] if held or app["note"] != HOLD_NOTE else None
                    conn.execute(update(T.applications).where(T.applications.c.application_id == app_id).values(
                        apply_url=item['apply_url'], platform=item['platform'], status=HELD if held else 'queued',
                        applier=APPLIER, note=note, updated_at=now))
        finally:
            store.engine.dispose()
        return {"token": token, "lease_until": lease.isoformat(), "application_id": app_id, "job": item,
                "resumed": bool(held)}

    @staticmethod
    def _latest_application(conn, job_id):
        return conn.execute(select(T.applications).where(T.applications.c.job_id == job_id)
                            .order_by(T.applications.c.application_id.desc()).limit(1)).mappings().first()

    def _own_hold(self, conn, job_id: int, agent: str, now: datetime) -> dict | None:
        """This agent's live claim on a held (filled, waiting for the user) application, or None."""
        claim = conn.execute(select(T.handoff_claims).where(T.handoff_claims.c.job_id == job_id)).mappings().first()
        if claim is None or claim["agent"] != agent or _aware(claim["lease_until"]) <= now:
            return None
        app = self._latest_application(conn, job_id)
        return dict(claim) if app is not None and app["status"] == HELD else None

    def _claim(self, conn, token: str, now: datetime, *, allow_expired: bool = False) -> dict:
        if not isinstance(token, str) or not token or len(token) > 256:
            raise HandoffError("invalid claim token")
        row = conn.execute(select(T.handoff_claims).where(
            T.handoff_claims.c.token_hash == _hash(token or ""))).mappings().first()
        if row is None:
            raise HandoffError("unknown or released claim token")
        if _aware(row["lease_until"]) <= now and not allow_expired:
            raise HandoffError("this claim expired; claim the job again")
        return dict(row)

    def release(self, token: str, now: datetime | None = None) -> dict:
        """Give the job back without an outcome. A held form (hold) goes back to queued and loses its hold note."""
        now = _aware(now or utcnow()).astimezone(timezone.utc)
        store = self._store()
        try:
            with store.transaction(immediate=True):
                conn = store._tx
                claim = self._claim(conn, token, now)
                app = self._latest_application(conn, claim["job_id"])
                if app is not None and app["status"] == HELD:
                    conn.execute(update(T.applications).where(
                        T.applications.c.application_id == app["application_id"]).values(
                        status="queued", note=None if app["note"] == HOLD_NOTE else app["note"], updated_at=now))
                conn.execute(delete(T.handoff_claims).where(T.handoff_claims.c.job_id == claim["job_id"]))
        finally:
            store.engine.dispose()
        return {"job_id": claim["job_id"], "released": True}

    def hold(self, token: str, now: datetime | None = None) -> dict:
        """The form is filled and waits for the user's go: keep the claim for hold_hours instead of the lease.

        The job stays out of the queue meanwhile, and the same agent can claim it again for a fresh token (claim).
        """
        now = _aware(now or utcnow()).astimezone(timezone.utc)
        store = self._store()
        try:
            with store.transaction(immediate=True):
                conn = store._tx
                claim = self._claim(conn, token, now)
                lease = max(now + timedelta(hours=self.guard().hold_hours), _aware(claim["lease_until"]))
                conn.execute(update(T.handoff_claims).where(T.handoff_claims.c.job_id == claim["job_id"])
                             .values(lease_until=lease))
                app = self._latest_application(conn, claim["job_id"])
                conn.execute(update(T.applications).where(
                    T.applications.c.application_id == app["application_id"]).values(
                    status=HELD, note=HOLD_NOTE, updated_at=now))
        finally:
            store.engine.dispose()
        return {"job_id": claim["job_id"], "status": HELD, "lease_until": lease.isoformat()}

    def held_applications(self, now: datetime | None = None) -> dict:
        """Filled forms waiting for the user's go: the latest application is held and its claim is still live.

        Oldest first, so the user can approve them together in one go. Read-only.
        """
        now = _aware(now or utcnow()).astimezone(timezone.utc)
        a, c, j, p = T.applications, T.handoff_claims, T.jobs, T.postings
        latest = select(func.max(a.c.application_id)).group_by(a.c.job_id)
        store = self._store()
        try:
            with store.engine.connect() as conn:
                rows = conn.execute(
                    select(a.c.job_id, a.c.apply_url, a.c.updated_at, c.c.agent, c.c.lease_until, p.c.title,
                           p.c.company)
                    .select_from(a.join(c, c.c.job_id == a.c.job_id).outerjoin(j, j.c.job_id == a.c.job_id)
                                 .outerjoin(p, p.c.posting_id == j.c.primary_posting_id))
                    .where(a.c.application_id.in_(latest), a.c.status == HELD)).mappings().all()
        finally:
            store.engine.dispose()
        live = sorted((r for r in rows if _aware(r["lease_until"]) > now),
                      key=lambda r: (_aware(r["updated_at"]), r["job_id"]))
        return {"items": [{"job_id": r["job_id"], "title": r["title"], "company": r["company"],
                           "apply_url": r["apply_url"], "agent": r["agent"],
                           "lease_until": _aware(r["lease_until"]).isoformat(),
                           "held_since": _aware(r["updated_at"]).isoformat()} for r in live]}

    # ---- the published packages (apply-kit), read with no claim token -----------------------------------------

    @property
    def kit_dir(self) -> Path:
        return self.home.parent / APPLY_KIT_DIR

    def _kit_read(self, *parts: str) -> bytes | None:
        """A file beneath apply-kit/ (no symlinks, the folder itself included; at most 10 MiB), or None if missing."""
        return self._read_beneath(self.kit_dir, parts, MAX_KIT_BYTES, APPLY_KIT_DIR, missing_ok=True)

    def kit_index(self) -> dict:
        """apply-kit/jobs/index.json: the published packages, or {"jobs": []} before anything is published."""
        data = self._kit_read("jobs", "index.json")
        if data is None:
            return {"jobs": []}
        try:
            index = json.loads(data)
        except ValueError:  # UnicodeDecodeError is one too
            raise HandoffError(f"{APPLY_KIT_DIR}/jobs/index.json is not valid JSON") from None
        if not isinstance(index, dict) or not isinstance(index.get("jobs"), list):
            raise HandoffError(f'{APPLY_KIT_DIR}/jobs/index.json is not a valid index (an object whose "jobs" is '
                               'a list)')
        return index

    def kit_file(self, job_id: int, name: str) -> bytes:
        """One file of a job's published package, apply-kit/jobs/<job_id>/<name>, as bytes."""
        _job_id(job_id)
        if not isinstance(name, str) or name not in KIT_FILES:
            raise HandoffError(f"name must be one of: {', '.join(KIT_FILES)}")
        data = self._kit_read("jobs", str(job_id), name)
        if data is None:
            raise HandoffError(f"job #{job_id} has no {name} in its package (not published yet?)")
        return data

    def kit_guide(self, which: str = "cheatsheet") -> bytes:
        """apply-kit/answers.md (which="cheatsheet", the agents' cheat sheet) or apply-kit/README.md ("readme")."""
        name = KIT_GUIDES.get(which) if isinstance(which, str) else None
        if name is None:
            raise HandoffError("which must be cheatsheet or readme")
        data = self._kit_read(name)
        if data is None:
            raise HandoffError(f"{APPLY_KIT_DIR}/{name} ({which}) is missing")
        return data

    # ---- the user's documents (apply-kit/documents/), read with no claim token --------------------------------

    def documents(self) -> dict:
        """The documents a form may ask for, by name, with size and sha256; regular files only, no symlinks."""
        folder = self.kit_dir / DOCUMENTS_DIR
        if folder.is_symlink() or not folder.is_dir():
            return {"documents": []}
        items = []
        for path in sorted(folder.iterdir()):
            if not DOCUMENT_NAME.fullmatch(path.name) or path.is_symlink() or not path.is_file():
                continue
            data = self._kit_read(DOCUMENTS_DIR, path.name)
            if data is not None:
                items.append({"name": path.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        return {"documents": items}

    def document(self, name: str) -> bytes:
        """One document's bytes, by the name `documents` lists."""
        if not isinstance(name, str) or not DOCUMENT_NAME.fullmatch(name):
            raise HandoffError("a document name is letters, digits, '.', '_' or '-' (see the list from `doc`)")
        data = self._kit_read(DOCUMENTS_DIR, name)
        if data is None:
            raise HandoffError(f"there is no document named {name} (see the list from `doc`)")
        return data

    # ---- open questions --------------------------------------------------------------------------------------

    def submit_questions(self, token: str, questions, now: datetime | None = None) -> dict:
        """Hand back the form's open questions that the agent has no answers for, and use up the claim.

        The job leaves the queue until the owner publishes the answers and marks them ready (answers_ready).
        """
        now = _aware(now or utcnow()).astimezone(timezone.utc)
        cleaned = _questions(questions)
        store = self._store()
        try:
            with store.transaction(immediate=True):
                conn = store._tx
                claim = self._claim(conn, token, now)
                app = self._latest_application(conn, claim["job_id"])
                set_id = conn.execute(insert(T.handoff_questions).values(
                    job_id=claim["job_id"], application_id=app["application_id"], agent=claim["agent"],
                    questions=cleaned, submitted_at=now)).inserted_primary_key[0]
                reason = f"{len(cleaned)} open questions sent for answers"
                conn.execute(insert(T.attempts).values(
                    application_id=app["application_id"], applier=claim.get("agent") or APPLIER,
                    **{"pass": "handoff"}, outcome="needs_input", reason=reason,
                    started_at=_aware(claim["claimed_at"]), ended_at=now))
                conn.execute(update(T.applications).where(
                    T.applications.c.application_id == app["application_id"]).values(
                    status=NEEDS_ANSWERS, note=f"waiting for answers to {len(cleaned)} open questions",
                    updated_at=now))
                conn.execute(delete(T.handoff_claims).where(T.handoff_claims.c.job_id == claim["job_id"]))
        finally:
            store.engine.dispose()
        kick(self.home)
        return {"job_id": claim["job_id"], "status": NEEDS_ANSWERS, "questions": len(cleaned),
                "question_set_id": set_id}

    def pending_questions(self) -> dict:
        """Jobs waiting for answers: each job's latest unanswered question set, oldest first."""
        store = self._store()
        try:
            with store.engine.connect() as conn:
                q, j, p = T.handoff_questions, T.jobs, T.postings
                sets = {}
                for row in conn.execute(select(q).where(q.c.answered_at.is_(None))
                                        .order_by(q.c.submitted_at, q.c.question_set_id)).mappings():
                    sets[row["job_id"]] = row  # a later set for the same job replaces an earlier one
                waiting = {job_id for job_id in sets
                           if (self._latest_application(conn, job_id) or {}).get("status") == NEEDS_ANSWERS}
                jobs = {r["job_id"]: r for r in conn.execute(
                    select(j.c.job_id, j.c.decision, p.c.title, p.c.company)
                    .select_from(j.outerjoin(p, p.c.posting_id == j.c.primary_posting_id))
                    .where(j.c.job_id.in_(waiting))).mappings()}
        finally:
            store.engine.dispose()
        items = []
        for row in sorted((sets[job_id] for job_id in waiting), key=lambda r: (_aware(r["submitted_at"]),
                                                                               r["question_set_id"])):
            job = jobs.get(row["job_id"]) or {}
            items.append({"job_id": row["job_id"], "title": job.get("title"), "company": job.get("company"),
                          "decision": job.get("decision"), "agent": row["agent"],
                          "question_set_id": row["question_set_id"],
                          "submitted_at": _aware(row["submitted_at"]).isoformat(), "questions": row["questions"]})
        return {"items": items}

    def answers_ready(self, job_id: int, now: datetime | None = None, asked_at: str | None = None) -> dict:
        """The owner's step once the answers are published: the job returns to the queue for the agent.

        `asked_at` is the `submitted_at` of the round the package answers, as answers-needed listed it when the work
        began. When the round changed since (a change request merged into it while the package was being made), the
        job stays parked and nothing is marked answered, so the next run handles the new items.
        """
        _job_id(job_id)
        now = _aware(now or utcnow()).astimezone(timezone.utc)
        expected = None
        if asked_at is not None:
            try:
                expected = _aware(datetime.fromisoformat(asked_at)).astimezone(timezone.utc)
            except (TypeError, ValueError):
                raise HandoffError("asked_at must be the submitted_at that answers-needed listed") from None
        store = self._store()
        try:
            with store.transaction(immediate=True):
                conn = store._tx
                job = store.job(job_id)
                if job is None or job.get("decision") != "want_to_apply" or job.get("archived_at") is not None:
                    raise HandoffError(f"job #{job_id} is no longer want_to_apply; nothing to put back in the queue")
                app = self._latest_application(conn, job_id)
                if app is None or app["status"] != NEEDS_ANSWERS:
                    raise HandoffError(f"job #{job_id} is not waiting for answers")
                if expected is not None:
                    current = self._unanswered(conn, job_id)
                    latest = _aware(current["submitted_at"]).astimezone(timezone.utc) if current else None
                    if latest != expected:
                        raise HandoffError(
                            f"job #{job_id}'s questions or changes changed at "
                            f"{latest.isoformat() if latest else 'an unknown time'}, after the round of "
                            f"{expected.isoformat()} this package answers: handle the new items, republish, then "
                            "run answers-ready with the new submitted_at")
                conn.execute(update(T.applications).where(
                    T.applications.c.application_id == app["application_id"]).values(
                    status="queued", note="answers published", updated_at=now))
                conn.execute(update(T.handoff_questions).where(
                    T.handoff_questions.c.job_id == job_id, T.handoff_questions.c.answered_at.is_(None))
                    .values(answered_at=now))
        finally:
            store.engine.dispose()
        return {"job_id": job_id, "status": "queued", "answered_at": now.isoformat()}

    def _open_questions(self, conn, job_id: int) -> dict | None:
        row = conn.execute(select(T.handoff_questions).where(T.handoff_questions.c.job_id == job_id)
                           .order_by(T.handoff_questions.c.question_set_id.desc()).limit(1)).mappings().first()
        if row is None:
            return None
        return {"submitted_at": _aware(row["submitted_at"]).isoformat(),
                "answered_at": _aware(row["answered_at"]).isoformat() if row["answered_at"] else None,
                "questions": row["questions"]}

    @staticmethod
    def _unanswered(conn, job_id: int):
        q = T.handoff_questions
        return conn.execute(select(q).where(q.c.job_id == job_id, q.c.answered_at.is_(None))
                            .order_by(q.c.question_set_id.desc()).limit(1)).mappings().first()

    @staticmethod
    def _job_row(conn, job_id: int):
        j, p = T.jobs, T.postings
        return conn.execute(select(j.c.job_id, j.c.decision, j.c.archived_at, j.c.decided_at, p.c.title,
                                   p.c.company, p.c.apply_url)
                            .select_from(j.outerjoin(p, p.c.posting_id == j.c.primary_posting_id))
                            .where(j.c.job_id == job_id)).mappings().first()

    # ---- the user's change requests outside a claim ----------------------------------------------------------

    def request_change(self, job_id: int, text: str, agent: str, now: datetime | None = None) -> dict:
        """The user's words asking for a change to a wanted job's materials, passed on while no form is open.

        Stored like a pre-submit change (kind "change") and the job leaves the queue, so no agent fills a form with
        the old package; once the revised package is published, the owner's answers_ready puts it back. A job whose
        form is being filled or held refuses: the change goes through that claim (`questions`, kind change) instead.
        A second request before the first is handled joins the same set, and moves its time to now, so a package
        published in between cannot pass answers_ready without it. A job parked for the user's input (needs_input)
        accepts a request, which carries the user's answers to Claude; a blocked one (action_needed) refuses.
        """
        from auto_app.enrich.targets import target_for

        _job_id(job_id)
        now = _aware(now or utcnow()).astimezone(timezone.utc)
        if not isinstance(agent, str) or not agent.strip() or len(agent) > 80:
            raise HandoffError("agent must be a short name")
        agent = agent.strip()
        if not isinstance(text, str) or not text.strip() or len(text.strip()) > MAX_REQUEST_TEXT:
            raise HandoffError(f"the request must be 1 to {MAX_REQUEST_TEXT} characters")
        [item] = _questions([{"text": text, "kind": "change"}])
        store = self._store()
        try:
            with store.transaction(immediate=True):
                conn = store._tx
                job = self._job_row(conn, job_id)
                if job is None or job["decision"] != "want_to_apply" or job["archived_at"] is not None:
                    raise HandoffError(f"job #{job_id} is not want_to_apply; change requests are only for jobs the "
                                       "user wants to apply to")
                lease = conn.execute(select(T.handoff_claims.c.lease_until)
                                     .where(T.handoff_claims.c.job_id == job_id)).scalar()
                if lease is not None and _aware(lease) > now:
                    raise HandoffError(f"job #{job_id}'s form is open (claimed until {_aware(lease).isoformat()}): "
                                       "pass the change on from that chat with `questions` (kind change)")
                app = self._latest_application(conn, job_id)
                # A job parked because the agent needs the user (needs_user → needs_input), e.g. items in answers.md
                # only the user can confirm, takes the user's answers here: they go to Claude like any change, and
                # answers_ready returns the job once the revised package is out. A blocked job (a login, a CAPTCHA,
                # the site) is not fixed by new materials: the user requeues it.
                if app is not None and app["status"] == "action_needed":
                    raise HandoffError(f"job #{job_id} is blocked ({app['note'] or 'no note'}); new materials don't fix "
                                       "that: the user requeues it once it is resolved")
                if app is None:
                    target = target_for(job["apply_url"])
                    app_id = conn.execute(insert(T.applications).values(
                        job_id=job_id, platform=target.platform if target else "other", apply_url=job["apply_url"],
                        status=NEEDS_ANSWERS, chain=[APPLIER], chain_pos=0, applier=APPLIER, created_at=now,
                        updated_at=now)).inserted_primary_key[0]
                else:
                    app_id = app["application_id"]
                current = self._unanswered(conn, job_id)
                if current is not None and app is not None and app["status"] == NEEDS_ANSWERS:
                    merged = _questions([*current["questions"], item])
                    set_id = current["question_set_id"]
                    conn.execute(update(T.handoff_questions).where(T.handoff_questions.c.question_set_id == set_id)
                                 .values(questions=merged, submitted_at=now))
                else:
                    merged = [item]
                    set_id = conn.execute(insert(T.handoff_questions).values(
                        job_id=job_id, application_id=app_id, agent=agent, questions=merged,
                        submitted_at=now)).inserted_primary_key[0]
                changes = sum(q.get("kind") == "change" for q in merged)
                conn.execute(update(T.applications).where(T.applications.c.application_id == app_id).values(
                    status=NEEDS_ANSWERS, note=f"waiting for {len(merged)} open questions or change requests",
                    updated_at=now))
        finally:
            store.engine.dispose()
        kick(self.home)
        return {"job_id": job_id, "status": NEEDS_ANSWERS, "question_set_id": set_id, "changes": changes}

    # ---- cheat-sheet requests: the user's general answers, for Claude to add to apply-kit/answers.md -----------

    def request_cheatsheet(self, text: str, agent: str, now: datetime | None = None) -> dict:
        """The user's words, verbatim, with an answer for every application (not one job's: that is request_change).
        Claude adds it to the cheat sheet's answer sections, or declines with a reason; the agent never edits it."""
        now = _aware(now or utcnow()).astimezone(timezone.utc)
        if not isinstance(agent, str) or not agent.strip() or len(agent) > 80:
            raise HandoffError("agent must be a short name")
        if not isinstance(text, str) or not text.strip() or len(text.strip()) > MAX_REQUEST_TEXT:
            raise HandoffError(f"the request must be 1 to {MAX_REQUEST_TEXT} characters")
        text = text.strip()
        if not _control_free(text):
            raise HandoffError("the request must not contain control characters (line breaks and tabs are fine)")
        store = self._store()
        try:
            with store.transaction(immediate=True):
                request_id = store._tx.execute(insert(T.cheatsheet_requests).values(
                    text=text, agent=agent.strip(), submitted_at=now)).inserted_primary_key[0]
        finally:
            store.engine.dispose()
        kick(self.home)
        return {"request_id": request_id, "status": "pending"}

    def cheatsheet_requests(self, now: datetime | None = None) -> dict:
        """Pending requests, oldest first, and the last 20 done ones, newest first, with outcome and note."""
        c = T.cheatsheet_requests
        store = self._store()
        try:
            with store.engine.connect() as conn:
                pending = conn.execute(select(c).where(c.c.done_at.is_(None))
                                       .order_by(c.c.submitted_at, c.c.request_id)).mappings().all()
                recent = conn.execute(select(c).where(c.c.done_at.is_not(None))
                                      .order_by(c.c.done_at.desc(), c.c.request_id.desc())
                                      .limit(RECENT_CHEATSHEET)).mappings().all()
        finally:
            store.engine.dispose()
        return {"pending": [{"request_id": r["request_id"], "text": r["text"], "agent": r["agent"],
                             "submitted_at": _aware(r["submitted_at"]).isoformat()} for r in pending],
                "recent": [{"request_id": r["request_id"], "text": r["text"], "agent": r["agent"],
                            "submitted_at": _aware(r["submitted_at"]).isoformat(),
                            "done_at": _aware(r["done_at"]).isoformat(), "outcome": r["outcome"], "note": r["note"]}
                           for r in recent]}

    def cheatsheet_done(self, request_id: int, outcome: str, note: str, now: datetime | None = None) -> dict:
        """The owner's step (Claude, after editing the cheat sheet or deciding not to): applied or declined, with a
        note saying what changed or why not."""
        _job_id(request_id)
        now = _aware(now or utcnow()).astimezone(timezone.utc)
        if outcome not in CHEATSHEET_OUTCOMES:
            raise HandoffError(f"outcome must be one of: {', '.join(CHEATSHEET_OUTCOMES)}")
        if not isinstance(note, str) or not note.strip() or len(note.strip()) > MAX_REQUEST_TEXT:
            raise HandoffError(f"the note must be 1 to {MAX_REQUEST_TEXT} characters")
        c = T.cheatsheet_requests
        store = self._store()
        try:
            with store.transaction(immediate=True):
                conn = store._tx
                row = conn.execute(select(c.c.done_at).where(c.c.request_id == request_id)).first()
                if row is None:
                    raise HandoffError(f"cheat-sheet request #{request_id} not found")
                if row.done_at is not None:
                    raise HandoffError(f"cheat-sheet request #{request_id} is already done")
                conn.execute(update(c).where(c.c.request_id == request_id)
                             .values(done_at=now, outcome=outcome, note=note.strip()))
        finally:
            store.engine.dispose()
        return {"request_id": request_id, "outcome": outcome, "done_at": now.isoformat()}

    # ---- frozen packages of submitted jobs -------------------------------------------------------------------

    def freeze_submitted(self, now: datetime | None = None) -> dict:
        """The owner's backfill: freeze the package of every applied job that has none frozen yet (marked
        backfilled, since the live package may have changed after the submission)."""
        now = _aware(now or utcnow()).astimezone(timezone.utc)
        store = self._store()
        try:
            with store.engine.connect() as conn:
                applied = conn.execute(select(T.jobs.c.job_id).where(T.jobs.c.decision == "applied")
                                       .order_by(T.jobs.c.job_id)).scalars().all()
        finally:
            store.engine.dispose()
        result = {"frozen": [], "already": [], "no_package": []}
        for job_id in applied:
            if frozen_at(self.kit_dir, job_id) is not None:
                result["already"].append(job_id)
            elif freeze_package(self.kit_dir, job_id, now, backfilled=True) is not None:
                result["frozen"].append(job_id)
            else:
                result["no_package"].append(job_id)
        return result

    # ---- one job's progress, in one read ---------------------------------------------------------------------

    def _runner(self) -> dict | None:
        """apply-kit/runner.json, which deploy/jd-apply-queue.sh writes; None when missing or unreadable."""
        try:
            data = self._kit_read(RUNNER_FILE)
            value = json.loads(data) if data is not None else None
        except (HandoffError, ValueError):
            return None
        if not isinstance(value, dict):
            return None
        keep = ("running", "round", "started_at", "ended_at", "checked_at", "exit", "items", "last_run")
        return {k: value[k] for k in keep if k in value}

    def status(self, job_id: int, now: datetime | None = None) -> dict:
        """Where a job stands, for "is #N ready yet?": its decision, application, open claim, questions or changes
        waiting for Claude, published package and the package runner, plus a one-line summary. Never a token."""
        _job_id(job_id)
        now = _aware(now or utcnow()).astimezone(timezone.utc)
        store = self._store()
        try:
            with store.engine.connect() as conn:
                job = self._job_row(conn, job_id)
                if job is None:
                    raise HandoffError(f"job #{job_id} not found")
                app = self._latest_application(conn, job_id)
                claim = conn.execute(select(T.handoff_claims.c.agent, T.handoff_claims.c.lease_until)
                                     .where(T.handoff_claims.c.job_id == job_id)).mappings().first()
                waiting = self._unanswered(conn, job_id)
                queue_block = None
                if job["decision"] == "want_to_apply" and job["archived_at"] is None:
                    # The same guardrails as the queue: a job they keep out never reaches the package runner either.
                    items, blocked, daily, _ = self._candidates(conn, now, only_job=job_id)
                    queue_block = next(iter(blocked), None) if not items else ("daily_limit" if daily else None)
        finally:
            store.engine.dispose()
        live = claim is not None and _aware(claim["lease_until"]) > now
        held = live and app is not None and app["status"] == HELD
        pending = None
        if waiting is not None and app is not None and app["status"] == NEEDS_ANSWERS:
            changes = sum(q.get("kind") == "change" for q in waiting["questions"])
            pending = {"questions": len(waiting["questions"]) - changes, "changes": changes,
                       "submitted_at": _aware(waiting["submitted_at"]).isoformat()}
        entry = next((e for e in self.kit_index()["jobs"] if isinstance(e, dict) and e.get("job_id") == job_id), None)
        package = {"published": entry is not None, "published_at": (entry or {}).get("published_at"),
                   "has_message": bool((entry or {}).get("has_message")),
                   "submitted_copy": frozen_at(self.kit_dir, job_id)}
        runner = self._runner()
        running = bool(runner and runner.get("running"))
        working = f"; the package runner is running (since {runner.get('started_at')})" if running else ""
        decision = job["decision"]
        if decision == "applied":
            summary = "applied" + ("; the follow-up message is in the package" if package["has_message"] else "")
        elif decision != "want_to_apply" or job["archived_at"] is not None:
            summary = f"not on the application list ({'archived' if job['archived_at'] else decision or 'undecided'})"
        elif pending is not None:
            summary = (f"waiting for Claude: {pending['changes']} change request(s) and {pending['questions']} open "
                       f"question(s), sent {pending['submitted_at']}{working}")
        elif held:
            summary = f"the form is filled and waiting for your go (until {_aware(claim['lease_until']).isoformat()})"
        elif live:
            summary = f"the form is being filled by {claim['agent']} (claimed until " \
                      f"{_aware(claim['lease_until']).isoformat()})"
        elif app is not None and app["status"] == "needs_input":
            summary = (f"parked for the user (needs_input): {app['note'] or 'no note'}; pass the user's answers or "
                       "changes on with `request` (Claude revises the package and the job returns to the queue)")
        elif app is not None and app["status"] in PARKED:
            summary = f"blocked ({app['status']}): {app['note'] or 'no note'}; the user requeues it once resolved"
        elif queue_block is not None and queue_block != "daily_limit":
            summary = f"not in the queue ({queue_block}), so no package is made or used for it"
        elif not package["published"]:
            summary = "the package is not made yet" + (
                "; today's submission limit hides the queue, so the package runner waits until it resets"
                if queue_block == "daily_limit" else
                working or "; the package runner starts on the next kick or within the hour")
        else:
            summary = f"the package is published ({package['published_at']}) and the job is in the queue" + (
                "; today's submission limit is reached" if queue_block == "daily_limit" else "")
        return {"job_id": job_id, "title": job["title"], "company": job["company"], "decision": decision,
                "application": None if app is None else {
                    "status": app["status"], "note": app["note"],
                    "updated_at": _aware(app["updated_at"]).isoformat()},
                "claim": {"agent": claim["agent"], "until": _aware(claim["lease_until"]).isoformat(),
                          "held": held} if live else None,
                "pending": pending, "package": package, "runner": runner, "queue_block": queue_block,
                "summary": summary}

    # ---- outcomes ------------------------------------------------------------------------------

    def record_attempt(self, token: str, outcome: str, note: str | None = None, evidence: str | None = None,
                       now: datetime | None = None) -> dict:
        now = _aware(now or utcnow()).astimezone(timezone.utc)
        if not isinstance(outcome, str) or outcome not in OUTCOMES:
            raise HandoffError(f"outcome must be one of: {', '.join(OUTCOMES)}")
        _note(note)
        evidence_path = self._evidence(evidence) if evidence is not None else None
        status, attempt_outcome, decision = OUTCOMES[outcome]
        note = (note or "").strip()[:500] or None
        store = self._store()
        try:
            with store.transaction(immediate=True):
                conn = store._tx
                # An outcome reports what already happened on the employer's site, so it is never
                # re-judged against eligibility or quotas (claim and packet enforce those before the
                # agent acts). Refusing a real submission would leave the job queued for a second one.
                # A late submission is still accepted while nobody else has claimed the job.
                claim = self._claim(conn, token, now, allow_expired=outcome == "submitted")
                job = store.job(claim["job_id"])
                if job is None:
                    raise HandoffError(f"job #{claim['job_id']} no longer exists")
                app = conn.execute(select(T.applications).where(
                    T.applications.c.job_id == claim["job_id"])
                    .order_by(T.applications.c.application_id.desc()).limit(1)).mappings().one()
                if app["status"] == "submitted":
                    raise HandoffError("this application is already submitted")
                # The attempt records which agent ran, not just the chain's name: the claim is deleted
                # below, so this row is the only lasting trace of who did it.
                conn.execute(insert(T.attempts).values(
                    application_id=app["application_id"], applier=claim.get("agent") or APPLIER,
                    **{"pass": "submit"}, outcome=attempt_outcome, reason=note, screenshot_path=evidence_path,
                    started_at=_aware(claim["claimed_at"]), ended_at=now))
                if outcome == "failed":
                    # `failed` returns the job to the queue, so without a cap a job that cannot be filled
                    # cycles forever. Park it instead and let the owner requeue once they have looked.
                    previous = conn.execute(select(func.count()).select_from(T.attempts).where(
                        T.attempts.c.application_id == app["application_id"],
                        T.attempts.c.outcome == "failed")).scalar_one()
                    if previous >= FAILED_ATTEMPT_LIMIT:
                        status, decision = "action_needed", None
                        note = ((note + " | ") if note else "") + f"parked after {previous} failed attempts"
                values = {"status": status, "updated_at": now, "note": note}
                if outcome == "submitted":
                    values.update(submitted_at=now, confirmation_path=evidence_path)
                conn.execute(update(T.applications).where(
                    T.applications.c.application_id == app["application_id"]).values(**values))
                if decision:
                    from auto_app.agent.service import apply_manual_decision_in_transaction

                    verb = "submitted" if outcome == "submitted" else "skipped"
                    reason = f"{verb} by your agent" + (f": {note}" if note else "")
                    # The decision can change while the claim is out; only a job whose state may still
                    # change takes the outcome's decision. A skip never overrides your own decision.
                    changeable = job.get("archived_at") is None and job.get("duplicate_of") is None
                    if outcome == "submitted" and changeable:
                        apply_manual_decision_in_transaction(store, claim["job_id"], decision, reason=reason,
                                                             event_source="agent", occurred_at=now)
                    elif outcome == "submitted":
                        from auto_app.track.events import record_event

                        record_event(store, claim["job_id"], "applied", occurred_at=now, source="agent",
                                     note=reason)
                    elif changeable and job.get("decision") == "want_to_apply":
                        apply_manual_decision_in_transaction(store, claim["job_id"], decision, reason=reason,
                                                             event_source="agent", occurred_at=now)
                conn.execute(delete(T.handoff_claims).where(T.handoff_claims.c.job_id == claim["job_id"]))
        finally:
            store.engine.dispose()
        if outcome == "submitted":
            freeze_package(self.kit_dir, claim["job_id"], now)  # what went out, apart from later republishing
        return {"job_id": claim["job_id"], "status": status, "outcome": outcome}

    def requeue(self, job_id: int) -> dict:
        _job_id(job_id)
        store = self._store()
        try:
            with store.transaction(immediate=True):
                job = store.job(job_id)
                if job is None or job.get("decision") != "want_to_apply" or job.get("archived_at") is not None:
                    raise HandoffError(f"job #{job_id} is no longer want_to_apply; nothing to requeue")
                changed = store._tx.execute(update(T.applications).where(
                    T.applications.c.job_id == job_id, T.applications.c.status.in_(PARKED))
                    .values(status="queued", updated_at=utcnow())).rowcount
        finally:
            store.engine.dispose()
        if not changed:
            raise HandoffError(f"job #{job_id} is not waiting for you")
        return {"job_id": job_id, "status": "queued"}

    def _evidence(self, value: str) -> str:
        if not isinstance(value, str):
            raise HandoffError('evidence must be a file path')
        path, _ = self._read_local('evidence', value, 10 * 1024 * 1024)
        return path

    SECRET_KEYS = re.compile(r"password|secret|token|otp|credential|api[_-]?key|private[_-]?key", re.I)
    RESUME_TYPES = {".pdf", ".docx"}
    MAX_RESUME_BYTES = 10 * 1024 * 1024

    def _read_local(self, folder: str, value: str, maximum: int, *, missing_ok=False):
        """Open beneath a fixed root with no symlink traversal and bounded reads."""
        root = self.home / folder
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.home / candidate
        if '..' in candidate.parts:
            raise HandoffError(f'files must stay inside $AUTO_APP_HOME/{folder}/')
        try:
            parts = candidate.relative_to(root).parts
        except ValueError:
            raise HandoffError(f'files must stay inside $AUTO_APP_HOME/{folder}/') from None
        if not parts:
            raise HandoffError(f'a regular file under {folder}/ is required')
        return str(candidate), self._read_beneath(root, parts, maximum, folder, missing_ok=missing_ok)

    @staticmethod
    def _read_beneath(root: Path, parts: tuple[str, ...], maximum: int, folder: str, *, missing_ok=False):
        """Read root/parts as bytes: each step opened with O_NOFOLLOW (root itself included), a regular file of at
        most `maximum` bytes, unchanged while read. A missing file is None with missing_ok, an error otherwise.
        `folder` names the root in error messages."""
        import os
        import stat

        descriptors = []
        try:
            descriptors.append(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
            for part in parts[:-1]:
                descriptors.append(os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                           dir_fd=descriptors[-1]))
            descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=descriptors[-1])
            descriptors.append(descriptor)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise HandoffError(f'a regular file under {folder}/ is required')
            if info.st_size > maximum:
                raise HandoffError('a resume must be at most 10 MB' if folder == 'resumes'
                                   else f'{folder}/ file is too large')
            with os.fdopen(os.dup(descriptor), 'rb') as handle:
                data = handle.read(maximum + 1)
            if len(data) > maximum:
                raise HandoffError(f'{folder}/ file is too large')
            after = os.fstat(descriptor)
            if info.st_size != after.st_size or info.st_mtime_ns != after.st_mtime_ns:
                raise HandoffError(f'{folder}/ file changed while reading')
            return data
        except FileNotFoundError:
            if missing_ok:
                return None
            raise HandoffError(f'an existing regular file under {folder}/ is required') from None
        except OSError:
            raise HandoffError(f'a regular file under {folder}/ with no symlinks is required') from None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _profile(self) -> tuple[dict, dict]:
        def read(name):
            _, data = self._read_local('profile', str(self.home / 'profile' / name), 1024 * 1024, missing_ok=True)
            if data is None:
                return {}
            try:
                loaded = yaml.safe_load(data) or {}
            except (yaml.YAMLError, UnicodeError):
                raise HandoffError('profile YAML is invalid') from None
            if not isinstance(loaded, dict):
                raise HandoffError('profile YAML must be a mapping')
            return loaded

        # These fields are the documented applicant schema. Unknown keys are never exported.
        fields = {
            'name': {'first', 'last', 'preferred'},
            'email': None, 'phone': None, 'cv': None,
            'location': {'city', 'state', 'country', 'address1', 'address2', 'postal_code'},
            'links': {'linkedin', 'github', 'portfolio'},
            'education': {'school', 'degree', 'field', 'start', 'end', 'gpa'},
            'experience': {'company', 'title', 'start', 'end'},
        }
        profile = read('profile.yaml')
        result = {}
        for key, allowed in fields.items():
            if key not in profile:
                continue
            value = profile[key]
            if allowed is None:
                if isinstance(value, (str, int, float, bool)) or value is None:
                    result[key] = value
            elif isinstance(value, dict):
                result[key] = {k: _scrub(v) for k, v in value.items() if k in allowed and _plain(v)}
            elif isinstance(value, list):
                result[key] = [{k: _scrub(v) for k, v in item.items() if k in allowed and _plain(v)}
                               for item in value if isinstance(item, dict)]
        answers = read('answers.yaml')
        # Answer-bank labels are arbitrary, but each entry has a fixed non-secret contract.
        safe_answers = {}
        for key, value in answers.items():
            if not isinstance(key, str) or self.SECRET_KEYS.search(key) or key.lower() == 'accounts':
                continue
            if isinstance(value, dict):
                safe_answers[key] = {k: _scrub(v) for k, v in value.items()
                                     if k in {'question', 'answer', 'note', 'kind', 'adapt'} and _plain(v)}
        return result, safe_answers

    def packet(self, token: str, now: datetime | None = None) -> dict:
        from auto_app.query.service import QueryService

        now = _aware(now or utcnow()).astimezone(timezone.utc)
        store = self._store()
        try:
            with store.transaction(immediate=True):
                claim = self._claim(store._tx, token, now)
                items, _, daily, _ = self._candidates(store._tx, now, ignore_claim_job=claim["job_id"],
                                                      only_job=claim["job_id"])
                item = next((i for i in items if i['job_id'] == claim['job_id']), None)
                if item is None or daily:
                    raise HandoffError('claimed job is no longer eligible; release the claim')
                resumes = []
                registrations = store._tx.execute(select(T.resume_artifacts).where(
                    T.resume_artifacts.c.job_id == claim['job_id'])
                    .order_by(T.resume_artifacts.c.artifact_id)).mappings().all()
                # A fresh registration supersedes the same path's prior fingerprint; retain its audit rows.
                current = {row['path']: row for row in registrations}
                for row in sorted(current.values(), key=lambda row: row['artifact_id']):
                    if Path(row['path']).suffix.lower() not in self.RESUME_TYPES:
                        raise HandoffError('registered resume must be a .pdf or .docx file')
                    path, data = self._read_local('resumes', row['path'], self.MAX_RESUME_BYTES)
                    digest = hashlib.sha256(data).hexdigest()
                    if digest != row['sha256'] or len(data) != row['bytes']:
                        raise HandoffError('registered resume changed; register the updated file again')
                    resumes.append({'artifact_id': row['artifact_id'], 'path': path, 'sha256': digest,
                                    'bytes': len(data), 'note': row['note']})
                # QueryService reads the same committed state while the writer lock prevents mutations.
                open_questions = self._open_questions(store._tx, claim['job_id'])
                with QueryService(self.db_path, self.preferences_path) as query:
                    job = query.job(claim['job_id'])
                profile, answers = self._profile()
        finally:
            store.engine.dispose()
        parsed = job.get('parsed') or {}
        return {'lease_until': _aware(claim['lease_until']).isoformat(),
                'job': {'job_id': job['job_id'], 'title': job.get('title'), 'company': job.get('company'),
                        'locations': job.get('locations'), 'apply_url': item['apply_url'],
                        'description': parsed.get('description_text') or '', 'screening': job.get('screening')},
                'profile': profile, 'answers': answers, 'resumes': resumes, 'open_questions': open_questions,
                'guard': {'note': 'Record exactly one outcome with this token before the lease ends.'}}

    def record_account(self, site: str, email: str, created_by_agent: bool = True, note: str | None = None,
                       **extra) -> dict:
        if any(self.SECRET_KEYS.search(str(key)) for key in extra) or (isinstance(note, str) and self.SECRET_KEYS.search(note)):
            raise HandoffError("accounts never store passwords or other secrets; keep them in your password manager")
        if extra:
            raise HandoffError("unknown account fields are not accepted")
        _note(note)
        if type(created_by_agent) is not bool:
            raise HandoffError("created_by_agent must be true or false")
        if not isinstance(site, str) or len(site) > 253 or not re.fullmatch(
                r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", site.lower()):
            raise HandoffError("site must be a host name, e.g. acme.wd5.myworkdayjobs.com")
        if not isinstance(email, str) or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254:
            raise HandoffError("email must be an address")
        now = utcnow()
        from sqlalchemy.dialects.sqlite import insert as upsert

        values = dict(site=site.lower(), email=email, created_by_agent=bool(created_by_agent), created_at=now,
                      last_used_at=now, note=(note or None))
        statement = upsert(T.accounts).values(**values)
        store = self._store()
        try:
            with store.engine.begin() as conn:
                conn.execute(statement.on_conflict_do_update(index_elements=["site"], set_={
                    "email": statement.excluded.email, "last_used_at": statement.excluded.last_used_at,
                    "note": statement.excluded.note}))
                persisted = conn.execute(select(T.accounts).where(T.accounts.c.site == values['site'])).mappings().one()
        finally:
            store.engine.dispose()
        return {"site": values["site"], "email": email, "created_by_agent": persisted["created_by_agent"]}

    def register_resume(self, job_id: int, path: str, note: str | None = None) -> dict:
        _job_id(job_id)
        _note(note)
        if not isinstance(path, str):
            raise HandoffError('a resume path is required')
        if Path(path).suffix.lower() not in self.RESUME_TYPES:
            raise HandoffError('a resume must be a .pdf or .docx file')
        resolved, data = self._read_local('resumes', path, self.MAX_RESUME_BYTES)
        digest = hashlib.sha256(data).hexdigest()
        store = self._store()
        try:
            if store.job(job_id) is None:
                raise HandoffError(f"job #{job_id} is missing")
            with store.engine.begin() as conn:
                artifact = conn.execute(insert(T.resume_artifacts).values(
                    job_id=job_id, path=str(resolved), sha256=digest, bytes=len(data), note=(note or None),
                    created_at=utcnow())).inserted_primary_key[0]
        finally:
            store.engine.dispose()
        return {"artifact_id": artifact, "sha256": digest, "bytes": len(data), "path": str(resolved)}


def _scrub(value):
    """Drop any nested key that looks like a secret, as a second line of defense."""
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if str(k).lower() != "accounts" and not HandoffService.SECRET_KEYS.search(str(k))}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value

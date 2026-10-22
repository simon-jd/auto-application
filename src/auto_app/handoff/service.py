"""Queue, claims, packets and outcomes for your own application agent (docs/agent-handoff.md).

Guardrails live here, not in an agent's instructions: one live claim per job; claims expire; a daily
submission limit; a per-company cooldown; no job that is applied, archived, rule-excluded or a repost of
one you applied to. A submitted outcome marks the job applied through the same service as chat tools.
"""

import hashlib
import ipaddress
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import yaml
from sqlalchemy import delete, func, insert, inspect, select, update

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow
from auto_app.handoff.guard import load_guard

OUTCOMES = {  # agent outcome → (applications.status, attempts.outcome, job decision or None)
    "submitted": ("submitted", "submitted", "applied"),
    "needs_user": ("needs_input", "needs_input", None),
    "blocked": ("action_needed", "action_needed", None),
    "failed": ("failed", "failed", None),
    "skipped": ("dropped", "unsupported", "not_applied"),
}
PARKED = ("needs_input", "action_needed")  # out of the queue until you requeue
APPLIER = "external-agent"


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
        company = {}
        for job_id, at in submissions.items():
            if at >= window:
                name = (jobs.get(job_id) or {}).get('company_norm') or ''
                company.setdefault(name, set()).add(job_id)
        submitted_today = len(daily)
        for row in conn.execute(select(T.handoff_claims).where(T.handoff_claims.c.lease_until > now)).mappings():
            job_id = row['job_id']
            if job_id == exclude_claim_job or job_id in submissions:
                continue
            daily.add(job_id)
            name = (jobs.get(job_id) or {}).get('company_norm') or ''
            company.setdefault(name, set()).add(job_id)
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
        source_states = {}
        source_evidence = conn.execute(select(p.c.job_id, p.c.kind, p.c.state, p.c.duplicate_of,
                                              p.c.parsed, T.ingest_entries.c.status.label('entry_status'))
                                       .select_from(p.outerjoin(T.ingest_entries,
                                                               T.ingest_entries.c.posting_id == p.c.posting_id))
                                       .where(p.c.job_id.in_(wanted))).mappings()
        for posting in source_evidence:
            state = posting['entry_status']
            if state is None:
                # Native browser/API postings have no ingest_entries row. A linked native record is
                # independent positive evidence; a feed's disappearance does not close that posting.
                imported = 'ingestion' in ((posting['parsed'] or {}).get('extras') or {})
                if (posting['kind'] not in ('browser', 'api') or posting['state'] != 'linked'
                        or posting['duplicate_of'] is not None or imported):
                    continue
                state = 'active'
            source_states.setdefault(posting['job_id'], []).append(state)
        gone = set(conn.execute(select(T.enrichments.c.url).where(T.enrichments.c.status == 'gone')).scalars())
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
            elif app_rows and app_rows[-1]['status'] in PARKED:
                reason = 'waiting_for_you'
            elif claim and job_id != ignore_claim_job and _aware(claim['lease_until']) > now:
                reason = 'claimed'
            elif source_states.get(job_id) and all(s in ('inactive', 'missing') for s in source_states[job_id]):
                reason = 'source_inactive'
            elif link in gone:
                reason = 'link_gone'
            elif not _safe_link(link):
                reason = 'invalid_apply_url'
            elif company_counts.get(row['company_norm'] or '', 0) >= guard.company_limit:
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
        store = self._store()
        try:
            with store.transaction(immediate=True):
                conn = store._tx
                items, blocked, daily, _ = self._candidates(conn, now, only_job=job_id)
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
                conn.execute(delete(T.handoff_claims).where(T.handoff_claims.c.job_id == job_id))
                conn.execute(insert(T.handoff_claims).values(job_id=job_id, agent=agent.strip(),
                                                             token_hash=_hash(token), claimed_at=now,
                                                             lease_until=lease))
                app_id = conn.execute(select(T.applications.c.application_id)
                                      .where(T.applications.c.job_id == job_id)
                                      .order_by(T.applications.c.application_id.desc()).limit(1)).scalar()
                if app_id is None:
                    target = target_for(item["apply_url"])
                    app_id = conn.execute(insert(T.applications).values(
                        job_id=job_id, platform=target.platform if target else "other",
                        apply_url=item["apply_url"], status="queued", chain=[APPLIER], chain_pos=0,
                        applier=APPLIER, created_at=now, updated_at=now)).inserted_primary_key[0]
                else:
                    conn.execute(update(T.applications).where(T.applications.c.application_id == app_id).values(
                        apply_url=item['apply_url'], platform=item['platform'], status='queued',
                        applier=APPLIER, updated_at=now))
        finally:
            store.engine.dispose()
        return {"token": token, "lease_until": lease.isoformat(), "application_id": app_id, "job": item}

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
        now = _aware(now or utcnow()).astimezone(timezone.utc)
        store = self._store()
        try:
            with store.transaction(immediate=True):
                claim = self._claim(store._tx, token, now)
                store._tx.execute(delete(T.handoff_claims).where(T.handoff_claims.c.job_id == claim["job_id"]))
        finally:
            store.engine.dispose()
        return {"job_id": claim["job_id"], "released": True}

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
                conn.execute(insert(T.attempts).values(
                    application_id=app["application_id"], applier=APPLIER, **{"pass": "submit"},
                    outcome=attempt_outcome, reason=note, screenshot_path=evidence_path,
                    started_at=_aware(claim["claimed_at"]), ended_at=now))
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
        import os
        import stat

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
            return str(candidate), data
        except FileNotFoundError:
            if missing_ok:
                return str(candidate), None
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
                'profile': profile, 'answers': answers, 'resumes': resumes,
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

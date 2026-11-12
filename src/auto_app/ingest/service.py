"""Save repeatable source observations and reuse the existing job pipeline."""

from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import re
from uuid import uuid4

from sqlalchemy import bindparam, case, func, insert, or_, select, update

from auto_app.core.db import tables as T
from auto_app.core.db.store import zjson
from auto_app.core.models import CommonPosting, utcnow
from auto_app.ingest.models import ParsedFeed
from auto_app.ingest.urls import canonical_url
from auto_app.process.dedup import load_items
from auto_app.process.link import apply_plan, plan_links, refresh_job_projection
from auto_app.process.normalize import NORMALIZER_VERSION, CompanyAliases, normalize_posting
from auto_app.screen.rules import stated_internship
from auto_app.sources.registry import default_registry


def _date(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).astimezone(timezone.utc)


def _payload(record):
    data = asdict(record)
    if record.listing_url is None:
        del data['listing_url']  # records without one keep their earlier shape, and so their content hash
    if record.posted_at is not None:
        data['posted_at'] = record.posted_at.isoformat()
    if record.description:
        # The text lives on the posting; import records keep only its fingerprint, which still changes
        # the content hash when the description changes. Records without one keep their earlier shape.
        del data['description']
        data['description_sha256'] = hashlib.sha256(record.description.encode()).hexdigest()
        data['description_chars'] = len(record.description)
    return data


def _content_hash(payload):
    # Relative ages, table layout and feed verification clocks are observations,
    # not changes to a job. Keep them in the saved payload but not its fit-change
    # fingerprint. All normalized fields and remaining metadata still count.
    metadata = payload.get("metadata", {})
    content = payload
    if "source_observation" in metadata:
        content = {**payload, "metadata": {k: v for k, v in metadata.items()
                                           if k != "source_observation"}}
    if metadata.get("posted_at_precision") == "approx":
        # A date estimated from a relative age moves with the reader's clock and time zone;
        # the posting keeps the estimate from its first observation until a real change.
        content = {k: v for k, v in content.items() if k != "posted_at"}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _current_normalization(posting):
    norm = (posting['normalized'] or {}) if posting else {}
    return norm.get('version') == NORMALIZER_VERSION


class IngestService:
    def __init__(self, store, registry=None):
        self.store = store
        self.registry = registry or default_registry()

    def ingest(self, source: str, parsed: ParsedFeed, *, kind: str, origin: str,
               revision: str | None = None, complete_snapshot: bool = False,
               skip_internships: bool = False) -> dict:
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', source):
            raise ValueError('source must contain lowercase letters, numbers, underscores or hyphens')
        if kind not in ('repository', 'email'):
            raise ValueError('kind must be repository or email')
        now = utcnow()
        batch_id = 'ingest-' + uuid4().hex
        report = dict(batch_id=batch_id, source=source, kind=kind, origin=origin, revision=revision,
                      records=len(parsed.records), created=0, updated=0, unchanged=0, restored=0,
                      inactive=0, missing=0, jobs_created=0, skipped_internships=0, issues=parsed.issues)
        raw_prepared, seen_ids = [], set()
        for record in parsed.records:
            url = canonical_url(record.url)
            if not url or not record.external_id or record.external_id in seen_ids:
                raise ValueError(f'Invalid or duplicate import identity: {record.external_id!r}')
            seen_ids.add(record.external_id)
            # Still listed, so never "missing"; but neither imported nor renewed (feeds.yaml skip_internships).
            if skip_internships and stated_internship(record.title or '', (record.employment_type,)):
                report['skipped_internships'] += 1
                continue
            record = replace(record, url=url, posted_at=_date(record.posted_at) if record.posted_at else None)
            payload = _payload(record)
            digest = _content_hash(payload)
            # A saved email's Date describes the observation, not the import run
            # or the job's publication. Reading it again must not renew the job.
            observed = _date(record.metadata['observed_at']) if kind == 'email' and record.metadata.get('observed_at') else now
            raw_prepared.append((record, payload, digest, observed))

        # Read only the source identities this input can affect. In particular a
        # mailbox message must not deserialize every previously imported message.
        with self.store.engine.connect() as conn:
            if conn.execute(select(T.ingest_entries.c.external_id).where(
                T.ingest_entries.c.source == source, T.ingest_entries.c.kind != kind,
            ).limit(1)).first():
                raise ValueError(f'{source} is already used by a different source kind')
            if conn.execute(select(T.postings.c.posting_id).where(
                T.postings.c.source == source,
                or_(T.postings.c.parser != 'ingest-v1', T.postings.c.parser.is_(None)),
            ).limit(1)).first():
                raise ValueError(f'{source} already contains collector postings; choose a distinct import source name')
            query = select(T.ingest_entries).where(T.ingest_entries.c.source == source)
            if not complete_snapshot or parsed.issues:
                query = query.where(T.ingest_entries.c.external_id.in_(seen_ids))
            entries = {r['external_id']: r for r in conn.execute(query).mappings()}
            c = T.postings.c
            query = select(c.posting_id, c.source_job_id, c.state, c.normalized,
                           c.first_seen_at, c.last_seen_at, c.job_id,
                           T.jobs.c.archived_at.label('job_archived_at')).outerjoin(
                               T.jobs, c.job_id == T.jobs.c.job_id).where(c.source == source)
            if not complete_snapshot or parsed.issues:
                query = query.where(or_(c.source_job_id.in_(seen_ids),
                    c.posting_id.in_([entry['posting_id'] for entry in entries.values()])))
            posts = {r['posting_id']: r for r in conn.execute(query).mappings()}
        posts_by_id = {p['source_job_id']: p for p in posts.values()}

        # Normalization and compression run before acquiring the write lock, and
        # unchanged observations reuse persisted normalized content.
        aliases, prepared = None, []
        for record, payload, digest, observed in raw_prepared:
            old = entries.get(record.external_id)
            existing = posts.get(old['posting_id']) if old else posts_by_id.get(record.external_id)
            if existing and old and old['content_hash'] == digest and _current_normalization(existing):
                prepared.append((record, payload, digest, observed, None, None, None, None))
                continue
            if aliases is None:
                aliases = CompanyAliases(self.registry.companies(), self.store.load_aliases())
            title = (record.title or '').strip()
            metadata = {'source_kind': kind, 'external_id': record.external_id, 'origin': origin,
                        'revision': revision, 'title_observed': bool(title),
                        'enrichment_status': 'complete' if title and record.description.strip() else 'pending'}
            posting = CommonPosting(
                source=source, source_job_id=record.external_id, title=title or '[Title pending]',
                company=record.company, locations=record.locations, description_text=record.description,
                posted_at=record.posted_at, employment_type=record.employment_type,
                posted_at_precision=("exact" if record.metadata.get("posted_at_precision") == "timestamp"
                                     else record.metadata.get("posted_at_precision")),
                apply_url=record.url, url=record.listing_url or record.url, apply_kind='external',
                req_id=record.metadata.get('upstream', {}).get('req_id'),
                extras={'ingestion': metadata},
            )
            posting_data = posting.model_dump(mode='json')
            norm = normalize_posting(posting_data, source, aliases, None)
            prepared.append((record, payload, digest, observed, posting, posting_data, norm, zjson(payload)))

        touched_jobs, seen_jobs = set(), set()
        post_touches, entry_updates = [], []
        with self.store.engine.begin() as conn:
            for record, payload, digest, observed, posting, posting_data, norm, compressed in prepared:
                old = entries.get(record.external_id)
                if old and kind == 'email' and not record.metadata.get('observed_at'):
                    observed = _date(old['first_seen_at'])
                existing = posts.get(old['posting_id']) if old else posts_by_id.get(record.external_id)
                renormalize = not _current_normalization(existing)
                pid = existing['posting_id'] if existing else None
                archived = existing and (existing['job_archived_at'] or existing['state'] == 'archived')
                changed = old is not None and old['content_hash'] != digest
                restored = old is not None and old['status'] == 'missing'
                event = 'created' if old is None else ('updated' if changed else ('restored' if restored else None))
                report[event or 'unchanged'] += 1
                if restored and changed:
                    report['restored'] += 1
                status = 'inactive' if record.active is False else ('active' if record.active else 'unknown')
                if record.active is False:
                    report['inactive'] += 1
                first_seen = _date(old['first_seen_at']) if old else (
                    min(_date(existing['first_seen_at']), observed) if existing else observed)
                last_seen = max(_date(old['last_seen_at']), observed) if old else observed
                if existing:
                    last_seen = max(_date(existing['last_seen_at']), last_seen)
                needs_reassessment = bool(old and old['needs_reassessment']) or bool(changed and existing and existing['job_id'])
                if not archived and (record.active is not False or existing):
                    posting_seen = _date(existing['last_seen_at']) if existing and record.active is False else last_seen
                    if changed or renormalize:
                        values = dict(
                            source=source, source_job_id=record.external_id,
                            source_url=record.listing_url or record.url,
                            kind='api', batch_id=batch_id, state='normalized',
                            parser='ingest-v1', parsed_at=now.isoformat(), parse_error=None,
                            first_seen_at=first_seen, last_seen_at=posting_seen, parsed=posting_data,
                            title=posting.title, company=posting.company, posted_at=posting.posted_at,
                            apply_url=posting.apply_url, apply_kind='external', normalized=norm,
                            company_norm=norm['company_norm'], title_norm=norm['title_norm'],
                            employer_key=norm['employer_key'], req_id=norm['req_id'], desc_hash=norm['desc_hash'], purged=False,
                        )
                        if existing:
                            conn.execute(update(T.postings).where(T.postings.c.posting_id == pid).values(**values))
                        else:
                            pid = conn.execute(insert(T.postings).values(**values)).inserted_primary_key[0]
                    elif posting_seen > _date(existing['last_seen_at']):
                        post_touches.append({'touch_pid': pid, 'touch_seen': posting_seen})
                    if existing and existing['job_id']:
                        if changed or renormalize or existing['state'] == 'normalized':
                            touched_jobs.add(existing['job_id'])
                        if posting_seen > _date(existing['last_seen_at']):
                            seen_jobs.add(existing['job_id'])
                    if changed or renormalize:
                        conn.execute(T.posting_raw.delete().where(T.posting_raw.c.posting_id == pid))
                        conn.execute(insert(T.posting_raw).values(
                            posting_id=pid, fetched_at=now, record=compressed,
                            capture={'origin': origin, 'revision': revision, 'kind': kind},
                        ))
                entry_values = dict(
                    posting_id=pid, kind=kind, origin=origin, revision=revision, content_hash=digest,
                    payload=payload, status=status, first_seen_at=first_seen, last_seen_at=last_seen,
                    last_imported_at=now, needs_reassessment=needs_reassessment,
                )
                if old:
                    entry_updates.append({'entry_source': source, 'entry_id': record.external_id, **entry_values})
                else:
                    conn.execute(insert(T.ingest_entries).values(source=source, external_id=record.external_id, **entry_values))
                if event:
                    self._change(conn, source, record.external_id, pid, event, revision, digest, payload, observed, now)
            if post_touches:
                conn.execute(update(T.postings).where(T.postings.c.posting_id == bindparam('touch_pid'))
                             .values(last_seen_at=bindparam('touch_seen')), post_touches)
            if entry_updates:
                conn.execute(update(T.ingest_entries).where(
                    T.ingest_entries.c.source == bindparam('entry_source'),
                    T.ingest_entries.c.external_id == bindparam('entry_id'),
                ).values(**{key: bindparam(key) for key in entry_values}), entry_updates)
            if seen_jobs:
                self._refresh_seen_dates(conn, T.jobs.c.job_id.in_(seen_jobs))
            # Only a fully parsed snapshot can establish absence. Email files
            # are independent messages, never a complete snapshot of a mailbox.
            if complete_snapshot and not parsed.issues:
                for external_id, old in entries.items():
                    if external_id not in seen_ids and old['status'] != 'missing':
                        conn.execute(update(T.ingest_entries).where(
                            T.ingest_entries.c.source == source, T.ingest_entries.c.external_id == external_id
                        ).values(status='missing', revision=revision, last_imported_at=now))
                        self._change(conn, source, external_id, old['posting_id'], 'missing', revision,
                                     old['content_hash'], old['payload'], now, now)
                        report['missing'] += 1

        # Linking is separately transactional. On an interrupted run, persisted
        # normalized postings are picked up by the next import without duplicates.
        with self.store.engine.connect() as conn:
            pending = conn.execute(select(T.postings.c.posting_id).where(
                T.postings.c.source == source, T.postings.c.job_id.is_(None),
                T.postings.c.state == 'normalized',
            ).limit(1)).first()
        if pending:
            links = self.store.posting_links()
            items = load_items(self.store, self.registry, source=source, include_linked=True)
            plan = plan_links(items, links)
            joined_ids = [items[pid].source_job_id for members in plan.joined.values() for pid in members
                          if items[pid].source == source]
            if joined_ids:
                # Persist before linking so an interrupted retry keeps reassessment.
                with self.store.engine.begin() as conn:
                    conn.execute(update(T.ingest_entries).where(
                        T.ingest_entries.c.source == source,
                        T.ingest_entries.c.external_id.in_(joined_ids),
                    ).values(needs_reassessment=True))
            report['jobs_created'] = len(apply_plan(self.store, plan))
        if touched_jobs:
            # Read after acquiring the write lock, so an enrichment committed
            # before this transaction cannot be replaced by a stale projection.
            with self.store.transaction(immediate=True):
                items = load_items(self.store, self.registry, job_ids=touched_jobs)
                refresh_job_projection(self.store, items, touched_jobs)
                # A normalized linked posting marks a pending projection refresh.
                # Clear only after it succeeds, so a failed import can retry it.
                self.store._tx.execute(update(T.postings).where(
                    T.postings.c.job_id.in_(touched_jobs), T.postings.c.source == source,
                    T.postings.c.state == 'normalized',
                ).values(state='linked'))
        self.store.write_batch(batch_id, {'batch_id': batch_id, 'started_at': now.isoformat(),
                                         'finished_at': utcnow().isoformat(), 'interrupted': False,
                                         'sources': {source: report}})
        return report

    @staticmethod
    def _change(conn, source, external_id, pid, event, revision, digest, payload, observed, now):
        conn.execute(insert(T.ingest_changes).values(
            source=source, external_id=external_id, posting_id=pid, event=event, revision=revision,
            content_hash=digest, payload=payload, observed_at=observed, created_at=now,
        ))

    @staticmethod
    def _refresh_seen_dates(conn, where):
        latest = select(func.max(T.postings.c.last_seen_at)).where(
            T.postings.c.job_id == T.jobs.c.job_id,
            T.postings.c.state != 'archived',
        ).correlate(T.jobs).scalar_subquery()
        conn.execute(update(T.jobs).where(where, T.jobs.c.archived_at.is_(None))
                     .values(last_seen_at=latest))

    def checked_unchanged(self, source: str) -> dict:
        """A successful HTTP 304 updates observation clocks without rewriting evidence."""
        now = utcnow()
        active = (T.ingest_entries.c.source == source) & T.ingest_entries.c.status.in_(['active', 'unknown'])
        posting_ids = select(T.ingest_entries.c.posting_id).where(active)
        observed = T.postings.c.posting_id.in_(posting_ids) & (T.postings.c.state != 'archived')
        with self.store.engine.begin() as conn:
            count = conn.execute(update(T.ingest_entries).where(active).values(
                last_seen_at=case((T.ingest_entries.c.last_seen_at < now, now),
                                  else_=T.ingest_entries.c.last_seen_at), last_imported_at=now,
            )).rowcount
            conn.execute(update(T.postings).where(observed, T.postings.c.last_seen_at < now)
                         .values(last_seen_at=now))
            self._refresh_seen_dates(conn, T.jobs.c.job_id.in_(
                select(T.postings.c.job_id).where(observed, T.postings.c.job_id.is_not(None))))
        return {'source': source, 'not_modified': True, 'unchanged': count, 'jobs_created': 0}

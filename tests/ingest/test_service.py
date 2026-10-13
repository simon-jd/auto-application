from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from auto_app.core.db import tables as T
from auto_app.core.db.store import DbStore
from auto_app.ingest.models import IngestRecord, ParsedFeed
from auto_app.settings import Paths


@pytest.fixture
def store(tmp_path):
    result = DbStore(tmp_path / 'jobs.db', Paths(home=tmp_path))
    yield result
    result.engine.dispose()


def service(store):
    from auto_app.ingest.service import IngestService
    return IngestService(store)


def record(id='a', **kw):
    fields = dict(external_id=id, url=f'https://jobs.lever.co/acme/{id}', title='Backend Engineer',
                  company='Acme', locations=['Boston, MA'], active=True)
    fields.update(kw)
    return IngestRecord(**fields)


def ingest(store, records, source='repo-one', **kw):
    return service(store).ingest(source, ParsedFeed(records), kind='repository', origin='fixture.json',
                                 revision='revision-1', complete_snapshot=True, **kw)


def test_insert_repeat_and_changed_content_preserve_job_and_decision(store):
    first = ingest(store, [record()])
    assert first['created'] == 1 and first['jobs_created'] == 1
    job = store.jobs()[0]
    store.mark_job(job['job_id'], 'want_to_apply')
    again = ingest(store, [record()])
    assert again['unchanged'] == 1 and again['jobs_created'] == 0
    changed = ingest(store, [record(title='Platform Engineer', locations=['Seattle, WA'])])
    assert changed['updated'] == 1
    jobs = store.jobs()
    assert len(jobs) == 1 and jobs[0]['job_id'] == job['job_id']
    assert jobs[0]['title'] == 'Platform Engineer'
    assert jobs[0]['locations'] == ['seattle, wa']
    assert jobs[0]['decision'] == 'want_to_apply'
    with store.engine.connect() as conn:
        entry = conn.execute(select(T.ingest_entries)).mappings().one()
        events = conn.execute(select(T.ingest_changes.c.event)).scalars().all()
    assert entry['needs_reassessment'] is True
    assert events == ['created', 'updated']


def test_two_repository_sources_share_one_official_job_but_keep_provenance(store):
    ingest(store, [record()])
    second = ingest(store, [record(id='other-id', url=record().url)], source='repo-two')
    assert second['jobs_created'] == 0
    assert len(store.jobs()) == 1
    assert store.jobs()[0]['sources'] == ['repo-one', 'repo-two']


def test_new_source_evidence_on_an_existing_job_flags_reassessment(store):
    from auto_app.query.service import QueryService
    url = 'https://jobs.lever.co/acme/00000000-0000-0000-0000-000000000123'
    ingest(store, [record(url=url, description='Build an API.')])
    jid = store.jobs()[0]['job_id']
    store.update_job(jid, step2='recommended')
    store.mark_job(jid, 'want_to_apply')
    ingest(store, [record('other', url=url, title='Senior Platform Engineer',
                          description='Design distributed infrastructure. ' * 30)], source='repo-two')
    with QueryService(store.db_path) as query:
        detail = query.job(jid)
        assert detail['title'] == 'Senior Platform Engineer'
        assert detail['decision'] == 'want_to_apply' and detail['step2'] == 'recommended'
        assert detail['ingestion']['needs_reassessment'] is True


def test_sparse_unknown_title_is_visible_without_a_fabricated_description(store):
    report = service(store).ingest('mail', ParsedFeed([record(title=None, company=None, active=None)]),
                                  kind='email', origin='recommendations.eml', revision='mail-1')
    assert report['created'] == 1
    job = store.jobs()[0]
    parsed, _ = store.job_inputs(job['job_id'])
    assert parsed['title'] == '[Title pending]'
    assert parsed['description_text'] == ''
    assert parsed['extras']['ingestion']['enrichment_status'] == 'pending'
    assert parsed['extras']['ingestion']['title_observed'] is False


def test_snapshot_removal_is_recorded_without_deleting_job_and_reappearance_restores(store):
    ingest(store, [record()])
    missing = ingest(store, [])
    assert missing['missing'] == 1 and len(store.jobs()) == 1
    with store.engine.connect() as conn:
        assert conn.execute(select(T.ingest_entries.c.status)).scalar_one() == 'missing'
    back = ingest(store, [record()])
    assert back['restored'] == 1 and len(store.jobs()) == 1


def test_partial_parse_never_marks_other_entries_missing(store):
    ingest(store, [record('a'), record('b')])
    result = service(store).ingest('repo-one', ParsedFeed([record('a')], [{'index': 1, 'reason': 'bad row'}]),
                                  kind='repository', origin='fixture.json', revision='r2', complete_snapshot=True)
    assert result['missing'] == 0 and len(result['issues']) == 1
    with store.engine.connect() as conn:
        assert set(conn.execute(select(T.ingest_entries.c.status)).scalars()) == {'active'}


def test_initial_inactive_record_does_not_create_an_opportunity(store):
    report = ingest(store, [record(active=False)])
    assert report['inactive'] == 1
    assert store.jobs() == []
    activated = ingest(store, [record(active=True)])
    assert activated['jobs_created'] == 1


def test_reimporting_old_email_does_not_advance_source_seen_time(store):
    observed = '2024-01-02T12:00:00+00:00'
    parsed = ParsedFeed([record(metadata={'observed_at': observed})])
    service(store).ingest('mail', parsed, kind='email', origin='old.eml', revision='r1')
    service(store).ingest('mail', parsed, kind='email', origin='old.eml', revision='r1')
    assert store.jobs()[0]['last_seen_at'] == observed


def test_archived_entry_is_not_resurrected_by_reimport(store):
    from sqlalchemy import update
    ingest(store, [record()])
    job = store.jobs()[0]
    # Simulate TTL's stable-ID retirement while keeping the observation mapping.
    with store.engine.begin() as conn:
        conn.execute(update(T.jobs).values(archived_at=datetime.now(timezone.utc), decision='applied'))
        conn.execute(update(T.postings).values(source_job_id='a#archived1', state='archived', normalized=None))
    result = ingest(store, [record()])
    assert result['jobs_created'] == 0
    assert store.jobs() == []
    assert store.jobs(archived=True)[0]['decision'] == 'applied'


def test_entry_history_survives_posting_deletion(store):
    from sqlalchemy import delete, update
    ingest(store, [record()])
    with store.engine.begin() as conn:
        conn.execute(delete(T.job_locations))
        conn.execute(delete(T.job_keys))
        conn.execute(update(T.postings).values(job_id=None))
        conn.execute(delete(T.jobs))
        conn.execute(delete(T.posting_raw))
        conn.execute(delete(T.postings))
        entry = conn.execute(select(T.ingest_entries)).mappings().one()
        assert entry['posting_id'] is None
        assert conn.execute(select(T.ingest_changes.c.change_id)).first()


def test_invalid_record_rolls_back_the_source_write(store):
    with pytest.raises(ValueError):
        ingest(store, [record(), record('bad', url='not-a-url')])
    assert store.jobs() == []
    with store.engine.connect() as conn:
        assert conn.execute(select(T.postings)).first() is None


def test_email_without_date_keeps_its_original_observation_time(store, monkeypatch):
    import auto_app.ingest.service as module
    clock = datetime(2025, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(module, 'utcnow', lambda: clock)
    parsed = ParsedFeed([record()])
    service(store).ingest('mail', parsed, kind='email', origin='undated.eml')
    clock = datetime(2026, 1, 1, tzinfo=timezone.utc)
    service(store).ingest('mail', parsed, kind='email', origin='undated.eml')
    assert store.jobs()[0]['last_seen_at'] == '2025-01-01T00:00:00+00:00'


def test_http_not_modified_refreshes_observation_without_content_revision(store, monkeypatch):
    import auto_app.ingest.service as module
    clock = datetime(2025, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(module, 'utcnow', lambda: clock)
    ingest(store, [record()])
    clock = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = service(store).checked_unchanged('repo-one')
    assert result['unchanged'] == 1
    assert store.jobs()[0]['last_seen_at'] == clock.isoformat()
    with store.engine.connect() as conn:
        assert len(conn.execute(select(T.ingest_changes)).all()) == 1


def test_email_observation_offset_is_converted_to_utc(store):
    parsed = ParsedFeed([record(metadata={'observed_at': '2026-10-09T10:30:00-07:00'})])
    service(store).ingest('mail', parsed, kind='email', origin='offset.eml')
    assert store.jobs()[0]['last_seen_at'] == '2026-10-09T17:30:00+00:00'


def test_inactive_sightings_do_not_keep_renewing_the_job(store, monkeypatch):
    import auto_app.ingest.service as module
    clock = datetime(2025, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(module, 'utcnow', lambda: clock)
    ingest(store, [record()])
    clock = datetime(2025, 2, 1, tzinfo=timezone.utc)
    ingest(store, [record(active=False)])
    clock = datetime(2025, 3, 1, tzinfo=timezone.utc)
    ingest(store, [record(active=False)])
    assert store.jobs()[0]['last_seen_at'] == '2025-01-01T00:00:00+00:00'
    with store.engine.connect() as conn:
        assert conn.execute(select(T.ingest_entries.c.last_seen_at)).scalar_one().month == 3


def test_empty_import_does_not_link_unrelated_normalized_postings(store):
    from sqlalchemy import insert
    from auto_app.core.models import CommonPosting
    from auto_app.process.normalize import CompanyAliases, normalize_posting
    other = CommonPosting(source='unrelated', source_job_id='elsewhere', title='Elsewhere', company='Other')
    parsed = other.model_dump(mode='json')
    norm = normalize_posting(parsed, 'unrelated', CompanyAliases(), None)
    with store.engine.begin() as conn:
        conn.execute(insert(T.postings).values(source='unrelated', source_job_id='elsewhere', kind='api',
                     state='normalized', parsed=parsed, normalized=norm,
                     first_seen_at=datetime.now(timezone.utc), last_seen_at=datetime.now(timezone.utc)))
    result = service(store).ingest('mail', ParsedFeed(), kind='email', origin='empty.eml')
    assert result['jobs_created'] == 0 and store.jobs() == []


def test_import_source_cannot_take_over_browser_collector_records(store):
    from tests.process.test_link import add, run
    add(store, 'nvidia', 'R42', title='Native Backend Engineer')
    run(store)
    before = store.jobs()
    with pytest.raises(ValueError, match='distinct.*source'):
        ingest(store, [record('R42', title='Imported Platform Engineer')], source='nvidia')
    assert store.jobs() == before
    with store.engine.connect() as conn:
        assert conn.execute(select(T.ingest_entries)).first() is None

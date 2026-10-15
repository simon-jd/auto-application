"""Bound database work for repeat observations, using only disposable fixtures."""
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import event, select, update

from auto_app.core.db import tables as T
from auto_app.ingest.runner import run_email
from tests.ingest.test_runner import message
from tests.ingest.test_service import ingest, record, service, store


@contextmanager
def statements(store):
    calls = []
    def track(conn, cursor, statement, parameters, context, executemany):
        calls.append(statement.lower())
    event.listen(store.engine, 'before_cursor_execute', track)
    try:
        yield calls
    finally:
        event.remove(store.engine, 'before_cursor_execute', track)


def test_repeat_snapshot_has_bounded_queries_and_preserves_projection(store):
    records = [record(str(i)) for i in range(40)]
    ingest(store, records)
    with statements(store) as calls:
        result = ingest(store, records)
    assert result['unchanged'] == 40
    assert len(calls) < 25, f'{len(calls)} queries for unchanged snapshot'
    assert not any('delete from job_keys' in q or 'delete from job_locations' in q for q in calls)
    assert len(store.jobs()) == 40


def test_changed_posting_refreshes_only_affected_job_without_loading_library(store):
    records = [record(str(i)) for i in range(40)]
    ingest(store, records)
    records[0] = record('0', title='Platform Engineer', locations=['Seattle, WA'])
    with statements(store) as calls:
        result = ingest(store, records)
    assert result['updated'] == 1
    assert len(calls) < 45, f'{len(calls)} queries for one changed posting'
    changed = next(job for job in store.jobs() if job['title'] == 'Platform Engineer')
    assert changed['locations'] == ['seattle, wa']
    assert sum('delete from job_keys' in q for q in calls) == 1


def test_304_uses_bounded_queries_and_does_not_rewrite_evidence(store, monkeypatch):
    import auto_app.ingest.service as module
    clock = datetime(2025, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(module, 'utcnow', lambda: clock)
    ingest(store, [record(str(i)) for i in range(40)])
    with store.engine.begin() as conn:
        conn.execute(update(T.ingest_entries).where(T.ingest_entries.c.external_id == '0').values(status='inactive'))
        conn.execute(update(T.ingest_entries).where(T.ingest_entries.c.external_id == '1').values(status='missing'))
        conn.execute(update(T.postings).where(T.postings.c.source_job_id == '2').values(state='archived'))
        conn.execute(update(T.postings).where(T.postings.c.source_job_id == '3').values(
            last_seen_at=datetime(2027, 1, 1, tzinfo=timezone.utc)))
    clock = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with statements(store) as calls:
        result = service(store).checked_unchanged('repo-one')
    assert result['unchanged'] == 38
    assert len(calls) < 10, f'{len(calls)} queries for 304'
    assert not any('delete from job_keys' in q or 'delete from job_locations' in q for q in calls)
    with store.engine.connect() as conn:
        dates = dict(conn.execute(select(T.postings.c.source_job_id, T.postings.c.last_seen_at)).all())
    assert dates['0'].year == dates['1'].year == dates['2'].year == 2025
    assert dates['3'].year == 2027
    assert dates['4'].year == 2026
    assert any(job['last_seen_at'].startswith('2027') for job in store.jobs())


def test_saved_email_revision_checkpoint_skips_completed_import(store, tmp_path):
    path = message(tmp_path / 'mail.eml')
    run_email(store, 'mail', [path])
    with store.engine.connect() as conn:
        before = conn.execute(select(T.ingest_entries.c.last_imported_at)).scalar_one()
    with statements(store) as calls:
        report = run_email(store, 'mail', [path])
    assert report['unchanged'] == 1 and report['created'] == 0
    with store.engine.connect() as conn:
        assert conn.execute(select(T.ingest_entries.c.last_imported_at)).scalar_one() == before
    assert not any(q.startswith('insert into batches') for q in calls)
    path.write_text(path.read_text().replace('Backend Engineer', 'Platform Engineer'))
    changed = run_email(store, 'mail', [path])
    assert changed['updated'] == 1
    assert store.jobs()[0]['title'] == 'Platform Engineer'


def test_interrupted_projection_refresh_recovers_on_unchanged_retry(store, monkeypatch):
    import pytest
    import auto_app.ingest.service as module
    ingest(store, [record()])
    changed = [record(locations=['Seattle, WA'])]
    def interrupt(*args, **kwargs):
        raise ValueError('projection interrupted')
    with monkeypatch.context() as failing:
        failing.setattr(module, 'refresh_job_projection', interrupt)
        with pytest.raises(ValueError, match='projection interrupted'):
            ingest(store, changed)
    assert store.jobs()[0]['locations'] == ['boston, ma']
    result = ingest(store, changed)
    assert result['unchanged'] == 1
    assert store.jobs()[0]['locations'] == ['seattle, wa']
    with store.engine.connect() as conn:
        assert conn.execute(select(T.postings.c.state)).scalar_one() == 'linked'


def test_failed_email_import_does_not_advance_revision_checkpoint(store, tmp_path, monkeypatch):
    import pytest
    import auto_app.ingest.service as module
    path = message(tmp_path / 'mail.eml')
    run_email(store, 'mail', [path])
    original = path.read_text()
    path.write_text(original.replace('Backend Engineer', 'Platform Engineer'))
    def interrupt(*args, **kwargs):
        raise RuntimeError('projection interrupted')
    with monkeypatch.context() as failing:
        failing.setattr(module, 'refresh_job_projection', interrupt)
        with pytest.raises(RuntimeError, match='projection interrupted'):
            run_email(store, 'mail', [path])
    assert str(path.resolve()) not in store.source_health('mail')['ingest']['files']
    # A rollback of file bytes must process the original again because the failed
    # newer attempt already committed its posting before projection failed.
    path.write_text(original)
    result = run_email(store, 'mail', [path])
    assert result['updated'] == 1
    assert store.jobs()[0]['title'] == 'Backend Engineer'


def test_unchanged_content_refreshes_stale_normalization_and_projection(store):
    from auto_app.process.normalize import NORMALIZER_VERSION
    imported = record(url='https://www.linkedin.com/jobs/view/4012345678')
    ingest(store, [imported])
    with store.engine.begin() as conn:
        norm = dict(conn.execute(select(T.postings.c.normalized)).scalar_one())
        norm.update(version='4', location_norm=['legacy city'])
        norm.pop('source_key', None)
        conn.execute(update(T.postings).values(normalized=norm))
        conn.execute(update(T.job_locations).values(location_norm='legacy city'))
    report = ingest(store, [imported])
    assert report['unchanged'] == 1 and report['updated'] == 0
    with store.engine.connect() as conn:
        norm = conn.execute(select(T.postings.c.normalized)).scalar_one()
        assert norm['version'] == NORMALIZER_VERSION
        assert norm['source_key'] == 'linkedin:jobs:4012345678'
        assert conn.execute(select(T.postings.c.state)).scalar_one() == 'linked'
    assert store.jobs()[0]['locations'] == ['boston, ma']


def test_saved_email_checkpoint_reimports_after_normalizer_upgrade(store, tmp_path):
    from auto_app.process.normalize import NORMALIZER_VERSION
    path = message(tmp_path / 'mail.eml')
    run_email(store, 'mail', [path])
    checkpoint = store.source_health('mail')['ingest']
    checkpoint['files'][str(path.resolve())]['normalizer_version'] = '4'
    store.update_health('mail', ingest=checkpoint)
    with store.engine.begin() as conn:
        norm = dict(conn.execute(select(T.postings.c.normalized)).scalar_one())
        norm.update(version='4')
        norm.pop('source_key', None)
        conn.execute(update(T.postings).values(normalized=norm))
    report = run_email(store, 'mail', [path])
    assert report['unchanged'] == 1 and not report['files'][0].get('not_modified')
    with store.engine.connect() as conn:
        norm = conn.execute(select(T.postings.c.normalized)).scalar_one()
        assert norm['version'] == NORMALIZER_VERSION
        assert norm['source_key'] == 'linkedin:jobs:100'
    assert store.source_health('mail')['ingest']['files'][str(path.resolve())]['normalizer_version'] == NORMALIZER_VERSION
    assert run_email(store, 'mail', [path])['files'][0]['not_modified'] is True


def test_saved_email_checkpoint_reimports_after_email_parser_upgrade(store, tmp_path):
    from auto_app.ingest.email import EMAIL_PARSER_VERSION

    path = message(tmp_path / 'mail.eml')
    run_email(store, 'mail', [path])
    checkpoint = store.source_health('mail')['ingest']
    checkpoint['files'][str(path.resolve())]['email_parser_version'] = 'old'
    store.update_health('mail', ingest=checkpoint)
    report = run_email(store, 'mail', [path])
    assert report['unchanged'] == 1 and not report['files'][0].get('not_modified')
    assert store.source_health('mail')['ingest']['files'][str(path.resolve())]['email_parser_version'] == EMAIL_PARSER_VERSION
    assert run_email(store, 'mail', [path])['files'][0]['not_modified'] is True


def test_mailbox_repeated_page_does_not_write_checkpoint_per_receipt(store, tmp_path):
    import httpx
    from auto_app.ingest.mailbox import run_mailbox
    from tests.ingest.test_mailbox import NOW, definition, gmail_message

    config = definition(tmp_path)

    def handler(request):
        if request.url.path.endswith('/messages'):
            return httpx.Response(200, json={'messages': [{'id': str(i)} for i in range(30)]})
        return httpx.Response(200, json=gmail_message(request.url.path.rsplit('/', 1)[-1], no_jobs=True))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        run_mailbox(store, 'mail', config, client=client, now=NOW)
        with statements(store) as calls:
            report = run_mailbox(store, 'mail', config, client=client, now=NOW)
    assert report['messages_skipped'] == 30
    assert sum(q.startswith('insert into source_health') for q in calls) <= 2

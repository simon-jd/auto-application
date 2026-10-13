"""Mailbox schema and application entry points use the real temporary database."""

import json
import base64
from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, text
import httpx

from auto_app.core.db.store import DbStore
from auto_app.ingest.runner import load_feed_config, run_config
from auto_app.settings import Paths


@pytest.fixture
def store(tmp_path):
    db = DbStore(tmp_path / 'jobs.db', Paths(home=tmp_path))
    yield db
    db.engine.dispose()


def test_migration_receipts_distinguish_accounts_and_preserve_existing_jobs(store):
    jid = store.create_job(title_norm='kept job', decision='want_to_apply')
    assert 'mail_messages' in inspect(store.engine).get_table_names()
    sql = text('''INSERT INTO mail_messages
        (source,provider,account_key,message_id,received_at,imported_at,job_links)
        VALUES ('mail','gmail',:account,'same-id',:stamp,:stamp,0)''')
    with store.engine.begin() as conn:
        for account in ('account-a', 'account-b'):
            conn.execute(sql, {'account': account, 'stamp': datetime.now(timezone.utc).isoformat()})
    reopened = DbStore(store.db_path, store.paths)
    try:
        assert reopened.job(jid)['decision'] == 'want_to_apply'
        with reopened.engine.connect() as conn:
            assert conn.execute(text('SELECT count(*) FROM mail_messages')).scalar_one() == 2
    finally:
        reopened.engine.dispose()


def test_mailbox_config_resolves_credentials_but_does_not_read_them(tmp_path):
    config = tmp_path / 'feeds.yaml'
    config.write_text('''sources:
  gmail-jobs:
    kind: mailbox
    provider: gmail
    credentials: ../credentials/gmail.json
    query: from:jobs@example.com
  outlook-jobs:
    kind: mailbox
    provider: outlook
    credentials: ../credentials/outlook.json
    folder: inbox
    senders: [jobs@example.com]
''')
    sources = load_feed_config(config)
    assert sources['gmail-jobs']['provider'] == 'gmail'
    assert str(sources['gmail-jobs']['credentials']) == str((tmp_path / '../credentials/gmail.json').resolve())
    assert sources['outlook-jobs']['folder'] == 'inbox'


def test_unconnected_mail_does_not_block_repository_and_persists_failure(store, tmp_path):
    (tmp_path / 'repo.json').write_text('[]')
    config = tmp_path / 'feeds.yaml'
    config.write_text('''sources:
  mail:
    kind: mailbox
    provider: gmail
    credentials: missing-token.json
    query: from:jobs@example.com
  repo:
    kind: repository
    location: repo.json
''')
    report = run_config(store, config)
    assert report['status'] == 'partial'
    assert report['sources']['repo']['status'] == 'ok'
    assert report['sources']['mail']['status'] == 'error'
    progress = store.source_health('mail')['ingest_run']
    assert progress['status'] == 'error' and progress['last_run_at']
    assert progress['error']
    from auto_app.query.service import QueryService
    with QueryService(store.db_path) as query:
        health = {source['source']: source for source in query.context()['sources']}
    assert health['mail']['status'] == 'error'
    assert health['repo']['status'] == 'ok'


def test_query_context_exposes_allowlisted_progress_without_mail_credentials(store):
    from auto_app.query.service import QueryService
    store.update_health('mail', ingest_run={
        'status': 'partial', 'last_run_at': '2026-10-10T00:00:00+00:00',
        'last_ok_at': '2026-10-09T00:00:00+00:00', 'has_more': True,
        'messages_processed': 50, 'messages_skipped': 4, 'error': 'mail temporarily unavailable',
        'access_token': 'SECRET', 'credentials': '/private/credentials.json',
    }, mailbox={'next_page': 'PRIVATE_CURSOR', 'refresh_token': 'SECRET'})
    with QueryService(store.db_path) as query:
        context = query.context()
    health = next(source for source in context['sources'] if source['source'] == 'mail')
    assert health['ingest_run']['has_more'] is True
    assert health['ingest_run']['messages_processed'] == 50
    assert all(secret not in json.dumps(context) for secret in ('SECRET', 'PRIVATE_CURSOR', '/private/credentials.json'))


def test_main_cli_mail_auth_commands_and_status_do_not_create_data(tmp_path):
    from typer.testing import CliRunner
    from auto_app.cli import app
    runner = CliRunner()
    env = {'AUTO_APP_HOME': str(tmp_path)}
    for provider in ('gmail', 'outlook'):
        result = runner.invoke(app, ['ingest', 'auth', provider, '--help'], env=env)
        assert result.exit_code == 0, result.output
    absent = tmp_path / 'not-created' / 'jobs.db'
    status = runner.invoke(app, ['ingest', 'status', '--db', str(absent)], env=env)
    assert status.exit_code == 0, status.output
    assert json.loads(status.stdout)
    assert not absent.parent.exists()


def test_manual_import_does_not_write_while_scheduled_import_holds_lock(tmp_path):
    from typer.testing import CliRunner
    from auto_app.ingest.cli import app
    from auto_app.ingest.scheduler import import_lock
    db = tmp_path / 'jobs.db'
    feed = tmp_path / 'empty.json'
    feed.write_text('[]')
    with import_lock(db):
        result = CliRunner().invoke(app, ['repository', str(feed), '--source', 'repo', '--db', str(db)],
                                    env={'AUTO_APP_HOME': str(tmp_path)})
    assert result.exit_code != 0
    assert 'already running' in json.loads(result.stdout)['error']
    assert not db.exists()


def test_both_mailboxes_refresh_tokens_merge_one_job_and_skip_repeated_messages(store, tmp_path):
    from auto_app.query.service import QueryService
    for provider in ('gmail', 'outlook'):
        (tmp_path / f'{provider}.json').write_text(json.dumps({
            'provider': provider, 'account_key': f'{provider}-account', 'client_id': 'test-client',
            'refresh_token': 'synthetic-old-refresh', 'expires_at': 0,
        }))
    config = tmp_path / 'feeds.yaml'
    config.write_text('''sources:
  gmail-jobs:
    kind: mailbox
    provider: gmail
    credentials: gmail.json
    query: from:jobs@example.com
  outlook-jobs:
    kind: mailbox
    provider: outlook
    credentials: outlook.json
    folder: inbox
    senders: [jobs@example.com]
''')
    body = (b'Message-ID: <same-recommendation@example.com>\nContent-Type: text/html\n\n'
            b'<a href="https://www.linkedin.com/jobs/view/4242">Backend Engineer</a>')
    refreshes = []
    def handler(request):
        if request.url.path.endswith('/token'):
            assert request.method == 'POST'
            refreshes.append(request.url.host)
            return httpx.Response(200, json={'access_token': 'synthetic-access',
                'refresh_token': 'synthetic-rotated-refresh', 'expires_in': 3600})
        assert request.method == 'GET'  # Mail content is only ever read.
        assert request.headers['authorization'] == 'Bearer synthetic-access'
        if request.url.host == 'gmail.googleapis.com':
            if request.url.path.endswith('/messages'):
                return httpx.Response(200, json={'messages': [{'id': 'g-one', 'threadId': 'thread'}]})
            return httpx.Response(200, json={'id': 'g-one', 'raw': base64.urlsafe_b64encode(body).decode(),
                                            'internalDate': '1791547200000'})
        if request.url.path.endswith('/$value'):
            return httpx.Response(200, content=body)
        return httpx.Response(200, json={'value': [{'id': 'o-one', 'receivedDateTime': '2026-10-09T12:00:00Z'}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = run_config(store, config, client=client)
        repeat = run_config(store, config, client=client)
    assert first['status'] == repeat['status'] == 'ok'
    assert len(refreshes) == 2
    assert all(result['messages_processed'] == 1 for result in first['sources'].values())
    assert all(result['messages_skipped'] == 1 and result['jobs_created'] == 0
               for result in repeat['sources'].values())
    with QueryService(store.db_path) as query:
        assert query.jobs()['total'] == 1
        detail = query.job(query.jobs()['items'][0]['job_id'])
        assert detail['sources'] == ['gmail-jobs', 'outlook-jobs']
        assert len(detail['ingestion']['entries']) == 2
        assert detail['ingestion']['pending_enrichment']
        assert 'synthetic-access' not in json.dumps(query.context())
    for provider in ('gmail', 'outlook'):
        assert json.loads((tmp_path / f'{provider}.json').read_text())['refresh_token'] == 'synthetic-rotated-refresh'

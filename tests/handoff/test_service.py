"""Handoff: one claim per job, leases, server-side guardrails, outcomes recorded once."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from auto_app.core.db import tables as T
from tests.imported_jobs import ingest, job_id_of, make_store, record

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def env(tmp_path):
    store = make_store(tmp_path)
    ingest(store, [record("101", "Backend Engineer"), record("102", "Data Engineer"),
                   record("103", "ML Engineer", company="Beta",
                          url="https://jobs.lever.co/beta/6ed76ce8-1111-2222-3333-444455556666")])
    for title in ("Backend Engineer", "Data Engineer", "ML Engineer"):
        store.set_job_state(job_id_of(store, title), decision="want_to_apply")
    ids = {t: job_id_of(store, t) for t in ("Backend Engineer", "Data Engineer", "ML Engineer")}
    store.engine.dispose()
    return tmp_path, ids


def service(home):
    from auto_app.handoff.service import HandoffService

    return HandoffService(home / "data" / "auto_app.db")


def test_the_queue_lists_wanted_jobs_with_their_platform(env):
    home, ids = env
    queue = service(home).queue(now=NOW)
    assert [i["job_id"] for i in queue["items"]] == [ids["Backend Engineer"], ids["Data Engineer"], ids["ML Engineer"]]
    assert queue["items"][0]["platform"] == "greenhouse" and queue["items"][2]["platform"] == "lever"


def test_a_job_can_be_claimed_once_until_the_lease_expires(env):
    from auto_app.handoff.service import HandoffError

    home, ids = env
    s = service(home)
    first = s.claim(ids["Backend Engineer"], "agent-a", now=NOW)
    with pytest.raises(HandoffError, match="claimed"):
        s.claim(ids["Backend Engineer"], "agent-b", now=NOW + timedelta(minutes=5))
    assert ids["Backend Engineer"] not in [i["job_id"] for i in s.queue(now=NOW)["items"]]
    later = s.claim(ids["Backend Engineer"], "agent-b", now=NOW + timedelta(minutes=31))
    assert later["token"] != first["token"]


def test_an_expired_claim_cannot_record(env):
    from auto_app.handoff.service import HandoffError

    home, ids = env
    s = service(home)
    token = s.claim(ids["Backend Engineer"], "agent-a", now=NOW)["token"]
    with pytest.raises(HandoffError, match="expired"):
        s.record_attempt(token, "failed", "timed out", now=NOW + timedelta(minutes=31))
    store = make_store(home)
    assert store.job(ids["Backend Engineer"])["decision"] == "want_to_apply"
    store.engine.dispose()


def test_a_late_submission_is_recorded_until_the_job_is_claimed_again(env):
    from auto_app.handoff.service import HandoffError

    home, ids = env
    s = service(home)
    late = s.claim(ids["Backend Engineer"], "agent-a", now=NOW)["token"]
    s.record_attempt(late, "submitted", "slow form", now=NOW + timedelta(minutes=45))
    store = make_store(home)
    assert store.job(ids["Backend Engineer"])["decision"] == "applied"
    store.engine.dispose()
    stale = s.claim(ids["Data Engineer"], "agent-a", now=NOW)["token"]
    s.claim(ids["Data Engineer"], "agent-b", now=NOW + timedelta(minutes=31))
    with pytest.raises(HandoffError, match="claim token"):
        s.record_attempt(stale, "submitted", now=NOW + timedelta(minutes=32))


def test_submitted_marks_applied_once_and_frees_the_queue(env):
    home, ids = env
    s = service(home)
    token = s.claim(ids["Backend Engineer"], "agent-a", now=NOW)["token"]
    result = s.record_attempt(token, "submitted", "confirmation page shown", now=NOW + timedelta(minutes=10))
    assert result["status"] == "submitted"
    store = make_store(home)
    assert store.job(ids["Backend Engineer"])["decision"] == "applied"
    assert "submitted by your agent" in store.reviews(ids["Backend Engineer"])[-1]["reason"]
    with store.engine.connect() as conn:
        app = conn.execute(select(T.applications).where(T.applications.c.job_id == ids["Backend Engineer"])).mappings().one()
    assert app["status"] == "submitted" and app["submitted_at"] is not None
    store.engine.dispose()
    assert ids["Backend Engineer"] not in [i["job_id"] for i in s.queue(now=NOW)["items"]]
    from auto_app.handoff.service import HandoffError

    with pytest.raises(HandoffError):
        s.record_attempt(token, "submitted", "again", now=NOW + timedelta(minutes=11))


def test_needs_user_leaves_the_queue_until_requeued(env):
    home, ids = env
    s = service(home)
    token = s.claim(ids["Data Engineer"], "agent-a", now=NOW)["token"]
    s.record_attempt(token, "needs_user", "CAPTCHA on sign-up", now=NOW)
    assert ids["Data Engineer"] not in [i["job_id"] for i in s.queue(now=NOW)["items"]]
    s.requeue(ids["Data Engineer"])
    assert ids["Data Engineer"] in [i["job_id"] for i in s.queue(now=NOW)["items"]]


def test_guardrails_block_the_daily_limit_and_company_cooldown(env):
    from auto_app.handoff.service import HandoffError

    home, ids = env
    (home / "config").mkdir(exist_ok=True)
    (home / "config" / "handoff.json").write_text(json.dumps({"daily_submissions": 5, "company_limit": 1}))
    s = service(home)
    token = s.claim(ids["Backend Engineer"], "agent-a", now=NOW)["token"]
    s.record_attempt(token, "submitted", "ok", now=NOW)
    queue = s.queue(now=NOW)
    assert [i["job_id"] for i in queue["items"]] == [ids["ML Engineer"]]  # Acme hit its 1-per-30-days limit
    assert queue["blocked"]["by_reason"] == {"company_cooldown": 1}
    with pytest.raises(HandoffError, match="company"):
        s.claim(ids["Data Engineer"], "agent-a", now=NOW)
    (home / "config" / "handoff.json").write_text(json.dumps({"daily_submissions": 1}))
    assert service(home).queue(now=NOW)["blocked"]["daily_limit"] is True
    with pytest.raises(HandoffError, match="daily"):
        service(home).claim(ids["ML Engineer"], "agent-a", now=NOW)


def test_repost_warnings_and_rule_exclusions_keep_jobs_out(env):
    home, ids = env
    store = make_store(home)
    with store.engine.begin() as conn:
        conn.execute(update(T.jobs).where(T.jobs.c.job_id == ids["Data Engineer"]).values(
            warnings=[{"kind": "repost", "job_id": 7, "text": "looks like job #7, which you applied to"}]))
    store.engine.dispose()
    queue = service(home).queue(now=NOW)
    assert ids["Data Engineer"] not in [i["job_id"] for i in queue["items"]]
    assert queue["blocked"]["by_reason"]["already_applied_repost"] == 1


def test_bad_outcomes_and_unknown_tokens_are_rejected(env):
    from auto_app.handoff.service import HandoffError

    home, ids = env
    s = service(home)
    token = s.claim(ids["ML Engineer"], "agent-a", now=NOW)["token"]
    with pytest.raises(HandoffError, match="outcome"):
        s.record_attempt(token, "maybe", now=NOW)
    with pytest.raises(HandoffError, match="claim"):
        s.record_attempt("not-a-token", "failed", now=NOW)


def test_live_claims_reserve_daily_and_company_quotas(env):
    from auto_app.handoff.service import HandoffError

    home, ids = env
    (home / 'config').mkdir(exist_ok=True)
    settings = home / 'config' / 'handoff.json'
    settings.write_text(json.dumps({'company_limit': 1}))
    s = service(home)
    first = s.claim(ids['Backend Engineer'], 'one', now=NOW)
    with pytest.raises(HandoffError, match='company'):
        s.claim(ids['Data Engineer'], 'two', now=NOW)
    s.release(first['token'], now=NOW)
    settings.write_text(json.dumps({'daily_submissions': 1}))
    first = s.claim(ids['Backend Engineer'], 'one', now=NOW)
    with pytest.raises(HandoffError, match='daily'):
        s.claim(ids['ML Engineer'], 'two', now=NOW)
    s.record_attempt(first['token'], 'submitted', now=NOW)


def test_a_submission_is_recorded_even_after_the_job_changed(env):
    from auto_app.handoff.service import HandoffError
    from auto_app.track.events import events_for

    home, ids = env
    s = service(home)
    token = s.claim(ids['Backend Engineer'], 'agent', now=NOW)['token']
    store = make_store(home)
    with store.engine.begin() as conn:
        conn.execute(update(T.jobs).where(T.jobs.c.job_id == ids['Backend Engineer']).values(
            archived_at=NOW))
    assert s.record_attempt(token, 'submitted', now=NOW)['status'] == 'submitted'
    assert [e['kind'] for e in events_for(store, ids['Backend Engineer'])] == ['applied']
    with store.engine.connect() as conn:
        assert conn.execute(select(T.applications.c.status)).scalar() == 'submitted'
        assert conn.execute(select(T.handoff_claims)).first() is None
    store.engine.dispose()
    with pytest.raises(HandoffError):
        s.claim(ids['Backend Engineer'], 'agent', now=NOW)


def test_a_skip_never_overrides_your_own_decision(env):
    home, ids = env
    s = service(home)
    token = s.claim(ids['Backend Engineer'], 'agent', now=NOW)['token']
    store = make_store(home)
    store.set_job_state(ids['Backend Engineer'], decision='applied')
    assert s.record_attempt(token, 'skipped', now=NOW)['status'] == 'dropped'
    assert store.job(ids['Backend Engineer'])['decision'] == 'applied'
    store.engine.dispose()


def test_record_rolls_back_atomically(env, monkeypatch):
    import auto_app.agent.service as decisions

    home, ids = env
    s = service(home)
    token = s.claim(ids['Backend Engineer'], 'agent', now=NOW)['token']
    store = make_store(home)
    def fail(*args, **kwargs):
        raise RuntimeError('decision failed')
    monkeypatch.setattr(decisions, 'apply_manual_decision_in_transaction', fail)
    with pytest.raises(RuntimeError, match='decision failed'):
        s.record_attempt(token, 'submitted', now=NOW)
    with store.engine.connect() as conn:
        assert conn.execute(select(T.attempts)).all() == []
        assert conn.execute(select(T.handoff_claims)).first() is not None
        assert conn.execute(select(T.applications.c.status)).scalar() == 'queued'
    assert store.job(ids['Backend Engineer'])['decision'] == 'want_to_apply'
    store.engine.dispose()


def test_manual_applied_decisions_count_toward_quota(env):
    from auto_app.handoff.service import HandoffError

    home, ids = env
    store = make_store(home)
    with store.engine.begin() as conn:
        conn.execute(update(T.jobs).where(T.jobs.c.job_id == ids['Backend Engineer']).values(
            decision='applied', decided_at=NOW))
    store.engine.dispose()
    (home / 'config').mkdir(exist_ok=True)
    (home / 'config' / 'handoff.json').write_text(json.dumps({'daily_submissions': 1}))
    assert service(home).queue(now=NOW)['submitted_today'] == 1
    with pytest.raises(HandoffError, match='daily'):
        service(home).claim(ids['ML Engineer'], 'agent', now=NOW)


def test_country_duplicate_screen_and_bad_links_are_unclaimable(env):
    from auto_app.handoff.service import HandoffError, HandoffService

    home, ids = env
    preferences = home / 'custom-preferences.json'
    preferences.write_text(json.dumps({'exclude_countries': ['United States', 'Canada']}))
    store = make_store(home)
    with store.engine.begin() as conn:
        conn.execute(update(T.postings).where(T.postings.c.job_id == ids['Backend Engineer']).values(
            parsed={'locations': ['Remote in Canada'], 'title': 'Backend Engineer'}))
        conn.execute(update(T.jobs).where(T.jobs.c.job_id == ids['Data Engineer']).values(duplicate_of=99))
        conn.execute(update(T.postings).where(T.postings.c.job_id == ids['ML Engineer']).values(
            apply_url='javascript:alert(1)'))
    store.engine.dispose()
    s = HandoffService(home / 'data' / 'auto_app.db', preferences)
    assert s.queue(now=NOW)['items'] == []
    for job_id in ids.values():
        with pytest.raises(HandoffError):
            s.claim(job_id, 'agent', now=NOW)


def test_two_parallel_distinct_jobs_cannot_bypass_daily_limit(env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from auto_app.handoff.service import HandoffError

    home, ids = env
    (home / 'config').mkdir(exist_ok=True)
    (home / 'config' / 'handoff.json').write_text(json.dumps({'daily_submissions': 1}))
    barrier = Barrier(2)
    def claim_job(job_id):
        barrier.wait()
        try:
            return service(home).claim(job_id, 'parallel', now=NOW)
        except HandoffError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim_job, [ids['Backend Engineer'], ids['ML Engineer']]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert 'daily' in next(result for result in results if isinstance(result, str))


def test_config_change_after_claim_blocks_the_packet_but_not_a_reported_submission(env):
    from auto_app.handoff.service import HandoffError

    home, ids = env
    s = service(home)
    token = s.claim(ids['Backend Engineer'], 'agent', now=NOW)['token']
    (home / 'config').mkdir(exist_ok=True)
    (home / 'config' / 'preferences.json').write_text(json.dumps({'exclude_countries': ['Canada']}))
    (home / 'config' / 'handoff.json').write_text(json.dumps({'daily_submissions': 1}))
    store = make_store(home)
    with store.engine.begin() as conn:
        conn.execute(update(T.postings).where(T.postings.c.job_id == ids['Backend Engineer']).values(
            parsed={'locations': ['Toronto, ON'], 'title': 'Backend Engineer'}))
        conn.execute(update(T.job_locations).where(T.job_locations.c.job_id == ids['Backend Engineer']).values(
            location_norm='toronto, on'))
    with pytest.raises(HandoffError, match='eligible'):
        s.packet(token, now=NOW)
    # The agent already pressed submit before the change: the outcome is the truth to record.
    assert s.record_attempt(token, 'submitted', now=NOW)['status'] == 'submitted'
    assert store.job(ids['Backend Engineer'])['decision'] == 'applied'
    store.engine.dispose()


def test_queue_total_is_not_limited_by_page_size(env):
    home, _ = env
    result = service(home).queue(limit=1, now=NOW)
    assert len(result['items']) == 1 and result['total'] == 3


@pytest.mark.parametrize('outcome,status,decision', [
    ('blocked', 'action_needed', 'want_to_apply'),
    ('failed', 'failed', 'want_to_apply'),
    ('skipped', 'dropped', 'not_applied'),
])
def test_remaining_outcome_mappings(env, outcome, status, decision):
    home, ids = env
    s = service(home)
    token = s.claim(ids['Backend Engineer'], 'agent', now=NOW)['token']
    assert s.record_attempt(token, outcome, now=NOW)['status'] == status
    store = make_store(home)
    assert store.job(ids['Backend Engineer'])['decision'] == decision
    store.engine.dispose()
    queued = ids['Backend Engineer'] in [i['job_id'] for i in s.queue(now=NOW)['items']]
    assert queued == (outcome == 'failed')


def test_existing_application_link_is_refreshed_on_claim(env):
    home, ids = env
    s = service(home)
    token = s.claim(ids['Backend Engineer'], 'agent', now=NOW)['token']
    s.record_attempt(token, 'failed', now=NOW)
    store = make_store(home)
    link = 'https://job-boards.greenhouse.io/acme/jobs/9999'
    with store.engine.begin() as conn:
        conn.execute(update(T.postings).where(T.postings.c.job_id == ids['Backend Engineer']).values(apply_url=link))
    s.claim(ids['Backend Engineer'], 'agent', now=NOW)
    with store.engine.connect() as conn:
        assert conn.execute(select(T.applications.c.apply_url)).scalar() == link
    store.engine.dispose()


def test_failure_after_shared_decision_rolls_back_decision_review_attempt_and_claim(env, monkeypatch):
    import auto_app.agent.service as decisions

    home, ids = env
    s = service(home)
    token = s.claim(ids['Backend Engineer'], 'agent', now=NOW)['token']
    original = decisions.apply_manual_decision_in_transaction
    def fail_after_decision(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('later transaction failure')
    monkeypatch.setattr(decisions, 'apply_manual_decision_in_transaction', fail_after_decision)
    with pytest.raises(RuntimeError, match='later transaction failure'):
        s.record_attempt(token, 'submitted', now=NOW)
    store = make_store(home)
    assert store.job(ids['Backend Engineer'])['decision'] == 'want_to_apply'
    assert store.reviews(ids['Backend Engineer']) == []
    with store.engine.connect() as conn:
        assert conn.execute(select(T.attempts)).all() == []
        assert conn.execute(select(T.handoff_claims)).first() is not None
        assert conn.execute(select(T.applications.c.status)).scalar() == 'queued'
        assert conn.execute(select(T.application_events)).all() == []
    store.engine.dispose()


def test_parallel_same_job_has_one_claim_winner(env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from auto_app.handoff.service import HandoffError

    home, ids = env
    barrier = Barrier(2)
    def attempt(agent):
        barrier.wait()
        try:
            return service(home).claim(ids['Backend Engineer'], agent, now=NOW)
        except HandoffError as exc:
            return str(exc)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, ['agent-a', 'agent-b']))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert 'claimed' in next(result for result in results if isinstance(result, str))


def test_manual_applied_history_cannot_be_submitted_twice_after_state_reset(env):
    from auto_app.agent.service import AgentService
    from auto_app.handoff.service import HandoffError

    home, ids = env
    AgentService(home / 'data' / 'auto_app.db').record_application_status(ids['Backend Engineer'], 'applied')
    store = make_store(home)
    store.set_job_state(ids['Backend Engineer'], decision='want_to_apply')
    store.engine.dispose()
    assert ids['Backend Engineer'] not in [item['job_id'] for item in service(home).queue()['items']]
    with pytest.raises(HandoffError):
        service(home).claim(ids['Backend Engineer'], 'agent')


def test_raw_applied_event_blocks_reapplication_and_counts_daily_quota(env):
    from auto_app.agent.service import AgentService
    from auto_app.handoff.service import HandoffError

    home, ids = env
    AgentService(home / 'data' / 'auto_app.db').record_event(
        ids['Backend Engineer'], 'applied', occurred_at=NOW.isoformat())
    store = make_store(home)
    assert store.job(ids['Backend Engineer'])['decision'] == 'want_to_apply'
    store.engine.dispose()
    s = service(home)
    assert ids['Backend Engineer'] not in [i['job_id'] for i in s.queue(now=NOW)['items']]
    (home / 'config').mkdir(exist_ok=True)
    (home / 'config' / 'handoff.json').write_text(json.dumps({'daily_submissions': 1}))
    assert s.queue(now=NOW)['submitted_today'] == 1
    with pytest.raises(HandoffError, match='daily'):
        s.claim(ids['ML Engineer'], 'agent', now=NOW)


@pytest.mark.parametrize('url', [
    'https://127.1/job', 'https://localhost./job', 'https://127.0.0.1./job',
    'https://user:private-value@jobs.example.com/job', 'http://jobs.example.com/job',
])
def test_unsafe_https_authorities_are_never_queued(env, url):
    home, ids = env
    store = make_store(home)
    with store.engine.begin() as conn:
        conn.execute(update(T.postings).where(T.postings.c.job_id == ids['Backend Engineer']).values(apply_url=url))
    store.engine.dispose()
    assert ids['Backend Engineer'] not in [item['job_id'] for item in service(home).queue(now=NOW)['items']]


@pytest.mark.parametrize("feed_status", ["missing", "inactive"])
def test_fresh_native_posting_keeps_mixed_source_job_eligible_when_feed_is_missing(env, feed_status):
    from sqlalchemy import insert
    from auto_app.handoff.service import HandoffError

    home, ids = env
    job_id = ids['Backend Engineer']
    store = make_store(home)
    with store.engine.begin() as conn:
        conn.execute(update(T.ingest_entries).where(T.ingest_entries.c.external_id == '101').values(status=feed_status))
    s = service(home)
    assert job_id not in [item['job_id'] for item in s.queue(now=NOW)['items']]
    with pytest.raises(HandoffError):
        s.claim(job_id, 'agent', now=NOW)
    link = 'https://job-boards.greenhouse.io/acme/jobs/101'
    with store.engine.begin() as conn:
        native_id = conn.execute(insert(T.postings).values(
            source='native-ats', source_job_id='native-101', kind='api', platform='greenhouse',
            state='linked', job_id=job_id, title='Backend Engineer', company='Acme', apply_url=link,
            source_url=link, parsed={'title': 'Backend Engineer', 'locations': ['Boston, MA'], 'apply_url': link},
            first_seen_at=NOW, last_seen_at=NOW, purged=False)).inserted_primary_key[0]
        conn.execute(update(T.jobs).where(T.jobs.c.job_id == job_id).values(primary_posting_id=native_id))
    assert job_id in [item['job_id'] for item in s.queue(now=NOW)['items']]
    token = s.claim(job_id, 'agent', now=NOW)['token']
    s.release(token, now=NOW)
    with store.engine.begin() as conn:
        conn.execute(insert(T.enrichments).values(url=link, platform='greenhouse', status='gone',
                                                attempts=1, updated_at=NOW))
    result = s.queue(now=NOW)
    assert job_id not in [item['job_id'] for item in result['items']]
    assert result['blocked']['by_reason']['link_gone'] == 1
    store.engine.dispose()


def test_requeue_refuses_a_job_you_no_longer_want(env):
    from auto_app.handoff.service import HandoffError

    home, ids = env
    s = service(home)
    token = s.claim(ids['Data Engineer'], 'agent', now=NOW)['token']
    s.record_attempt(token, 'needs_user', now=NOW)
    store = make_store(home)
    store.set_job_state(ids['Data Engineer'], decision='not_applied')
    store.engine.dispose()
    with pytest.raises(HandoffError, match='want_to_apply'):
        s.requeue(ids['Data Engineer'])


def test_the_daily_limit_follows_the_configured_local_day(env):
    from auto_app.handoff.service import HandoffError

    home, ids = env
    (home / 'config').mkdir(exist_ok=True)
    (home / 'config' / 'handoff.json').write_text(
        json.dumps({'daily_submissions': 1, 'timezone': 'America/New_York'}))
    s = service(home)
    evening = datetime(2026, 10, 12, 23, 30, tzinfo=timezone.utc)  # 19:30 in New York
    token = s.claim(ids['Backend Engineer'], 'agent', now=evening)['token']
    s.record_attempt(token, 'submitted', now=evening)
    # 00:30 UTC is a new UTC day but still the same New York evening.
    with pytest.raises(HandoffError, match='daily'):
        s.claim(ids['ML Engineer'], 'agent', now=evening + timedelta(hours=1))
    assert s.claim(ids['ML Engineer'], 'agent', now=evening + timedelta(hours=5))['token']

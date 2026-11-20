"""Only confirmed deliveries mark kept, undecided jobs as notified."""

from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import insert, select, update

from auto_app.core.db import tables as T
from auto_app.core.discord import DiscordError
from auto_app.worker.digest import batches, embed, mark_notified, pending, send_alerts, send_digest
from tests.imported_jobs import ingest, job_id_of, make_store, record

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


class FakeDiscord:
    def __init__(self, fail_on: int | None = None, error="Discord rejected the message: HTTP 500"):
        self.posts, self.fail_on, self.error = [], fail_on, error

    def post(self, content=None, embeds=None):
        if self.fail_on is not None and len(self.posts) == self.fail_on:
            raise DiscordError(self.error)
        self.posts.append((content, embeds or []))


def screened(store, rows):
    """Rows: title, sponsorship (None means unscreened), description presence."""
    ingest(store, [record(str(100 + i), title) for i, (title, _, _) in enumerate(rows)])
    ids = {title: job_id_of(store, title) for title, _, _ in rows}
    with store.engine.begin() as conn:
        conn.execute(update(T.jobs).values(first_seen_at=NOW))
        for title, sponsorship, described in rows:
            if sponsorship is not None:
                conn.execute(insert(T.job_screenings).values(
                    job_id=ids[title], rules_version="t", facts_hash="h", outcome="kept", reasons=[],
                    signals={"sponsorship": sponsorship, "has_description": described}, screened_at=NOW))
    return ids


@pytest.fixture
def store(tmp_path):
    result = make_store(tmp_path)
    yield result
    result.engine.dispose()


def notified(store):
    with store.engine.connect() as conn:
        return set(conn.execute(select(T.jobs.c.job_id).where(T.jobs.c.notified_at.is_not(None))).scalars())


def test_batches_split_by_five_and_mark_only_what_is_passed(store):
    screened(store, [(f"Engineer {i}", "offers", True) for i in range(7)])
    plan, more = batches(store, limit=30, since=None, now=NOW)
    assert [len(batch) for batch in plan] == [5, 2] and more == 0
    mark_notified(store, plan[0], NOW)
    assert len(notified(store)) == 5


def test_sponsoring_jobs_come_first_and_handled_jobs_never(store):
    ids = screened(store, [("Backend Engineer", "no", True), ("Data Engineer", "offers", True),
                           ("ML Engineer", "unknown", True), ("Web Developer", "offers", True),
                           ("QA Engineer", "offers", True), ("SRE", "offers", True),
                           ("Unscreened Engineer", None, True), ("Archived Engineer", "offers", True)])
    store.set_job_state(ids["Web Developer"], decision="want_to_apply")
    store.set_job_state(ids["SRE"], step2="un_recommended")
    with store.engine.begin() as conn:
        conn.execute(update(T.job_screenings).where(T.job_screenings.c.job_id == ids["QA Engineer"])
                     .values(outcome="excluded"))
        conn.execute(update(T.jobs).where(T.jobs.c.job_id == ids["Archived Engineer"]).values(archived_at=NOW))
    jobs, more = pending(store, limit=10, now=NOW)
    assert [j["title"] for j in jobs] == ["Data Engineer", "ML Engineer", "Backend Engineer"] and more == 0


def test_your_filters_recommendations_come_first_and_are_marked(store):
    ids = screened(store, [("Backend Engineer", "offers", True), ("Data Engineer", "offers", True)])
    store.set_job_state(ids["Data Engineer"], step2="recommended")
    jobs, _ = pending(store, limit=10, now=NOW)
    assert [j["title"] for j in jobs] == ["Data Engineer", "Backend Engineer"]
    assert embed(jobs[0])["fields"][-1]["value"].startswith("⭐ your filter recommends it")


def test_newest_first_within_each_sponsorship_and_recommendation_group(store):
    ids = screened(store, [("Older Engineer", "offers", True), ("Newer Engineer", "offers", True)])
    with store.engine.begin() as conn:
        conn.execute(update(T.postings).where(T.postings.c.job_id == ids["Older Engineer"])
                     .values(posted_at=NOW - timedelta(days=1)))
        conn.execute(update(T.postings).where(T.postings.c.job_id == ids["Newer Engineer"]).values(posted_at=NOW))
    assert [j["title"] for j in pending(store, limit=10, now=NOW)[0]] == ["Newer Engineer", "Older Engineer"]


def test_title_only_jobs_wait_exactly_two_hours_for_enrichment(store):
    screened(store, [("Backend Engineer", "unknown", False)])
    assert pending(store, limit=10, now=NOW + timedelta(hours=2, microseconds=-1)) == ([], 0)
    later, _ = pending(store, limit=10, now=NOW + timedelta(hours=2))
    assert later[0]["title_only"] and "title only" in embed(later[0])["fields"][-1]["value"]


def test_digest_posts_in_batches_and_marks_what_was_delivered(store):
    screened(store, [(f"Engineer {i}", "offers", True) for i in range(7)])
    fake = FakeDiscord()
    report = send_digest(store, fake, limit=30, now=NOW)
    assert report == {"sent": 7, "waiting": 0}
    assert [len(embeds) for _, embeds in fake.posts] == [5, 2]
    assert fake.posts[0][0].startswith("**7 new job(s)**") and fake.posts[1][0] is None
    assert fake.posts[0][1][0]["fields"][1]["value"] == "🟢 sponsors visas"
    assert fake.posts[0][1][0]["url"] == "https://job-boards.greenhouse.io/acme/jobs/100"
    assert len(notified(store)) == 7
    assert send_digest(store, fake, now=NOW) == {"sent": 0, "waiting": 0}


def test_a_failed_post_marks_only_the_delivered_jobs(store):
    screened(store, [(f"Engineer {i}", "unknown", True) for i in range(7)])
    report = send_digest(store, FakeDiscord(fail_on=1), limit=30, now=NOW)
    assert report["sent"] == 5 and report["waiting"] == 2 and "HTTP 500" in report["error"]
    assert len(notified(store)) == 5
    assert send_digest(store, FakeDiscord(), now=NOW)["sent"] == 2


def test_limit_leaves_remaining_jobs_unmarked(store):
    screened(store, [(f"Engineer {i}", "offers", True) for i in range(7)])
    assert send_digest(store, FakeDiscord(), limit=3, now=NOW) == {"sent": 3, "waiting": 4}
    assert len(notified(store)) == 3


def test_shared_links_are_split_so_discord_does_not_hide_delivered_cards(store):
    screened(store, [("Backend Engineer", "offers", True), ("Data Engineer", "offers", True)])
    with store.engine.begin() as conn:
        conn.execute(update(T.jobs).values(canonical_url="https://example.com/careers"))
    fake = FakeDiscord()
    assert send_digest(store, fake, now=NOW) == {"sent": 2, "waiting": 0}
    assert [len(embeds) for _, embeds in fake.posts] == [1, 1]
    assert len(notified(store)) == 2


def test_jobs_seen_before_since_are_not_announced_but_boundary_is_included(store):
    screened(store, [("Backend Engineer", "offers", True)])
    assert pending(store, limit=10, since=NOW + timedelta(microseconds=1), now=NOW) == ([], 0)
    assert len(pending(store, limit=10, since=NOW, now=NOW)[0]) == 1


def test_current_country_preferences_apply_to_pending_jobs(store):
    ids = screened(store, [("US Engineer", "offers", True), ("Canada Engineer", "offers", True)])
    store.paths.preferences.parent.mkdir(parents=True, exist_ok=True)
    store.paths.preferences.write_text(json.dumps({"exclude_countries": ["Canada"]}))
    with store.engine.begin() as conn:
        row = conn.execute(select(T.postings.c.parsed).where(T.postings.c.job_id == ids["Canada Engineer"])).scalar()
        conn.execute(update(T.postings).where(T.postings.c.job_id == ids["Canada Engineer"])
                     .values(parsed={**row, "locations": ["Remote in Canada"]}))
        conn.execute(update(T.job_locations).where(T.job_locations.c.job_id == ids["Canada Engineer"])
                     .values(location_norm="remote"))
    assert [j["title"] for j in pending(store, limit=10, now=NOW)[0]] == ["US Engineer"]
    assert pending(store, limit=1, now=NOW, job_id=ids["Canada Engineer"]) == ([], 0)
    with store.engine.begin() as conn:  # a US place beside Canada keeps the job in the digest
        conn.execute(update(T.postings).where(T.postings.c.job_id == ids["Canada Engineer"])
                     .values(parsed={**row, "locations": ["Boston, MA", "Remote in Canada"]}))
    assert {j["title"] for j in pending(store, limit=10, now=NOW)[0]} == {"US Engineer", "Canada Engineer"}
    [job], more = pending(store, limit=1, now=NOW, job_id=ids["Canada Engineer"])
    assert job["title"] == "Canada Engineer" and more == 0


def test_maximum_embeds_stay_within_discord_limits():
    job = {"job_id": 123456789, "title": "😀" * 1000, "company": "x" * 500,
           "locations": ["😀" * 500, "Boston, MA"], "posted_at": "2026-10-12T12:00:00Z",
           "recommended": True, "reason": "😀" * 1000, "title_only": True, "years_required": 2}
    result = embed(job)
    units = lambda text: len(text.encode("utf-16-le")) // 2
    assert units(result["title"]) <= 256
    strings = [result["title"], result["footer"]["text"]]
    for field in result["fields"]:
        assert units(field["name"]) <= 256 and units(field["value"]) <= 1024
        strings.extend([field["name"], field["value"]])
    assert 5 * sum(units(value) for value in strings) <= 6000


def test_digest_error_does_not_persist_webhook_credentials(store):
    screened(store, [("Backend Engineer", "offers", True)])
    fake = FakeDiscord(fail_on=0, error="Error https://discord.com/api/webhooks/1/secret-token")
    report = send_digest(store, fake, now=NOW)
    assert report["sent"] == 0 and report["waiting"] == 1
    assert "secret-token" not in json.dumps(report)


def test_the_same_problem_is_alerted_once_per_twelve_hours():
    fake, state = FakeDiscord(), {}
    report = {"steps": {"enrich": {"status": "error", "error": "OperationalError: database is locked"}}}
    ingest_state = {"consecutive_failures": 3}
    assert len(send_alerts(fake, state, report, ingest_state, NOW)) == 2
    assert send_alerts(fake, state, report, ingest_state, NOW + timedelta(hours=12, microseconds=-1)) == []
    assert len(send_alerts(fake, state, report, ingest_state, NOW + timedelta(hours=12))) == 2
    assert len(fake.posts) == 4 and fake.posts[0][0].startswith("⚠️")


def test_failed_alert_is_not_deduplicated_as_sent():
    state = {}
    report = {"steps": {"enrich": {"status": "error", "error": "database is locked"}}}
    assert send_alerts(FakeDiscord(fail_on=0), state, report, {}, NOW) == []
    assert len(send_alerts(FakeDiscord(), state, report, {}, NOW)) == 1


def test_alert_text_and_persisted_keys_redact_credentials_and_handle_stale_state():
    fake = FakeDiscord()
    state = {"alerts": {"bad": "not-a-date", "old": (NOW - timedelta(days=3)).isoformat()}}
    report = {"steps": {"notify": {"status": "error",
                                   "error": "failed https://discord.com/api/webhooks/1/secret-token"}}}
    assert len(send_alerts(fake, state, report, {}, NOW)) == 1
    assert "secret-token" not in json.dumps([fake.posts, state])
    assert "bad" not in state["alerts"] and "old" not in state["alerts"]


def test_none_discord_never_changes_alert_state():
    state = {}
    assert send_alerts(None, state, {}, {"consecutive_failures": 3}, NOW) == []
    assert state == {}


def test_oldest_first_orders_by_collection_time_whatever_the_sponsorship(store):
    """The Discord bot's daily 50 (2026-11-19): oldest collected first, as the owner chose."""
    ids = screened(store, [("Newest Engineer", "offers", True), ("Oldest Engineer", "no", True),
                           ("Middle Engineer", "unknown", True)])
    with store.engine.begin() as conn:
        for title, days in (("Oldest Engineer", 3), ("Middle Engineer", 2), ("Newest Engineer", 1)):
            conn.execute(update(T.jobs).where(T.jobs.c.job_id == ids[title])
                         .values(first_seen_at=NOW - timedelta(days=days)))
    jobs, _ = pending(store, limit=10, now=NOW, oldest_first=True)
    assert [j["title"] for j in jobs] == ["Oldest Engineer", "Middle Engineer", "Newest Engineer"]
    assert [j["collected_days"] for j in jobs] == [3, 2, 1]
    assert pending(store, limit=10, now=NOW)[0][0]["title"] == "Newest Engineer"  # the default is unchanged


def test_closed_jobs_are_not_pushed(store):
    """A job whose every feed entry is inactive or missing, or whose link is gone, can't be queued: don't push it."""
    ids = screened(store, [("Open Engineer", "offers", True), ("Closed Engineer", "offers", True),
                           ("Gone Engineer", "offers", True)])
    with store.engine.begin() as conn:
        closed = select(T.postings.c.posting_id).where(T.postings.c.job_id == ids["Closed Engineer"])
        conn.execute(update(T.ingest_entries).where(T.ingest_entries.c.posting_id.in_(closed)).values(status="missing"))
        conn.execute(insert(T.enrichments).values(url="https://job-boards.greenhouse.io/acme/jobs/102", status="gone",
                                                  attempts=0, updated_at=NOW))
    jobs, more = pending(store, limit=10, now=NOW, oldest_first=True)
    assert [j["title"] for j in jobs] == ["Open Engineer"] and more == 0


def test_the_card_shows_when_the_job_was_collected():
    card = embed({"job_id": 7, "title": "SWE", "company": "Acme", "first_seen_at": "2026-11-02T10:00:00+00:00",
                  "collected_days": 18})
    assert {"name": "收录", "value": "2026-11-02 · 18 天前", "inline": True} in card["fields"]
    today = embed({"job_id": 7, "title": "SWE", "company": "Acme", "first_seen_at": "2026-11-20T03:00:00+00:00",
                   "collected_days": 0})  # 19:00 the evening before, Pacific
    assert {"name": "收录", "value": "2026-11-19 · 今天", "inline": True} in today["fields"]

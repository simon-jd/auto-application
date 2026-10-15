from datetime import timedelta

import pytest

from auto_app.collect.apply_link import decode_redirect
from auto_app.collect.filters import reject_reason
from auto_app.collect.store import FileStore
from auto_app.core.config import AppConfig, CardFilters, SourceConfig, SourceLimits
from auto_app.core.models import Card, utcnow


def card(title, lines=()):
    return Card(source="x", source_job_id="1", url="u", page=0, rank=1, title=title, lines=list(lines))


def test_title_word_filter_matches_whole_words():
    f = CardFilters(exclude_title_words=["senior", "staff"])
    assert reject_reason(card("Senior ML Engineer"), f) == "title word: senior"
    assert reject_reason(card("Staffing Coordinator"), f) is None
    assert reject_reason(card("ML Engineer"), f) is None


def test_company_filter_matches_card_lines():
    f = CardFilters(exclude_companies=["SomeAgency"])
    assert reject_reason(card("ML Engineer", ["ML Engineer", "SomeAgency Inc", "Remote"]), f) == "company: SomeAgency"
    assert reject_reason(card("ML Engineer", ["ML Engineer", "Acme"]), f) is None


@pytest.mark.parametrize("title,visible_title", [
    ("Salesforce Developer", "Salesforce Developer"),
    (None, "Salesforce Developer"),
    ("Salesforce Developer", "Salesforce Developer (Verified job)"),
])
def test_company_filter_ignores_the_title_line(title, visible_title):
    f = CardFilters(exclude_companies=["Salesforce"])
    assert reject_reason(card(title, [visible_title, "Acme", "Remote"]), f) is None
    assert reject_reason(card("Engineer", ["Engineer", "Salesforce Inc", "Remote"]), f) == "company: Salesforce"


@pytest.mark.parametrize("term,title,other", [
    ("Sr.", "Sr. Backend Engineer", "Research Engineer"),
    ("C++", "C++ Engineer", "C Engineer"),
])
def test_title_filter_handles_terms_ending_in_punctuation(term, title, other):
    f = CardFilters(exclude_title_words=[term])
    assert reject_reason(card(title), f) == f"title word: {term}"
    assert reject_reason(card(other), f) is None


def test_decode_redirect():
    assert (
        decode_redirect("https://www.linkedin.com/redir/redirect?url=https%3A%2F%2Fboards.greenhouse.io%2Facme%2Fjobs%2F1")
        == "https://boards.greenhouse.io/acme/jobs/1"
    )
    assert decode_redirect("https://jobright.ai/jobs/info/abc") is None


def test_source_limit_overrides_merge_with_builtin_defaults():
    cfg = AppConfig(sources={"linkedin": SourceConfig(limits=SourceLimits.model_validate({"max_details_per_hour": 20}))})
    li = cfg.limits("linkedin")
    assert li.max_details_per_hour == 20
    assert li.max_details_per_day == 300  # LinkedIn's built-in ceiling survives
    assert li.min_delay_s == 4  # LinkedIn's stricter default survives


def test_retry_candidates_respect_attempts_and_existing_postings(tmp_path):
    store = FileStore(tmp_path)
    now = utcnow()

    def sighting(jid, outcome, days_ago=0):
        store.add_sighting(
            "b1",
            {"source": "s", "source_job_id": jid, "outcome": outcome, "url": f"u/{jid}",
             "seen_at": (now - timedelta(days=days_ago)).isoformat()},
            None,
        )

    sighting("a", "fetch_failed")
    sighting("b", "fetch_failed")
    sighting("b", "fetch_failed")
    sighting("c", "budget_skipped")
    sighting("d", "fetch_failed", days_ago=30)  # too old
    got = [r["source_job_id"] for r in store.retry_candidates("s", within_days=7, max_attempts=2)]
    assert sorted(got) == ["a", "c"]  # b hit max attempts


def test_retry_candidates_exclude_gone_details_from_both_stores(store):
    store.write_batch("earlier", {"started_at": utcnow()})
    run_id = store.start_search_run("earlier", {"source": "s", "search_name": "s", "started_at": utcnow()})
    for jid, detail in [("missing", "HTTP 404"), ("gone", "HTTP 410 for https://example.test/job/2"),
                        ("temporary", "HTTP 503")]:
        store.add_sighting("earlier", {
            "search_run_id": run_id,
            "source": "s", "source_job_id": jid, "url": f"https://example.test/{jid}",
            "outcome": "fetch_failed", "outcome_detail": detail, "seen_at": utcnow().isoformat(),
        }, None)
    assert [r["source_job_id"] for r in store.retry_candidates("s", within_days=7, max_attempts=3)] == ["temporary"]


def test_file_store_latest_batch_uses_collection_timestamp(tmp_path):
    store = FileStore(tmp_path)
    assert store.get_batch() is None
    now = utcnow()
    for bid, started_at in [("z-old", now - timedelta(days=2)), ("a-new", now - timedelta(days=1)),
                            ("ingest-last", now)]:
        store.write_batch(bid, {"batch_id": bid, "started_at": started_at})
    assert store.get_batch()["batch_id"] == "a-new"
    assert store.get_batch("ingest-last")["batch_id"] == "ingest-last"
    assert store.get_batch("missing") is None


def test_file_store_no_collection_batch_after_ingestion(tmp_path):
    store = FileStore(tmp_path)
    store.write_batch("ingest-only", {"batch_id": "ingest-only", "started_at": utcnow()})
    assert store.get_batch() is None
    assert store.get_batch("ingest-only")["batch_id"] == "ingest-only"


def test_login_chrome_uses_same_keychain_as_playwright(monkeypatch, tmp_path):
    # Playwright passes --use-mock-keychain on macOS; the login window must match or the
    # collector can't decrypt the saved login cookies.
    from auto_app.collect import browser

    captured = {}
    monkeypatch.setattr(browser, "chrome_executable", lambda: "/bin/chrome")
    monkeypatch.setattr(browser.subprocess, "Popen", lambda args, **kw: captured.setdefault("args", args))
    browser.open_plain_chrome(tmp_path / "p", "https://example.com/login")
    assert "--use-mock-keychain" in captured["args"]
    assert "--enable-automation" not in captured["args"]


async def test_rate_limiter_pauses_and_counts_earlier_runs():
    from auto_app.collect.pacer import RateLimiter

    now = utcnow()
    history = [now - timedelta(minutes=50), now - timedelta(minutes=10), now - timedelta(hours=30)]
    rate = RateLimiter("s", SourceLimits(max_details_per_hour=2, max_details_per_day=3), history, fast=True)
    assert rate.day_exhausted() is None  # the 30-hour-old open fell out of the day window
    await rate.acquire()  # 2 opens in the last hour → must pause until the oldest ages out
    assert len(rate.pauses) == 1 and 9 * 60 < rate.pauses[0] < 11 * 60
    rate.record()
    assert rate.day_exhausted() == "daily ceiling 3 job pages reached"


def test_scheduled_sources_respects_window_and_interval(tmp_path):
    from datetime import datetime

    from auto_app.cli import _scheduled_sources

    store = FileStore(tmp_path)
    cfg = AppConfig(min_hours_between_runs=6)
    store.update_health("linkedin", last_run_at=utcnow())
    at_noon = datetime(2026, 9, 29, 12, 0)
    assert _scheduled_sources(cfg, store, ["linkedin", "nvidia"], now=at_noon) == ["nvidia"]
    assert _scheduled_sources(cfg, store, ["nvidia"], now=datetime(2026, 9, 29, 3, 0)) == []  # outside 08:00–23:00

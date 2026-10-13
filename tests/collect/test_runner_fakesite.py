import pytest

from auto_app.collect.plan import ConfigError
from auto_app.collect.runner import CollectionRunner, CollectOptions
from auto_app.collect.store import PostingRef
from auto_app.core.config import AppConfig, CardFilters, Defaults, SourceConfig, SourceSearch, SourceLimits
from auto_app.sources.registry import PluginInfo, load_registry
from auto_app.settings import Paths
from tests.fakesite.site import FakeAdapter, FakeSite


@pytest.fixture
def site():
    return FakeSite()


@pytest.fixture
def env(tmp_path, site, store):
    cfg = AppConfig(
        defaults=Defaults(max_pages_per_search=5, early_stop_known_streak=2,
                          card_filters=CardFilters(exclude_title_words=["staff"])),
        sources={"fake": SourceConfig(searches=[SourceSearch(name="ml", keywords=["ml"])])},
    )
    registry = load_registry()
    registry.add(PluginInfo("fake", "browser", FakeAdapter, "test"))
    paths = Paths(home=tmp_path)

    def run(**opts):
        options = CollectOptions(headless=True, fast=True, screenshots=True, context_hook=site.install, **opts)
        runner = CollectionRunner(cfg, paths, store, options, registry=registry)
        return runner

    run.cfg = cfg
    return store, run


def sightings(store, batch_id: str) -> list[dict]:
    return store.get_sightings(batch_id)


async def test_first_run_saves_raw_postings(env, site):
    store, make = env
    batch = await make().run(["fake"])
    src = batch["sources"]["fake"]
    assert src["status"] == "ok"
    assert src["outcomes"] == {"fetched": 4, "filtered": 1, "fetch_failed": 1}

    ref = PostingRef("fake", "101")
    assert store.get_posting(ref)["state"] == "fetched"
    raw = store.load_raw(ref)
    capture = raw.capture
    assert capture["ready"] is True
    assert capture["apply_kind"] == "external"
    assert capture["apply_href"] == "https://careers.acme.example/jobs/101"
    payloads = raw.payloads
    assert [p["body"]["description"] for p in payloads] == ["Description of job 101"]
    assert raw.embedded["jsonld"][0]["title"] == "Machine Learning Engineer"
    assert "Description of job 101" in raw.page_text
    assert store.screenshot_path(ref).stat().st_size > 0

    rows = {r["source_job_id"]: r for r in sightings(store, batch["batch_id"])}
    assert rows["102"]["outcome"] == "filtered" and rows["102"]["outcome_detail"] == "title word: staff"
    assert rows["103"]["is_promoted"] is True
    assert rows["404"]["outcome"] == "fetch_failed"
    assert not store.posting_exists("fake", "102")  # filtered cards are never fetched

    runs = store.get_search_runs(batch["batch_id"])
    assert runs[-1]["stop_reason"] == "done" and runs[-1]["pages_loaded"] == 3


async def test_second_run_skips_known_and_stops_early(env, site):
    store, make = env
    await make().run(["fake"])
    site.requests.clear()
    batch = await make(retry=False).run(["fake"])
    src = batch["sources"]["fake"]
    # 101 known, 102 filtered (doesn't break streak), 103 known but promoted (doesn't count), 104 known → streak 2
    assert src["outcomes"] == {"known": 3, "filtered": 1}
    assert src["stop_reasons"] == {"early_stop": 1}
    assert not [r for r in site.requests if r.startswith("/job/")]  # no job page opened


async def test_gone_cards_are_not_retried(env, site):
    store, make = env
    await make().run(["fake"])
    assert store.retry_candidates("fake", within_days=7, max_attempts=3) == []
    batch = await make().run(["fake"])
    rows = [r for r in sightings(store, batch["batch_id"]) if r["search_name"] == "__retry__"]
    assert rows == []


@pytest.mark.parametrize("interactive", [False, True])
async def test_one_source_error_preserves_other_sources_and_finishes_batch(env, monkeypatch, interactive):
    from playwright.async_api import Error as PlaywrightError

    class BrokenAdapter(FakeAdapter):
        async def collect_cards(self, *args):
            raise PlaywrightError("Execution context was destroyed")

    store, make = env
    runner = make(interactive=interactive)
    runner.registry.add(PluginInfo("broken", "browser", BrokenAdapter, "test"))
    runner.cfg.sources["broken"] = SourceConfig(searches=[SourceSearch(name="ml", keywords=["ml"])])
    monkeypatch.setattr("builtins.input", lambda prompt: "")

    batch = await runner.run(["broken", "fake"])

    assert batch["finished_at"]
    assert batch["sources"]["broken"]["status"] == "error"
    assert "Execution context was destroyed" in batch["sources"]["broken"]["error"]
    assert batch["sources"]["broken"]["caught_up"] is False
    assert batch["sources"]["fake"]["outcomes"]["fetched"] == 4
    assert store.posting_exists("fake", "101")
    assert store.source_health("broken")["status"] == "error"
    assert store.get_batch(batch["batch_id"])["finished_at"]
    assert all(r["status"] == "finished" for r in store.get_search_runs(batch["batch_id"]))


@pytest.mark.parametrize("failures, status", [(2, "ok"), (3, "error")])
async def test_session_transient_failures_retry_without_cooldown(env, monkeypatch, failures, status):
    from playwright.async_api import Error as PlaywrightError, Page

    store, make = env
    original_goto = Page.goto
    attempts = 0

    async def goto(self, url, **kwargs):
        nonlocal attempts
        if url.endswith("/home"):
            attempts += 1
            if attempts <= failures:
                raise PlaywrightError("net::ERR_INTERNET_DISCONNECTED")
        return await original_goto(self, url, **kwargs)

    monkeypatch.setattr(Page, "goto", goto)
    batch = await make().run(["fake"])

    assert attempts == 3
    assert batch["sources"]["fake"]["status"] == status
    assert not store.source_health("fake").get("cooldown_until")
    assert store.source_health("fake")["status"] == status


async def test_three_gone_details_do_not_block_source(env, site, monkeypatch):
    store, make = env
    original_handle = site.handle

    async def handle(route):
        if "/job/" in route.request.url:
            return await route.fulfill(status=404, body="not found")
        return await original_handle(route)

    monkeypatch.setattr(site, "handle", handle)
    batch = await make().run(["fake"])

    assert batch["sources"]["fake"]["status"] == "ok"
    assert batch["sources"]["fake"]["outcomes"]["fetch_failed"] == 5
    assert not store.source_health("fake").get("cooldown_until")
    assert store.retry_candidates("fake", within_days=7, max_attempts=3) == []


@pytest.mark.parametrize("failure, stop", [("block", "blocked"), ("error", "error"), ("cancel", "interrupted")])
async def test_retry_run_is_finished_when_fetch_stops(env, monkeypatch, failure, stop):
    import asyncio

    from playwright.async_api import Error as PlaywrightError

    from auto_app.collect.guard import HardBlock
    from auto_app.collect.runner import SourceCollector
    from auto_app.core.models import utcnow

    store, make = env
    store.write_batch("20000101-000000", {"batch_id": "20000101-000000", "started_at": utcnow()})
    previous_run = store.start_search_run("20000101-000000", {"source": "fake", "search_name": "ml", "started_at": utcnow()})
    store.add_sighting("20000101-000000", {
        "search_run_id": previous_run, "is_promoted": False,
        "source": "fake", "source_job_id": "101", "url": "http://fake.test/job/101",
        "outcome": "fetch_failed", "outcome_detail": "HTTP 503", "seen_at": utcnow().isoformat(),
    }, None)

    async def fail(self, card, run_id):
        if failure == "cancel":
            raise asyncio.CancelledError()
        if failure == "block":
            raise HardBlock("HTTP 429")
        raise PlaywrightError("Execution context was destroyed")

    monkeypatch.setattr(SourceCollector, "_fetch_card", fail)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await make().run(["fake"])
        batch = store.get_batch()
    else:
        batch = await make().run(["fake"])
    [run] = store.get_search_runs(batch["batch_id"])
    assert run["search_name"] == "__retry__"
    assert run["status"] == "finished" and run["finished_at"]
    assert run["stop_reason"] == stop


async def test_retry_pass_refetches_transient_failed_cards(env):
    from auto_app.core.models import utcnow

    store, make = env
    store.write_batch("20000101-000000", {"batch_id": "20000101-000000", "started_at": utcnow()})
    previous_run = store.start_search_run("20000101-000000", {"source": "fake", "search_name": "ml", "started_at": utcnow()})
    store.add_sighting("20000101-000000", {
        "search_run_id": previous_run, "is_promoted": False,
        "source": "fake", "source_job_id": "101", "url": "http://fake.test/job/101",
        "outcome": "fetch_failed", "outcome_detail": "HTTP 503", "seen_at": utcnow().isoformat(),
    }, None)
    batch = await make().run(["fake"])
    rows = [r for r in sightings(store, batch["batch_id"]) if r["search_name"] == "__retry__"]
    assert [(r["source_job_id"], r["outcome"]) for r in rows] == [("101", "fetched")]


async def test_daily_ceiling_skips_are_recorded(env, site):
    store, make = env
    make.cfg.sources["fake"].limits = SourceLimits(max_details_per_day=1)
    batch = await make().run(["fake"])
    outcomes = [(r["source_job_id"], r["outcome"]) for r in sightings(store, batch["batch_id"])]
    assert outcomes == [("101", "fetched"), ("102", "filtered"), ("103", "budget_skipped")]
    assert batch["sources"]["fake"]["stop_reasons"] == {"budget": 1}


async def test_cards_only_fetches_nothing(env, site):
    store, make = env
    batch = await make(cards_only=True).run(["fake"])
    assert batch["sources"]["fake"]["outcomes"] == {"cards_only": 5, "filtered": 1}
    assert list(store.iter_postings()) == []


async def test_captcha_stops_source_and_sets_cooldown(env, site):
    store, make = env
    site.captcha = True
    batch = await make().run(["fake"])
    assert batch["sources"]["fake"]["status"] == "blocked"
    health = store.source_health("fake")
    assert health["status"] == "cooldown" and "captcha" in health["last_block_reason"]
    assert list(store.failure_dir(batch["batch_id"], "fake").glob("*-blocked-results.png"))

    site.captcha = False
    again = await make().run(["fake"])
    assert again["sources"]["fake"]["status"] == "skipped_cooldown"
    forced = await make(force=True).run(["fake"])
    assert forced["sources"]["fake"]["status"] == "ok"


async def test_security_challenge_interstitial_is_a_block(env, site):
    store, make = env
    site.challenge = True
    batch = await make().run(["fake"])
    assert batch["sources"]["fake"]["status"] == "blocked"
    assert store.source_health("fake")["status"] == "cooldown"


async def test_manual_check_waits_for_user_then_continues(env, site):
    store, make = env
    site.challenge_once = True
    batch = await make(manual_checks=True).run(["fake"])
    src = batch["sources"]["fake"]
    assert src["status"] == "ok"
    assert src["manual_checks"] == 1
    assert src["outcomes"]["fetched"] == 4


async def test_manual_check_not_completed_times_out_and_blocks(env, site):
    store, make = env
    make.cfg.sources["fake"].limits = SourceLimits(manual_check_timeout_s=1)
    site.challenge = True
    batch = await make(manual_checks=True).run(["fake"])
    assert batch["sources"]["fake"]["status"] == "blocked"
    assert "not completed" in store.source_health("fake")["last_block_reason"]


async def test_logged_out_is_needs_login(env, site):
    store, make = env
    site.logged_in = False
    batch = await make().run(["fake"])
    assert batch["sources"]["fake"]["status"] == "needs_login"
    assert store.source_health("fake")["status"] == "needs_login"


async def test_source_without_a_search_is_reported(env, site):
    store, make = env
    with pytest.raises(ConfigError, match="'linkedin' has no searches"):  # asked for, but nothing to run
        await make().run(["fake", "linkedin"])


async def test_hourly_limit_pauses_instead_of_skipping(env, site):
    store, make = env
    make.cfg.sources["fake"].limits = SourceLimits(max_details_per_hour=2)
    batch = await make().run(["fake"])
    src = batch["sources"]["fake"]
    assert src["outcomes"] == {"fetched": 4, "filtered": 1, "fetch_failed": 1}  # nothing skipped
    assert src["rate_pauses"] >= 2


async def test_retry_pass_uses_at_most_half_the_hourly_limit(env, site):
    store, make = env
    make.cfg.sources["fake"].limits = SourceLimits(max_details_per_day=1)
    await make().run(["fake"])  # leaves 103 as budget_skipped (+ nothing else fetched)
    make.cfg.sources["fake"].limits = SourceLimits(max_details_per_hour=2, max_details_per_day=100)
    batch = await make().run(["fake"])
    rows = [r for r in sightings(store, batch["batch_id"]) if r["search_name"] == "__retry__"]
    assert len(rows) == 1  # cap = 2 * 0.5
    assert batch["sources"]["fake"]["outcomes"].get("fetched", 0) >= 1  # the search still ran


async def test_stopping_during_a_pause_keeps_progress_and_records_it(env, site, monkeypatch):
    import asyncio

    from auto_app.collect.pacer import RateLimiter

    store, make = env
    make.cfg.sources["fake"].limits = SourceLimits(max_details_per_hour=2)
    paused = asyncio.Event()
    real_acquire = RateLimiter.acquire

    async def acquire(self):
        if len(self._in_last_hour()) >= self.per_hour:
            paused.set()
            await asyncio.Event().wait()  # a pause that lasts until the run is stopped
        await real_acquire(self)

    monkeypatch.setattr(RateLimiter, "acquire", acquire)
    task = asyncio.create_task(make().run(["fake"]))
    await asyncio.wait_for(paused.wait(), timeout=30)
    task.cancel()  # what Ctrl+C does
    with pytest.raises(asyncio.CancelledError):
        await task

    batch = store.get_batch()
    assert batch["interrupted"] is True and batch["finished_at"]
    assert batch["sources"]["fake"]["status"] == "interrupted"
    assert batch["sources"]["fake"]["details_fetched"] == 2
    assert store.get_search_runs(batch["batch_id"])[-1]["stop_reason"] == "interrupted"
    assert store.posting_exists("fake", "101")  # saved jobs are kept

    # the next run skips what was saved and continues
    site.requests.clear()
    monkeypatch.setattr(RateLimiter, "acquire", real_acquire)
    again = await make().run(["fake"])
    assert again["sources"]["fake"]["outcomes"]["known"] >= 1


async def test_plugin_can_end_the_results_early(env, site, monkeypatch):
    """A plugin's end_of_results() (e.g. LinkedIn's "may not be exact matches" note) stops paging."""
    store, make = env

    async def ended(self, page):
        return True

    monkeypatch.setattr(FakeAdapter, "end_of_results", ended)
    batch = await make().run(["fake"])
    [run] = store.get_search_runs(batch["batch_id"])
    assert run["stop_reason"] == "done" and run["pages_loaded"] == 1  # page 2 (104, 105 …) is never opened
    assert {r["source_job_id"] for r in store.get_sightings(batch["batch_id"])} == {"101", "102", "103"}


async def test_round_goal_on_a_browser_source(env, site):
    from auto_app.collect.goal import RoundGoal

    store, make = env
    batch = await make(goal=RoundGoal(max_new_postings=2)).run(["fake"])
    src = batch["sources"]["fake"]
    assert src["outcomes"].get("fetched") == 2  # stopped at the cap …
    assert src["stop_reasons"] == {"goal": 1} and src["caught_up"] is False and src["goal_hit"] == "goal"

    batch = await make(goal=RoundGoal(time_limit_s=600), retry=False).run(["fake"])
    src = batch["sources"]["fake"]  # … the next round finds the rest and catches up (known streak / end)
    assert src["caught_up"] is True and src["goal_hit"] is None
    assert src["outcomes"].get("fetched", 0) >= 1

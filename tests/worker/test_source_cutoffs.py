"""A feed added later is baselined like the first run: its existing jobs are browsed, not pushed."""

from datetime import timedelta

import pytest

from auto_app.core.models import utcnow
from auto_app.worker.digest import pending, register_sources
from auto_app.worker.round import WorkerOptions, run_round
from tests.imported_jobs import ingest, make_store, record
from tests.worker.test_digest import FakeDiscord

BOARD = "board-greenhouse-acme"


@pytest.fixture
def store(tmp_path):
    result = make_store(tmp_path)
    yield result
    result.engine.dispose()


def described(external_id, title):
    return record(external_id, title, description=f"{title}: build services in Python.")


def test_sources_present_at_the_first_run_follow_notify_since_and_later_ones_their_own_cutoff(store):
    ingest(store, [described("101", "Backend Engineer")])
    since = utcnow()
    state = {"notify_since": since.isoformat()}
    assert register_sources(store, state, since) == ["simplify-newgrad"]
    assert state["source_since"] == {"simplify-newgrad": None}  # None = use notify_since
    ingest(store, [described("201", "Data Engineer")], source=BOARD)
    later = utcnow()
    assert register_sources(store, state, later) == [BOARD]
    assert state["source_since"][BOARD] == later.isoformat()
    assert register_sources(store, state, later + timedelta(hours=1)) == []  # registered once


def test_unregistered_sources_wait_and_registered_cutoffs_gate_their_jobs(store):
    from auto_app.screen.rules import ScreeningRules
    from auto_app.screen.store import screen_jobs

    ingest(store, [described("201", "Data Engineer")], source=BOARD)
    screen_jobs(store, ScreeningRules())
    early = utcnow() - timedelta(days=1)
    assert pending(store, 10, since=early, source_since={})[0] == []  # the worker hasn't seen this feed yet
    cutoff = utcnow()
    assert pending(store, 10, since=early, source_since={BOARD: cutoff.isoformat()})[0] == []
    titles = [j["title"] for j in pending(store, 10, since=early, source_since={BOARD: early.isoformat()})[0]]
    assert titles == ["Data Engineer"]
    # Without a registry (direct callers, a worker that predates it) only notify_since applies.
    assert [j["title"] for j in pending(store, 10, since=early)[0]] == ["Data Engineer"]


def test_a_new_board_pushes_only_jobs_that_appear_after_it_was_added(store, tmp_path):
    opts = WorkerOptions(screening_path=tmp_path / "screening.json", steps=("screen", "notify"))
    ingest(store, [described("101", "Backend Engineer")])
    state = {}
    run_round(store, opts, fetcher=None, discord=FakeDiscord(), state=state, now=utcnow())
    ingest(store, [described("201", "Data Engineer")], source=BOARD)  # the board's existing backlog
    discord = FakeDiscord()
    run_round(store, opts, fetcher=None, discord=discord, state=state, now=utcnow())
    assert discord.posts == []
    ingest(store, [described("201", "Data Engineer"), described("202", "ML Engineer")], source=BOARD)
    run_round(store, opts, fetcher=None, discord=discord, state=state, now=utcnow())
    assert [embed["title"].split(" — ")[0].split(" ", 1)[1] for _, embeds in discord.posts for embed in embeds] == [
        "ML Engineer"]

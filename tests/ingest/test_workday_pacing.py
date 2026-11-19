"""Workday sites share one service and rate-limit a burst (HTTP 429 on 2026-11-18 for all five boards, every round):
one Workday site per round, the most overdue first, at most once per two hours each, and a cool-down after a 429
that holds every Workday site (tests/ingest/test_workday_guard.py)."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import yaml

from auto_app.ingest import repository_workday as wd
from auto_app.ingest.runner import run_config
from tests.imported_jobs import make_store

FIXTURES = Path(__file__).parents[1] / "fixtures" / "workday"
T0 = datetime(2026, 11, 19, 8, 0, tzinfo=timezone.utc)
SITES = {"board-workday-gm": "https://generalmotors.wd5.myworkdayjobs.com/Careers_GM",
         "board-workday-intel": "https://intel.wd1.myworkdayjobs.com/External",
         "board-workday-cisco": "https://cisco.wd5.myworkdayjobs.com/Cisco_Careers"}


@pytest.fixture(autouse=True)
def no_pauses(monkeypatch):
    monkeypatch.setattr(wd, "_sleep", lambda seconds: None)


def page(offset):
    name = {0: "gm_firmware_us_p0", 20: "gm_firmware_us_p1"}.get(offset)
    return json.loads((FIXTURES / f"{name}.json").read_text()) if name else {"total": 0, "jobPostings": []}


class Sites:
    """Every tenant answers with the recorded GM pages, except those told to answer 429."""

    def __init__(self, limited=(), retry_after=None):
        self.limited, self.retry_after, self.hosts = set(limited), retry_after, []

    def __call__(self, request):
        host = request.url.host
        self.hosts.append(host)
        if host.split(".")[0] in self.limited:
            headers = {"Retry-After": str(self.retry_after)} if self.retry_after else {}
            return httpx.Response(429, headers=headers)
        return httpx.Response(200, json=page(json.loads(request.content)["offset"]))


def config(tmp_path, *, with_json_feed=False):
    sources = {name: {"kind": "repository", "format": "workday-board", "company": name.split("-")[-1],
                      "location": url} for name, url in SITES.items()}
    if with_json_feed:
        (tmp_path / "feed.json").write_text(json.dumps([]))
        sources["plain"] = {"kind": "repository", "location": "feed.json"}
    path = tmp_path / "feeds.yaml"
    path.write_text(yaml.safe_dump({"sources": sources}))
    return path


def run(store, path, handler, now):
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return run_config(store, path, client=client, now=now)


def read_this_round(report):
    return sorted(name for name, r in report["sources"].items()
                  if name.startswith("board-workday") and r["status"] != "deferred")


def test_one_workday_site_per_round_most_overdue_first(tmp_path):
    store = make_store(tmp_path)
    path = config(tmp_path, with_json_feed=True)
    try:
        seen = []
        for step in range(3):
            report = run(store, path, Sites(), T0 + timedelta(minutes=30 * step))
            [name] = read_this_round(report)
            seen.append(name)
            assert report["sources"]["plain"]["status"] == "ok"  # other feeds run every round
            assert report["status"] == "ok"  # waiting a turn is not a failure
        assert sorted(seen) == sorted(SITES)  # each site once, never the same twice in a row
        deferred = report["sources"][next(n for n in SITES if n != seen[-1])]
        assert deferred["status"] == "deferred" and "turn" in deferred["reason"]
        assert read_this_round(run(store, path, Sites(), T0 + timedelta(minutes=90))) == []  # none due: all < 2 h
        assert read_this_round(run(store, path, Sites(), T0 + timedelta(minutes=125))) == [seen[0]]
    finally:
        store.engine.dispose()


def test_a_429_cools_the_site_down_by_retry_after_or_a_growing_backoff(tmp_path):
    store = make_store(tmp_path)
    path = config(tmp_path)
    try:
        first = run(store, path, Sites(limited={"generalmotors", "intel", "cisco"}, retry_after=7200), T0)
        [name] = read_this_round(first)
        assert first["sources"][name]["status"] == "rate_limited" and first["status"] == "partial"
        health = store.source_health(name)["ingest_run"]
        assert health["retry_after"] == (T0 + timedelta(hours=2)).isoformat() and health["rate_limited_count"] == 1
        # Workday blocks the address, so every Workday site waits out the two hours (workday_guard) ...
        later = [read_this_round(run(store, path, Sites(), T0 + timedelta(minutes=30 * k))) for k in (1, 2, 3)]
        assert later == [[], [], []]
        # ... then the two never read go first, and the limited site after them.
        after = [read_this_round(run(store, path, Sites(), T0 + timedelta(minutes=125 + 30 * k))) for k in (0, 1)]
        assert sorted(n for turn in after for n in turn) == sorted(set(SITES) - {name})
        assert read_this_round(run(store, path, Sites(), T0 + timedelta(minutes=185))) == [name]
        assert store.source_health(name)["ingest_run"]["rate_limited_count"] == 0  # a good read clears it
        assert "retry_after" not in store.source_health(name)["ingest_run"]
    finally:
        store.engine.dispose()


def test_without_retry_after_the_cool_down_doubles(tmp_path):
    store = make_store(tmp_path)
    path = config(tmp_path)
    try:
        limited = Sites(limited={"generalmotors", "intel", "cisco"})
        run(store, path, limited, T0)
        [name] = [n for n in SITES if store.source_health(n).get("ingest_run", {}).get("rate_limited_count")]
        assert store.source_health(name)["ingest_run"]["retry_after"] == (T0 + timedelta(hours=1)).isoformat()
        moment = T0
        while True:  # step until this site gets its next turn, and is limited again
            moment += timedelta(minutes=30)
            if name in read_this_round(run(store, path, limited, moment)):
                break
        health = store.source_health(name)["ingest_run"]
        assert health["rate_limited_count"] == 2
        assert health["retry_after"] == (moment + timedelta(hours=2)).isoformat()
    finally:
        store.engine.dispose()

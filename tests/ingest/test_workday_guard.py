"""The feed worker shares JD enrichment's Workday cool-down: it waits it out, and a 403 or 429 starts one."""

from datetime import timedelta

import httpx

from auto_app.platforms.workday_guard import WorkdayGuard
from tests.imported_jobs import make_store
from tests.ingest.test_workday_pacing import (  # noqa: F401
    SITES,
    T0,
    Sites,
    config,
    no_pauses,
    read_this_round,
    run,
)


class Blocked(Sites):
    def __init__(self, status):
        super().__init__()
        self.status = status

    def __call__(self, request):
        self.hosts.append(request.url.host)
        return httpx.Response(self.status)


def test_workday_feeds_wait_while_enrichment_cooled_workday_down(tmp_path):
    store = make_store(tmp_path)
    try:
        WorkdayGuard.beside(store.db_path).strike(T0, status=403, by="enrich")
        sites = Sites()
        report = run(store, config(tmp_path, with_json_feed=True), sites, T0 + timedelta(minutes=30))
        assert read_this_round(report) == [] and sites.hosts == []
        assert report["sources"]["plain"]["status"] == "ok" and report["status"] == "ok"
        waiting = report["sources"]["board-workday-gm"]
        assert waiting["status"] == "deferred" and "cool" in waiting["reason"] and "403" in waiting["reason"]
        assert len(read_this_round(run(store, config(tmp_path), Sites(), T0 + timedelta(minutes=61)))) == 1
        assert WorkdayGuard.beside(store.db_path).state() == {}  # a good read cleared it
    finally:
        store.engine.dispose()


def test_a_workday_403_is_a_rate_limit_and_cools_enrichment_too(tmp_path):
    store = make_store(tmp_path)
    try:
        report = run(store, config(tmp_path), Blocked(403), T0)
        [name] = read_this_round(report)
        assert report["sources"][name]["status"] == "rate_limited"
        assert store.source_health(name)["ingest_run"]["rate_limited_count"] == 1
        guard = WorkdayGuard.beside(store.db_path)
        assert guard.cooling_until(T0) == T0 + timedelta(hours=1) and guard.state()["by"] == "ingest"
        assert read_this_round(run(store, config(tmp_path), Sites(), T0 + timedelta(minutes=30))) == []
    finally:
        store.engine.dispose()


def test_a_403_from_another_kind_of_feed_is_still_an_error(tmp_path):
    import yaml

    from auto_app.ingest.runner import run_config

    path = tmp_path / "feeds.yaml"
    path.write_text(yaml.safe_dump({"sources": {"board-greenhouse-acme": {
        "kind": "repository", "format": "greenhouse-board", "company": "Acme",
        "location": "https://boards-api.greenhouse.io/v1/boards/acme/jobs"}}}))
    store = make_store(tmp_path)
    try:
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(403))) as client:
            report = run_config(store, path, client=client, now=T0)
        assert report["sources"]["board-greenhouse-acme"]["status"] == "error"
        assert WorkdayGuard.beside(store.db_path).state() == {}
    finally:
        store.engine.dispose()

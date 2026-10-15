"""Imported descriptions are cached, merged conservatively and projected into jobs."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import insert, select, update

from auto_app.core.db import tables as T
from auto_app.enrich.fetch import FetchResult
from auto_app.enrich.service import enrich_jobs
from auto_app.process.normalize import pending_enrichment
from auto_app.screen.rules import ScreeningRules
from auto_app.screen.store import excluded_job_ids, load_facts, screen_jobs
from tests.enrich.test_fetch import ASHBY, OTHER, UUID, fetcher as real_fetcher
from tests.imported_jobs import FakeFetcher, ingest, job_id_of, make_store, ok, record

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
API = "https://boards-api.greenhouse.io/v1/boards/acme/jobs/101"


@pytest.fixture
def store(tmp_path):
    result = make_store(tmp_path)
    ingest(result, [record("101", "Software Engineer, New Grad")])
    yield result
    result.engine.dispose()


def cache_row(store):
    with store.engine.connect() as conn:
        return conn.execute(select(T.enrichments)).mappings().one()


def test_enrichment_fills_description_normalization_and_projection(store):
    job = job_id_of(store, "Software Engineer, New Grad")
    store.mark_job(job, "want_to_apply")
    store.update_job(job, step2="recommended")
    fetcher = FakeFetcher(ok("Build APIs in Python.", employment_type="full_time", req_id="REQ_123"))
    report = enrich_jobs(store, fetcher, limit=10, now=NOW)
    assert (report["ok"], report["jobs_enriched"], report["fetched"]) == (1, 1, 1)
    assert fetcher.calls == [API]
    parsed, _ = store.job_inputs(job)
    assert parsed["description_text"] == "Build APIs in Python." and not pending_enrichment(parsed)
    assert parsed["employment_type"] == "full_time"
    assert parsed["extras"]["enrichment"]["platform"] == "greenhouse"
    assert cache_row(store)["status"] == "ok"
    with store.engine.connect() as conn:
        posting = conn.execute(select(T.postings)).mappings().one()
        keys = set(conn.execute(select(T.job_keys.c.key_type, T.job_keys.c.key_value)).all())
        job_row = conn.execute(select(T.jobs)).mappings().one()
    assert posting["desc_hash"] == "cf4690b14c7d0257789638edc736db267aed5675"
    assert posting["normalized"]["employment_type"] == "full_time"
    assert ("req_id", "acme|REQ_123") in keys
    assert (job_row["step2"], job_row["decision"]) == ("recommended", "want_to_apply")


def test_lever_country_survives_enrichment_cache_and_preserves_import_provenance(store):
    url = f"https://jobs.lever.co/acme/{UUID}"
    ingest(store, [record("101", "Software Engineer, New Grad", url=url, locations=["Remote"])])
    before = store.job_inputs(store.jobs()[0]["job_id"])[0]["extras"]["ingestion"]
    payload = {"id": UUID, "text": "Backend Engineer", "country": "CA", "hostedUrl": url,
               "categories": {"location": "Remote", "commitment": "Full-time"},
               "descriptionPlain": "Build APIs with Python."}
    fetcher = real_fetcher(lambda request: httpx.Response(200, json=payload))
    assert enrich_jobs(store, fetcher, now=NOW)["ok"] == 1
    parsed = store.job_inputs(store.jobs()[0]["job_id"])[0]
    assert parsed["extras"].get("country") == "CA"
    assert parsed["extras"]["ingestion"] == {**before, "enrichment_status": "complete"}
    assert load_facts(store)[0].countries == ("CA",)
    assert screen_jobs(store, ScreeningRules())["excluded"] == 1

    # A feed update removes the merged JD; the successful cache must retain its country too.
    ingest(store, [record("101", "Software Engineer, New Grad", url=url, locations=["Remote", "US"])])
    screen_jobs(store, ScreeningRules())
    never = FakeFetcher(lambda target: pytest.fail("cached employer evidence must not refetch"))
    assert enrich_jobs(store, never, limit=0, now=NOW)["from_cache"] == 1
    assert load_facts(store)[0].countries == ("CA",)


def test_reimport_restores_description_without_a_request_even_when_limit_is_zero(store):
    job = job_id_of(store, "Software Engineer, New Grad")
    enrich_jobs(store, FakeFetcher(ok("Build APIs in Python.")), now=NOW)
    ingest(store, [record("101", "Software Engineer, New Grad", locations=["Austin, TX"])])
    assert pending_enrichment(store.job_inputs(job)[0])
    never = FakeFetcher(lambda target: pytest.fail("cache must not be fetched again"))
    report = enrich_jobs(store, never, limit=0, now=NOW)
    assert report["from_cache"] == 1 and report["fetched"] == 0
    parsed, locations = store.job_inputs(job)
    assert parsed["description_text"] == "Build APIs in Python." and locations == ["austin, tx"]


def test_gone_job_is_screened_out_and_not_requested_again(store):
    job = job_id_of(store, "Software Engineer, New Grad")
    fetcher = FakeFetcher(FetchResult("gone", error="HTTP 404"))
    assert enrich_jobs(store, fetcher, now=NOW)["gone"] == 1
    screen_jobs(store, ScreeningRules())
    assert job in excluded_job_ids(store)
    enrich_jobs(store, fetcher, now=NOW + timedelta(days=2))
    assert len(fetcher.calls) == 1


def test_failures_back_off_exponentially_and_cap_extreme_attempt_counts(store):
    fetcher = FakeFetcher(FetchResult("failed", error="HTTPStatusError: HTTP 500"))
    enrich_jobs(store, fetcher, now=NOW)
    soon = enrich_jobs(store, fetcher, now=NOW + timedelta(minutes=5))
    assert soon["waiting"] == 1 and len(fetcher.calls) == 1
    later = enrich_jobs(store, fetcher, now=NOW + timedelta(minutes=31))
    assert later["failed"] == 1 and len(fetcher.calls) == 2
    row = cache_row(store)
    assert row["attempts"] == 2
    assert row["next_attempt_at"].replace(tzinfo=timezone.utc) == NOW + timedelta(minutes=31, hours=1)
    with store.engine.begin() as conn:
        conn.execute(update(T.enrichments).values(attempts=100000, next_attempt_at=NOW))
    enrich_jobs(store, fetcher, now=NOW)
    assert cache_row(store)["next_attempt_at"].replace(tzinfo=timezone.utc) == NOW + timedelta(days=1)


def test_unsupported_links_do_not_consume_the_request_limit(tmp_path):
    store = make_store(tmp_path)
    try:
        ingest(store, [record("201", "Backend Engineer", url="https://careers.acme.com/jobs/201"),
                       record("202", "Data Engineer"), record("203", "ML Engineer"), record("204", "Web Developer")])
        fetcher = FakeFetcher(ok("Ship features."))
        report = enrich_jobs(store, fetcher, limit=2, now=NOW)
        assert (report["unsupported"], report["fetched"], report["skipped_limit"]) == (1, 2, 1)
    finally:
        store.engine.dispose()


def test_excluded_and_archived_jobs_are_not_candidates(store):
    job = job_id_of(store, "Software Engineer, New Grad")
    with store.engine.begin() as conn:
        conn.execute(insert(T.job_screenings).values(job_id=job, rules_version="t", facts_hash="h",
                                                     outcome="excluded", reasons=["internship"], signals={}, screened_at=NOW))
    fetcher = FakeFetcher(ok("unused"))
    assert enrich_jobs(store, fetcher, now=NOW)["candidates"] == 0
    with store.engine.begin() as conn:
        conn.execute(T.job_screenings.delete())
        conn.execute(update(T.jobs).values(archived_at=NOW))
    assert enrich_jobs(store, fetcher, now=NOW)["candidates"] == 0 and fetcher.calls == []


def test_empty_descriptions_stop_retries(store):
    empty = FakeFetcher(ok(" \n "))
    enrich_jobs(store, empty, now=NOW)
    enrich_jobs(store, empty, now=NOW + timedelta(days=3))
    assert len(empty.calls) == 1 and cache_row(store)["status"] == "unsupported"


def test_enrichment_fills_only_missing_fields_and_keeps_import_provenance(store):
    job = job_id_of(store, "Software Engineer, New Grad")
    ingest(store, [record("101", "Software Engineer, New Grad", employment_type="contract")])
    fetcher = FakeFetcher(ok("<actual description>", title="Employer title", locations=["Toronto, Canada"],
                             employment_type="full_time", workplace_type="remote", salary={"min": 100000},
                             req_id="REQ1", description_html="<p>actual description</p>"))
    enrich_jobs(store, fetcher, now=NOW)
    parsed, locs = store.job_inputs(job)
    assert parsed["title"] == "Software Engineer, New Grad" and parsed["employment_type"] == "contract"
    assert parsed["locations"] == ["Boston, MA"] and locs == ["boston, ma"]
    assert parsed["workplace_type"] == "remote" and parsed["salary"]["min"] == 100000
    assert parsed["description_html"] == "<p>actual description</p>"
    with store.engine.connect() as conn:
        entry = conn.execute(select(T.ingest_entries)).mappings().one()
        assert entry["payload"]["description"] == ""


def test_placeholder_title_and_missing_locations_are_filled_and_projected(store):
    ingest(store, [record("101", None, locations=[])])
    enrich_jobs(store, FakeFetcher(ok("Build APIs.", title="Backend Engineer", locations=["Austin, TX"])), now=NOW)
    job = store.jobs()[0]
    parsed, locations = store.job_inputs(job["job_id"])
    assert job["title"] == "Backend Engineer" and locations == ["austin, tx"]
    assert parsed["extras"]["ingestion"]["title_observed"] is True


def test_same_url_across_imports_uses_one_request(store):
    ingest(store, [record("other", "Software Engineer", url="https://job-boards.greenhouse.io/acme/jobs/101")], source="repo-two")
    fetcher = FakeFetcher(ok("Build APIs."))
    report = enrich_jobs(store, fetcher, now=NOW)
    assert report["fetched"] == 1 and report["from_cache"] == 1 and report["jobs_enriched"] == 1
    with store.engine.connect() as conn:
        assert all(not pending_enrichment(p) for p in conn.execute(select(T.postings.c.parsed)).scalars())


def test_limit_counts_http_requests_not_ashby_jobs(tmp_path):
    store = make_store(tmp_path)
    try:
        ingest(store, [record("one", "Backend Engineer", url=f"https://jobs.ashbyhq.com/acme/{UUID}"),
                       record("two", "Data Engineer", url=f"https://jobs.ashbyhq.com/acme/{OTHER}")])
        fetcher = real_fetcher(lambda request: httpx.Response(200, json=ASHBY))
        report = enrich_jobs(store, fetcher, limit=1, now=NOW)
        assert report["fetched"] == fetcher.request_count == 1
        assert report["ok"] == report["jobs_enriched"] == 2 and report["skipped_limit"] == 0
    finally:
        store.engine.dispose()


def test_failed_ashby_board_is_not_retried_for_each_job_in_one_round(tmp_path):
    store = make_store(tmp_path)
    try:
        ingest(store, [record("one", "Backend Engineer", url=f"https://jobs.ashbyhq.com/acme/{UUID}"),
                       record("two", "Data Engineer", url=f"https://jobs.ashbyhq.com/acme/{OTHER}")])
        fetcher = real_fetcher(lambda request: httpx.Response(429))
        report = enrich_jobs(store, fetcher, now=NOW)
        assert report["fetched"] == fetcher.request_count == 1 and report["failed"] == 2
    finally:
        store.engine.dispose()


def test_round_never_exceeds_200_requests_even_with_a_higher_limit(tmp_path):
    store = make_store(tmp_path)
    try:
        ingest(store, [record(str(i), f"Software Engineer {i}") for i in range(1, 206)])
        fetcher = FakeFetcher(FetchResult("failed", error="timeout"))
        report = enrich_jobs(store, fetcher, limit=1000, now=NOW)
        assert report["fetched"] == len(fetcher.calls) == 200 and report["skipped_limit"] == 5
    finally:
        store.engine.dispose()


def test_newest_posting_is_fetched_first(tmp_path):
    store = make_store(tmp_path)
    try:
        ingest(store, [record("1", "Backend Engineer", posted_at=NOW),
                       record("2", "Data Engineer", posted_at=NOW - timedelta(days=1))])
        fetcher = FakeFetcher(ok("Build APIs."))
        enrich_jobs(store, fetcher, limit=1, now=NOW)
        assert fetcher.calls == ["https://boards-api.greenhouse.io/v1/boards/acme/jobs/1"]
    finally:
        store.engine.dispose()


def test_feed_changing_apply_url_during_fetch_cannot_receive_old_description(store):
    def changed(target):
        ingest(store, [record("101", "Software Engineer", url="https://job-boards.greenhouse.io/acme/jobs/999")])
        return ok("Stale job description.")
    report = enrich_jobs(store, FakeFetcher(changed), now=NOW)
    assert report["jobs_enriched"] == 0
    with store.engine.connect() as conn:
        parsed = conn.execute(select(T.postings.c.parsed)).scalar_one()
    assert parsed["description_text"] == "" and pending_enrichment(parsed)


def test_imported_description_arriving_during_fetch_is_never_overwritten(store):
    def changed(target):
        # Import has committed its parsed evidence, but not refreshed the projection yet.
        with store.engine.begin() as conn:
            parsed = conn.execute(select(T.postings.c.parsed)).scalar_one()
            parsed["description_text"] = "New source description."
            parsed["extras"]["ingestion"]["enrichment_status"] = "complete"
            conn.execute(update(T.postings).values(parsed=parsed, state="normalized"))
        return ok("Older employer description.")
    report = enrich_jobs(store, FakeFetcher(changed), now=NOW)
    assert report["jobs_enriched"] == 0
    with store.engine.connect() as conn:
        assert conn.execute(select(T.postings.c.parsed)).scalar_one()["description_text"] == "New source description."


def test_unknown_requisition_is_replaced_with_employer_evidence(store):
    with store.engine.begin() as conn:
        parsed = conn.execute(select(T.postings.c.parsed)).scalar_one()
        parsed["req_id"] = "unknown"
        conn.execute(update(T.postings).values(parsed=parsed))
    enrich_jobs(store, FakeFetcher(ok("Build APIs.", req_id="REQ1")), now=NOW)
    with store.engine.connect() as conn:
        assert conn.execute(select(T.postings.c.req_id)).scalar_one() == "REQ1"


def test_projection_failure_can_be_recovered_from_cache(store, monkeypatch):
    import auto_app.enrich.service as module
    original = module.refresh_job_projection
    def failure(*args, **kwargs):
        raise RuntimeError("projection interrupted")
    monkeypatch.setattr(module, "refresh_job_projection", failure)
    with pytest.raises(RuntimeError, match="interrupted"):
        enrich_jobs(store, FakeFetcher(ok("Build APIs.", req_id="REQ1")), now=NOW)
    monkeypatch.setattr(module, "refresh_job_projection", original)
    never = FakeFetcher(lambda target: pytest.fail("projection recovery must use cached description"))
    report = enrich_jobs(store, never, now=NOW)
    assert report["jobs_enriched"] == 1 and report["from_cache"] == 1
    with store.engine.connect() as conn:
        assert conn.execute(select(T.postings.c.state)).scalar_one() == "linked"


def test_next_round_recovers_projection_even_after_enriched_job_is_screened_out(store, tmp_path, monkeypatch):
    import auto_app.enrich.service as module
    from auto_app.worker.round import WorkerOptions, run_round

    job = job_id_of(store, "Software Engineer, New Grad")
    ingest(store, [record("101", "Software Engineer, New Grad"), record("102", "Software Engineer Intern")])
    options = WorkerOptions(screening_path=tmp_path / "screening.json", steps=("screen", "enrich"))
    original = module.refresh_job_projection
    def failure(*args, **kwargs):
        raise RuntimeError("projection interrupted")
    monkeypatch.setattr(module, "refresh_job_projection", failure)
    fetcher = FakeFetcher(ok("Required: 5 years of experience.", req_id="REQ1"))
    first = run_round(store, options, fetcher=fetcher, discord=None, state={}, now=NOW)
    assert first["status"] == "partial" and first["steps"]["enrich"]["status"] == "error"
    assert job in excluded_job_ids(store) and fetcher.calls == [API]
    with store.engine.connect() as conn:
        assert conn.execute(select(T.postings.c.state).where(T.postings.c.job_id == job)).scalar_one() == "normalized"
        assert conn.execute(select(T.job_keys).where(T.job_keys.c.key_type == "req_id")).first() is None

    monkeypatch.setattr(module, "refresh_job_projection", original)
    never = FakeFetcher(lambda target: pytest.fail("excluded jobs must never issue a fresh request"))
    second = run_round(store, options, fetcher=never, discord=None, state={}, now=NOW + timedelta(minutes=15))
    assert second["status"] == "ok" and second["steps"]["enrich"]["jobs_enriched"] == 1
    assert second["steps"]["enrich"]["from_cache"] == 1 and second["steps"]["enrich"]["fetched"] == 0
    assert job in excluded_job_ids(store)
    with store.engine.connect() as conn:
        assert conn.execute(select(T.postings.c.state).where(T.postings.c.job_id == job)).scalar_one() == "linked"
        assert conn.execute(select(T.job_keys.c.key_value).where(T.job_keys.c.key_type == "req_id")).scalar_one() == "acme|REQ1"

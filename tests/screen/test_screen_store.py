"""Screening verdicts are stored per active job and recomputed only when facts or rules change."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import insert, select, update

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow
from auto_app.screen.rules import ScreeningRules
from auto_app.screen.store import chunks, excluded_job_ids, load_facts, screen_jobs
from tests.imported_jobs import ingest, job_id_of, make_store, record


@pytest.fixture
def store(tmp_path):
    result = make_store(tmp_path)
    ingest(result, [record("101", "Software Engineer, New Grad", sponsorship="Offers Sponsorship"),
                    record("102", "Software Engineer Intern"),
                    record("103", "Software Engineer", sponsorship="U.S. Citizenship is Required")])
    yield result
    result.engine.dispose()


def verdicts(store):
    with store.engine.connect() as conn:
        return {r.job_id: (r.outcome, tuple(r.reasons), r.signals)
                for r in conn.execute(select(T.job_screenings))}


def test_verdicts_reasons_and_signals_are_saved(store):
    report = screen_jobs(store, ScreeningRules())
    assert (report["screened"], report["kept"], report["excluded"]) == (3, 1, 2)
    assert report["reasons"] == {"internship": 1, "citizenship_required": 1}
    saved = verdicts(store)
    ng, intern, citizen = (job_id_of(store, t) for t in
                           ("Software Engineer, New Grad", "Software Engineer Intern", "Software Engineer"))
    assert saved[ng][0] == "kept" and saved[ng][2]["sponsorship"] == "offers"
    assert saved[intern][:2] == ("excluded", ("internship",))
    assert saved[citizen][:2] == ("excluded", ("citizenship_required",))
    assert excluded_job_ids(store) == {intern, citizen}


def test_unchanged_jobs_are_not_rescreened_until_the_rules_change(store):
    screen_jobs(store, ScreeningRules())
    again = screen_jobs(store, ScreeningRules())
    assert (again["screened"], again["unchanged"]) == (0, 3)
    relaxed = screen_jobs(store, ScreeningRules(exclude_internships=False))
    assert relaxed["screened"] == 3
    assert verdicts(store)[job_id_of(store, "Software Engineer Intern")][0] == "kept"


def test_archived_jobs_lose_their_verdict(store):
    screen_jobs(store, ScreeningRules())
    intern = job_id_of(store, "Software Engineer Intern")
    with store.engine.begin() as conn:
        conn.execute(update(T.jobs).where(T.jobs.c.job_id == intern).values(archived_at=utcnow()))
    report = screen_jobs(store, ScreeningRules())
    assert report["removed"] == 1 and intern not in verdicts(store)


def test_facts_come_from_postings_upstream_rows_and_enrichment_results(store):
    ng = job_id_of(store, "Software Engineer, New Grad")
    fact = next(f for f in load_facts(store) if f.job_id == ng)
    assert fact.titles == ("Software Engineer, New Grad",)
    assert fact.sources == ("simplify-newgrad",)
    assert fact.sponsorship_labels == ("Offers Sponsorship",) and fact.categories == ("Software",)
    assert fact.closed_at_source is False
    with store.engine.begin() as conn:
        apply_url = conn.execute(select(T.postings.c.apply_url).where(T.postings.c.job_id == ng)).scalar()
        conn.execute(insert(T.enrichments).values(url=apply_url, platform="greenhouse", status="gone",
                                                  attempts=0, updated_at=utcnow()))
    screen_jobs(store, ScreeningRules())
    assert verdicts(store)[ng][:2] == ("excluded", ("closed_at_source",))


def test_changed_description_rescreens_only_that_job(store):
    earlier = datetime(2026, 1, 1, tzinfo=timezone.utc)
    later = datetime(2026, 1, 2, tzinfo=timezone.utc)
    screen_jobs(store, ScreeningRules(), now=earlier)
    intern = job_id_of(store, "Software Engineer Intern")
    with store.engine.begin() as conn:
        posting = conn.execute(select(T.postings).where(T.postings.c.job_id == intern)).mappings().one()
        parsed = dict(posting["parsed"], description_text="We offer visa sponsorship.")
        conn.execute(update(T.postings).where(T.postings.c.posting_id == posting["posting_id"]).values(parsed=parsed))
    report = screen_jobs(store, ScreeningRules(), now=later)
    assert (report["screened"], report["unchanged"]) == (1, 2)
    assert verdicts(store)[intern][2]["sponsorship"] == "offers"
    with store.engine.connect() as conn:
        times = dict(conn.execute(select(T.job_screenings.c.job_id, T.job_screenings.c.screened_at)).all())
    assert times[intern].day == 2
    assert all(value.day == 1 for job_id, value in times.items() if job_id != intern)


def test_screening_does_not_change_manual_or_filter_state(store):
    intern = job_id_of(store, "Software Engineer Intern")
    with store.engine.begin() as conn:
        conn.execute(update(T.jobs).where(T.jobs.c.job_id == intern).values(
            step2="recommended", decision="want_to_apply", notified_at=utcnow()))
        before = conn.execute(select(T.jobs).order_by(T.jobs.c.job_id)).mappings().all()
    screen_jobs(store, ScreeningRules())
    assert intern in excluded_job_ids(store)
    screen_jobs(store, ScreeningRules(exclude_internships=False))
    assert intern not in excluded_job_ids(store)
    with store.engine.connect() as conn:
        after = conn.execute(select(T.jobs).order_by(T.jobs.c.job_id)).mappings().all()
    assert before == after


def test_active_job_without_postings_is_kept_as_unknown(store):
    with store.engine.begin() as conn:
        job_id = conn.execute(insert(T.jobs).values(first_seen_at=utcnow())).inserted_primary_key[0]
    report = screen_jobs(store, ScreeningRules())
    assert report["jobs"] == 4
    assert verdicts(store)[job_id] == ("kept", (), {
        "role": "unknown", "sponsorship": "unknown", "years_required": None,
        "new_grad_title": False, "has_description": False, "location": "unknown",
    })


def test_all_apply_urls_must_be_gone_before_excluding_a_job(store):
    ng = job_id_of(store, "Software Engineer, New Grad")
    ingest(store, [record("104", "Another Software Role")], source="other")
    with store.engine.begin() as conn:
        another = conn.execute(select(T.postings.c.posting_id).where(T.postings.c.source == "other")).scalar_one()
        conn.execute(update(T.postings).where(T.postings.c.posting_id == another).values(job_id=ng))
        url = conn.execute(select(T.postings.c.apply_url).where(
            T.postings.c.job_id == ng, T.postings.c.posting_id != another)).scalar_one()
        conn.execute(insert(T.enrichments).values(url=url, status="gone", attempts=0, updated_at=utcnow()))
    assert next(f for f in load_facts(store) if f.job_id == ng).closed_at_source is False


def test_more_than_one_chunk_of_jobs_can_be_written_and_removed(tmp_path):
    store = make_store(tmp_path)
    try:
        with store.engine.begin() as conn:
            conn.execute(insert(T.jobs), [{"job_id": job_id} for job_id in range(1, 1101)])
        first = screen_jobs(store, ScreeningRules())
        assert (first["screened"], first["kept"]) == (1100, 1100)
        with store.engine.begin() as conn:
            conn.execute(update(T.jobs).values(archived_at=utcnow()))
        second = screen_jobs(store, ScreeningRules())
        assert (second["jobs"], second["removed"], second["kept"]) == (0, 1100, 0)
        assert verdicts(store) == {}
    finally:
        store.engine.dispose()


def test_chunks_accepts_iterators_and_rejects_nonpositive_sizes():
    assert list(chunks(iter([1, 2, 3, 4, 5]), size=2)) == [[1, 2], [3, 4], [5]]
    for size in (0, -1):
        with pytest.raises(ValueError):
            list(chunks([1], size=size))


def test_facts_carry_raw_locations_and_board_countries(tmp_path):
    lone = make_store(tmp_path / "lone")
    ingest(lone, [record("301", "Backend Engineer", locations=["London, UK", "Remote"],
                         metadata={"upstream": {"country": "GB"}})])
    fact = load_facts(lone)[0]
    assert fact.locations == ("London, UK", "Remote") and fact.countries == ("GB",)
    report = screen_jobs(lone, ScreeningRules())
    assert report["reasons"] == {"outside_us": 1}
    lone.engine.dispose()

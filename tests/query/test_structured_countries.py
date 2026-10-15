"""Structured country evidence must filter every view without losing its audit record."""

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from auto_app.core.config import JobTTL
from auto_app.core.db import tables as T
from auto_app.core.db.job_ttl import expire_jobs
from auto_app.core.models import utcnow
from auto_app.query.service import QueryService
from tests.imported_jobs import ingest, make_store, record


@pytest.mark.parametrize("evidence", ["posting", "upstream"])
@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize("primary_country,excluded", [(None, True), ("US", False)])
def test_structured_canada_on_secondary_source_excludes_a_job_without_a_us_option(
        tmp_path, evidence, archived, primary_country, excluded):
    """Canada evidence from a second source hides the job, unless another source places it in the US."""
    store = make_store(tmp_path)
    try:
        store.paths.preferences.parent.mkdir(exist_ok=True)
        store.paths.preferences.write_text('{"exclude_countries": ["Canada"]}')
        ingest(store, [record("1", "Backend Engineer", locations=["Remote"], description="Build APIs.")])
        # Identical employer URL links a second source into the same job.
        ingest(store, [record("2", "Backend Engineer", url=record("1", "Backend Engineer").url,
                              locations=["Remote"], description="Build APIs.",
                              metadata={"upstream": {"country": "CA"}} if evidence == "upstream" else {})],
               source="second-source")
        job_id = store.jobs()[0]["job_id"]
        with store.engine.begin() as conn:
            postings = conn.execute(select(T.postings.c.posting_id, T.postings.c.parsed)
                                    .order_by(T.postings.c.posting_id)).all()
            primary, secondary = postings
            for posting, country in ((primary, primary_country),
                                     (secondary, "CA" if evidence == "posting" else None)):
                parsed = posting.parsed
                parsed["extras"]["country"] = country
                conn.execute(update(T.postings).where(T.postings.c.posting_id == posting.posting_id)
                             .values(parsed=parsed))
            conn.execute(update(T.jobs).where(T.jobs.c.job_id == job_id)
                         .values(primary_posting_id=primary.posting_id))
        if archived:
            report = expire_jobs(store, JobTTL(), now=utcnow() + timedelta(days=60))
            assert [item.job_id for item in report.archived] == [job_id]
        visible = [] if excluded else [job_id]
        assert [job["job_id"] for job in store.jobs(archived=archived, include_excluded=False)] == visible
        assert [job["job_id"] for job in store.jobs(archived=archived)] == [job_id]
        with QueryService(store.db_path) as query:
            assert query.jobs(archived=archived)["total"] == len(visible)
            assert query.jobs(archived=archived, include_excluded=True)["total"] == 1
            detail = query.job(job_id)
            assert detail["screening"]["excluded"] is excluded
            if excluded:
                assert detail["screening"]["matched_countries"] == ["CA"]
                assert detail["screening"]["reasons"] == ["excluded_country:Canada"]
            if not archived:
                assert query.context()["preferences"]["excluded_active_jobs"] == int(excluded)
    finally:
        store.engine.dispose()

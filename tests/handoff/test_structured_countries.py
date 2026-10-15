"""Explicit country preferences also guard direct handoff claims."""

import pytest
from sqlalchemy import select, update

from auto_app.core.db import tables as T
from auto_app.handoff.service import HandoffError
from tests.handoff.test_service import NOW, env, service
from tests.imported_jobs import make_store


@pytest.mark.parametrize("evidence", ["posting", "upstream"])
@pytest.mark.parametrize("us_location", [False, True])
def test_structured_canada_blocks_queue_and_claim_unless_a_us_location_remains(env, evidence, us_location):
    home, ids = env
    job_id = ids["Backend Engineer"]
    (home / "config").mkdir(exist_ok=True)
    (home / "config" / "preferences.json").write_text('{"exclude_countries": ["Canada"]}')
    store = make_store(home)
    try:
        with store.engine.begin() as conn:
            posting = conn.execute(select(T.postings.c.posting_id, T.postings.c.parsed)
                                   .where(T.postings.c.job_id == job_id)).one()
            if not us_location:
                posting.parsed["locations"] = ["Remote"]
                conn.execute(update(T.job_locations).where(T.job_locations.c.job_id == job_id)
                             .values(location_norm="remote"))
            if evidence == "posting":
                posting.parsed["extras"]["country"] = "CA"
                conn.execute(update(T.postings).where(T.postings.c.posting_id == posting.posting_id)
                             .values(parsed=posting.parsed))
            else:
                conn.execute(update(T.postings).where(T.postings.c.posting_id == posting.posting_id)
                             .values(parsed=posting.parsed))
                payload = conn.execute(select(T.ingest_entries.c.payload)
                                       .where(T.ingest_entries.c.posting_id == posting.posting_id)).scalar_one()
                payload["metadata"]["upstream"]["country"] = "CA"
                conn.execute(update(T.ingest_entries).where(T.ingest_entries.c.posting_id == posting.posting_id)
                             .values(payload=payload))
        handoff = service(home)
        queue = handoff.queue(now=NOW)
        if us_location:  # Boston, MA keeps a job that also carries Canadian evidence
            assert job_id in {job["job_id"] for job in queue["items"]}
            assert handoff.claim(job_id, "test-agent", now=NOW)["token"]
            return
        assert job_id not in {job["job_id"] for job in queue["items"]}
        assert queue["blocked"]["by_reason"]["excluded_country"] == 1
        with pytest.raises(HandoffError, match="not in the queue"):
            handoff.claim(job_id, "test-agent", now=NOW)
    finally:
        store.engine.dispose()

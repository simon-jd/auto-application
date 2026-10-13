"""Imported evidence must not erase identity, completeness or user decisions."""

from datetime import datetime, timezone

import pytest
from sqlalchemy import insert

from auto_app.collect.store import PostingRef
from auto_app.core.db import tables as T
from auto_app.core.db.store import DbStore
from auto_app.core.models import CommonPosting
from auto_app.process.dedup import dedup, load_items, match
from auto_app.process.link import link_jobs, plan_links
from auto_app.process.normalize import normalize_postings
from auto_app.select.engine import SelectOptions, plan
from auto_app.select.filters import Filter, FilterConfig
from auto_app.settings import Paths
from auto_app.sources.api import RawJob
from tests.helpers import test_registry
from tests.process.test_dedup import item


REG = test_registry()
DESCRIPTION = " ".join(f"build{i} distributed systems team{i % 9}" for i in range(100))


@pytest.fixture
def db(tmp_path):
    store = DbStore(tmp_path / "jobs.db", Paths(home=tmp_path))
    yield store
    store.engine.dispose()


def save(db, source, identity, *, url=None, title="Software Engineer", pending=False,
         description=DESCRIPTION, locations=("Seattle, WA",), req=None):
    url = url or f"https://careers.example/jobs/{identity}"
    ref = PostingRef(source, identity)
    posting = CommonPosting(
        source=source, source_job_id=identity, url=url, apply_url=url, title=title,
        company="NVIDIA", description_text=description, locations=list(locations), req_id=req,
        posted_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        extras={"ingestion": {"enrichment_status": "pending" if pending else "complete",
                              "title_observed": title != "[Title pending]"}},
    )
    db.save_api_posting(source, RawJob(id=identity, title=title, url=url, record={}), None,
                        platform="import", board=None, batch_id="test", search_run_id=1)
    db.save_parsed(ref, posting, "import:1")
    return ref


def run(db):
    normalize_postings(db, REG)
    return link_jobs(db, REG, ttl=None)


@pytest.mark.parametrize("same_source", [True, False])
@pytest.mark.parametrize("identity", ["req", "board"])
def test_distinct_requisitions_with_identical_templates_do_not_merge(same_source, identity):
    other_source = "nvidia" if same_source else "linkedin"
    a = item("nvidia/A", req="R1" if identity == "req" else None,
             key="workday:nvidia/site:R1" if identity == "board" else None)
    b = item(f"{other_source}/B", req="R2" if identity == "req" else None,
             key="workday:nvidia/site:R2" if identity == "board" else None)
    items = {a.pid: a, b.pid: b}
    assert len(dedup(items).groups) == 2
    assert len(plan_links(items, {}).new_jobs) == 2
    incremental = plan_links(items, {a.pid: {"job_id": 7}})
    assert len(incremental.new_jobs) == 1
    assert incremental.joined == {}


@pytest.mark.parametrize("same_source", [True, False])
@pytest.mark.parametrize("pending", [True, False])
def test_canonical_url_does_not_override_distinct_requisition_ids(db, same_source, pending):
    url = "https://careers.example/jobs/software-engineer"
    save(db, "repository", "one", url=url, req="R100", pending=pending)
    save(db, "repository" if same_source else "email", "two", url=url, req="R200", pending=pending)
    normalize_postings(db, REG)
    items = load_items(db, REG)
    assert {it.employer_key for it in items.values()} == {f"url:{url}"}
    assert match(*items.values()) is None
    assert len(dedup(items).groups) == 2
    assert len(plan_links(items, {}).new_jobs) == 2
    incremental = plan_links(items, {"repository/one": {"job_id": 7}})
    assert len(incremental.new_jobs) == 1
    assert incremental.joined == {}


@pytest.mark.parametrize("same_source", [True, False])
def test_exact_ats_identity_still_overrides_differing_reported_requisition_ids(same_source):
    key = "workday:nvidia/site:R100"
    a = item("nvidia/A", req="R100", key=key)
    b = item(f"{'nvidia' if same_source else 'linkedin'}/B", req="R200", key=key)
    items = {a.pid: a, b.pid: b}
    assert len(dedup(items).groups) == 1
    assert len(plan_links(items, {}).new_jobs) == 1
    incremental = plan_links(items, {a.pid: {"job_id": 7}})
    assert not incremental.new_jobs
    assert incremental.joined == {7: [b.pid]}


@pytest.mark.parametrize("same_source", [True, False])
@pytest.mark.parametrize("description", ["", DESCRIPTION], ids=["empty", "template"])
def test_pending_imports_never_merge_on_titles_dates_requisitions_or_templates(db, same_source, description):
    save(db, "imported", "one", pending=True, description=description, req="R1")
    save(db, "imported" if same_source else "email", "two", pending=True, description=description, req="R1")
    run(db)
    assert len(db.jobs()) == 2
    assert len(dedup(load_items(db, REG)).groups) == 2


def test_same_canonical_non_ats_url_links_recommendations_without_descriptions(db):
    url = "https://careers.example/jobs/42?job_id=42"
    save(db, "email", "message1", url=url, title="[Title pending]", pending=True, description="")
    save(db, "repository", "row1", url=url, pending=True, description="")
    run(db)
    [job] = db.jobs()
    assert job["postings"] == 2
    assert job["title"] == "Software Engineer"
    assert ("employer_key", "url:https://careers.example/jobs/42?job_id=42") in db.job_keys_of(job["job_id"])


def test_complete_posting_is_primary_even_when_pending_posting_is_company_source(db, tmp_path):
    url = "https://nvidia.wd5.myworkdayjobs.com/site/job/US-CA/Engineer_R42"
    save(db, "nvidia", "email", url=url, title="[Title pending]", pending=True, description="")
    save(db, "linkedin", "full", url=url)
    run(db)
    [job] = db.jobs()
    assert job["primary_source"] == "linkedin"
    assert job["title"] == "Software Engineer"
    flt = Filter("test", tmp_path, "test", "test", FilterConfig(), {}, version="v1")
    assert [(jid, f.name) for jid, f in plan(db, [flt], SelectOptions())] == [(job["job_id"], "test")]


def test_automatic_filter_plan_excludes_pending_jobs(db, tmp_path):
    save(db, "email", "pending", pending=True, description="")
    run(db)
    [job] = db.jobs()
    flt = Filter("test", tmp_path, "test", "test", FilterConfig(), {}, version="v1")
    assert plan(db, [flt], SelectOptions()) == []
    assert [(jid, f.name) for jid, f in plan(db, [flt], SelectOptions(jobs=[job["job_id"]]))] == [
        (job["job_id"], "test")]


def test_refresh_changed_posting_replaces_projection_but_preserves_decision_and_judgment(db):
    from auto_app.process import link

    ref = save(db, "repository", "one")
    run(db)
    [before] = db.jobs()
    jid = before["job_id"]
    db.set_job_state(jid, step2="recommended", decision="applied")
    decision = db.job(jid)
    with db.engine.begin() as conn:
        conn.execute(insert(T.filter_versions).values(filter_version="v1", filter="test", command="test",
                                                     created_at=datetime.now(timezone.utc)))
        conn.execute(insert(T.judgments).values(job_id=jid, filter_version="v1", filter="test", status="ok",
                                               recommend=True, reason="saved judgment", created_at=datetime.now(timezone.utc)))
    judgments = db.judgment_rows(job_id=jid)
    old_hashes = {k for k in db.job_keys_of(jid) if k[0] == "desc_hash"}
    save(db, "repository", "one", url="https://careers.example/jobs/one-updated", title="Backend Engineer",
         description=" ".join(f"storage{i} server reliability" for i in range(100)), locations=("Austin, TX",))
    normalize_postings(db, REG)
    refresh = getattr(link, "refresh_job_projection", None)
    assert callable(refresh), "changed linked postings need an explicit projection refresh"
    with db.transaction():
        refresh(db, load_items(db, REG), {jid})
    after = db.job(jid)
    assert db.get_posting(ref)["job_id"] == jid
    assert (after["title"], after["canonical_url"], after["locations"]) == (
        "Backend Engineer", "https://careers.example/jobs/one-updated", ["austin, tx"])
    assert (after["decision"], after["decided_at"], after["step2"]) == (
        decision["decision"], decision["decided_at"], decision["step2"])
    assert db.judgment_rows(job_id=jid) == judgments
    keys = set(db.job_keys_of(jid))
    assert not old_hashes & keys
    assert ("fingerprint", "nvidia|backend engineer") in keys
    assert ("fingerprint", "nvidia|software engineer") not in keys

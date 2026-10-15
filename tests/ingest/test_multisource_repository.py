"""Real repository runner, deduplication and queries against disposable databases."""

import json

import pytest
from sqlalchemy import select

from auto_app.core.db import tables as T
from auto_app.core.db.store import DbStore
from auto_app.ingest.runner import run_repository
from auto_app.query.service import QueryService
from auto_app.settings import Paths


FORMATS = ("simplify", "applyguy", "ricsign", "speedyapply")
GREENHOUSE = "https://job-boards.greenhouse.io/example/jobs/123456"
LEVER = "https://jobs.lever.co/example/4debf36a-384b-4ddf-ac2b-188e7b24f101"


@pytest.fixture
def store(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "preferences.json").write_text('{"exclude_countries": ["Canada"]}')
    value = DbStore(tmp_path / "jobs.db", Paths(home=tmp_path))
    yield value
    value.engine.dispose()


def snapshot(format, url, *, location="Seattle, WA", observation=1):
    """Small independently authored examples of the published source formats."""
    tracked_url = f"{url}?utm_source={format}"
    if format == "simplify":
        return json.dumps([{
            "id": "simplify-123", "url": tracked_url, "title": "Backend Engineer",
            "company_name": "Example", "locations": [location], "active": True,
            "is_visible": True, "date_posted": 1704067200, "source": "Simplify",
            "sponsorship": "Other", "degrees": ["Master's"],
        }])
    if format == "applyguy":
        return json.dumps({"updatedAt": f"2026-10-{observation:02}T05:00:00Z", "jobs": [{
            "id": "applyguy-123", "company": "Example", "title": "Backend Engineer",
            "location": location, "posted": "2024-01-01", "age": f"{observation}d",
            "eligibility": "New Grad", "matchKind": "explicit",
            "url": "https://applyguy.ai/jobs?job=applyguy-123", "listingUrl": tracked_url,
        }]})
    if format == "ricsign":
        return json.dumps({
            "api_version": 1, "generated_at": f"2026-10-{observation:02}T05:00:00+00:00",
            "source": "https://github.com/ricsign/Ricsign-New-Grads-Jobs-2027", "license": "MIT",
            "count": 1, "jobs": [{
                "uid": "ricsign-123", "company_name": "Example", "title": "Backend Engineer",
                "apply_url": tracked_url, "locations": [location], "source": "greenhouse",
                "posted_at": "2024-01-01", "first_seen": "2026-10-01",
                "last_verified": f"2026-10-{observation:02}", "active": True,
                "link_status": "ok", "track": "new-grad-swe", "sponsorship": "unknown",
            }],
        })
    if format == "speedyapply":
        return (
            "| Company | Position | Location | Salary | Posting | Age |\n"
            "|---|---|---|---|---|---|\n"
            f'| <a href="https://example.com"><strong>Example</strong></a> | Backend Engineer | '
            f'{location} | $100,000 | <a href="{tracked_url}"><img src="https://example.com/apply.png" '
            f'alt="Apply"/></a> | {observation}d |\n'
        )
    if format == "jobright":
        return (
            "| Company | Job Title | Location | Work Model | Date Posted |\n"
            "|---|---|---|---|---|\n"
            f"| **[Example](https://example.com)** | **[Backend Engineer]({tracked_url})** | "
            f"{location} | Hybrid | Oct {observation:02} |\n"
        )
    raise AssertionError(f"unknown test format: {format}")


def import_source(store, format, url=GREENHOUSE, *, source=None, **kwargs):
    source = source or format
    suffix = "md" if format in {"speedyapply", "jobright"} else "json"
    path = store.db_path.parent / f"{source}.{suffix}"
    path.write_text(snapshot(format, url, **kwargs), encoding="utf-8")
    result = run_repository(store, source, str(path), format=format)
    assert result["status"] == "ok" and result["issues"] == []
    return result


@pytest.mark.parametrize("url", [GREENHOUSE, LEVER], ids=["greenhouse", "lever"])
def test_four_formats_share_one_exact_employer_job_and_keep_four_source_records(store, url):
    reports = [import_source(store, format, url) for format in FORMATS]
    assert sum(report["jobs_created"] for report in reports) == 1
    assert sum(report["created"] for report in reports) == 4
    [job] = store.jobs()
    assert job["sources"] == ["applyguy", "ricsign", "simplify", "speedyapply"]
    with store.engine.connect() as conn:
        entries = conn.execute(select(T.ingest_entries)).mappings().all()
        posting_jobs = conn.execute(select(T.postings.c.job_id)).scalars().all()
    assert len(entries) == 4 and len({entry["posting_id"] for entry in entries}) == 4
    assert posting_jobs == [job["job_id"]] * 4
    with QueryService(store.db_path) as query:
        detail = query.job(job["job_id"])
    assert detail["canonical_url"] == url
    assert detail["postings"] == 4
    assert {entry["source"] for entry in detail["ingestion"]["entries"]} == set(FORMATS)


@pytest.mark.parametrize("first,second", [
    (GREENHOUSE, "https://job-boards.greenhouse.io/example/jobs/654321"),
    (LEVER, "https://jobs.lever.co/example/44ce30b4-306a-4111-a36c-5398ea9f8102"),
], ids=["greenhouse", "lever"])
def test_same_company_and_title_do_not_merge_distinct_requisitions(store, first, second):
    import_source(store, "simplify", first)
    import_source(store, "applyguy", first)
    import_source(store, "ricsign", second)
    import_source(store, "speedyapply", second)
    with QueryService(store.db_path) as query:
        page = query.jobs()
    assert page["total"] == 2
    assert {item["canonical_url"]: item["sources"] for item in page["items"]} == {
        first: ["applyguy", "simplify"], second: ["ricsign", "speedyapply"],
    }
    assert {item["title"] for item in page["items"]} == {"Backend Engineer"}


@pytest.mark.parametrize("decision", ["want_to_apply", "applied"])
def test_three_import_rounds_preserve_manual_mark_and_review_without_duplicate_history(store, decision):
    for format in FORMATS:
        import_source(store, format)
    [initial] = store.jobs()
    job_id = initial["job_id"]
    store.mark_job(job_id, decision)
    review_id = store.add_review(job_id, decision, "User chose this role", [], "Backend Engineer at Example")
    marked = store.job(job_id)
    for observation in (2, 3):
        reports = [import_source(store, format, observation=observation) for format in FORMATS]
        assert sum(report["created"] for report in reports) == 0
        assert sum(report["jobs_created"] for report in reports) == 0
        assert sum(report["updated"] for report in reports) == 0
        assert sum(report["unchanged"] for report in reports) == 4
        [job] = store.jobs()
        assert job["job_id"] == job_id
        assert job["decision"] == decision and job["step2"] == "recommended"
        assert job["decided_at"] == marked["decided_at"]
    with QueryService(store.db_path) as query:
        detail = query.job(job_id)
    assert detail["state"] == decision
    assert [(review["review_id"], review["reason"]) for review in detail["reviews"]] == [
        (review_id, "User chose this role"),
    ]
    with store.engine.connect() as conn:
        changes = conn.execute(select(T.ingest_changes.c.event)).scalars().all()
        entries = conn.execute(select(T.ingest_entries.c.source, T.ingest_entries.c.payload)).all()
    assert changes == ["created"] * 4
    payloads = dict(entries)
    assert payloads["applyguy"]["metadata"]["source_observation"]["age"] == "3d"
    assert payloads["ricsign"]["metadata"]["source_observation"]["last_verified"] == "2026-10-03"


def test_merged_us_and_canadian_evidence_stays_visible_and_canada_only_is_audited(store):
    import_source(store, "simplify", location="Toronto, ON, Canada")
    import_source(store, "applyguy", location="Seattle, WA")
    california_url = "https://job-boards.greenhouse.io/example/jobs/789012"
    import_source(store, "ricsign", california_url, location="San Jose, CA")
    canada_url = "https://job-boards.greenhouse.io/example/jobs/345678"
    import_source(store, "simplify", canada_url, source="simplify-canada", location="Vancouver, BC")
    with QueryService(store.db_path) as query:
        normal = query.jobs()
        audit = query.jobs(include_excluded=True)
        # A job that is also open in Seattle stays visible; only the Canada-only job is hidden.
        assert {item["canonical_url"] for item in normal["items"]} == {GREENHOUSE, california_url}
        assert audit["total"] == 3
        mixed = next(item for item in audit["items"] if item["canonical_url"] == GREENHOUSE)
        canadian = next(item for item in audit["items"] if item["canonical_url"] == canada_url)
        detail = query.job(mixed["job_id"])
    assert mixed["sources"] == ["applyguy", "simplify"]
    assert set(mixed["locations"]) == {"seattle, wa", "toronto, on, canada"}
    assert mixed["screening"]["excluded"] is False
    assert canadian["screening"]["excluded"] is True
    assert canadian["screening"]["reasons"] == ["excluded_country:Canada"]
    assert len(detail["ingestion"]["entries"]) == 2
    assert detail["decision"] is None


def test_jobright_identity_merges_repeated_platform_url_without_guessing_employer_equivalence(store):
    jobright_url = "https://jobright.ai/jobs/info/abc123"
    import_source(store, "jobright", jobright_url, source="jobright-newgrad")
    import_source(store, "jobright", jobright_url, source="jobright-internships")
    import_source(store, "simplify", GREENHOUSE)
    with QueryService(store.db_path) as query:
        page = query.jobs()
    assert page["total"] == 2
    assert {item["canonical_url"]: item["sources"] for item in page["items"]} == {
        jobright_url: ["jobright-internships", "jobright-newgrad"], GREENHOUSE: ["simplify"],
    }
    assert {item["title"] for item in page["items"]} == {"Backend Engineer"}
    assert {item["company"] for item in page["items"]} == {"Example"}

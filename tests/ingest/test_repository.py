"""Compatible public repository snapshots, using synthetic listing records."""

import json
from datetime import datetime, timezone

import pytest

from auto_app.ingest.repository import parse_repository


def listing(**changes):
    row = {
        "source": "Simplify",
        "category": "Software",
        "company_name": "Example Systems",
        "id": "repo-123",
        "title": "Backend Engineer",
        "active": True,
        "date_updated": 1704153600,
        "date_posted": 1704067200,
        "url": "https://careers.example.com/jobs/123",
        "locations": ["Seattle, WA", "Remote"],
        "company_url": "https://simplify.jobs/c/Example-Systems",
        "is_visible": True,
        "sponsorship": "Other",
        "degrees": ["Bachelor's", "Master's"],
    }
    return {**row, **changes}


def parse(*rows):
    return parse_repository(json.dumps(rows).encode())


def test_new_grad_fields_dates_and_full_upstream_evidence():
    original = listing()
    result = parse(original)
    assert result.issues == []
    [record] = result.records
    assert (record.external_id, record.url, record.title, record.company) == (
        "repo-123", "https://careers.example.com/jobs/123", "Backend Engineer", "Example Systems",
    )
    assert record.locations == ["Seattle, WA", "Remote"]
    assert record.posted_at == datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert record.active is True
    assert record.description == ""
    assert record.metadata["upstream"] == original


def test_internships_keep_old_and_unknown_terms_without_claiming_description():
    result = parse(
        listing(id="old", terms=["Summer 2026"]),
        listing(id="current", terms=["Summer 2027", "Fall 2027"]),
        listing(id="unspecified", terms=["N/A"]),
    )
    assert [r.external_id for r in result.records] == ["old", "current", "unspecified"]
    assert [r.metadata["upstream"]["terms"] for r in result.records] == [
        ["Summer 2026"], ["Summer 2027", "Fall 2027"], ["N/A"],
    ]
    assert all(r.description == "" for r in result.records)


@pytest.mark.parametrize("active,visible,want", [
    (True, True, True), (False, True, False), (True, False, False), (False, False, False),
])
def test_closed_and_hidden_records_remain_available_for_status_updates(active, visible, want):
    result = parse(listing(active=active, is_visible=visible))
    [record] = result.records
    assert record.active is want
    assert record.metadata["upstream"]["active"] is active
    assert record.metadata["upstream"]["is_visible"] is visible


def test_missing_optional_description_evidence_is_not_invented():
    row = listing(title=None, company_name=None, locations=[], date_posted=None)
    [record] = parse(row).records
    assert record.title is None and record.company is None
    assert record.description == "" and record.posted_at is None


@pytest.mark.parametrize("content", [b"not json", b"{}", b'{"jobs": []}', b"null"])
def test_invalid_document_raises_instead_of_reporting_empty_success(content):
    with pytest.raises(ValueError):
        parse_repository(content)


@pytest.mark.parametrize("bad,field", [
    (42, "object"),
    (listing(id=""), "id"),
    (listing(id=123), "id"),
    (listing(url="/jobs/123"), "url"),
    (listing(url="javascript:alert(1)"), "url"),
    (listing(url="https://user:password@careers.example.com/jobs/123"), "url"),
    (listing(url="https://bad host.example/jobs/123"), "url"),
    (listing(url="https://careers.example.com:invalid/jobs/123"), "url"),
    (listing(url="https://careers.example.com:65536/jobs/123"), "url"),
    (listing(active="false"), "active"),
    (listing(is_visible=None), "is_visible"),
    (listing(locations="Remote"), "locations"),
    (listing(locations=["Remote", 1]), "locations"),
    (listing(date_posted="yesterday"), "date_posted"),
    (listing(date_posted=True), "date_posted"),
    (listing(date_posted=10**100), "date_posted"),
    (listing(title=["Backend Engineer"]), "title"),
])
def test_bad_rows_report_position_without_losing_valid_siblings(bad, field):
    result = parse(listing(id="first"), bad, listing(id="last"))
    assert [r.external_id for r in result.records] == ["first", "last"]
    assert len(result.issues) == 1
    assert result.issues[0]["index"] == 1
    assert field in result.issues[0]["reason"]


def test_duplicate_external_id_reports_issue_and_keeps_first_observation():
    result = parse(listing(title="First title"), listing(title="Changed title"))
    assert [(r.external_id, r.title) for r in result.records] == [("repo-123", "First title")]
    assert result.issues[0]["index"] == 1
    assert "duplicate" in result.issues[0]["reason"].lower()


def test_empty_valid_snapshot_has_no_records_or_issues():
    result = parse_repository(b"[]")
    assert result.records == [] and result.issues == []


def test_compatible_feed_can_supply_observed_description_and_employment_type():
    original = listing(description_text="Build backend services.", employment_type="full_time", req_id="ENG-123")
    [record] = parse(original).records
    assert record.description == "Build backend services."
    assert record.employment_type == "full_time"
    assert record.metadata["upstream"]["req_id"] == "ENG-123"


@pytest.mark.parametrize("value, expected", [
    ("FULL_TIME", "full_time"), ("Full-time", "full_time"), ("PART_TIME", "part_time"),
    ("INTERNSHIP", "internship"), ("contract", "contract"), ("OTHER", "unknown"),
])
def test_employment_type_spellings_share_one_vocabulary(value, expected):
    # LinkedIn exports FULL_TIME; dedup vetoes a pair whose employment types differ.
    [record] = parse(listing(employment_type=value)).records
    assert record.employment_type == expected


def test_listing_url_names_the_site_page_while_url_stays_the_employer_link():
    [record] = parse(listing(url="https://job-boards.greenhouse.io/rocketlab/jobs/8038139003?jr_id=6ae5",
                             listing_url="https://jobright.ai/jobs/info/6ae5?utm_source=grok")).records
    assert record.url == "https://job-boards.greenhouse.io/rocketlab/jobs/8038139003"
    assert record.listing_url == "https://jobright.ai/jobs/info/6ae5"
    assert parse(listing()).records[0].listing_url is None


@pytest.mark.parametrize("value", ["jobright.ai/jobs/info/6ae5", "mailto:jobs@example.test", 7])
def test_invalid_listing_url_is_a_row_issue(value):
    result = parse(listing(listing_url=value))
    assert result.records == [] and len(result.issues) == 1


def test_description_alias_is_observed_text_and_missing_job_type_remains_unknown():
    [record] = parse(listing(description="Maintain distributed queues.")).records
    assert record.description == "Maintain distributed queues."
    assert record.employment_type == "unknown"

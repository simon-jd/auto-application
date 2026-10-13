"""Provider JSON contracts using small, synthetic rows shaped like public feeds."""

import json
from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from auto_app.ingest.repository_json import parse_applyguy, parse_ricsign


def applyguy_row(**changes):
    return {
        "id": "ag-123", "company": "Example Systems", "title": "Backend Engineer",
        "location": "Seattle, WA", "eligibility": "New Grad", "matchKind": "explicit",
        "posted": "2026-10-09", "age": "2d", "applySupported": False,
        "url": "https://applyguy.ai/jobs?job=ag-123&utm_source=github",
        "listingUrl": "https://careers.example.com/jobs/123?utm_source=applyguy&req=123#apply",
        **changes,
    }


def ricsign_row(**changes):
    return {
        "uid": "ric-123", "company_slug": "example", "company_name": "Example Systems",
        "company_tier": 1, "company_category": "infra", "source": "greenhouse",
        "title": "Backend Engineer", "apply_url": "https://careers.example.com/jobs/123?gh_src=tracker",
        "locations": ["Seattle, WA", "Remote"], "track": "new-grad-swe",
        "degree": "masters-preferred", "sponsorship": "sponsors",
        "first_seen": "2026-10-07", "last_verified": "2026-10-10",
        "posted_at": "2026-10-06", "closed_at": None, "season": "2027",
        "compensation": {"min": 100000, "max": 150000, "currency": "USD", "interval": "1 YEAR"},
        "department": "Engineering", "active": True, "link_status": "ok", "openings": 1,
        **changes,
    }


def parse_ag(*rows, **header):
    return parse_applyguy(json.dumps({"updatedAt": "2026-10-11T05:00:00Z", "jobs": rows, **header}).encode())


def parse_ric(*rows, **header):
    return parse_ricsign(json.dumps({
        "api_version": 1, "generated_at": "2026-10-10T21:49:12+00:00",
        "source": "https://github.com/ricsign/Ricsign-New-Grads-Jobs-2027",
        "license": "MIT", "count": len(rows), "jobs": rows, **header,
    }).encode())


def test_applyguy_uses_employer_destination_and_preserves_eligibility():
    result = parse_ag(applyguy_row())
    assert result.issues == []
    [record] = result.records
    assert (record.external_id, record.url) == ("ag-123", "https://careers.example.com/jobs/123?req=123")
    assert (record.company, record.title, record.locations) == ("Example Systems", "Backend Engineer", ["Seattle, WA"])
    assert record.active is True
    assert record.posted_at == datetime(2026, 10, 9, tzinfo=timezone.utc)
    assert record.metadata["posted_at_precision"] == "day"
    assert record.metadata["upstream"]["eligibility"] == "New Grad"
    assert record.metadata["upstream"]["matchKind"] == "explicit"
    assert record.metadata["upstream"]["url"].startswith("https://applyguy.ai/")
    assert record.description == "" and record.employment_type == "unknown"


def test_ricsign_keeps_original_dates_sponsorship_and_provenance():
    result = parse_ric(ricsign_row())
    assert result.issues == []
    [record] = result.records
    assert (record.external_id, record.url) == ("ric-123", "https://careers.example.com/jobs/123")
    assert (record.company, record.title) == ("Example Systems", "Backend Engineer")
    assert record.locations == ["Seattle, WA", "Remote"]
    assert record.posted_at == datetime(2026, 10, 6, tzinfo=timezone.utc)
    assert record.metadata["posted_at_precision"] == "day"
    assert record.metadata["feed"] == {
        "api_version": 1, "source": "https://github.com/ricsign/Ricsign-New-Grads-Jobs-2027", "license": "MIT",
    }
    upstream = record.metadata["upstream"]
    assert upstream["first_seen"] == "2026-10-07"
    assert upstream["sponsorship"] == "sponsors" and upstream["degree"] == "masters-preferred"
    assert upstream["compensation"]["min"] == 100000


@pytest.mark.parametrize("active,status,want", [
    (True, "ok", True), (True, "dead", False), (True, "closed", False),
    (True, "blocked", True), (True, "error", True), (True, "unknown", True),
    (False, "ok", False), (None, "unknown", None), (None, "closed", False),
])
def test_ricsign_closure_depends_on_explicit_flags_not_clearance(active, status, want):
    [record] = parse_ric(ricsign_row(active=active, link_status=status, sponsorship="security-clearance", title="Engineer 🔒")).records
    assert record.active is want
    assert record.metadata["upstream"]["sponsorship"] == "security-clearance"


def test_verification_times_and_relative_age_do_not_change_parsed_content():
    first_ag = parse_ag(applyguy_row(age="2d"))
    second_ag = parse_ag(applyguy_row(age="3d"), updatedAt="2026-10-12T05:00:00Z")
    first_ric = parse_ric(ricsign_row(last_verified="2026-10-10"))
    second_ric = parse_ric(ricsign_row(last_verified="2026-10-11"), generated_at="2026-10-11T21:00:00Z", count=90)
    for first, second in ((first_ag, second_ag), (first_ric, second_ric)):
        before, after = asdict(first.records[0]), asdict(second.records[0])
        before["metadata"].pop("source_observation")
        after["metadata"].pop("source_observation")
        assert before == after
    assert "age" not in first_ag.records[0].metadata["upstream"]
    assert "last_verified" not in first_ric.records[0].metadata["upstream"]
    assert first_ag.records[0].metadata["source_observation"] == {"age": "2d", "updatedAt": "2026-10-11T05:00:00Z"}
    assert second_ric.records[0].metadata["source_observation"] == {
        "last_verified": "2026-10-11", "generated_at": "2026-10-11T21:00:00Z", "count": 90,
    }


def test_missing_publication_date_does_not_fall_back_to_observation_dates():
    [ag] = parse_ag(applyguy_row(posted=None, age="1d", location=None)).records
    [ric] = parse_ric(ricsign_row(posted_at=None)).records
    assert ag.posted_at is None and ric.posted_at is None
    assert ag.locations == []
    assert "posted_at_precision" not in ric.metadata
    assert "posted_at_precision" not in ag.metadata


def test_ricsign_iso_timestamp_preserves_instant_and_precision():
    [record] = parse_ric(ricsign_row(posted_at="2026-10-09T23:15:20-07:00")).records
    assert record.posted_at == datetime(2026, 10, 10, 6, 15, 20, tzinfo=timezone.utc)
    assert record.metadata["posted_at_precision"] == "timestamp"


@pytest.mark.parametrize("parser", [parse_applyguy, parse_ricsign])
@pytest.mark.parametrize("content", [b"not json", b"null", b"[]", b"{}", b'{"jobs":{}}', b'{"jobs":null}', b'\xff'])
def test_invalid_documents_raise(parser, content):
    with pytest.raises(ValueError):
        parser(content)


def test_unknown_ricsign_schema_version_is_not_silently_imported():
    with pytest.raises(ValueError):
        parse_ric(ricsign_row(), api_version=2)


@pytest.mark.parametrize("bad,field", [
    (42, "object"), (applyguy_row(id=""), "id"), (applyguy_row(id=123), "id"),
    (applyguy_row(listingUrl=None), "listingUrl"),
    (applyguy_row(listingUrl="javascript:alert(1)"), "listingUrl"),
    (applyguy_row(location=["Seattle"]), "location"),
    (applyguy_row(posted="2026-02-30"), "posted"),
    (applyguy_row(posted="2d"), "posted"), (applyguy_row(title=123), "title"),
])
def test_applyguy_bad_rows_report_indexes_and_preserve_siblings(bad, field):
    result = parse_ag(applyguy_row(id="first"), bad, applyguy_row(id="last"))
    assert [record.external_id for record in result.records] == ["first", "last"]
    assert len(result.issues) == 1 and result.issues[0]["index"] == 1
    assert field in result.issues[0]["reason"]


@pytest.mark.parametrize("bad,field", [
    (None, "object"), (ricsign_row(uid=""), "uid"),
    (ricsign_row(apply_url="/jobs/123"), "apply_url"),
    (ricsign_row(active="false"), "active"),
    (ricsign_row(link_status=True), "link_status"),
    (ricsign_row(locations="Seattle"), "locations"),
    (ricsign_row(locations=["Seattle", 1]), "locations"),
    (ricsign_row(posted_at=1724000), "posted_at"),
    (ricsign_row(posted_at="2026-10-09T12:00:00"), "posted_at"),
])
def test_ricsign_bad_rows_report_indexes_and_preserve_siblings(bad, field):
    result = parse_ric(ricsign_row(uid="first"), bad, ricsign_row(uid="last"))
    assert [record.external_id for record in result.records] == ["first", "last"]
    assert len(result.issues) == 1 and result.issues[0]["index"] == 1
    assert field in result.issues[0]["reason"]


@pytest.mark.parametrize("parser,row", [(parse_ag, applyguy_row), (parse_ric, ricsign_row)])
def test_duplicate_id_retains_first_record_and_reports_duplicate(parser, row):
    result = parser(row(title="First title"), row(title="Second title"))
    assert [record.title for record in result.records] == ["First title"]
    assert result.issues[0]["index"] == 1
    assert "duplicate" in result.issues[0]["reason"]


def test_empty_snapshots_are_successful():
    for result in (parse_ag(), parse_ric()):
        assert result.records == [] and result.issues == []

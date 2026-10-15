"""Public employer boards retain descriptions, identity, and snapshot safety."""

import json

import httpx
import pytest
from sqlalchemy import select

from auto_app.core.db import tables as T
from auto_app.ingest.repository import parse_repository
from auto_app.ingest.runner import load_feed_config, run_config, run_repository
from tests.imported_jobs import ingest, make_store, record

GH_JOB = {"id": 4012345, "title": "Software Engineer, New Grad", "company_name": "Acme",
          "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/4012345",
          "location": {"name": "Boston, MA"}, "content": "&lt;p&gt;Build APIs in Python.&lt;/p&gt;",
          "updated_at": "2026-10-01T00:00:00Z"}
LEVER_JOB = {"id": "6ed76ce8-1111-2222-3333-444455556666", "text": "Data Engineer",
             "hostedUrl": "https://jobs.lever.co/acme/6ed76ce8-1111-2222-3333-444455556666",
             "applyUrl": "https://jobs.lever.co/acme/6ed76ce8-1111-2222-3333-444455556666/apply",
             "categories": {"location": "Remote", "commitment": "Full-time"},
             "descriptionPlain": "Own pipelines.", "createdAt": 1760000000000}
ASHBY_JOB = {"id": "91eed659-1111-2222-3333-444455556666", "title": "Backend Engineer",
             "location": "New York", "jobUrl": "https://jobs.ashbyhq.com/acme/91eed659-1111-2222-3333-444455556666",
             "descriptionPlain": "Own services.", "employmentType": "FullTime", "isListed": True}


def gh(rows):
    return json.dumps({"jobs": rows}).encode()


def test_greenhouse_board_carries_descriptions_and_bad_rows_are_isolated():
    parsed = parse_repository(gh([GH_JOB, {"id": 4012346, "title": "", "absolute_url": "not a url"}]),
                              format="greenhouse-board")
    assert [r.external_id for r in parsed.records] == ["4012345"]
    first = parsed.records[0]
    assert first.company == "Acme" and first.description == "Build APIs in Python."
    assert first.url == "https://job-boards.greenhouse.io/acme/jobs/4012345" and first.active is True
    assert first.posted_at.isoformat() == "2026-10-01T00:00:00+00:00"
    assert [issue["index"] for issue in parsed.issues] == [1]


def test_lever_and_ashby_use_company_and_skip_unlisted():
    lever = parse_repository(json.dumps([LEVER_JOB]).encode(), format="lever-board", company="Acme")
    ashby = parse_repository(json.dumps({"jobs": [ASHBY_JOB, {"isListed": False}]}).encode(),
                            format="ashby-board", company="Acme")
    assert lever.records[0].company == "Acme" and lever.records[0].employment_type == "full_time"
    assert lever.records[0].description == "Own pipelines."
    assert [r.title for r in ashby.records] == ["Backend Engineer"] and not ashby.issues
    assert ashby.records[0].company == "Acme" and ashby.records[0].description == "Own services."


@pytest.mark.parametrize("changes", [
    {"absolute_url": "javascript:alert(1)"}, {"absolute_url": "https://user:secret@careers.example/job"},
    {"absolute_url": "https://bad host.example/job"}, {"absolute_url": "https://example.com:wrong/job"},
    {"location": "Boston"}, {"offices": [3]}, {"title": 8}, {"id": None}, {"id": True},
])
def test_malformed_rows_do_not_prevent_valid_siblings(changes):
    parsed = parse_repository(gh([{**GH_JOB, **changes}, GH_JOB]), format="greenhouse-board")
    assert [r.external_id for r in parsed.records] == ["4012345"]
    assert [issue["index"] for issue in parsed.issues] == [0]
    assert "secret" not in parsed.issues[0]["reason"]


def test_duplicate_ids_keep_first_and_report_second():
    parsed = parse_repository(gh([GH_JOB, {**GH_JOB, "title": "Changed title"}]), format="greenhouse-board")
    assert [r.title for r in parsed.records] == ["Software Engineer, New Grad"]
    assert [issue["index"] for issue in parsed.issues] == [1]


@pytest.mark.parametrize("format,content", [
    ("greenhouse-board", b"bad json"), ("greenhouse-board", b"[]"),
    ("ashby-board", b'{"jobs": {}}'), ("lever-board", b'{"jobs": []}'),
])
def test_bad_document_is_an_error_instead_of_empty_snapshot(format, content):
    with pytest.raises(ValueError):
        parse_repository(content, format=format)


def test_board_job_merges_repository_copy_and_preserves_other_employer(tmp_path):
    store = make_store(tmp_path)
    try:
        ingest(store, [record("4012345", "Software Engineer, New Grad"),
                       record("other", "Software Engineer, New Grad", company="Beta",
                              url="https://job-boards.greenhouse.io/beta/jobs/4012346")])
        from auto_app.ingest.service import IngestService
        parsed = parse_repository(gh([GH_JOB]), format="greenhouse-board")
        IngestService(store).ingest("board-greenhouse-acme", parsed, kind="repository", origin="board",
                                    revision="r", complete_snapshot=True)
        jobs = store.jobs()
        assert len(jobs) == 2
        acme = next(j for j in jobs if j["company"] == "Acme")
        assert set(acme["sources"]) == {"simplify-newgrad", "board-greenhouse-acme"}
    finally:
        store.engine.dispose()


@pytest.mark.parametrize("company", [3, "", "  ", [], True])
def test_feed_config_rejects_invalid_company(tmp_path, company):
    import yaml
    path = tmp_path / "feeds.yaml"
    path.write_text(yaml.safe_dump({"sources": {"x": {"kind": "repository", "format": "lever-board",
                                                     "company": company, "location": "feed.json"}}}))
    with pytest.raises(ValueError, match="company"):
        load_feed_config(path)


def test_config_import_applies_company_and_complete_snapshot(tmp_path):
    store = make_store(tmp_path)
    try:
        feed, config = tmp_path / "feed.json", tmp_path / "feeds.yaml"
        feed.write_text(json.dumps([LEVER_JOB]))
        config.write_text("sources:\n  board-lever-acme:\n    kind: repository\n    format: lever-board\n"
                          "    company: ' Acme '\n    location: feed.json\n")
        assert load_feed_config(config)["board-lever-acme"]["company"] == "Acme"
        assert run_config(store, config)["sources"]["board-lever-acme"]["created"] == 1
        assert store.jobs()[0]["company"] == "Acme"
        feed.write_text("[]")
        assert run_config(store, config)["sources"]["board-lever-acme"]["missing"] == 1
    finally:
        store.engine.dispose()


def test_partial_board_keeps_absent_rows_and_invalidates_validators(tmp_path):
    store = make_store(tmp_path)
    responses = [httpx.Response(200, content=gh([GH_JOB]), headers={"ETag": '"v1"'}),
                 httpx.Response(200, content=gh([{"id": 7}]), headers={"ETag": '"v2"'})]
    try:
        with httpx.Client(transport=httpx.MockTransport(lambda request: responses.pop(0))) as client:
            run_repository(store, "board", "https://example.com/board", client=client, format="greenhouse-board")
            partial = run_repository(store, "board", "https://example.com/board", client=client,
                                     format="greenhouse-board")
        assert partial["status"] == "partial" and partial["missing"] == 0
        with store.engine.connect() as conn:
            assert conn.execute(select(T.ingest_entries.c.status)).scalar_one() == "active"
        assert store.source_health("board")["ingest"]["etag"] is None
    finally:
        store.engine.dispose()


def test_changing_company_forces_parse_instead_of_304(tmp_path):
    store = make_store(tmp_path)
    def handler(request):
        return (httpx.Response(304) if request.headers.get("if-none-match") else
                httpx.Response(200, json=[LEVER_JOB], headers={"ETag": '"v1"'}))
    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            run_repository(store, "board", "https://example.com/board", client=client,
                           format="lever-board", company="Acme")
            again = run_repository(store, "board", "https://example.com/board", client=client,
                                   format="lever-board", company="Acme Inc")
        assert not again.get("not_modified") and again["updated"] == 1
        assert store.jobs()[0]["company"] == "Acme Inc"
    finally:
        store.engine.dispose()


def test_lever_country_is_kept_for_location_screening():
    row = {**LEVER_JOB, "country": "GB", "categories": {"location": "Remote", "commitment": "Full-time"}}
    parsed = parse_repository(json.dumps([row]).encode(), format="lever-board", company="Acme")
    assert parsed.records[0].metadata["upstream"]["country"] == "GB"

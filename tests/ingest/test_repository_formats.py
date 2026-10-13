"""Format selection and observation-only refreshes through real storage."""

import json

import httpx
import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from auto_app.core.db import tables as T
from auto_app.core.db.store import DbStore
from auto_app.ingest.cli import app
from auto_app.ingest.models import IngestRecord, ParsedFeed
from auto_app.ingest.runner import load_feed_config, run_config, run_repository
from auto_app.ingest.service import IngestService
from auto_app.settings import Paths


@pytest.fixture
def store(tmp_path):
    db = DbStore(tmp_path / "jobs.db", Paths(home=tmp_path))
    yield db
    db.engine.dispose()


def test_config_rejects_unknown_repository_format_before_database_creation(tmp_path):
    config = tmp_path / "feeds.yaml"
    config.write_text("sources:\n  repo:\n    kind: repository\n    format: typo\n    location: feed.json\n")
    with pytest.raises(ValueError, match="format"):
        load_feed_config(config)
    db = tmp_path / "absent.db"
    result = CliRunner().invoke(app, ["run", "--config", str(config), "--db", str(db)],
                                env={"AUTO_APP_HOME": str(tmp_path)})
    assert result.exit_code == 2
    assert not db.exists()


def test_config_retains_format_and_simplify_is_the_legacy_default(tmp_path):
    config = tmp_path / "feeds.yaml"
    config.write_text("sources:\n  old:\n    kind: repository\n    location: old.json\n"
                      "  new:\n    kind: repository\n    format: applyguy\n    location: new.json\n")
    sources = load_feed_config(config)
    assert sources["old"]["format"] == "simplify"
    assert sources["new"]["format"] == "applyguy"


def test_changing_parser_format_invalidates_conditional_snapshot(store):
    calls = []

    def handler(request):
        calls.append(request.headers.get("if-none-match"))
        if len(calls) == 1:
            return httpx.Response(200, json=[], headers={"ETag": '"v1"'})
        return httpx.Response(200, json={"updatedAt": "2026-10-10", "jobs": []},
                              headers={"ETag": '"v2"'})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        url = "https://example.com/feed"
        run_repository(store, "repo", url, client=client)
        report = run_repository(store, "repo", url, client=client, format="applyguy")
    assert calls == [None, None]
    assert report["format"] == "applyguy"
    assert store.source_health("repo")["ingest"]["format"] == "applyguy"


def test_observation_age_refresh_keeps_decision_and_semantic_history(store):
    service = IngestService(store)

    def ingest(age, title="Backend Engineer"):
        rec = IngestRecord(external_id="one", url="https://jobs.lever.co/acme/one", title=title,
                           company="Acme", locations=["Boston, MA"], active=True,
                           metadata={"source_observation": {"age": age}, "salary": "$100k"})
        return service.ingest("repo", ParsedFeed([rec]), kind="repository", origin="table.md",
                              revision=age, complete_snapshot=True)

    ingest("1d")
    job_id = store.jobs()[0]["job_id"]
    store.mark_job(job_id, "want_to_apply")
    second = ingest("2d")
    assert second["updated"] == 0 and second["unchanged"] == 1
    with store.engine.connect() as conn:
        entry = conn.execute(select(T.ingest_entries)).mappings().one()
        assert entry["payload"]["metadata"]["source_observation"]["age"] == "2d"
        assert entry["revision"] == "2d"
        assert conn.execute(select(T.ingest_changes.c.event)).scalars().all() == ["created"]
    third = ingest("3d", "Platform Engineer")
    assert third["updated"] == 1 and third["jobs_created"] == 0
    assert store.jobs()[0]["job_id"] == job_id
    assert store.jobs()[0]["decision"] == "want_to_apply"


def test_applyguy_config_and_cli_use_employer_link_and_repeat_safely(store, tmp_path):
    row = {"id": "ag-one", "company": "Acme", "title": "Backend Engineer",
           "location": "Boston, MA", "posted": "2026-10-09",
           "url": "https://applyguy.ai/jobs?job=ag-one",
           "listingUrl": "https://jobs.lever.co/acme/one"}
    path = tmp_path / "applyguy.json"
    path.write_text(json.dumps({"updatedAt": "2026-10-10", "jobs": [row]}))
    config = tmp_path / "feeds.yaml"
    config.write_text("sources:\n  applyguy:\n    kind: repository\n    format: applyguy\n"
                      "    location: applyguy.json\n")
    assert run_config(store, config)["sources"]["applyguy"]["jobs_created"] == 1
    result = CliRunner().invoke(app, ["repository", str(path), "--source", "applyguy", "--format",
                                "applyguy", "--db", str(store.db_path)],
                                env={"AUTO_APP_HOME": str(tmp_path)})
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["unchanged"] == 1
    assert len(store.jobs()) == 1
    with store.engine.connect() as conn:
        assert conn.execute(select(T.postings.c.apply_url)).scalar_one() == row["listingUrl"]
        assert conn.execute(select(T.postings.c.parsed)).scalar_one()["posted_at_precision"] == "day"


def test_complete_snapshot_interruption_requires_full_body_after_upstream_rollback(store, monkeypatch):
    from auto_app.ingest import service

    def document(title):
        return {"jobs": [{"id": "ag-one", "company": "Acme", "title": title,
                          "listingUrl": "https://jobs.lever.co/acme/one"}]}

    validators = {"ETag": '"v1"', "Last-Modified": "Thu, 01 Oct 2026 00:00:00 GMT"}
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 2:
            return httpx.Response(200, json=document("Updated title"), headers={"ETag": '"v2"'})
        # The upstream snapshot rolls back to v1 after the interrupted v2 import.
        if (request.headers.get("if-none-match") == validators["ETag"]
                or request.headers.get("if-modified-since") == validators["Last-Modified"]):
            return httpx.Response(304)
        return httpx.Response(200, json=document("Original title"), headers=validators)

    def interrupt_projection(*args):
        raise ValueError("Interrupted after the snapshot transaction committed")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        location = "https://example.com/feed.json"
        run_repository(store, "repo", location, client=client, format="applyguy")
        job_id = store.jobs()[0]["job_id"]
        with monkeypatch.context() as interruption:
            interruption.setattr(service, "refresh_job_projection", interrupt_projection)
            with pytest.raises(ValueError, match="snapshot transaction committed"):
                run_repository(store, "repo", location, client=client, format="applyguy")
        with store.engine.connect() as conn:
            assert conn.execute(select(T.ingest_entries.c.payload)).scalar_one()["title"] == "Updated title"
        report = run_repository(store, "repo", location, client=client, format="applyguy")

    with store.engine.connect() as conn:
        entry = conn.execute(select(T.ingest_entries)).mappings().one()
        assert entry["payload"]["title"] == "Original title"
        assert entry["revision"] == store.source_health("repo")["ingest"]["revision"]
        assert conn.execute(select(T.postings.c.title)).scalar_one() == "Original title"
    assert report["status"] == "ok" and not report.get("not_modified")
    assert "if-none-match" not in requests[-1].headers
    assert "if-modified-since" not in requests[-1].headers
    assert [job["job_id"] for job in store.jobs()] == [job_id]

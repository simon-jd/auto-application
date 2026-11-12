"""Import orchestration with disposable databases and synthetic HTTP responses."""

import json

import httpx
import pytest
from sqlalchemy import select

from auto_app.core.db import tables as T
from auto_app.core.db.store import DbStore
from auto_app.ingest.runner import load_feed_config, run_config, run_email, run_repository
from auto_app.settings import Paths

URL = "https://raw.githubusercontent.com/Example/jobs/dev/listings.json"


@pytest.fixture
def store(tmp_path):
    value = DbStore(tmp_path / "jobs.db", Paths(home=tmp_path))
    yield value
    value.engine.dispose()


def row(id="one", **changes):
    return {"id": id, "url": f"https://careers.example.com/jobs/{id}", "title": "Backend Engineer",
            "company_name": "Example", "locations": ["Boston, MA"], "active": True, "is_visible": True,
            "date_posted": 1704067200, "date_updated": 1704067200, "source": "Simplify", "category": "Software",
            "company_url": "", "sponsorship": "Other", "degrees": [], **changes}


def entries(store):
    with store.engine.connect() as conn:
        return {r["external_id"]: dict(r) for r in conn.execute(select(T.ingest_entries)).mappings()}


def message(path, id="one"):
    path.write_text(f"Message-ID: <{id}@example.com>\nSubject: Jobs\nContent-Type: text/html\n\n"
                    f'<a href="https://www.linkedin.com/jobs/view/{100 if id == "one" else 200}">Backend Engineer</a>')
    return path


def test_repository_304_touches_observations_without_duplicate_jobs(store):
    def handler(request):
        if request.headers.get("if-none-match") == '"v1"':
            assert request.headers["if-modified-since"] == "Thu, 01 Oct 2026 00:00:00 GMT"
            return httpx.Response(304)
        return httpx.Response(200, json=[row()], headers={"ETag": '"v1"',
                              "Last-Modified": "Thu, 01 Oct 2026 00:00:00 GMT"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = run_repository(store, "repo", URL, client=client)
        assert first["created"] == 1 and first["status"] == "ok"
        old_seen = entries(store)["one"]["last_seen_at"]
        again = run_repository(store, "repo", URL, client=client)
    assert again["not_modified"] and again["unchanged"] == 1
    assert entries(store)["one"]["last_seen_at"] >= old_seen
    assert len(store.jobs()) == 1
    checkpoint = store.source_health("repo")["ingest"]
    assert checkpoint["location"] == URL and checkpoint["etag"] == '"v1"'
    assert checkpoint["revision"].startswith("sha256:")


@pytest.mark.parametrize("response", [httpx.Response(500), httpx.Response(200, content=b"invalid json")])
def test_failed_fetch_or_parse_preserves_checkpoint_and_prior_status(store, response):
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=[row()], headers={"ETag": '"v1"'}),
    )) as client:
        run_repository(store, "repo", URL, client=client)
    before = store.source_health("repo")["ingest"]
    with httpx.Client(transport=httpx.MockTransport(lambda request: response)) as client:
        with pytest.raises((httpx.HTTPError, ValueError)):
            run_repository(store, "repo", URL, client=client)
    assert store.source_health("repo")["ingest"] == before
    assert entries(store)["one"]["status"] == "active"


def test_partial_feed_keeps_revision_but_invalidates_validators_and_does_not_mark_absent(store):
    responses = [
        httpx.Response(200, json=[row(), row("two")], headers={"ETag": '"v1"'}),
        httpx.Response(200, json=[row(title="Platform Engineer"), {"id": "broken"}], headers={"ETag": '"bad"'}),
        httpx.Response(200, json=[row(title="Platform Engineer"), row("two")], headers={"ETag": '"v2"'}),
    ]
    sent = []

    def handler(request):
        sent.append(request.headers.get("if-none-match"))
        return responses.pop(0)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        run_repository(store, "repo", URL, client=client)
        previous_revision = store.source_health("repo")["ingest"]["revision"]
        partial = run_repository(store, "repo", URL, client=client)
        assert partial["status"] == "partial" and partial["issues"]
        assert partial["missing"] == 0 and entries(store)["two"]["status"] == "active"
        checkpoint = store.source_health("repo")["ingest"]
        assert checkpoint["revision"] == previous_revision
        assert checkpoint.get("etag") is None and checkpoint.get("last_modified") is None
        repaired = run_repository(store, "repo", URL, client=client)
    assert repaired["status"] == "ok" and sent == [None, '"v1"', None]
    assert store.source_health("repo")["ingest"]["etag"] == '"v2"'


@pytest.mark.parametrize("url", [
    "https://user:password@careers.example.com/jobs/bad",
    "https://bad host.example/jobs/bad",
    "https://careers.example.com:invalid/jobs/bad",
])
def test_malformed_row_url_keeps_valid_siblings_and_retryable_checkpoint(store, url):
    payloads = [[row("old")], [row("new"), row("bad", url=url)], [row("new"), row("bad")]]
    validators = []

    def handler(request):
        validators.append(request.headers.get("if-none-match"))
        return httpx.Response(200, json=payloads.pop(0), headers={"ETag": '"feed"'})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        run_repository(store, "repo", URL, client=client)
        partial = run_repository(store, "repo", URL, client=client)
        assert partial["status"] == "partial" and partial["created"] == 1
        assert partial["issues"][0]["index"] == 1
        assert entries(store)["old"]["status"] == "active"
        assert store.source_health("repo")["ingest"]["etag"] is None
        repaired = run_repository(store, "repo", URL, client=client)
    assert repaired["status"] == "ok" and repaired["created"] == 1
    assert validators == [None, '"feed"', None]


def test_changing_origin_does_not_send_previous_origin_validators(store):
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get("if-none-match")))
        return httpx.Response(200, json=[row()], headers={"ETag": '"v1"'})

    other = "https://raw.githubusercontent.com/Example/other/dev/listings.json"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        run_repository(store, "repo", URL, client=client)
        run_repository(store, "repo", other, client=client)
    assert seen == [(URL, None), (other, None)]
    assert store.source_health("repo")["ingest"]["location"] == other


def test_unsolicited_304_is_not_a_successful_observation(store):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(304))) as client:
        with pytest.raises(ValueError, match="304"):
            run_repository(store, "repo", URL, client=client)
    assert "ingest" not in store.source_health("repo")


def test_local_repository_repeat_is_idempotent(store, tmp_path):
    path = tmp_path / "feed.json"
    path.write_text(json.dumps([row()]))
    assert run_repository(store, "repo", str(path))["created"] == 1
    assert run_repository(store, "repo", str(path))["unchanged"] == 1
    assert len(store.jobs()) == 1


def test_source_kind_cannot_change_even_after_an_empty_successful_feed(store, tmp_path):
    path = tmp_path / "empty.json"
    path.write_text("[]")
    run_repository(store, "shared", str(path))
    with pytest.raises(ValueError, match="kind"):
        run_email(store, "shared", [message(tmp_path / "mail.eml")])


def test_independent_email_files_do_not_mark_previous_recommendations_missing(store, tmp_path):
    first = message(tmp_path / "first.eml")
    second = message(tmp_path / "second.eml", "two")
    assert run_email(store, "mail", [first])["created"] == 1
    report = run_email(store, "mail", [second, tmp_path / "missing.eml"])
    assert report["created"] == 1 and report["status"] == "partial"
    assert len(report["errors"]) == 1 and "missing.eml" in report["errors"][0]["path"]
    assert len(entries(store)) == 2
    assert {r["status"] for r in entries(store).values()} == {"unknown"}


def test_config_resolves_relative_files_and_email_globs_and_skips_disabled(store, tmp_path):
    directory = tmp_path / "inputs"
    directory.mkdir()
    (directory / "feed.json").write_text(json.dumps([row()]))
    message(directory / "first.eml")
    message(directory / "second.eml", "two")
    config = directory / "feeds.yaml"
    config.write_text("sources:\n  repo:\n    kind: repository\n    location: feed.json\n"
                      "  mail:\n    kind: email\n    paths: ['*.eml']\n"
                      "  paused:\n    enabled: false\n    kind: repository\n    location: missing.json\n")
    report = run_config(store, config)
    assert report["status"] == "ok"
    assert report["sources"]["repo"]["created"] == 1
    assert report["sources"]["mail"]["created"] == 2
    assert "paused" not in report["sources"]
    assert store.source_health("repo")["ingest"]["location"] == str(directory / "feed.json")


def test_config_skip_internships_drops_internship_rows_and_a_source_can_keep_them(store, tmp_path):
    (tmp_path / "feed.json").write_text(json.dumps([
        row("ng", title="Software Engineer, New Grad"), row("intern", title="Software Engineer Intern"),
        row("summer", title="Software Engineer (Summer 2027)"), row("typed", employment_type="INTERNSHIP")]))
    (tmp_path / "kept.json").write_text(json.dumps([row("data-intern", title="Data Intern")]))
    message(tmp_path / "job.eml")
    (tmp_path / "intern.eml").write_text(
        "Message-ID: <intern@example.com>\nSubject: Jobs\nContent-Type: text/html\n\n"
        '<a href="https://www.linkedin.com/jobs/view/300">Software Engineering Co-op</a>')
    config = tmp_path / "feeds.yaml"
    config.write_text("skip_internships: true\nsources:\n"
                      "  repo:\n    kind: repository\n    location: feed.json\n"
                      "  kept:\n    kind: repository\n    location: kept.json\n    skip_internships: false\n"
                      "  mail:\n    kind: email\n    paths: ['*.eml']\n")
    report = run_config(store, config)["sources"]
    assert (report["repo"]["records"], report["repo"]["created"], report["repo"]["skipped_internships"]) == (4, 1, 3)
    assert (report["kept"]["created"], report["kept"]["skipped_internships"]) == (1, 0)
    assert (report["mail"]["created"], report["mail"]["skipped_internships"]) == (1, 1)
    assert sorted(e["source"] for e in entries(store).values()) == ["kept", "mail", "repo"]
    assert {"ng", "data-intern"} <= set(entries(store)) and len(store.jobs()) == 3
    assert store.source_health("repo")["ingest_run"]["skipped_internships"] == 3


@pytest.mark.parametrize("setting", ["skip_internships: yes-please\nsources: {}\n",
                                     "sources:\n  repo:\n    kind: repository\n    location: feed.json\n"
                                     "    skip_internships: 1\n"])
def test_skip_internships_must_be_a_boolean(tmp_path, setting):
    config = tmp_path / "feeds.yaml"
    config.write_text(setting)
    with pytest.raises(ValueError, match="skip_internships must be a boolean"):
        load_feed_config(config)


def test_a_skipped_row_is_neither_renewed_nor_marked_missing(store, tmp_path):
    feed = tmp_path / "feed.json"
    feed.write_text(json.dumps([row("one", title="Software Engineer Intern")]))
    run_repository(store, "repo", str(feed))
    before = entries(store)["one"]
    feed.write_text(json.dumps([row("one", title="Software Engineer Intern"), row("two")]))
    again = run_repository(store, "repo", str(feed), skip_internships=True)
    assert (again["created"], again["skipped_internships"], again["missing"]) == (1, 1, 0)
    after = entries(store)["one"]
    assert (after["status"], after["last_seen_at"]) == (before["status"], before["last_seen_at"])
    feed.write_text(json.dumps([row("two")]))
    gone = run_repository(store, "repo", str(feed), skip_internships=True)
    assert gone["missing"] == 1 and entries(store)["one"]["status"] == "missing"


def test_config_failed_source_does_not_stop_an_independent_source(store, tmp_path):
    config = tmp_path / "feeds.yaml"
    config.write_text(f"sources:\n  broken:\n    kind: repository\n    location: {URL}\n"
                      "  good:\n    kind: repository\n    location: feed.json\n")
    (tmp_path / "feed.json").write_text(json.dumps([row()]))
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(500))) as client:
        report = run_config(store, config, client=client)
    assert report["status"] == "partial"
    assert report["sources"]["broken"]["status"] == "error"
    assert report["sources"]["good"]["created"] == 1
    assert "ingest" not in store.source_health("broken")


def test_unmatched_email_glob_reports_failure(store, tmp_path):
    config = tmp_path / "feeds.yaml"
    config.write_text("sources:\n  mail:\n    kind: email\n    paths: ['missing/*.eml']\n")
    report = run_config(store, config)
    assert report["status"] == "error"
    assert report["sources"]["mail"]["status"] == "error"
    assert entries(store) == {}


@pytest.mark.parametrize("partial_location", [URL, "https://example.com/other-feed.json"])
def test_server_rollback_after_partial_requires_full_body_even_across_origins(store, partial_location):
    calls = []
    stamp = "Thu, 01 Oct 2026 00:00:00 GMT"

    def handler(request):
        calls.append(request)
        if len(calls) == 2:
            return httpx.Response(200, json=[row(title="Partial title"), {"id": "broken"}],
                                  headers={"ETag": '"v2"', "Last-Modified": stamp})
        # The server has rolled back to v1. Old validators legitimately yield
        # 304, but the local database now contains part of the v2 snapshot.
        if request.headers.get("if-none-match") == '"v1"' or request.headers.get("if-modified-since") == stamp:
            return httpx.Response(304)
        return httpx.Response(200, json=[row()], headers={"ETag": '"v1"', "Last-Modified": stamp})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        run_repository(store, "repo", URL, client=client)
        partial = run_repository(store, "repo", partial_location, client=client)
        assert partial["updated"] == 1 and entries(store)["one"]["payload"]["title"] == "Partial title"
        repaired = run_repository(store, "repo", URL, client=client)
    assert entries(store)["one"]["payload"]["title"] == "Backend Engineer"
    assert repaired["status"] == "ok" and not repaired.get("not_modified")
    assert repaired["updated"] == 1
    assert "if-none-match" not in calls[-1].headers and "if-modified-since" not in calls[-1].headers
    assert store.source_health("repo")["ingest"]["etag"] == '"v1"'


def test_unsolicited_304_after_partial_does_not_refresh_observations(store):
    responses = [
        httpx.Response(200, json=[row()], headers={"ETag": '"v1"'}),
        httpx.Response(200, json=[row(title="Partial title"), {"id": "broken"}], headers={"ETag": '"v2"'}),
        httpx.Response(304),
    ]
    with httpx.Client(transport=httpx.MockTransport(lambda request: responses.pop(0))) as client:
        run_repository(store, "repo", URL, client=client)
        run_repository(store, "repo", URL, client=client)
        previous_seen = entries(store)["one"]["last_seen_at"]
        with pytest.raises(ValueError, match="304"):
            run_repository(store, "repo", URL, client=client)
    assert entries(store)["one"]["last_seen_at"] == previous_seen

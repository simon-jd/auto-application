"""Read-only provider API fixtures exercise real parsing, persistence and resume."""

import base64
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest
from sqlalchemy import select

from auto_app.core.db import tables as T
from auto_app.core.db.store import DbStore
from auto_app.ingest.mailbox import run_mailbox, validate_mailbox_config
from auto_app.settings import Paths


NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
RECEIVED = "2026-10-10T10:00:00+00:00"


@pytest.fixture
def store(tmp_path):
    value = DbStore(tmp_path / "jobs.db", Paths(home=tmp_path))
    yield value
    value.engine.dispose()


def definition(tmp_path, provider="gmail", **updates):
    credentials = tmp_path / f"{provider}.json"
    credentials.write_text(json.dumps({"provider": provider, "client_id": "fixture-client",
                                      "access_token": "fixture-access", "refresh_token": "fixture-refresh",
                                      "expires_at": 4102444800, "account_key": f"{provider}-account"}))
    scope = {"query": "from:jobs@example.com"} if provider == "gmail" else {"folder": "inbox"}
    return {"kind": "mailbox", "provider": provider, "credentials": str(credentials), **scope, **updates}


def mime(message_id="one", job_id="123", *, no_jobs=False):
    body = "No recommendations today" if no_jobs else (
        f'<a href="https://www.linkedin.com/jobs/view/{job_id}">Backend Engineer</a>')
    return (f"Message-ID: <{message_id}@example.com>\nSubject: Recommended jobs\n"
            "Date: Sat, 10 Oct 2026 03:00:00 -0700\nContent-Type: text/html; charset=utf-8\n\n" + body).encode()


def gmail_message(message_id="one", job_id="123", *, no_jobs=False):
    return {"id": message_id, "threadId": "thread-1", "labelIds": ["INBOX", "UNREAD"],
            "internalDate": "1791626400000", "raw": base64.urlsafe_b64encode(
                mime(message_id, job_id, no_jobs=no_jobs)).decode().rstrip("=")}


def receipts(store):
    with store.engine.connect() as conn:
        return [dict(row) for row in conn.execute(select(T.mail_messages)).mappings()]


def entries(store):
    with store.engine.connect() as conn:
        return [dict(row) for row in conn.execute(select(T.ingest_entries)).mappings()]


def test_gmail_import_keeps_provenance_and_no_job_receipts_without_marking_read(store, tmp_path):
    config = definition(tmp_path, label_ids=["Label_Jobs"])
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer fixture-access"
        if request.url.path.endswith("/messages"):
            assert request.url.params["q"].startswith("(from:jobs@example.com) after:")
            assert request.url.params["labelIds"] == "Label_Jobs"
            return httpx.Response(200, json={"messages": [{"id": "one", "threadId": "t1"},
                                                         {"id": "empty", "threadId": "t2"}],
                                            "resultSizeEstimate": 2})
        assert request.url.params["format"] == "raw"
        message_id = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=gmail_message(message_id, no_jobs=message_id == "empty"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = run_mailbox(store, "gmail-jobs", config, client=client, now=NOW)
        again = run_mailbox(store, "gmail-jobs", config, client=client, now=NOW + timedelta(minutes=30))
    assert first["status"] == "ok" and first["messages_processed"] == 2
    assert first["created"] == 1 and first["jobs_created"] == 1
    assert again["messages_processed"] == 0 and again["messages_skipped"] == 2
    assert len(calls) == 4  # Two list calls; each message fetched only once.
    assert len(store.jobs()) == 1
    assert sorted(row["job_links"] for row in receipts(store)) == [0, 1]
    metadata = entries(store)[0]["payload"]["metadata"]["mailbox"]
    assert metadata == {"provider": "gmail", "account_key": "gmail-account", "message_id": "one",
                        "received_at": RECEIVED}
    assert first["checkpoint"]["watermark"] == NOW.isoformat()
    assert again["checkpoint"]["window_start"] == (NOW - timedelta(seconds=120)).isoformat()


def test_gmail_pagination_cap_resumes_same_window_before_new_scan(store, tmp_path):
    config = definition(tmp_path, max_pages=1)
    seen_windows = []

    def handler(request):
        if request.url.path.endswith("/messages"):
            seen_windows.append(request.url.params["q"])
            if request.url.params.get("pageToken") == "next-page":
                return httpx.Response(200, json={"messages": [{"id": "two", "threadId": "t2"}]})
            return httpx.Response(200, json={"messages": [{"id": "one", "threadId": "t1"}],
                                            "nextPageToken": "next-page"})
        message_id = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=gmail_message(message_id, "123" if message_id == "one" else "456"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = run_mailbox(store, "gmail-jobs", config, client=client, now=NOW)
        assert first["has_more"] and first["checkpoint"]["next_page"] == "next-page"
        assert first["checkpoint"]["watermark"] is None
        second = run_mailbox(store, "gmail-jobs", config, client=client, now=NOW + timedelta(hours=2))
    assert seen_windows[0] == seen_windows[1]
    assert second["status"] == "ok" and not second["has_more"]
    assert second["checkpoint"]["watermark"] == NOW.isoformat()
    assert len(receipts(store)) == 2 and len(store.jobs()) == 2


def test_failed_message_retries_page_skipping_successful_receipts(store, tmp_path):
    config = definition(tmp_path)
    fail = True
    fetches = []

    def handler(request):
        if request.url.path.endswith("/messages"):
            assert not request.url.params.get("pageToken")
            return httpx.Response(200, json={"messages": [{"id": "one"}, {"id": "two"}]})
        message_id = request.url.path.rsplit("/", 1)[-1]
        fetches.append(message_id)
        if fail and message_id == "two":
            return httpx.Response(503, text="SECRET-ERROR-CONTENT")
        return httpx.Response(200, json=gmail_message(message_id, "123" if message_id == "one" else "456"))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        failed = run_mailbox(store, "gmail-jobs", config, client=client, now=NOW)
        assert failed["status"] == "partial" and failed["has_more"]
        assert failed["checkpoint"]["watermark"] is None
        assert "SECRET" not in json.dumps(failed)
        fail = False
        retry = run_mailbox(store, "gmail-jobs", config, client=client, now=NOW + timedelta(minutes=30))
    assert fetches == ["one", "two", "two"]
    assert retry["messages_skipped"] == 1 and retry["messages_processed"] == 1
    assert retry["checkpoint"]["watermark"] == NOW.isoformat()
    assert len(receipts(store)) == 2 and len(store.jobs()) == 2


def test_failed_later_page_does_not_rewind_completed_page_or_advance_watermark(store, tmp_path):
    config = definition(tmp_path)
    fail = True
    tokens = []

    def handler(request):
        if request.url.path.endswith("/messages"):
            page = request.url.params.get("pageToken")
            tokens.append(page)
            if page:
                return httpx.Response(429, text="SECRET") if fail else httpx.Response(200, json={})
            return httpx.Response(200, json={"messages": [], "nextPageToken": "p2"})
        raise AssertionError("No MIME expected")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        failed = run_mailbox(store, "gmail-jobs", config, client=client, now=NOW)
        assert failed["status"] == "error" and failed["checkpoint"]["next_page"] == "p2"
        fail = False
        retry = run_mailbox(store, "gmail-jobs", config, client=client, now=NOW + timedelta(hours=1))
    assert tokens == [None, "p2", "p2"]
    assert retry["status"] == "ok" and retry["checkpoint"]["watermark"] == NOW.isoformat()


def test_scope_change_restarts_lookback_but_does_not_duplicate_message(store, tmp_path):
    config = definition(tmp_path, max_pages=1)
    queries = []

    def handler(request):
        if request.url.path.endswith("/messages"):
            queries.append(dict(request.url.params))
            return httpx.Response(200, json={"messages": [{"id": "one"}], "nextPageToken": "p2"})
        return httpx.Response(200, json=gmail_message())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = run_mailbox(store, "gmail-jobs", config, client=client, now=NOW)
        second = run_mailbox(store, "gmail-jobs", {**config, "query": "from:other@example.com"},
                             client=client, now=NOW + timedelta(hours=1))
    assert first["checkpoint"]["scope_hash"] != second["checkpoint"]["scope_hash"]
    assert "pageToken" not in queries[1] and "from:other@example.com" in queries[1]["q"]
    assert second["messages_skipped"] == 1 and len(receipts(store)) == 1
    assert second["checkpoint"]["window_start"] == (NOW + timedelta(hours=1) - timedelta(days=14)).isoformat()


def test_provider_account_change_keeps_distinct_observations_but_same_job(store, tmp_path):
    config = definition(tmp_path)

    def handler(request):
        return httpx.Response(200, json={"messages": [{"id": "one"}]}) if request.url.path.endswith(
            "/messages") else httpx.Response(200, json=gmail_message())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = run_mailbox(store, "mail", config, client=client, now=NOW)
        path = tmp_path / "gmail.json"
        credentials = json.loads(path.read_text())
        credentials["account_key"] = "another-account"
        path.write_text(json.dumps(credentials))
        second = run_mailbox(store, "mail", config, client=client, now=NOW)
    assert first["checkpoint"]["scope_hash"] != second["checkpoint"]["scope_hash"]
    assert second["messages_processed"] == 1
    assert len(receipts(store)) == 2 and len(entries(store)) == 2 and len(store.jobs()) == 1


def test_outlook_folder_sender_scope_and_opaque_next_link_get_mime(store, tmp_path):
    config = definition(tmp_path, "outlook", folder="folder+id", senders=["jobs@example.com", "j'ob@example.com"])
    next_link = "https://graph.microsoft.com/v1.0/me/mailFolders/folder%2Bid/messages?$skip=3&$top=50"
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.headers["Prefer"] == 'IdType="ImmutableId"'
        if request.url.path.endswith("/$value"):
            return httpx.Response(200, content=mime("same-message"), headers={"Content-Type": "text/plain"})
        if request.url.params.get("$skip"):
            assert str(request.url) == next_link
            return httpx.Response(200, json={"@odata.context": "https://graph.microsoft.com/v1.0/$metadata#messages", "value": []})
        assert request.url.path.endswith("/mailFolders/folder+id/messages")
        query = request.url.params["$filter"]
        assert "receivedDateTime ge 2026-09-26T12:00:00Z" in query
        assert "receivedDateTime lt 2026-10-10T12:00:00Z" in query
        assert "from/emailAddress/address eq 'j''ob@example.com'" in query
        return httpx.Response(200, json={"@odata.context": "https://graph.microsoft.com/v1.0/$metadata#messages",
                                        "value": [{"id": "A+/=", "receivedDateTime": "2026-10-10T10:00:00Z"}],
                                        "@odata.nextLink": next_link})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = run_mailbox(store, "outlook-jobs", config, client=client, now=NOW)
    assert report["status"] == "ok" and report["messages_processed"] == 1 and not report["has_more"]
    assert len(calls) == 3 and len(store.jobs()) == 1
    assert receipts(store)[0]["provider"] == "outlook"
    assert entries(store)[0]["payload"]["metadata"]["mailbox"]["received_at"] == RECEIVED


def test_malformed_message_does_not_receive_receipt_or_advance_window(store, tmp_path):
    config = definition(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=(
        {"messages": [{"id": "one"}]} if request.url.path.endswith("/messages") else
        {"id": "one", "raw": "%%%invalid", "internalDate": "1791626400000"})))) as client:
        report = run_mailbox(store, "mail", config, client=client, now=NOW)
    assert report["status"] == "error" and report["has_more"]
    assert receipts(store) == [] and report["checkpoint"]["watermark"] is None


def test_source_kind_collision_cannot_overwrite_existing_repository_health(store, tmp_path):
    store.update_health("mail", ingest={"kind": "repository", "revision": "sha256:old"})
    config = definition(tmp_path)
    with pytest.raises(ValueError, match="kind"):
        run_mailbox(store, "mail", config, now=NOW)
    assert store.source_health("mail")["ingest"]["kind"] == "repository"


@pytest.mark.parametrize("provider, scope", [("gmail", {"query": ""}), ("outlook", {"folder": ""})])
def test_explicit_scope_required(tmp_path, provider, scope):
    with pytest.raises(ValueError, match="scope"):
        validate_mailbox_config(definition(tmp_path, provider, **scope), tmp_path)


def test_validate_paths_defaults_and_provider_limits(tmp_path):
    config = validate_mailbox_config({"kind": "mailbox", "provider": "gmail", "credentials": "auth.json",
                                      "label_ids": ["Label_Jobs"]}, tmp_path)
    assert config["credentials"] == str(tmp_path / "auth.json")
    assert (config["lookback_days"], config["page_size"], config["max_pages"]) == (14, 50, 4)
    for key, value in (("lookback_days", 0), ("max_pages", True), ("page_size", 501)):
        with pytest.raises(ValueError, match=key):
            validate_mailbox_config({**config, key: value}, tmp_path)


def test_outlook_cannot_follow_cursor_to_another_origin(store, tmp_path):
    config = definition(tmp_path, "outlook")
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
        "value": [], "@odata.nextLink": "https://wrong.example/steal"}))) as client:
        report = run_mailbox(store, "mail", config, client=client, now=NOW)
    assert report["status"] == "error" and report["checkpoint"]["watermark"] is None
    assert report["checkpoint"].get("next_page") is None


def test_mail_without_date_uses_provider_received_time_not_import_time(store, tmp_path):
    config = definition(tmp_path)
    undated = mime().replace(b"Date: Sat, 10 Oct 2026 03:00:00 -0700\n", b"")

    def handler(request):
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "one"}]})
        return httpx.Response(200, json={**gmail_message(), "raw": base64.urlsafe_b64encode(undated).decode()})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        run_mailbox(store, "mail", config, client=client, now=NOW)
    assert entries(store)[0]["first_seen_at"] == datetime(2026, 10, 10, 10)
    assert entries(store)[0]["payload"]["metadata"]["observed_at"] == RECEIVED


def test_expired_cursor_restarts_same_window_without_losing_receipts(store, tmp_path):
    config = definition(tmp_path, max_pages=1)
    fail_cursor = True
    seen = []

    def handler(request):
        if request.url.path.endswith("/messages"):
            page = request.url.params.get("pageToken")
            seen.append((page, request.url.params["q"]))
            if page:
                return httpx.Response(400) if fail_cursor else httpx.Response(200, json={})
            return httpx.Response(200, json={"messages": [{"id": "one"}], "nextPageToken": "p2"})
        return httpx.Response(200, json=gmail_message())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        run_mailbox(store, "mail", config, client=client, now=NOW)
        failed = run_mailbox(store, "mail", config, client=client, now=NOW + timedelta(hours=1))
        assert failed["status"] == "error" and failed["checkpoint"]["next_page"] is None
        assert failed["checkpoint"]["watermark"] is None
        fail_cursor = False
        replay = run_mailbox(store, "mail", config, client=client, now=NOW + timedelta(hours=2))
        finished = run_mailbox(store, "mail", config, client=client, now=NOW + timedelta(hours=3))
    assert [page for page, query in seen] == [None, "p2", None, "p2"]
    assert len({query for page, query in seen}) == 1
    assert replay["messages_skipped"] == 1 and len(receipts(store)) == 1
    assert finished["checkpoint"]["watermark"] == NOW.isoformat()


@pytest.mark.parametrize("gone_status", [404, 410])
def test_message_deleted_after_listing_does_not_block_following_mail(store, tmp_path, gone_status):
    config = definition(tmp_path)

    def handler(request):
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "gone"}, {"id": "one"}]})
        if request.url.path.endswith("/gone"):
            return httpx.Response(gone_status)
        return httpx.Response(200, json=gmail_message())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        report = run_mailbox(store, "mail", config, client=client, now=NOW)
        again = run_mailbox(store, "mail", config, client=client, now=NOW + timedelta(minutes=30))
    assert report["status"] == "ok" and not report["has_more"]
    assert report["messages_gone"] == 1 and report["messages_processed"] == 1
    assert report["warnings"][0]["message_id"] == "gone"
    gone = next(row for row in receipts(store) if row["message_id"] == "gone")
    assert gone["job_links"] == 0 and gone["received_at"] is None
    assert len(store.jobs()) == 1 and entries(store)[0]["status"] == "unknown"
    assert again["messages_skipped"] == 2

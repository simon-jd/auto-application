import base64
from datetime import timedelta
import json

import httpx
import pytest
from sqlalchemy import select

from auto_app.core.db import tables as T
from auto_app.ingest.mailbox import run_mailbox, validate_mailbox_config
from tests.ingest.test_mailbox import definition, receipts
from tests.track.test_updates import NOW, mail, store


def test_updates_branch_receipts_without_job_body_and_replays_changed_purpose(store, tmp_path):
    content = mail("Beta <jobs@beta.com>", "Your application", "We have decided to move forward with other candidates. PRIVATE-BODY")
    fetches = []
    def handler(request):
        assert request.method == "GET"
        if request.url.path.endswith('/messages'):
            return httpx.Response(200, json={"messages": [{"id": "one"}]})
        fetches.append(request)
        return httpx.Response(200, json={"raw": base64.urlsafe_b64encode(content).decode(),
            "internalDate": str(int((NOW - timedelta(hours=1)).timestamp() * 1000))})
    config = definition(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        first = run_mailbox(store, "gmail-applications", config, client=client, now=NOW)
        assert first["messages_processed"] == 1
        second = run_mailbox(store, "gmail-applications", {**config, "purpose": "updates"}, client=client, now=NOW + timedelta(minutes=1))
        third = run_mailbox(store, "gmail-applications", {**config, "purpose": "updates"}, client=client, now=NOW + timedelta(minutes=2))
    assert second["messages_processed"] == 1 and third["messages_skipped"] == 1
    assert len(fetches) == 2
    assert first["checkpoint"]["scope_hash"] != second["checkpoint"]["scope_hash"]
    with store.engine.connect() as conn:
        updates = list(conn.execute(select(T.mail_updates)).mappings())
        assert len(updates) == 1 and updates[0]["status"] == "matched"
        assert "PRIVATE-BODY" not in str(updates)
        assert list(conn.execute(select(T.ingest_entries).where(T.ingest_entries.c.source == "gmail-applications"))) == []
    assert receipts(store)[0]["job_links"] == 0
    assert "PRIVATE-BODY" not in json.dumps(second)


def test_receipt_failure_rolls_back_update_and_event(store, monkeypatch):
    from auto_app.ingest import mailbox
    from auto_app.track.events import events_for
    from tests.imported_jobs import job_id_of
    content = mail("Beta <jobs@beta.com>", "Your application", "We have decided to move forward with other candidates.")
    monkeypatch.setattr(mailbox, '_message', lambda *args: (content, NOW))
    def fail(*args, **kwargs):
        with store._conn() as conn:
            assert len(list(conn.execute(select(T.mail_updates)))) == 1
        raise RuntimeError("receipt failure")
    monkeypatch.setattr(mailbox, '_receipt', fail)
    with pytest.raises(RuntimeError):
        mailbox._import_message(store, "mail", {"provider": "gmail", "purpose": "updates"},
            "account", {"id": "one"}, None, {}, NOW)
    with store.engine.connect() as conn:
        assert list(conn.execute(select(T.mail_updates))) == []
        assert list(conn.execute(select(T.mail_messages))) == []
    assert [event["kind"] for event in events_for(store, job_id_of(store, "ML Engineer"))] == ["applied"]


def test_purpose_validation(tmp_path):
    assert validate_mailbox_config(definition(tmp_path, purpose="updates"), tmp_path)["purpose"] == "updates"
    with pytest.raises(ValueError, match="purpose"):
        validate_mailbox_config(definition(tmp_path, purpose="bad"), tmp_path)


def test_malformed_update_mail_is_receipted_instead_of_retried(store, monkeypatch):
    from auto_app.ingest import mailbox
    content = (b"From: Beta <jobs@beta.com>\r\nSubject: Your application\r\nMIME-Version: 1.0\r\n"
               b"Content-Type: multipart/alternative; boundary=\"b1\"\r\n\r\n--b1\r\n"
               b"Content-Type: text/plain\r\n\r\nWe have decided to move forward with other candidates.\r\n")
    monkeypatch.setattr(mailbox, '_message', lambda *args: (content, NOW))
    assert mailbox._import_message(store, "mail", {"provider": "gmail", "purpose": "updates"},
        "account", {"id": "one"}, None, {}, NOW) == {}
    with store.engine.connect() as conn:
        assert len(list(conn.execute(select(T.mail_messages)))) == 1
        assert list(conn.execute(select(T.mail_updates))) == []

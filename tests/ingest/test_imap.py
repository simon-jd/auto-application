"""Read-only IMAP fixtures exercise credentials, receipts and restartable scans."""
import imaplib
import json
import os
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import pytest
from sqlalchemy import select

from auto_app.core.db import tables as T
from auto_app.ingest.imap import ImapError, ImapSession, load_imap_credentials
from auto_app.ingest.mailbox import run_mailbox, validate_mailbox_config
from tests.imported_jobs import make_store

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


def alert(job_id="4012345678"):
    message = EmailMessage()
    message["From"] = "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>"
    message["Subject"] = "Software Engineer, New Grad at Acme"
    message["Date"] = "Mon, 12 Oct 2026 10:00:00 +0000"
    message.set_content(f"Software Engineer, New Grad\nAcme\nhttps://www.linkedin.com/comm/jobs/view/{job_id}/\n")
    return message.as_bytes()


class FakeConnection:
    def __init__(self, messages, validity="777"):
        self.messages, self.calls, self.validity = messages, [], validity

    def login(self, user, password):
        self.calls.append(("login", user))
        return "OK", [b"ok"]

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        return code, [self.validity.encode()]

    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        if command == "SEARCH":
            return "OK", [" ".join(self.messages).encode()]
        uid, items = args
        if uid not in self.messages:
            return "OK", [None]
        stamp = b'INTERNALDATE "12-Oct-2026 10:00:00 +0000"'
        return "OK", [(b"1 (UID " + uid.encode() + b" " + stamp + b" BODY[] {9}", self.messages[uid]), b")"]

    def logout(self):
        self.calls.append(("logout",))


def credentials(tmp_path, mode=0o600, **updates):
    path = tmp_path / "imap.json"
    data = {"host": "imap.example.com", "username": "me@example.com", "password": "app-pass"}
    data.update(updates)
    path.write_text(json.dumps(data))
    os.chmod(path, mode)
    return path


def definition(tmp_path, **updates):
    return {"provider": "imap", "credentials": str(credentials(tmp_path)), "folder": "INBOX",
            "senders": ["jobalerts-noreply@linkedin.com"], **updates}


@pytest.fixture
def store(tmp_path):
    value = make_store(tmp_path)
    yield value
    value.engine.dispose()


def run(store, config, fake, now=NOW):
    with ImapSession(load_imap_credentials(config["credentials"]), connect=lambda *a, **k: fake) as session:
        return run_mailbox(store, "gmail-alerts", config, client=session, now=now)


def receipts(store):
    with store.engine.connect() as conn:
        return list(conn.execute(select(T.mail_messages)).mappings())


def test_credentials_must_be_private_and_complete(tmp_path):
    assert load_imap_credentials(credentials(tmp_path))["port"] == 993
    with pytest.raises(ImapError, match="chmod 600"):
        load_imap_credentials(credentials(tmp_path, 0o644))
    for update in ({"password": ""}, {"host": "bad\r\nhost"}, {"port": True}, {"username": None}):
        with pytest.raises(ImapError) as failure:
            load_imap_credentials(credentials(tmp_path, **update))
        assert "app-pass" not in str(failure.value)
    path = credentials(tmp_path)
    path.write_text('{"password":"app-pass", invalid json')
    with pytest.raises(ImapError) as failure:
        load_imap_credentials(path)
    assert "app-pass" not in str(failure.value) and failure.value.__cause__ is None


@pytest.mark.parametrize("mode", [0o400, 0o700])
def test_credentials_require_exact_private_mode(tmp_path, mode):
    with pytest.raises(ImapError, match="chmod 600"):
        load_imap_credentials(credentials(tmp_path, mode))


def test_credentials_reject_symlink(tmp_path):
    real = credentials(tmp_path)
    link = tmp_path / "linked.json"
    link.symlink_to(real)
    with pytest.raises(ImapError):
        load_imap_credentials(link)


def test_config_validation_does_not_hide_credential_symlinks(tmp_path):
    real = credentials(tmp_path)
    link = tmp_path / "linked.json"
    link.symlink_to(real)
    config = validate_mailbox_config({"provider": "imap", "credentials": str(link), "senders": ["linkedin.com"]},
                                     tmp_path)
    with pytest.raises(ImapError):
        load_imap_credentials(config["credentials"])


@pytest.mark.parametrize("internal_date,query_start,query_end", [
    ("11-Oct-2026 20:15:00 -0700", "2026-10-12T02:58:00+00:00", "2026-10-12T03:30:00+00:00"),
    ("13-Oct-2026 10:15:00 +1400", "2026-10-12T20:00:00+00:00", "2026-10-12T20:30:00+00:00"),
])
def test_date_search_covers_internaldate_timezone_boundaries(tmp_path, internal_date, query_start, query_end):
    class DateSearch(FakeConnection):
        def uid(self, command, *args):
            if command == "SEARCH":
                since = datetime.strptime(args[args.index("SINCE") + 1], "%d-%b-%Y").date()
                before = datetime.strptime(args[args.index("BEFORE") + 1], "%d-%b-%Y").date()
                day = datetime.strptime(internal_date, "%d-%b-%Y %H:%M:%S %z").date()
                return "OK", [b"5" if since <= day < before else b""]
            return super().uid(command, *args)

    with ImapSession(load_imap_credentials(credentials(tmp_path)), connect=lambda *a, **k: DateSearch({"5": alert()})) as session:
        session.select("INBOX")
        assert session.uids({"senders": []}, datetime.fromisoformat(query_start),
                            datetime.fromisoformat(query_end)) == ["5"]


def test_session_is_read_only_and_never_marks_seen(tmp_path):
    fake = FakeConnection({"5": alert()})
    with ImapSession(load_imap_credentials(credentials(tmp_path)), connect=lambda *a, **k: fake) as session:
        session.select("INBOX")
        uids = session.uids({"folder": "INBOX", "senders": ["a@x.com", "b@y.com"], "gmail_raw": None},
                            datetime(2026, 10, 1, tzinfo=timezone.utc), NOW)
        content, received = session.fetch(uids[0])
    assert uids == ["5"] and content.startswith(b"From:") and received == NOW - timedelta(hours=2)
    assert ("select", '"INBOX"', True) in fake.calls
    search = next(c for c in fake.calls if c[:2] == ("uid", "SEARCH"))
    assert search[2:] == ("SINCE", "30-Sep-2026", "BEFORE", "14-Oct-2026", "OR", "FROM", '"a@x.com"', "FROM", '"b@y.com"')
    fetch = next(c for c in fake.calls if c[:2] == ("uid", "FETCH"))
    assert fetch[3] == "(INTERNALDATE BODY.PEEK[])"
    assert not any("STORE" in str(c) for c in fake.calls)


@pytest.mark.parametrize("operation", ["login", "select", "response", "uid"])
def test_provider_failures_never_include_supplied_password(tmp_path, operation):
    fake = FakeConnection({"5": alert()})
    def refuses(*a, **k):
        raise imaplib.IMAP4.error("bad password app-pass")
    setattr(fake, operation, refuses)
    with pytest.raises(ImapError) as failure:
        with ImapSession(load_imap_credentials(credentials(tmp_path)), connect=lambda *a, **k: fake) as session:
            session.select("INBOX")
            session.uids({"folder": "INBOX", "senders": []}, NOW, NOW)
    assert "app-pass" not in str(failure.value) and failure.value.__cause__ is None
    assert ("logout",) in fake.calls


def test_login_no_is_rejected(tmp_path):
    fake = FakeConnection({})
    fake.login = lambda *a: ("NO", [b"app-pass"])
    with pytest.raises(ImapError):
        with ImapSession(load_imap_credentials(credentials(tmp_path)), connect=lambda *a, **k: fake):
            pass


def test_an_imap_source_imports_alert_links_once(store, tmp_path):
    config = definition(tmp_path)
    first = run(store, config, FakeConnection({"5": alert(), "6": alert("4012345679")}))
    again = run(store, config, FakeConnection({"5": alert(), "6": alert("4012345679")}), NOW + timedelta(hours=1))
    assert first["status"] == "ok" and first["messages_processed"] == 2 and first["created"] == 2
    assert again["created"] == 0 and again["messages_skipped"] == 2
    assert {row["message_id"] for row in receipts(store)} == {"777:5", "777:6"}
    assert first["checkpoint"]["uidvalidity"] == "777"


def test_resume_after_deleted_previous_uid_does_not_skip_mail(store, tmp_path):
    config = definition(tmp_path, page_size=1, max_pages=1)
    first = run(store, config, FakeConnection({"5": alert(), "6": alert("4012345679"), "7": alert("4012345680")}))
    second = run(store, config, FakeConnection({"6": alert("4012345679"), "7": alert("4012345680")}), NOW + timedelta(hours=1))
    assert first["has_more"] and first["messages_processed"] == 1
    assert second["messages_processed"] == 1
    assert {row["message_id"] for row in receipts(store)} == {"777:5", "777:6"}
    assert second["checkpoint"]["window_end"] == NOW.isoformat()


def test_uidvalidity_change_restarts_window_and_retires_stale_retries(store, tmp_path):
    config = definition(tmp_path, page_size=1, max_pages=1)
    first_fake = FakeConnection({"5": alert(), "6": alert("4012345679")})
    first = run(store, config, first_fake)
    cp = first["checkpoint"]
    cp["message_retries"]["stale"] = {"provider": "imap", "account_key": cp["account_key"],
        "message": {"id": "777:6"}, "deferred": True, "attempts": 3, "next_retry_at": NOW.isoformat()}
    store.update_health("gmail-alerts", mailbox=cp)
    fake = FakeConnection({"1": alert("4012345680"), "2": alert("4012345681")}, validity="888")
    second = run(store, config, fake, NOW + timedelta(hours=1))
    assert second["messages_processed"] == 1 and second["messages_pending"] == 0
    assert second["checkpoint"]["uidvalidity"] == "888"
    assert {row["message_id"] for row in receipts(store)} == {"777:5", "888:1"}
    assert not any(call[:3] == ("uid", "FETCH", "6") for call in fake.calls)


def test_failed_fetch_remains_retryable_and_report_is_private(store, tmp_path):
    config = definition(tmp_path)
    fake = FakeConnection({"5": alert()})
    original = fake.uid
    def fails(command, *args):
        if command == "FETCH":
            raise imaplib.IMAP4.error("app-pass")
        return original(command, *args)
    fake.uid = fails
    failed = run(store, config, fake)
    assert failed["status"] == "error" and failed["messages_pending"] == 1
    assert "app-pass" not in json.dumps(failed)
    again = run(store, config, FakeConnection({"5": alert()}), NOW + timedelta(minutes=30))
    assert again["messages_processed"] == 1 and again["messages_pending"] == 0


def test_missing_uidvalidity_is_rejected(tmp_path):
    fake = FakeConnection({}, validity="")
    with pytest.raises(ImapError):
        with ImapSession(load_imap_credentials(credentials(tmp_path)), connect=lambda *a, **k: fake) as session:
            session.select("INBOX")


def test_config_rejects_unscoped_or_control_character_strings(tmp_path):
    for updates in ({"senders": []}, {"folder": "INBOX\r\nSTORE"}, {"senders": ["a\r\nb"]}, {"page_size": 501}):
        with pytest.raises(ValueError):
            validate_mailbox_config(definition(tmp_path, **updates), tmp_path)


def test_scope_change_preserves_same_namespace_deferred_retry(store, tmp_path):
    config = definition(tmp_path, page_size=1, max_pages=1)
    first = run(store, config, FakeConnection({"5": alert(), "6": alert("4012345679")}))
    cp = first["checkpoint"]
    cp["message_retries"]["same-namespace"] = {"provider": "imap", "account_key": cp["account_key"],
        "message": {"id": "777:6"}, "deferred": True, "attempts": 3,
        "next_retry_at": (NOW + timedelta(hours=24)).isoformat()}
    store.update_health("gmail-alerts", mailbox=cp)
    report = run(store, {**config, "senders": ["other@example.com"]},
                 FakeConnection({"5": alert()}), NOW + timedelta(hours=1))
    assert report["messages_pending"] == 1
    assert "same-namespace" in report["checkpoint"]["message_retries"]


def test_daily_search_does_not_receipt_messages_after_fixed_window(store, tmp_path):
    config = definition(tmp_path)
    fake = FakeConnection({"5": alert()})
    original = fake.uid
    def at_upper_bound(command, *args):
        status, data = original(command, *args)
        if command == "FETCH":
            header, content = data[0]
            data[0] = (header.replace(b"10:00:00", b"12:00:00"), content)
        return status, data
    fake.uid = at_upper_bound
    first = run(store, config, fake)
    assert not receipts(store) and first["created"] == 0
    second = run(store, config, fake, NOW + timedelta(minutes=30))
    assert second["created"] == 1 and len(receipts(store)) == 1

"""Read scoped Gmail/Outlook/IMAP mail with durable, bounded scan windows."""

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import quote, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from auto_app.core.db import tables as T
from auto_app.ingest.email import EMAIL_PARSER_VERSION, parse_email
from auto_app.ingest.service import IngestService


COUNTERS = ("records", "created", "updated", "unchanged", "restored", "inactive", "missing", "jobs_created")
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
GRAPH = "https://graph.microsoft.com/v1.0/me"
MESSAGE_PAGE_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1800


class MailboxError(ValueError):
    """Provider failure text that contains neither response bodies nor tokens."""

    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def validate_mailbox_config(definition: dict, base: Path) -> dict:
    provider = definition.get("provider")
    if provider not in {"gmail", "outlook", "imap"}:
        raise ValueError("mailbox provider must be gmail, outlook or imap")
    credentials = definition.get("credentials")
    if not isinstance(credentials, str) or not credentials.strip():
        raise ValueError("mailbox credentials must be a file path")
    path = Path(credentials).expanduser()
    if not path.is_absolute():
        path = base / path
    # Keep an IMAP credential symlink visible to the descriptor-based private-file check.
    credential_path = path.absolute() if provider == "imap" else path.resolve()
    result = {"kind": "mailbox", "provider": provider, "credentials": str(credential_path)}
    for key, default, maximum in (("lookback_days", 14, None), ("page_size", 50, 500 if provider in {"gmail", "imap"} else 1000),
                                  ("max_pages", 4, None)):
        value = definition.get(key, default)
        if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
            raise ValueError(f"mailbox {key} must be a positive integer" + (f" no greater than {maximum}" if maximum else ""))
        result[key] = value
    string_key, list_key = {"gmail": ("query", "label_ids"), "outlook": ("folder", "senders"),
                            "imap": ("gmail_raw", "senders")}[provider]
    string = definition.get(string_key, "")
    values = definition.get(list_key, [])
    if not isinstance(string, str):
        raise ValueError(f"mailbox {string_key} must be a string")
    if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError(f"mailbox {list_key} must be a list of nonempty strings")
    result[string_key] = string.strip()
    result[list_key] = sorted(set(value.strip() for value in values))
    if not result[string_key] and not result[list_key]:
        raise ValueError(f"mailbox scope requires {string_key} or {list_key}")
    if provider == "imap":
        folder = definition.get("folder", "INBOX")
        if not isinstance(folder, str) or not folder.strip():
            raise ValueError("imap folder must be a nonempty string")
        if any(char in value for value in [folder, string, *values] for char in "\r\n\x00"):
            raise ValueError("imap scope must not contain control characters")
        result["folder"] = folder.strip()
    purpose = definition.get("purpose", "jobs")
    if purpose not in ("jobs", "updates"):
        raise ValueError("mailbox purpose must be jobs or updates")
    result["purpose"] = purpose
    return result


def _date(value: str | datetime) -> datetime:
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _stamp(value: str) -> str:
    return _date(value).isoformat().replace("+00:00", "Z")


def _account_key(path: Path) -> str:
    try:
        credentials = json.loads(path.read_text())
        key = credentials.get("account_key")
    except (OSError, ValueError, AttributeError) as exc:
        raise MailboxError("Cannot read mailbox credentials; run mailbox authorization again") from exc
    if isinstance(key, str) and key.strip():
        return key
    return hashlib.sha256(str(path.resolve()).encode()).hexdigest()


def _check_source(store, source: str) -> None:
    if not isinstance(source, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", source):
        raise ValueError("source must contain lowercase letters, numbers, underscores or hyphens")
    if store.source_health(source).get("ingest", {}).get("kind") not in (None, "email"):
        raise ValueError(f"{source} is already used by a different source kind")
    with store.engine.connect() as conn:
        kinds = set(conn.execute(select(T.ingest_entries.c.kind).where(T.ingest_entries.c.source == source)).scalars())
    if kinds - {"email"}:
        raise ValueError(f"{source} is already used by a different source kind")


def _request(client, url, headers, *, params=None):
    try:
        response = client.get(url, headers=headers, params=params, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise MailboxError("Mailbox request failed due to a network error; retry next run") from exc
    if not 200 <= response.status_code < 300:
        action = "authorize the mailbox again" if response.status_code in (401, 403) else "retry next run"
        raise MailboxError(f"Mailbox request failed (HTTP {response.status_code}); {action}",
                           status_code=response.status_code)
    return response


def _json(response):
    try:
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except ValueError as exc:
        raise MailboxError("Mailbox returned an invalid JSON response; retry next run") from exc


def _graph_cursor(value):
    if not value:
        return None
    if not isinstance(value, str):
        raise MailboxError("Outlook returned an invalid page link; retry next run")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.netloc != "graph.microsoft.com"
            or not parsed.path.startswith("/v1.0/")):
        raise MailboxError("Outlook returned an invalid page link; retry next run")
    return value


def _page(client, config, checkpoint, headers):
    cursor = checkpoint["next_page"]
    if config["provider"] == "imap":
        from auto_app.ingest.imap import ImapError

        try:
            uids = client.uids(config, _date(checkpoint["window_start"]), _date(checkpoint["window_end"]))
            after = int(cursor) if cursor else 0
            remaining = [uid for uid in uids if int(uid) > after]
            page = remaining[:config["page_size"]]
            nxt = page[-1] if len(remaining) > len(page) else None
            return [{"id": f"{client.uidvalidity}:{uid}"} for uid in page], nxt
        except (ImapError, ValueError, TypeError):
            raise MailboxError("IMAP listing failed; retry next run") from None
    elif config["provider"] == "gmail":
        lower = int(_date(checkpoint["window_start"]).timestamp())
        upper = int(_date(checkpoint["window_end"]).timestamp())
        query = f"after:{lower} before:{upper}"
        if config["query"]:
            query = f"({config['query']}) {query}"
        params = {"q": query, "maxResults": config["page_size"], "labelIds": config["label_ids"]}
        if cursor:
            params["pageToken"] = cursor
        data = _json(_request(client, GMAIL, headers, params=params))
        messages, next_page = data.get("messages", []), data.get("nextPageToken")
        if next_page is not None and (not isinstance(next_page, str) or not next_page):
            raise MailboxError("Gmail returned an invalid page token; retry next run")
    else:
        if cursor:
            url, params = _graph_cursor(cursor), None
        else:
            folder = config["folder"]
            url = f"{GRAPH}/mailFolders/{quote(folder, safe='')}/messages" if folder else f"{GRAPH}/messages"
            where = (f"receivedDateTime ge {_stamp(checkpoint['window_start'])} and "
                     f"receivedDateTime lt {_stamp(checkpoint['window_end'])}")
            if config["senders"]:
                senders = ["from/emailAddress/address eq '" + sender.replace("'", "''") + "'"
                           for sender in config["senders"]]
                where += " and (" + " or ".join(senders) + ")"
            params = {"$filter": where, "$select": "id,receivedDateTime", "$top": config["page_size"]}
        data = _json(_request(client, url, headers, params=params))
        messages, next_page = data.get("value"), _graph_cursor(data.get("@odata.nextLink"))
    if not isinstance(messages, list) or any(not isinstance(message, dict) or
            not isinstance(message.get("id"), str) or not message["id"] for message in messages):
        raise MailboxError("Mailbox returned an invalid message list; retry next run")
    return messages, next_page


def _message(client, provider, message, headers):
    message_id = quote(message["id"], safe="")
    try:
        if provider == "imap":
            validity, uid = message["id"].split(":", 1)
            if validity != client.uidvalidity:
                raise ValueError()
            content, received = client.fetch(uid)
        elif provider == "gmail":
            data = _json(_request(client, f"{GMAIL}/{message_id}", headers, params={"format": "raw"}))
            raw = data["raw"]
            if not isinstance(raw, str) or not raw:
                raise ValueError()
            content = base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True)
            received = datetime.fromtimestamp(int(data["internalDate"]) / 1000, timezone.utc)
        else:
            content = _request(client, f"{GRAPH}/messages/{message_id}/$value", headers).content
            received = _date(message["receivedDateTime"])
        if not content:
            raise ValueError()
    except (KeyError, TypeError, ValueError, OverflowError, OSError) as exc:
        if isinstance(exc, MailboxError):
            raise
        raise MailboxError("Mailbox returned an invalid MIME message or received date; retry next run") from None
    return content, received


def _already_imported(store, source, provider, account, message_id, reparse_before):
    with store.engine.connect() as conn:
        return conn.execute(select(T.mail_messages.c.message_id).where(
            T.mail_messages.c.source == source, T.mail_messages.c.provider == provider,
            T.mail_messages.c.account_key == account, T.mail_messages.c.message_id == message_id,
            T.mail_messages.c.imported_at >= _date(reparse_before),
        )).first() is not None


def _receipt(store, source, provider, account, message_id, received, now, job_links):
    with store._conn() as conn:
        values = dict(source=source, provider=provider, account_key=account, message_id=message_id,
                      received_at=received, imported_at=now, job_links=job_links)
        conn.execute(insert(T.mail_messages).values(**values).on_conflict_do_update(
            index_elements=["source", "provider", "account_key", "message_id"],
            set_={key: values[key] for key in ("received_at", "imported_at", "job_links")},
        ))


def _import_message(store, source, config, account, message, client, headers, now, *, window=None):
    provider, message_id = config["provider"], message["id"]
    content, received = _message(client, provider, message, headers)
    if provider == "imap" and window is not None and not (
            _date(window["window_start"]) <= _date(received) < _date(window["window_end"])):
        # IMAP SEARCH dates are day-granular. Do not permanently receipt mail
        # arriving after this fixed window: the next scan must still import it.
        return {"outside_window": True}
    if config.get("purpose") == "updates":
        from auto_app.track.events import write_transaction
        from auto_app.track.updates import record_update

        # The receipt, metadata-only update and event commit together. Nothing
        # enters the feed ingestion/raw-blob path for application mail.
        identity = json.dumps([provider, account, message_id], separators=(",", ":"))
        with write_transaction(store):
            record_update(store, source, "mail:" + hashlib.sha256(identity.encode()).hexdigest(), content, received)
            _receipt(store, source, provider, account, message_id, received, now, 0)
        return {}
    parsed = parse_email(content)
    if parsed.issues:
        raise MailboxError("Mailbox message could not be fully parsed; retry next run")
    for record in parsed.records:
        identity = json.dumps([provider, account, message_id, record.url], separators=(",", ":"))
        record.external_id = "mail-" + hashlib.sha256(identity.encode()).hexdigest()
        record.metadata["mailbox"] = {"provider": provider, "account_key": account, "message_id": message_id,
                                      "received_at": received.isoformat()}
        # Server receipt time is available even when the MIME Date is missing.
        if not record.metadata.get("observed_at"):
            record.metadata["observed_at"] = received.isoformat()
    origin = f"{provider}://{quote(account, safe='')}/messages/{quote(message_id, safe='')}"
    report = IngestService(store).ingest(source, parsed, kind="email", origin=origin,
                                        revision="sha256:" + hashlib.sha256(content).hexdigest())
    _receipt(store, source, provider, account, message_id, received, now, len(parsed.records))
    return report


def _retry_key(provider, account, message_id):
    return hashlib.sha256(json.dumps([provider, account, message_id]).encode()).hexdigest()


def _process_message(store, source, config, account, message, client, headers, now,
                     checkpoint, report, attempted, *, retry=False):
    """Return whether this message may let its listing page advance."""
    provider, message_id = config["provider"], message["id"]
    key = _retry_key(provider, account, message_id)
    pending = checkpoint["message_retries"]
    previous = pending.get(key, {})
    if key in attempted or (previous.get("deferred") and not retry):
        return previous.get("deferred", True)
    attempted.add(key)
    if _already_imported(store, source, provider, account, message_id, checkpoint["reparse_before"]):
        pending.pop(key, None)
        report["messages_skipped"] += 1
        if previous:
            store.update_health(source, mailbox=checkpoint)
        return True
    # An upgrade may run in the same clock tick as the previous import. The
    # successful receipt must cross its persisted cutoff even in that case.
    receipt_time = max(now, _date(checkpoint["reparse_before"]))
    try:
        imported = _import_message(store, source, config, account, message, client, headers, receipt_time,
                                   window=None if retry or previous else checkpoint)
    except (MailboxError, ValueError) as exc:
        if isinstance(exc, MailboxError) and exc.status_code in (404, 410):
            try:
                received = _date(message["receivedDateTime"]) if message.get("receivedDateTime") else None
            except (TypeError, ValueError):
                received = None
            _receipt(store, source, provider, account, message_id, received, receipt_time, 0)
            pending.pop(key, None)
            report["messages_gone"] += 1
            report["warnings"].append({"message_id": message_id, "reason": "Message was removed before retrieval"})
            if previous:
                store.update_health(source, mailbox=checkpoint)
            return True
        error = str(exc) if isinstance(exc, MailboxError) else "Message import failed; retry next run"
        attempts = previous.get("attempts", 0) + 1
        deferred = previous.get("deferred", False) or attempts >= MESSAGE_PAGE_ATTEMPTS
        delay = min(86400, RETRY_DELAY_SECONDS * 2 ** min(max(0, attempts - MESSAGE_PAGE_ATTEMPTS), 6))
        pending[key] = {
            "provider": provider, "account_key": account,
            "message": {field: message[field] for field in ("id", "receivedDateTime") if field in message},
            "attempts": attempts, "deferred": deferred, "error": error,
            "first_failed_at": previous.get("first_failed_at", now.isoformat()),
            "last_failed_at": now.isoformat(), "next_retry_at": (now + timedelta(seconds=delay)).isoformat(),
        }
        report["issues"].append({"stage": "message", "message_id": message_id, "error": error,
                                 "attempts": attempts, "deferred": deferred})
        # Persist the failed identity before allowing any cursor to pass it.
        store.update_health(source, mailbox=checkpoint)
        return deferred
    pending.pop(key, None)
    if imported.get("outside_window"):
        report["messages_skipped"] += 1
        return True
    report["messages_processed"] += 1
    for key in COUNTERS:
        report[key] += imported.get(key, 0)
    if previous:
        store.update_health(source, mailbox=checkpoint)
    return True


def run_mailbox(store, source: str, definition: dict, *, client: httpx.Client | None = None,
                now: datetime | None = None) -> dict:
    """Import bounded pages, retaining repeated failures as durable retries.

    Windows are fixed while paginating and overlap by two minutes on the next
    scan. Successful receipts make retries safe, including mail without jobs.
    After three failed attempts a message no longer blocks its page. Deferred
    retries use capped backoff and at most one extra page_size of requests/run.
    HTTP providers use GET; IMAP uses EXAMINE and BODY.PEEK.
    """
    config = validate_mailbox_config(definition, Path.cwd())
    _check_source(store, source)
    if config["provider"] == "imap" and client is None:
        from auto_app.ingest.imap import ImapSession, load_imap_credentials

        with ImapSession(load_imap_credentials(Path(config["credentials"]))) as session:
            return run_mailbox(store, source, config, client=session, now=now)
    if client is None:
        with httpx.Client(timeout=30) as owned:
            return run_mailbox(store, source, config, client=owned, now=now)
    from auto_app.ingest.mail_auth import access_token

    now = _date(now or datetime.now(timezone.utc)).replace(microsecond=0)
    provider, path = config["provider"], Path(config["credentials"])
    if provider == "imap":
        from auto_app.ingest.imap import load_imap_credentials

        credentials = load_imap_credentials(path)
        client.select(config["folder"])
        account = hashlib.sha256(json.dumps([credentials["host"], credentials["port"],
                                            credentials["username"], config["folder"]]).encode()).hexdigest()
        headers = {}
    else:
        account = _account_key(path)
        token = access_token(path, provider=provider, client=client)
        headers = {"Authorization": f"Bearer {token}"}
    if provider == "outlook":
        headers["Prefer"] = 'IdType="ImmutableId"'
    scope = {key: value for key, value in config.items() if key not in ("credentials", "max_pages")}
    scope["account_key"] = account
    scope_hash = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
    checkpoint = store.source_health(source).get("mailbox", {})
    if checkpoint.get("scope_hash") != scope_hash:
        pending = checkpoint.get("message_retries", {})
        for item in pending.values():
            item["deferred"] = True
        checkpoint = {"provider": provider, "account_key": account, "scope_hash": scope_hash,
                      "watermark": None, "message_retries": pending,
                      **{key: checkpoint[key] for key in ("email_parser_version", "reparse_before", "uidvalidity")
                         if key in checkpoint}}
    checkpoint.setdefault("message_retries", {})
    if provider == "imap" and checkpoint.get("uidvalidity") != client.uidvalidity:
        # A new UID namespace invalidates both cursors and deferred fetches.
        # Receipts retain their old namespace for provenance.
        checkpoint["message_retries"] = {
            key: item for key, item in checkpoint["message_retries"].items()
            if item["provider"] != provider or item["account_key"] != account
        }
        checkpoint.update(uidvalidity=client.uidvalidity, watermark=None,
                          in_progress=False, next_page=None)
    parser_version = (EMAIL_PARSER_VERSION if config["purpose"] == "jobs"
                      else f"{EMAIL_PARSER_VERSION}:updates:1")
    if checkpoint.get("email_parser_version") != parser_version:
        # Revisit only the unfinished window or configured lookback. Old
        # receipts survive until a successful replay replaces them.
        previous_cutoff = _date(checkpoint["reparse_before"]) if checkpoint.get("reparse_before") else now
        checkpoint.update(email_parser_version=parser_version,
                          reparse_before=(max(now, previous_cutoff) + timedelta(microseconds=1)).isoformat(),
                          next_page=None)
        if not checkpoint.get("in_progress"):
            checkpoint["watermark"] = None
    if not checkpoint.get("in_progress"):
        watermark = _date(checkpoint["watermark"]) if checkpoint.get("watermark") else None
        upper = max(now, watermark) if watermark else now
        lower = watermark - timedelta(seconds=120) if watermark else now - timedelta(days=config["lookback_days"])
        checkpoint.update(window_start=lower.isoformat(), window_end=upper.isoformat(), next_page=None,
                          in_progress=True, has_more=True)
    store.update_health(source, ingest={"kind": "email"}, mailbox=checkpoint)
    report = {"source": source, "kind": "mailbox", "provider": provider, "status": "ok", "issues": [], "warnings": [],
              "messages_processed": 0, "messages_skipped": 0, "messages_gone": 0, "pages_processed": 0,
              **{key: 0 for key in COUNTERS}}
    attempted = set()
    due = sorted((item for item in checkpoint["message_retries"].values()
                  if item.get("deferred") and item["provider"] == provider and item["account_key"] == account
                  and _date(item["next_retry_at"]) <= now), key=lambda item: item["next_retry_at"])
    for item in due[:config["page_size"]]:
        _process_message(store, source, config, account, item["message"], client, headers, now,
                         checkpoint, report, attempted, retry=True)
    for _ in range(config["max_pages"]):
        try:
            messages, next_page = _page(client, config, checkpoint, headers)
        except MailboxError as exc:
            report["issues"].append({"stage": "list", "error": str(exc)})
            if checkpoint["next_page"] and exc.status_code in (400, 410):
                # An expired pagination cursor should replay this bounded
                # window, never skip ahead or stay stuck on the stale cursor.
                checkpoint["next_page"] = None
                store.update_health(source, mailbox=checkpoint)
            break
        page_complete = True
        for message in messages:
            if not _process_message(store, source, config, account, message, client, headers, now,
                                    checkpoint, report, attempted):
                page_complete = False
        if not page_complete:
            break
        report["pages_processed"] += 1
        checkpoint.update(next_page=next_page, in_progress=bool(next_page), has_more=bool(next_page))
        if not next_page:
            checkpoint["watermark"] = checkpoint["window_end"]
        store.update_health(source, mailbox=checkpoint)
        if not next_page:
            break
    pending = checkpoint["message_retries"]
    report["messages_pending"] = len(pending)
    report["messages_deferred"] = sum(bool(item.get("deferred")) for item in pending.values())
    if pending:
        report["warnings"].append({"reason": "Unresolved messages remain in the durable retry queue",
                                    "count": len(pending)})
    if report["issues"] or pending:
        report["status"] = "partial" if (report["messages_processed"] or report["messages_skipped"]
                                            or (pending and report["pages_processed"])) else "error"
    report.update(has_more=checkpoint["has_more"], checkpoint=checkpoint)
    return report

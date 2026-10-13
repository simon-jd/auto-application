"""Read scoped Gmail/Outlook recommendations with durable, bounded scan windows."""

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import quote, urlsplit

import httpx
from sqlalchemy import insert, select

from auto_app.core.db import tables as T
from auto_app.ingest.email import parse_email
from auto_app.ingest.service import IngestService


COUNTERS = ("records", "created", "updated", "unchanged", "restored", "inactive", "missing", "jobs_created")
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
GRAPH = "https://graph.microsoft.com/v1.0/me"


class MailboxError(ValueError):
    """Provider failure text that contains neither response bodies nor tokens."""

    def __init__(self, message, *, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def validate_mailbox_config(definition: dict, base: Path) -> dict:
    provider = definition.get("provider")
    if provider not in {"gmail", "outlook"}:
        raise ValueError("mailbox provider must be gmail or outlook")
    credentials = definition.get("credentials")
    if not isinstance(credentials, str) or not credentials.strip():
        raise ValueError("mailbox credentials must be a file path")
    path = Path(credentials).expanduser()
    if not path.is_absolute():
        path = base / path
    result = {"kind": "mailbox", "provider": provider, "credentials": str(path.resolve())}
    for key, default, maximum in (("lookback_days", 14, None), ("page_size", 50, 500 if provider == "gmail" else 1000),
                                  ("max_pages", 4, None)):
        value = definition.get(key, default)
        if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
            raise ValueError(f"mailbox {key} must be a positive integer" + (f" no greater than {maximum}" if maximum else ""))
        result[key] = value
    string_key, list_key = ("query", "label_ids") if provider == "gmail" else ("folder", "senders")
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
    if config["provider"] == "gmail":
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
        if provider == "gmail":
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
        raise MailboxError("Mailbox returned an invalid MIME message or received date; retry next run") from exc
    return content, received


def _already_imported(store, source, provider, account, message_id):
    with store.engine.connect() as conn:
        return conn.execute(select(T.mail_messages.c.message_id).where(
            T.mail_messages.c.source == source, T.mail_messages.c.provider == provider,
            T.mail_messages.c.account_key == account, T.mail_messages.c.message_id == message_id,
        )).first() is not None


def _receipt(store, source, provider, account, message_id, received, now, job_links):
    with store.engine.begin() as conn:
        conn.execute(insert(T.mail_messages).values(source=source, provider=provider, account_key=account,
                     message_id=message_id, received_at=received, imported_at=now, job_links=job_links))


def _import_message(store, source, config, account, message, client, headers, now):
    provider, message_id = config["provider"], message["id"]
    content, received = _message(client, provider, message, headers)
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


def run_mailbox(store, source: str, definition: dict, *, client: httpx.Client | None = None,
                now: datetime | None = None) -> dict:
    """Import bounded pages; only complete pages advance the durable cursor.

    Windows are fixed while paginating and overlap by two minutes on the next
    scan. Successful receipts make retries safe, including mail without jobs.
    All mailbox operations are GETs, leaving folders and read flags untouched.
    """
    config = validate_mailbox_config(definition, Path.cwd())
    _check_source(store, source)
    if client is None:
        with httpx.Client(timeout=30) as owned:
            return run_mailbox(store, source, config, client=owned, now=now)
    from auto_app.ingest.mail_auth import access_token

    now = _date(now or datetime.now(timezone.utc)).replace(microsecond=0)
    provider, path = config["provider"], Path(config["credentials"])
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
        checkpoint = {"provider": provider, "account_key": account, "scope_hash": scope_hash, "watermark": None}
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
        for message in messages:
            message_id = message["id"]
            if _already_imported(store, source, provider, account, message_id):
                report["messages_skipped"] += 1
                continue
            try:
                imported = _import_message(store, source, config, account, message, client, headers, now)
            except (MailboxError, ValueError) as exc:
                if isinstance(exc, MailboxError) and exc.status_code in (404, 410):
                    received = _date(message["receivedDateTime"]) if message.get("receivedDateTime") else None
                    _receipt(store, source, provider, account, message_id, received, now, 0)
                    report["messages_gone"] += 1
                    report["warnings"].append({"message_id": message_id, "reason": "Message was removed before retrieval"})
                    continue
                error = str(exc) if isinstance(exc, MailboxError) else "Message import failed; retry next run"
                report["issues"].append({"stage": "message", "message_id": message_id, "error": error})
                continue
            report["messages_processed"] += 1
            for key in COUNTERS:
                report[key] += imported.get(key, 0)
        if report["issues"]:
            break
        report["pages_processed"] += 1
        checkpoint.update(next_page=next_page, in_progress=bool(next_page), has_more=bool(next_page))
        if not next_page:
            checkpoint["watermark"] = checkpoint["window_end"]
        store.update_health(source, mailbox=checkpoint)
        if not next_page:
            break
    if report["issues"]:
        report["status"] = "partial" if report["messages_processed"] or report["messages_skipped"] else "error"
    report.update(has_more=checkpoint["has_more"], checkpoint=checkpoint)
    return report

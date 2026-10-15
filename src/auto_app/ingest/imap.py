"""Private, read-only IMAP sessions: EXAMINE folders and fetch with BODY.PEEK."""

import imaplib
import json
import os
import re
import ssl
import stat
from datetime import datetime, timedelta
from pathlib import Path


class ImapError(ValueError):
    """A provider failure whose text never contains credentials or server data."""


def load_imap_credentials(path: Path) -> dict:
    try:
        fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
                raise ImapError("IMAP credentials must be a private regular file (chmod 600)")
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise ImapError("IMAP credential file is too large")
        data = json.loads(raw)
    except ImapError:
        raise
    except (OSError, ValueError, TypeError):
        raise ImapError("Cannot read IMAP credentials") from None
    if not isinstance(data, dict):
        raise ImapError("IMAP credentials must be a JSON object")
    host, port = data.get("host"), data.get("port", 993)
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9.-]+", host):
        raise ImapError("IMAP credentials need a host name")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ImapError("IMAP port must be an integer between 1 and 65535")
    for key in ("username", "password"):
        if (not isinstance(data.get(key), str) or not data[key]
                or any(char in data[key] for char in "\r\n\x00")):
            raise ImapError(f"IMAP credentials need a valid {key}")
    return {"host": host, "port": port, "username": data["username"], "password": data["password"]}


def _quote(value: str) -> str:
    if not isinstance(value, str) or any(char in value for char in "\r\n\x00"):
        raise ImapError("Invalid IMAP search or folder value")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


INTERNALDATE = re.compile(rb'INTERNALDATE "([^"]+)"')
UID = re.compile(r"[1-9][0-9]*\Z")


class ImapSession:
    def __init__(self, credentials: dict, *, connect=imaplib.IMAP4_SSL):
        self.credentials, self._connect = credentials, connect
        self.conn, self.uidvalidity, self.folder = None, None, None

    def __enter__(self):
        try:
            self.conn = self._connect(self.credentials["host"], self.credentials["port"],
                                      ssl_context=ssl.create_default_context(), timeout=30)
            status, _ = self.conn.login(self.credentials["username"], self.credentials["password"])
            if status != "OK":
                raise ImapError("IMAP login failed")
        except Exception:
            self.__exit__()
            raise ImapError("IMAP login failed; verify credentials or retry next run") from None
        return self

    def __exit__(self, *exc):
        try:
            if self.conn is not None:
                self.conn.logout()
        except Exception:
            pass
        self.conn = None

    def select(self, folder: str) -> None:
        try:
            status, _ = self.conn.select(_quote(folder), readonly=True)  # imaplib sends EXAMINE
            if status != "OK":
                raise ImapError("IMAP folder could not be opened")
            _, data = self.conn.response("UIDVALIDITY")
            validity = data[0].decode("ascii") if data and data[0] else ""
            if not UID.fullmatch(validity):
                raise ImapError("IMAP returned no valid UIDVALIDITY")
        except Exception:
            raise ImapError("IMAP folder or UIDVALIDITY could not be read; retry next run") from None
        self.uidvalidity, self.folder = validity, folder

    def uids(self, config: dict, start: datetime, end: datetime) -> list[str]:
        # Search again on each page/run. UID cursors remain safe when earlier
        # mail is deleted; caching an index-based list would skip messages.
        try:
            # IMAP compares INTERNALDATE calendar dates, ignoring their UTC offsets.
            # Pad both edges for offsets -12..+14; mailbox.py enforces the precise UTC window.
            criteria = ["SINCE", (start - timedelta(days=1)).strftime("%d-%b-%Y"), "BEFORE",
                        (end + timedelta(days=2)).strftime("%d-%b-%Y")]
            senders = config.get("senders") or []
            if senders:
                criteria += ["OR"] * (len(senders) - 1)
                for sender in senders:
                    criteria += ["FROM", _quote(sender)]
            if config.get("gmail_raw"):
                criteria += ["X-GM-RAW", _quote(config["gmail_raw"])]
            status, data = self.conn.uid("SEARCH", *criteria)
            if status != "OK" or not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], bytes):
                raise ImapError("IMAP search failed")
            uids = data[0].decode("ascii").split()
            if any(not UID.fullmatch(uid) for uid in uids):
                raise ImapError("IMAP returned invalid UIDs")
            return sorted(set(uids), key=int)
        except Exception:
            raise ImapError("IMAP search failed; retry next run") from None

    def fetch(self, uid: str) -> tuple[bytes, datetime]:
        try:
            if not isinstance(uid, str) or not UID.fullmatch(uid):
                raise ImapError("Invalid IMAP UID")
            status, data = self.conn.uid("FETCH", uid, "(INTERNALDATE BODY.PEEK[])")
            part = next((item for item in data or [] if isinstance(item, tuple) and len(item) == 2), None)
            if status != "OK" or part is None or not isinstance(part[1], bytes) or not part[1]:
                raise ImapError("IMAP returned no message")
            stamp = INTERNALDATE.search(part[0])
            if stamp is None:
                raise ImapError("IMAP returned no received date")
            received = datetime.strptime(stamp.group(1).decode("ascii"), "%d-%b-%Y %H:%M:%S %z")
            return part[1], received
        except Exception:
            raise ImapError("IMAP message or received date could not be read; retry next run") from None

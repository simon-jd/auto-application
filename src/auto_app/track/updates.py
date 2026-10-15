"""Conservative deterministic mail classification; message bodies never leave memory."""

import re
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, quote
from html import unescape

from sqlalchemy import insert, select, update

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow
from auto_app.track.events import record_event, require_job, valid_date, valid_id, write_transaction

# Every rule describes a concrete status or requested next step, not a keyword.
RULES = (
    ("offer", r"\bwe are pleased to offer you\b|\bwe (?:are extending|extend) (?:you )?(?:an|the) (?:job )?offer\b|\byour offer letter (?:is (?:attached|available|ready)|has been (?:sent|issued|attached))\b"),
    ("rejected", r"\bwe (?:have )?decided to (?:pursue|move forward with) other candidates\b|\bwe (?:will not|won't|are not) (?:be )?(?:moving|move) forward with (?:your|the) application\b|\byour application (?:has been|was|is) (?:unsuccessful|rejected)\b|\bwe regret to inform you.{0,100}\b(?:position.{0,40}filled|not.{0,40}(?:selected|moving forward))\b"),
    ("interview", r"\bwe(?:'d| would) like to (?:schedule|invite you (?:to|for)) (?:a |an |your )?(?:phone screen|interview)\b|\bplease (?:schedule|book|confirm) (?:a |an |your )?(?:phone screen|interview)\b|\binvit(?:e|ing) you (?:to|for) (?:a |an )?(?:interview|phone screen)\b"),
    ("assessment", r"\bplease (?:complete|take|submit) (?:the |a |an |your )?(?:\w+ )?(?:assessment|coding challenge|hackerrank challenge|codesignal|codility|online test|take[- ]home)\b|\binvit(?:e|ing) you to (?:complete|take) (?:an? |the |your )?(?:\w+ )?(?:assessment|coding challenge|online test)\b"),
    ("applied", r"\bthank(?:s| you) for (?:applying|your application)\b|\bwe(?:'ve| have) received your application\b|\byour application (?:was |has been |is )?(?:received|submitted)\b"),
)
COMPILED = tuple((kind, re.compile(pattern, re.I)) for kind, pattern in RULES)
PROMOTIONAL = re.compile(r"\b(?:recommended jobs?|job recommendations?|job alerts?|interview tips|newsletter|career advice)\b", re.I)


def _unquoted(body: str) -> str:
    lines = body.splitlines()
    current = []
    for index, line in enumerate(lines):
        if re.match(r"\s*(?:[-_]{2,}\s*(?:original|forwarded) message|begin forwarded message:|on .+wrote:)" , line, re.I):
            break
        # Outlook reply/forward header blocks may have no quote prefix.
        if re.match(r"\s*from:", line, re.I) and re.search(
                r"(?im)^\s*(?:sent|date|subject|to):", "\n".join(lines[index + 1:index + 7])):
            break
        if not line.lstrip().startswith(">"):
            current.append(line)
    return "\n".join(current)


def classify(subject: str, body: str) -> str | None:
    if not isinstance(subject, str) or not isinstance(body, str):
        raise ValueError("subject and body must be strings")
    if PROMOTIONAL.search(subject):
        return None
    # Quoted replies are history rather than a new status update.
    body = _unquoted(body[:6000])
    text = re.sub(r"\s+", " ", f"{subject}\n{body}".replace("’", "'"))
    for kind, pattern in COMPILED:
        for match in pattern.finditer(text):
            prefix = re.split(r"[.!?;]", text[:match.start()])[-1]
            suffix = re.split(r"[.!?;]", text[match.end():], maxsplit=1)[0]
            if kind == "rejected":
                # The matched rejection itself can contain "not" ("will not move
                # forward"). Check qualifications outside it, not its own wording.
                prefix = re.split(r"\b(?:but|however)\b", prefix, flags=re.I)[-1]
                # A conditional anywhere up to the verdict qualifies it ("if ... not selected"); a polite
                # modal inside the matched sentence ("your skills may fit other roles") does not.
                if (re.search(r"\b(?:if|unless|once|whether)\b", prefix + match.group(), re.I)
                    or re.search(r"\b(?:could|might|may)\b", prefix, re.I)
                    or re.search(r"\b(?:not|never|doesn't|don't|didn't|cannot|can't|wouldn't|shouldn't)\s+"
                                 r"(?:\w+\s+){0,3}(?:mean|imply|indicate|suggest|assume|conclude|true|correct|case|"
                                 r"confirm(?:ed|ing)?|stat(?:e|ed|ing)|report(?:ed|ing)?|say(?:ing)?|said)\b", prefix, re.I)
                    or re.match(r"\s+(?:is|was)\s+(?:not\s+(?:true|correct)|false|incorrect)\b", suffix, re.I)
                    or re.match(r"\s*,?\s*(?:only\s+)?(?:if|unless|once|provided)\b", suffix, re.I)):
                    continue
            else:
                clause = prefix + match.group() + suffix
                if re.search(r"\b(?:not|never|no|don't|won't|cannot|can't|if|unless|once|could|might|whether|pending|cancelled|canceled|cancellation)\b", clause, re.I):
                    continue
            if kind == "offer" and not re.search(r"\b(?:position|role|employment|job offer|offer letter)\b", text, re.I):
                continue
            return kind
    return None


def _parts(content: bytes) -> tuple[str, str, str]:
    if not isinstance(content, bytes) or not content:
        raise ValueError("content must be nonempty MIME bytes")
    try:
        message = BytesParser(policy=policy.default).parsebytes(content)
        if message.defects:
            raise ValueError()
        name, address = parseaddr(str(message.get("From", "")))
        part = message.get_body(preferencelist=("plain", "html"))
        body = part.get_content() if part is not None else ""
        if not isinstance(body, str):
            raise ValueError()
        if part is not None and part.get_content_type() == "text/html":
            # Outlook/Gmail containers mark the start of old message history;
            # discard the entire tail, including siblings following the header.
            marker = re.search(r"<[^>]+\b(?:id|class)\s*=\s*[\"'][^\"']*(?:divRplyFwdMsg|gmail_quote|yahoo_quoted|moz-cite-prefix)[^\"']*[\"'][^>]*>", body, re.I)
            if marker:
                body = body[:marker.start()]
            body = re.sub(r"<(script|style|blockquote)\b[^>]*>.*?</\1>", " ", body, flags=re.I | re.S)
            body = re.sub(r"<(?:br|/?p|/?div)\b[^>]*>", "\n", body, flags=re.I)
            body = unescape(re.sub(r"<[^>]+>", " ", body))
        # Quote a display name such as "Acme, Inc." so the matcher can parse it back (formataddr
        # would also RFC 2047-encode non-ASCII names).
        if name and re.search(r'[()<>@,;:\\".\[\]]', name):
            name = '"' + quote(name) + '"'
        return (f"{name} <{address}>" if name else address), str(message.get("Subject", "")), body
    except (ValueError, TypeError, LookupError, UnicodeError):
        raise ValueError("cannot parse MIME update") from None


def _words(value):
    return " ".join(re.findall(r"[a-z0-9]+", value.lower()))


ROLE_SUFFIX = re.compile(r"(?: (?:recruiting|recruitment|careers|jobs|hiring|human resources|hr|people|"
                         r"talent(?: acquisition)?))?(?: team)?$")


def _organization(name: str) -> str:
    """The organization a sender or label names: "The NVIDIA Corporation Recruiting Team" → "nvidia".

    Only a leading "The", a trailing "via <platform>", a recruiting-team suffix and legal suffixes
    are dropped; the rest must still equal the employer exactly, so prose never matches.
    """
    from auto_app.process.normalize import company_basic

    words = _words(company_basic(re.sub(r"\s+via\s+.*$", "", name or "", flags=re.I)))
    words = ROLE_SUFFIX.sub("", re.sub(r"^the ", "", words))
    return _words(company_basic(words))


def _contains(haystack, needle):
    return bool(needle) and f" {needle} " in f" {haystack} "


def _match(conn, sender: str, subject: str, body: str) -> int | None:
    rows = conn.execute(select(T.jobs.c.job_id, T.jobs.c.company_norm, T.jobs.c.title_norm)
                        .where(T.jobs.c.decision == "applied")).all()
    sender_name, _ = parseaddr(sender)
    sender_identity = _organization(sender_name)
    opening = _unquoted(body)[:500]
    # Only independently stated organization identities can establish employer
    # attribution. Prose/prepositions and unverified domain-name guesses cannot.
    attributed = {
        _organization(match.group(1)) for match in re.finditer(
            r"(?im)^\s*(?:employer|company|organization)\s*:\s*([^\r\n]+)\s*$",
            f"{subject}\n{opening}")
    }
    companies = {}
    for job_id, company, title in rows:
        normalized = _organization(company or "")
        if not normalized:
            continue
        if normalized == sender_identity or normalized in attributed:
            companies.setdefault(normalized, []).append((job_id, title))
    if len(companies) != 1:
        return None
    jobs = next(iter(companies.values()))
    if len(jobs) == 1:
        return jobs[0][0]
    subject_words = _words(subject)
    titled = [job_id for job_id, title in jobs if _contains(subject_words, _words(title or ""))]
    return titled[0] if len(titled) == 1 else None


def record_update(store, source: str, message_key: str, content: bytes, received: datetime) -> dict:
    if not isinstance(source, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", source):
        raise ValueError("source must be a nonempty source name")
    if not isinstance(message_key, str) or not message_key.strip():
        raise ValueError("message_key must be a nonempty string")
    received = valid_date(received, "received")
    if not isinstance(content, bytes) or not content:
        raise ValueError("content must be nonempty MIME bytes")
    try:
        sender, subject, body = _parts(content)
    except ValueError:
        # Malformed mail is unclassifiable, not a transient failure: the mailbox receipts it
        # instead of retrying it forever.
        return {"kind": None, "unreadable": True}
    kind = classify(subject, body)
    if kind is None:
        return {"kind": None}
    u = T.mail_updates
    with write_transaction(store) as conn:
        existing = conn.execute(select(u).where(u.c.message_key == message_key)).mappings().first()
        if existing is not None:
            return {"kind": existing["kind"], "status": "duplicate", "job_id": existing["job_id"], "update_id": existing["update_id"]}
        job_id = _match(conn, sender, subject, body)
        status = "matched" if job_id is not None else "unmatched"
        update_id = conn.execute(insert(u).values(source=source, message_key=message_key, received_at=received,
            sender=sender[:300], subject=subject[:300], kind=kind, job_id=job_id, status=status,
            created_at=utcnow())).inserted_primary_key[0]
        if job_id is not None:
            record_event(store, job_id, kind, occurred_at=received, source="email", note=subject[:200], mail_update_id=update_id)
        return {"kind": kind, "status": status, "job_id": job_id, "update_id": update_id}


def unmatched(store) -> list[dict]:
    u = T.mail_updates
    with store._conn() as conn:
        return [{**dict(row), "received_at": row["received_at"].replace(tzinfo=timezone.utc).isoformat()
                 if row["received_at"] is not None else None}
                for row in conn.execute(select(u.c.update_id, u.c.received_at, u.c.sender, u.c.subject, u.c.kind)
                    .where(u.c.status == "unmatched").order_by(u.c.update_id)).mappings()]


def assign(store, update_id: int, job_id: int) -> dict:
    valid_id(update_id, "update_id")
    valid_id(job_id)
    u = T.mail_updates
    with write_transaction(store) as conn:
        require_job(conn, job_id)
        row = conn.execute(select(u).where(u.c.update_id == update_id, u.c.status == "unmatched")).mappings().first()
        if row is None:
            raise ValueError(f"update #{update_id} is not waiting for review")
        conn.execute(update(u).where(u.c.update_id == update_id, u.c.status == "unmatched").values(job_id=job_id, status="matched"))
        from auto_app.track.events import _aware
        record_event(store, job_id, row["kind"], occurred_at=_aware(row["received_at"]) if row["received_at"] else None,
                     source="email", note=(row["subject"] or "")[:200], mail_update_id=update_id)
    return {"update_id": update_id, "job_id": job_id, "status": "matched"}


def dismiss(store, update_id: int) -> dict:
    valid_id(update_id, "update_id")
    u = T.mail_updates
    with write_transaction(store) as conn:
        changed = conn.execute(update(u).where(u.c.update_id == update_id, u.c.status == "unmatched").values(status="dismissed")).rowcount
        if not changed:
            raise ValueError(f"update #{update_id} is not waiting for review")
    return {"update_id": update_id, "status": "dismissed"}

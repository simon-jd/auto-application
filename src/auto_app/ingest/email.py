"""Parse recommended-job emails without contacting any remote service."""

import hashlib
import re
from email import policy
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

from .models import IngestRecord, ParsedFeed
from .urls import canonical_url


EMAIL_PARSER_VERSION = "2"
_PLAIN_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_NAVIGATION = {
    "search", "browse", "all", "settings", "preferences", "unsubscribe", "alerts",
    "notifications", "login", "signin", "signup", "register", "premium", "about",
    "benefits", "teams", "locations", "home", "saved", "collections", "help",
}
_JOB_SEGMENTS = {"job", "jobs", "career", "careers", "position", "positions", "opening", "openings"}
_GENERIC_LABEL = re.compile(
    r"^(?:apply(?:\s+(?:now|here|(?:for|to)\s+(?:this\s+)?(?:job|role|position)))?"
    r"|(?:view|see|open)(?:\s+(?:this|the))?\s+(?:job|role|position|opening|details|job details)"
    r"|(?:learn|read) more|click here|(?:job )?details|view (?:&|and) apply)$", re.I,
)
_NAV_LABEL = re.compile(
    r"unsubscribe|manage (?:alerts|preferences|notifications)|upgrade to premium|"
    r"^(?:browse|search|find|see all|view all) (?:jobs|openings|positions)$", re.I,
)


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.href: str | None = None
        self.label: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if self.hidden:
            return
        if tag == "a":
            self._finish_anchor()
            self.href = dict(attrs).get("href")
        elif tag == "br" and self.href:
            self.label.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag == "a" and not self.hidden:
            self._finish_anchor()

    def handle_data(self, data):
        if self.href and not self.hidden:
            self.label.append(data)

    def _finish_anchor(self):
        if self.href:
            self.links.append((self.href, "".join(self.label)))
        self.href = None
        self.label = []

    def close(self):
        super().close()
        self._finish_anchor()


def _body_parts(message):
    """Yield body text parts; attached files and attached messages are not jobs."""
    if message.get_content_disposition() == "attachment" or message.get_filename():
        return
    if message.get_content_maintype() == "multipart":
        for part in message.iter_parts():
            yield from _body_parts(part)
    elif message.get_content_type() in {"text/plain", "text/html"}:
        payload = message.get_payload(decode=True) or b""
        try:
            text = payload.decode(message.get_content_charset() or "utf-8", errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        yield message.get_content_type(), text


def _is_job_url(url: str) -> bool:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    path = parsed.path
    parts = [part.lower() for part in path.split("/") if part]
    # These job-detail routes contain words also used by generic navigation.
    # Match the full provider route and its numeric identity before that filter.
    if host in {"google.com", "www.google.com"}:
        return bool(re.fullmatch(r"/about/careers/applications/jobs/results/\d+(?:-[^/]+)?", path))
    if host in {"tesla.com", "www.tesla.com"}:
        return bool(re.fullmatch(r"/careers/search/job/(?:[^/]+-)?\d+", path))
    if any(part in _NAVIGATION for part in parts):
        return False
    if host in {"linkedin.com", "www.linkedin.com", "m.linkedin.com"}:
        return bool(re.fullmatch(r"/jobs/view/\d+", path))
    if host in {"jobright.ai", "www.jobright.ai"}:
        return bool(re.fullmatch(r"/jobs/(?:info/|view/)?[^/]+", path)) and parts[-1] not in {"info", "view"}
    if host in {"simplify.jobs", "www.simplify.jobs"}:
        return bool(re.match(r"^/p/[^/]+(?:/|$)", path))
    if host in {"jobs.lever.co", "jobs.eu.lever.co", "jobs.ashbyhq.com"}:
        return bool(re.match(rf"^/[^/]+/{_UUID}(?:/|$)", path, re.I))
    if host == "greenhouse.io" or host.endswith(".greenhouse.io"):
        return bool(re.search(r"/jobs/\d+(?:/|$)", path))
    query = parse_qs(parsed.query)
    if any(value.isdigit() for value in query.get("gh_jid", [])):
        return True
    if any(part in _JOB_SEGMENTS for part in parts):
        if any(key.lower() in {"jobid", "job_id", "requisitionid", "reqid"} and any(values)
               for key, values in query.items()):
            return True
        return any(part in _JOB_SEGMENTS for part in parts[:-1])
    return False


def _job_links(links):
    for original_url, label in links:
        label = " ".join(label.split()).strip(" \t\r\n›»→!.")
        if _NAV_LABEL.search(label):
            continue
        url = canonical_url(original_url)
        if url and _is_job_url(url):
            title = label if label and not _GENERIC_LABEL.fullmatch(label) and not _PLAIN_URL.search(label) else None
            yield url, original_url, title


def parse_email(content: bytes) -> ParsedFeed:
    """Extract observed job links, preferring HTML over the plain-text alternative.

    A message's Date is observation context, never the job's publication date.
    Buttons, subjects and surrounding prose do not fill missing job titles/JDs.
    """
    message = BytesParser(policy=policy.default).parsebytes(content)
    message_id = str(message["Message-ID"]).strip() if message["Message-ID"] else None
    subject = str(message["Subject"]) if message["Subject"] else None
    date = str(message["Date"]) if message["Date"] else None
    observed_at = None
    if date:
        try:
            observed_at = parsedate_to_datetime(date).isoformat()
        except (ValueError, TypeError, OverflowError):
            pass
    identity = message_id or hashlib.sha256(content).hexdigest()
    html_links = []
    plain_links = []
    for content_type, text in _body_parts(message):
        if content_type == "text/html":
            parser = _Links()
            parser.feed(text)
            parser.close()
            html_links.extend(parser.links)
        else:
            plain_links.extend((match.rstrip(".,;:!?)]}"), "") for match in _PLAIN_URL.findall(text))
    candidates = list(_job_links(html_links)) or list(_job_links(plain_links))
    records = {}
    for url, original_url, title in candidates:
        if url in records:
            record = records[url]
            if record.title is None and title:
                record.title = title
            if original_url not in record.metadata["original_urls"]:
                record.metadata["original_urls"].append(original_url)
            continue
        external_id = hashlib.sha256(f"{identity}\n{url}".encode()).hexdigest()
        records[url] = IngestRecord(
            external_id=f"email:{external_id}", url=url, title=title,
            metadata={
                "message_id": message_id, "subject": subject, "date": date,
                "observed_at": observed_at, "discovered_url": url,
                "original_url": original_url, "original_urls": [original_url],
            },
        )
    return ParsedFeed(records=list(records.values()))

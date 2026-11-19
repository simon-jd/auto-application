"""Read a public record (JSON, or a job page) once per target; pace hosts and cache Ashby boards per round."""

import math
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from auto_app.core.models import CommonPosting, RawPosting
from auto_app.enrich import sites
from auto_app.enrich.targets import Target, valid_target
from auto_app.platforms.ashby import AshbySource
from auto_app.platforms.greenhouse import GreenhouseSource
from auto_app.platforms.lever import LeverSource
from auto_app.platforms.workday import WorkdaySource
from auto_app.platforms.workday_guard import retry_after_seconds
from auto_app.sources.parsing import ParseError

USER_AGENT = "auto-app/1 (personal job search; one request per job)"
# Eightfold answers bursts with HTTP 429 (seen 2026-11-06): its tenants get two seconds between requests.
PLATFORM_INTERVAL = {"eightfold": 2.0}
# Workday blocks the address, not one tenant (HTTP 403 from dozens of tenants on 2026-11-19): any two Workday
# requests are three seconds apart, whichever tenants they go to.
PLATFORM_GAP = {"workday": 3.0}
PARSERS = {"greenhouse": GreenhouseSource, "lever": LeverSource, "ashby": AshbySource, "workday": WorkdaySource}
GONE = (404, 410)
# prefix letters, a core starting with a digit (more digit/letter groups allowed), an optional 1–2 digit copy suffix
REQUISITION = re.compile(r"([a-z]*)[-_]?(\d[\da-z]*(?:[-_]\d[\da-z]*)*?)(?:[-_](\d{1,2}))?", re.I)


@dataclass
class FetchResult:
    status: str  # ok | gone | failed | unsupported (the site has no page of its own for this link)
    posting: CommonPosting | None = None
    error: str | None = None
    http_status: int | None = None  # the status of a failed HTTP answer (403, 429 …)
    retry_after: int | None = None  # its Retry-After, in seconds


def _same_requisition(expected: str, found: str) -> bool:
    """Compare Workday ids: punctuation, a copy suffix on one side ("R123-1") and an omitted letter prefix
    ("123456" for "JR123456") are tolerated; different prefixes or two different copies never match."""
    if expected.strip() and expected.strip().lower() == found.strip().lower():
        return True
    a, b = (REQUISITION.fullmatch(value.strip()) for value in (expected, found))
    if a is None or b is None:
        return False
    (prefix_a, core_a, copy_a), (prefix_b, core_b, copy_b) = (
        (m[1].lower(), re.sub(r"[-_]", "", m[2].lower()), m[3]) for m in (a, b))
    if prefix_a != prefix_b and (prefix_a and prefix_b or sum(c.isdigit() for c in core_a) < 4):
        return False
    if copy_a and copy_b and copy_a != copy_b:
        return False
    return core_a == core_b


def _validate_record(platform: str, data: dict, job_id: str) -> None:
    if not isinstance(data, dict):
        raise ValueError("record must be an object")
    record = data.get("jobPostingInfo") if platform == "workday" else data
    title_key, id_key = ("text", "id") if platform == "lever" else ("title", "jobReqId" if platform == "workday" else "id")
    if not isinstance(record, dict) or not isinstance(record.get(title_key), str) or not record[title_key].strip():
        raise ValueError("record must have a title")
    found = record.get(id_key)
    if platform == "workday":
        # The allowlisted request already names the exact posting path.
        if found not in (None, "") and not _same_requisition(job_id, str(found)):
            raise ValueError("record does not match requested job")
    elif str(found or "").lower() != job_id.lower():
        raise ValueError("record does not match requested job")
    for key in ("content", "descriptionPlain", "description", "additional", "additionalPlain", "descriptionHtml", "jobDescription"):
        if record.get(key) is not None and not isinstance(record[key], str):
            raise ValueError("description must be text")


class Fetcher:
    def __init__(self, client: httpx.Client | None = None, *, min_interval: float = 1.0,
                 clock=time.monotonic, sleep=time.sleep):
        self.client = client or httpx.Client(timeout=20, follow_redirects=False, trust_env=False)
        self.min_interval = max(1.0, min_interval) if math.isfinite(min_interval) else 1.0
        self.clock, self.sleep = clock, sleep
        self.request_count = 0
        self._last: dict[str, float] = {}
        self._boards: dict[str, dict | FetchResult] = {}

    def close(self) -> None:
        self.client.close()

    def request_needed(self, target: Target) -> int:
        """Network cost of fetching this target with the current round cache."""
        return int(not (target.platform == "ashby" and target.board in self._boards))

    def _get(self, url: str, params: dict | None = None, accept: str = "application/json",
             interval: float = 0.0, platform: str | None = None) -> httpx.Response:
        gaps = [(urlsplit(url).hostname, max(self.min_interval, interval))]
        if platform in PLATFORM_GAP:
            gaps.append((f"platform:{platform}", PLATFORM_GAP[platform]))
        wait = max((self._last[key] + gap - self.clock() for key, gap in gaps if key in self._last), default=0)
        if wait > 0:
            self.sleep(wait)
        self.request_count += 1
        try:
            return self.client.get(url, params=params, follow_redirects=False,
                                   headers={"User-Agent": USER_AGENT, "Accept": accept}, timeout=20)
        finally:
            for key, _ in gaps:
                self._last[key] = self.clock()

    def fetch(self, target: Target) -> FetchResult:
        if not valid_target(target):
            return FetchResult("failed", error="invalid public job API target")
        try:
            if target.platform == "ashby":
                record = self._ashby_record(target)
                if isinstance(record, FetchResult):
                    return record
                if record is None:
                    return FetchResult("gone", error="not on the employer's public board")
                data = record
            else:
                page = target.platform in sites.PAGES
                response = self._get(target.api_url, {"mode": "json"} if target.platform == "lever" else None,
                                     "text/html" if page else "application/json",
                                     PLATFORM_INTERVAL.get(target.platform, 0.0), target.platform)
                if response.status_code in GONE:
                    return FetchResult("gone", error=f"HTTP {response.status_code}")
                if target.platform == "jobright" and response.is_redirect:
                    # Some listings send logged-out visitors to another job board: Jobright has no page to read.
                    return FetchResult("unsupported", error="Jobright sends this listing to another site")
                response.raise_for_status()
                if target.platform in sites.READERS:
                    posting = sites.READERS[target.platform](response.text if page else response.json(), target)
                    if posting is None:
                        return FetchResult("gone", error="no longer listed on the employer's site")
                    return FetchResult("ok", posting=posting)
                data = response.json()
            _validate_record(target.platform, data, target.job_id)
            raw = RawPosting(source="enrichment", source_job_id=target.job_id, kind="api", posting={}, capture={},
                             card={}, record={} if target.platform == "workday" else data,
                             detail=data if target.platform == "workday" else None)
            return FetchResult("ok", posting=PARSERS[target.platform]().parse(raw))
        except (httpx.HTTPError, ValueError, ParseError, KeyError, TypeError, AttributeError, OverflowError) as exc:
            # Exceptions may include request credentials, arbitrary response text or URLs.
            error, status, retry_after = type(exc).__name__, None, None
            if isinstance(exc, httpx.HTTPStatusError):
                status, retry_after = exc.response.status_code, retry_after_seconds(exc.response.headers)
                error += f": HTTP {status}"
            failed = FetchResult("failed", error=error, http_status=status, retry_after=retry_after)
            if target.platform == "ashby" and target.board not in self._boards:
                self._boards[target.board] = failed
            return failed

    def _ashby_record(self, target: Target) -> dict | FetchResult | None:
        if target.board not in self._boards:
            response = self._get(target.api_url, {"includeCompensation": "true"})
            if response.status_code in GONE:
                self._boards[target.board] = FetchResult("gone", error=f"HTTP {response.status_code}")
            else:
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
                    raise ValueError("board must have a jobs array")
                jobs = {}
                for job in data["jobs"]:
                    if not isinstance(job, dict) or not isinstance(job.get("id"), str) or not job["id"]:
                        continue
                    try:
                        _validate_record("ashby", job, job["id"])
                    except ValueError:
                        jobs[job["id"].lower()] = FetchResult("failed", error="invalid job record on the board")
                        continue
                    # Unlisted jobs are still readable by their direct link.
                    jobs[job["id"].lower()] = job
                self._boards[target.board] = jobs
        jobs = self._boards[target.board]
        return jobs if isinstance(jobs, FetchResult) else jobs.get(target.job_id)

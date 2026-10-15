"""Resolve supported HTTPS application links to allowlisted public job APIs.

No arbitrary hosts, credentials, nonstandard ports, path traversal or embedded
company sites are followed. Query strings from feeds never reach the API.
"""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, unquote, urlsplit

from auto_app.sources.common.urls import UUID, workday_job_id

LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
BOARD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
WORKDAY_HOST = re.compile(r"([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)\.wd\d+\.myworkdayjobs\.com")
GREENHOUSE_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io"}
LEVER_API = {"jobs.lever.co": "api.lever.co", "jobs.eu.lever.co": "api.eu.lever.co"}


@dataclass(frozen=True)
class Target:
    platform: str
    board: str
    job_id: str
    api_url: str


def _url(url: str):
    if not isinstance(url, str) or not url or re.search(r"[\x00-\x20\x7f\\]", url):
        return None
    try:
        value = urlsplit(url)
        if value.scheme != "https" or not value.hostname or value.username is not None or value.password is not None:
            return None
        if value.port not in (None, 443):
            return None
        return value
    except ValueError:
        return None


def _parts(path: str) -> list[str] | None:
    parts = [unquote(part) for part in path.strip("/").split("/")]
    if any(not p or p in (".", "..") or re.search(r"[\x00-\x1f\x7f/\\%?#;]", p) for p in parts):
        return None
    return parts


def target_for(url: str | None) -> Target | None:
    u = _url(url)
    if u is None or (parts := _parts(u.path)) is None:
        return None
    host = u.hostname.lower()
    if host in GREENHOUSE_HOSTS:
        if parts == ["embed", "job_app"]:
            query = parse_qs(u.query)
            if len(query.get("for", [])) != 1 or len(query.get("token", [])) != 1:
                return None
            board, job = query["for"][0], query["token"][0]
        elif len(parts) == 3 and parts[1] == "jobs":
            board, job = parts[0], parts[2]
        else:
            return None
        if not BOARD.fullmatch(board) or not re.fullmatch(r"[0-9]+", job):
            return None
        return Target("greenhouse", board.lower(), job,
                      f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job}")
    if host in {*LEVER_API, "jobs.ashbyhq.com"}:
        suffix = "application" if host == "jobs.ashbyhq.com" else "apply"
        if not (len(parts) == 2 or (len(parts) == 3 and parts[2] == suffix)):
            return None
        board, job = parts[:2]
        if not BOARD.fullmatch(board) or not UUID.fullmatch(job):
            return None
        if host == "jobs.ashbyhq.com":
            return Target("ashby", board.lower(), job.lower(), f"https://api.ashbyhq.com/posting-api/job-board/{board}")
        return Target("lever", board.lower(), job.lower(),
                      f"https://{LEVER_API[host]}/v0/postings/{board.lower()}/{job.lower()}")
    if match := WORKDAY_HOST.fullmatch(host):
        if LOCALE.fullmatch(parts[0]):
            parts = parts[1:]
        if parts and parts[-1].lower() in {"apply", "application", "applymanually"}:
            parts = parts[:-1]
        if len(parts) < 3 or parts[1] != "job" or not BOARD.fullmatch(parts[0]):
            return None
        job = workday_job_id(parts[-1])
        if job == parts[-1]:
            return None
        tenant, site = match[1], parts[0]
        path = "/".join(quote(p, safe="-_.~") for p in parts[1:])
        return Target("workday", f"{tenant}/{site.lower()}", job,
                      f"https://{host}/wday/cxs/{tenant}/{site}/{path}")
    return None


def valid_target(target: Target) -> bool:
    """Revalidate even directly constructed Targets at the network boundary."""
    if not isinstance(target, Target) or (u := _url(target.api_url)) is None or u.query or u.fragment:
        return False
    parts = _parts(u.path)
    if parts is None:
        return False
    host, source = u.hostname.lower(), None
    if target.platform == "greenhouse" and host == "boards-api.greenhouse.io":
        if len(parts) == 5 and parts[:2] == ["v1", "boards"] and parts[3] == "jobs":
            source = f"https://job-boards.greenhouse.io/{parts[2]}/jobs/{parts[4]}"
    elif target.platform == "lever" and host in LEVER_API.values():
        if len(parts) == 4 and parts[:2] == ["v0", "postings"]:
            app_host = next(k for k, value in LEVER_API.items() if value == host)
            source = f"https://{app_host}/{parts[2]}/{parts[3]}"
    elif target.platform == "ashby" and host == "api.ashbyhq.com":
        if len(parts) == 3 and parts[:2] == ["posting-api", "job-board"]:
            source = f"https://jobs.ashbyhq.com/{parts[2]}/{target.job_id}"
    elif target.platform == "workday" and WORKDAY_HOST.fullmatch(host):
        if len(parts) >= 6 and parts[:2] == ["wday", "cxs"] and parts[2] == host.split(".")[0]:
            source = f"https://{host}/" + "/".join(quote(p, safe="-_.~") for p in parts[3:])
    return source is not None and target_for(source) == target

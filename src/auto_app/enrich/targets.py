"""Resolve supported HTTPS application links to allowlisted public job APIs and pages.

No arbitrary hosts, credentials, nonstandard ports, path traversal or embedded
company sites are followed. Query strings from feeds never reach the API: every
request URL is rebuilt from the validated board and job id.
"""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, unquote, urlsplit

from auto_app.sources.common.urls import UUID, workday_job_id

LOCALE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
BOARD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
ASHBY_BOARD = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_. -]*[A-Za-z0-9_-])?")  # "jerry.ai", "Spectral Labs"
WORKDAY_HOST = re.compile(r"([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)\.wd\d+\.myworkdayjobs\.com")
# EU boards are read through the global boards API too: boards-api.eu.greenhouse.io does not exist.
GREENHOUSE_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io",
                    "job-boards.eu.greenhouse.io"}
LEVER_API = {"jobs.lever.co": "api.lever.co", "jobs.eu.lever.co": "api.eu.lever.co"}
# TikTok and ByteDance share one job system; jobs.bytedance.com redirects its English pages to joinbytedance.com.
TIKTOK_SITES = {"lifeattiktok.com": "lifeattiktok.com", "joinbytedance.com": "joinbytedance.com",
                "jobs.bytedance.com": "joinbytedance.com"}
AMAZON_LOCALE = re.compile(r"[a-z]{2}(?:-[a-z]{2})?")
# Eightfold tenants (<tenant>.eightfold.ai) answer for the career-site domain named in the link: ?domain=qualcomm.com.
EIGHTFOLD_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.eightfold\.ai")
DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,63}")
# Oracle Recruiting Cloud pods: efds.fa.em5.oraclecloud.com …
ORACLE_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.fa\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.oraclecloud\.com")
ORACLE_SITE = re.compile(r"[A-Za-z0-9_]{1,40}")
# Sites read only for enrichment: the request URL, and a public link that resolves to the same Target.
SITE_API = {
    "microsoft": lambda board, job: "https://apply.careers.microsoft.com/api/pcsx/position_details"
                                    f"?position_id={job}&domain=microsoft.com&hl=en",
    "tiktok": lambda board, job: f"https://{board}/search/{job}",
    "jobright": lambda board, job: f"https://jobright.ai/jobs/info/{job}",
    "smartrecruiters": lambda board, job: f"https://api.smartrecruiters.com/v1/companies/{board}/postings/{job}",
    "workable": lambda board, job: f"https://apply.workable.com/api/v2/accounts/{board}/jobs/{job}",
    "amazon": lambda board, job: f"https://www.amazon.jobs/en/search.json?base_query={job}&result_limit=10",
    # board "<tenant>.eightfold.ai/<domain>"
    "eightfold": lambda board, job: "https://{0}/api/pcsx/position_details?position_id={2}&domain={1}&hl=en".format(
        *board.partition("/")[::2], job),
    # board "<pod host>/<site>"; the finder's own syntax, the requisition id quoted
    "oracle": lambda board, job: ("https://{0}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
                                  "?expand=all&onlyData=true&finder=ById;Id=%22{2}%22,siteNumber={1}").format(
        *board.partition("/")[::2], job),
}
SITE_LINK = {
    "microsoft": lambda board, job: f"https://apply.careers.microsoft.com/careers/job/{job}",
    "tiktok": lambda board, job: f"https://{board}/search/{job}",
    "jobright": lambda board, job: f"https://jobright.ai/jobs/info/{job}",
    "smartrecruiters": lambda board, job: (
        f"https://jobs.smartrecruiters.com/oneclick-ui/company/{board}/publication/{job}" if UUID.fullmatch(job)
        else f"https://jobs.smartrecruiters.com/{board}/{job}"),
    "workable": lambda board, job: f"https://apply.workable.com/{board}/j/{job}",
    "amazon": lambda board, job: f"https://www.amazon.jobs/en/jobs/{job}",
    "eightfold": lambda board, job: "https://{0}/careers/job/{2}?domain={1}".format(*board.partition("/")[::2], job),
    "oracle": lambda board, job: "https://{0}/hcmUI/CandidateExperience/en/sites/{1}/job/{2}".format(
        *board.partition("/")[::2], job),
}


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


def _site(platform: str, board: str, job: str) -> Target:
    return Target(platform, board, job, SITE_API[platform](board, job))


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
        if not (ASHBY_BOARD if host == "jobs.ashbyhq.com" else BOARD).fullmatch(board) or not UUID.fullmatch(job):
            return None
        if host == "jobs.ashbyhq.com":
            return Target("ashby", board.lower(), job.lower(),
                          f"https://api.ashbyhq.com/posting-api/job-board/{quote(board, safe='.-_')}")
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
    return _site_target(host, parts, u.query)


def _site_target(host: str, parts: list[str], query: str = "") -> Target | None:
    if host == "apply.careers.microsoft.com":
        if len(parts) == 3 and parts[:2] == ["careers", "job"] and re.fullmatch(r"[0-9]{1,20}", parts[2]):
            return _site("microsoft", "microsoft.com", parts[2])
    elif host in TIKTOK_SITES:
        if host == "jobs.bytedance.com":
            parts = parts[1:] if parts[0] == "en" else parts
            parts = ["search", parts[1]] if len(parts) == 3 and parts[0] == "position" and parts[2] == "detail" else []
        if len(parts) == 2 and parts[0] == "search" and re.fullmatch(r"[0-9]{6,20}", parts[1]):
            return _site("tiktok", TIKTOK_SITES[host], parts[1])
    elif host in ("jobright.ai", "www.jobright.ai"):
        if len(parts) == 3 and parts[:2] == ["jobs", "info"] and re.fullmatch(r"[0-9a-f]{24}", parts[2]):
            return _site("jobright", "jobright.ai", parts[2])
    elif host == "jobs.smartrecruiters.com":
        if len(parts) == 5 and parts[:2] == ["oneclick-ui", "company"] and parts[3] == "publication":
            company, job = parts[2], parts[4].lower() if UUID.fullmatch(parts[4]) else ""
        elif len(parts) == 2 and (m := re.fullmatch(r"([0-9]{4,20})(?:-.*)?", parts[1])):  # "<id>-<title-slug>"
            company, job = parts[0], m[1]
        else:
            return None
        if job and BOARD.fullmatch(company):
            return _site("smartrecruiters", company, job)
    elif host == "apply.workable.com":
        if parts[-1] == "apply":
            parts = parts[:-1]
        if (len(parts) == 3 and parts[1] == "j" and BOARD.fullmatch(parts[0])
                and re.fullmatch(r"[0-9A-Za-z]{6,16}", parts[2])):
            return _site("workable", parts[0].lower(), parts[2].upper())
    elif host in ("www.amazon.jobs", "amazon.jobs"):
        if AMAZON_LOCALE.fullmatch(parts[0]):
            parts = parts[1:]
        if 2 <= len(parts) <= 3 and parts[0] == "jobs" and re.fullmatch(r"[0-9]{4,12}", parts[1]):
            return _site("amazon", "amazon.jobs", parts[1])
    elif EIGHTFOLD_HOST.fullmatch(host):
        domains = parse_qs(query).get("domain", [])
        if (len(parts) == 3 and parts[:2] == ["careers", "job"] and re.fullmatch(r"[0-9]{1,20}", parts[2])
                and len(domains) == 1 and DOMAIN.fullmatch(domains[0])):
            return _site("eightfold", f"{host}/{domains[0]}", parts[2])
    elif ORACLE_HOST.fullmatch(host):
        # /hcmUI/CandidateExperience/<language>/sites/<site>/job/<requisition>
        if (len(parts) == 7 and [p.lower() for p in parts[:2]] == ["hcmui", "candidateexperience"]
                and re.fullmatch(r"[a-z]{2}(?:-[A-Za-z]{2})?", parts[2]) and parts[3] == "sites"
                and ORACLE_SITE.fullmatch(parts[4]) and parts[5] == "job"
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}", parts[6])):  # 72872, req480976
            return _site("oracle", f"{host}/{parts[4]}", parts[6])
    return None


def valid_target(target: Target) -> bool:
    """Revalidate even directly constructed Targets at the network boundary."""
    if not isinstance(target, Target):
        return False
    if target.platform in SITE_LINK:
        # The request URL is rebuilt from the board and id, so it must equal the one a public link yields.
        try:
            return target_for(SITE_LINK[target.platform](target.board, target.job_id)) == target
        except (TypeError, AttributeError):
            return False
    if (u := _url(target.api_url)) is None or u.query or u.fragment:
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
            source = f"https://jobs.ashbyhq.com/{quote(parts[2], safe='.-_')}/{target.job_id}"
    elif target.platform == "workday" and WORKDAY_HOST.fullmatch(host):
        if len(parts) >= 6 and parts[:2] == ["wday", "cxs"] and parts[2] == host.split(".")[0]:
            source = f"https://{host}/" + "/".join(quote(p, safe="-_.~") for p in parts[3:])
    return source is not None and target_for(source) == target

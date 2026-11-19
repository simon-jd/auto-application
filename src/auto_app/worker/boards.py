"""Read-only suggestions for employer board feeds, ranked by kept jobs."""

from collections import Counter, defaultdict
import re
from urllib.parse import quote, unquote

from sqlalchemy import select
import yaml

from auto_app.core.db import tables as T
from auto_app.enrich.targets import ASHBY_BOARD, BOARD, target_for

LIST_APIS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{board}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true",
}
FORMATS = {"greenhouse": "greenhouse-board", "lever": "lever-board", "ashby": "ashby-board"}


def board_location(platform: str, board: str) -> str:
    # Ashby names may contain dots and spaces ("rivianvw.tech", "spectral labs"); its list API accepts them encoded.
    allowed = ASHBY_BOARD if platform == "ashby" else BOARD
    if platform not in LIST_APIS or not isinstance(board, str) or not allowed.fullmatch(board):
        raise ValueError("unsupported platform or invalid board identifier")
    return LIST_APIS[platform].format(board=quote(board, safe=".-_"))


def _same(location) -> str:
    """A board location compared without letter case, percent-encoding or a trailing slash: a feed configured as
    ".../job-board/Roadrunner" is the board "roadrunner"."""
    return unquote(str(location)).strip().lower().rstrip("/")


def suggest_boards(store, *, configured: set[str], top: int = 30, min_jobs: int = 2) -> list[dict]:
    known = {_same(location) for location in configured}
    p, j, s = T.postings, T.jobs, T.job_screenings
    visible = {job["job_id"] for job in store.jobs(include_excluded=False)}
    query = (select(p.c.apply_url, p.c.company, j.c.job_id, s.c.signals)
             .join(j, j.c.job_id == p.c.job_id).join(s, s.c.job_id == j.c.job_id)
             .where(j.c.archived_at.is_(None), j.c.duplicate_of.is_(None), s.c.outcome == "kept",
                    p.c.state == "linked", p.c.apply_url.is_not(None)))
    jobs, sponsoring, companies = defaultdict(set), defaultdict(set), defaultdict(dict)
    with store.engine.connect() as conn:
        for url, company, job_id, signals in conn.execute(query):
            if job_id not in visible:
                continue
            target = target_for(url)
            if target is None or target.platform not in LIST_APIS:
                continue
            try:
                location = board_location(target.platform, target.board)
            except ValueError:
                continue  # one unusable identifier must not end the whole read-only listing
            if target.platform == "lever":
                # The allowlisted target retains Lever's US/EU API region.
                location = target.api_url.rsplit("/", 1)[0] + "?mode=json"
            key = (target.platform, target.board, location)
            jobs[key].add(job_id)
            if (signals or {}).get("sponsorship") == "offers":
                sponsoring[key].add(job_id)
            if company:
                companies[key].setdefault(job_id, company)
    out = []
    for (platform, board, location), ids in jobs.items():
        if len(ids) < min_jobs:
            continue
        names = Counter(companies[(platform, board, location)].values())
        # Resolve equal votes explicitly so database row order cannot change output.
        company = min(names, key=lambda name: (-names[name], name)) if names else None
        out.append({"platform": platform, "board": board, "kept_jobs": len(ids),
                    "sponsoring_jobs": len(sponsoring[(platform, board, location)]), "company": company,
                    "location": location, "format": FORMATS[platform],
                    "source": re.sub(r"[^a-z0-9_-]+", "-", f"board-{platform}-{board}".lower()).strip("-")
                              + ("-eu" if location.startswith("https://api.eu.lever.co/") else ""),
                    "configured": _same(location) in known})
    out.sort(key=lambda b: (b["sponsoring_jobs"], b["kept_jobs"], b["board"], b["platform"]), reverse=True)
    return out[:top]


def as_yaml(boards: list[dict]) -> str:
    """Feed entries to paste under sources, omitting already configured boards."""
    entries = {}
    for board in boards:
        if board["configured"]:
            continue
        definition = {"kind": "repository", "format": board["format"]}
        if board["company"]:
            definition["company"] = board["company"]
        definition["location"] = board["location"]
        entries[board["source"]] = definition
    if not entries:
        return ""
    return "".join("  " + line + "\n" for line in yaml.safe_dump(entries, sort_keys=False, allow_unicode=True).splitlines())

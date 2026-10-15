"""Keep each active job's screening verdict current (docs/linux-worker.md §2).

A job is screened again only when its facts or the rules changed, so a round over ten thousand unchanged
jobs is a few reads and no writes. Rows for jobs that were archived or deleted are dropped.
"""

from collections import Counter
from datetime import datetime

from sqlalchemy import case, delete, func, insert, select

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow
from auto_app.screen.rules import JobFacts, ScreeningRules, screen

PLACEHOLDER_TITLE = "[Title pending]"  # IngestService's title for imports without one


def chunks(items, size: int = 500):
    """Lists of at most `size` items: keeps IN (…) under SQLite's bound-parameter limit."""
    if size <= 0:
        raise ValueError("chunk size must be positive")
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


UPSTREAM_KEYS = ("sponsorship", "category", "country")


def _upstream_text(payload, key: str):
    """metadata.upstream.<key> when it is a JSON string, else NULL (objects and numbers are not labels)."""
    path = f"$.metadata.upstream.{key}"
    return case((func.json_type(payload, path) == "text", func.json_extract(payload, path)), else_=None)


def _country(acc: dict, value) -> None:
    """Structured ISO-3166 alpha-2 country codes only; free text stays in locations."""
    if isinstance(value, str) and len(value.strip()) == 2 and value.strip().isalpha():
        acc["countries"].add(value.strip().upper())


def load_facts(store) -> list[JobFacts]:
    j, p, e, n = T.jobs, T.postings, T.ingest_entries, T.enrichments
    active = j.c.archived_at.is_(None)
    with store.engine.connect() as conn:
        postings = conn.execute(
            select(j.c.job_id, p.c.posting_id, p.c.source, p.c.title, p.c.parsed, p.c.apply_url,
                   j.c.primary_posting_id)
            .select_from(j.outerjoin(p, j.c.job_id == p.c.job_id))
            .where(active).order_by(j.c.job_id, p.c.posting_id)).mappings().all()
        # Only the three labels screening uses: never deserialize whole import payloads every round.
        upstream = conn.execute(
            select(p.c.job_id, *(_upstream_text(e.c.payload, key) for key in UPSTREAM_KEYS))
            .join(p, p.c.posting_id == e.c.posting_id)
            .join(j, j.c.job_id == p.c.job_id).where(active)).all()
        gone = set(conn.execute(select(n.c.url).where(n.c.status == "gone")).scalars())
    acc: dict[int, dict] = {}
    for r in postings:
        a = acc.setdefault(r["job_id"], {"titles": set(), "employment": set(), "sources": set(), "primary": "",
                                         "longest": "", "labels": set(), "categories": set(), "urls": set(),
                                         "locations": [], "countries": set()})
        parsed = r["parsed"] or {}
        a["titles"].add((r["title"] or parsed.get("title") or "").strip())
        if parsed.get("employment_type") not in (None, "", "unknown"):
            a["employment"].add(parsed["employment_type"])
        if r["source"]:
            a["sources"].add(r["source"])
        description = parsed.get("description_text") or ""
        if r["posting_id"] == r["primary_posting_id"]:
            a["primary"] = description
        if len(description) > len(a["longest"]):
            a["longest"] = description
        if r["apply_url"]:
            a["urls"].add(r["apply_url"])
        for location in parsed.get("locations") or []:
            if isinstance(location, str) and location.strip() and location.strip() not in a["locations"]:
                a["locations"].append(location.strip())
        _country(a, (parsed.get("extras") or {}).get("country"))
    for job_id, sponsorship, category, country in upstream:
        a = acc.get(job_id)
        if a is None:
            continue
        for value, target in ((sponsorship, "labels"), (category, "categories")):
            if isinstance(value, str) and value.strip():
                a[target].add(value.strip())
        _country(a, country)
    return [JobFacts(job_id=job_id, titles=tuple(sorted(a["titles"] - {"", PLACEHOLDER_TITLE})),
                     employment_types=tuple(sorted(a["employment"])), sources=tuple(sorted(a["sources"])),
                     description=a["primary"] or a["longest"], sponsorship_labels=tuple(sorted(a["labels"])),
                     categories=tuple(sorted(a["categories"])),
                     closed_at_source=bool(a["urls"]) and a["urls"] <= gone,
                     locations=tuple(a["locations"]), countries=tuple(sorted(a["countries"])))
            for job_id, a in sorted(acc.items())]


def screen_jobs(store, rules: ScreeningRules, *, now: datetime | None = None) -> dict:
    now = now or utcnow()
    s = T.job_screenings
    facts = load_facts(store)
    version = rules.version
    with store.engine.connect() as conn:
        saved = {r.job_id: (r.rules_version, r.facts_hash)
                 for r in conn.execute(select(s.c.job_id, s.c.rules_version, s.c.facts_hash))}
    rows, unchanged = [], 0
    for fact in facts:
        digest = fact.digest
        if saved.get(fact.job_id) == (version, digest):
            unchanged += 1
            continue
        result = screen(fact, rules)
        rows.append({"job_id": fact.job_id, "rules_version": version, "facts_hash": digest,
                     "outcome": result.outcome, "reasons": list(result.reasons), "signals": result.signals,
                     "screened_at": now})
    active = {fact.job_id for fact in facts}
    stale = [job_id for job_id in saved if job_id not in active]
    with store.engine.begin() as conn:
        for part in chunks(stale):
            conn.execute(delete(s).where(s.c.job_id.in_(part)))
        for part in chunks(rows):
            conn.execute(delete(s).where(s.c.job_id.in_([r["job_id"] for r in part])))
            conn.execute(insert(s), part)
    report = {"rules_version": version, "jobs": len(facts), "screened": len(rows), "unchanged": unchanged,
              "removed": len(stale), "kept": 0, "excluded": 0, "reasons": {}}
    with store.engine.connect() as conn:
        for outcome, count in conn.execute(select(s.c.outcome, func.count()).group_by(s.c.outcome)):
            report[outcome] = count
        reasons = Counter(reason.split(":", 1)[0]
                          for (values,) in conn.execute(select(s.c.reasons).where(s.c.outcome == "excluded"))
                          for reason in values or [])
    report["reasons"] = dict(reasons.most_common())
    return report


def excluded_job_ids(store) -> set[int]:
    s = T.job_screenings
    with store.engine.connect() as conn:
        return set(conn.execute(select(s.c.job_id).where(s.c.outcome == "excluded")).scalars())

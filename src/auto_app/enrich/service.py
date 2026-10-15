"""Enrich pending active imported jobs, using URL caches and bounded retry delays.

Only actual HTTP requests consume the round budget (hard maximum 200); reused
Ashby boards and cached successes are free. Failures retry after 30 minutes ×
2^(attempts-1), capped at 24 hours. Gone/unsupported records never retry. Empty
employer descriptions are unsupported. Import provenance and user decisions
remain intact. A reimport can restore a cached JD without another HTTP request.
"""

import copy
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as upsert

from auto_app.core.db import tables as T
from auto_app.core.models import CommonPosting, utcnow
from auto_app.enrich.targets import target_for
from auto_app.process.dedup import load_items
from auto_app.process.link import refresh_job_projection
from auto_app.process.normalize import CompanyAliases, normalize_posting, pending_enrichment
from auto_app.screen.store import PLACEHOLDER_TITLE, chunks
from auto_app.sources.registry import default_registry

PENDING = func.coalesce(func.json_extract(T.postings.c.parsed, "$.extras.ingestion.enrichment_status"), "") == "pending"
ENRICHED = func.json_extract(T.postings.c.parsed, "$.extras.enrichment.url").is_not(None)
FIELDS = {"title", "locations", "employment_type", "workplace_type", "salary", "req_id", "description_text",
          "description_html"}


def _aware(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def candidates(store) -> list[dict]:
    """Fresh requests require kept jobs; interrupted projections recover even if excluded."""
    p, j, s = T.postings, T.jobs, T.job_screenings
    excluded = select(s.c.job_id).where(s.c.outcome == "excluded")
    screened_out = j.c.job_id.in_(excluded)
    recovery = (p.c.state == "normalized") & ENRICHED
    query = (select(p.c.posting_id, p.c.job_id, p.c.apply_url, screened_out.label("excluded"))
             .join(j, j.c.job_id == p.c.job_id)
             .where(j.c.archived_at.is_(None), p.c.state != "archived", p.c.parser == "ingest-v1",
                    (PENDING & ~screened_out) | recovery,
                    p.c.apply_url.is_not(None), p.c.apply_url != "")
             .order_by(p.c.posted_at.desc().nulls_last(), p.c.posting_id.desc()))
    with store.engine.connect() as conn:
        return [dict(row) for row in conn.execute(query).mappings()]


def _cache(store, urls) -> dict[str, dict]:
    out = {}
    with store.engine.connect() as conn:
        for part in chunks(sorted(urls)):
            for row in conn.execute(select(T.enrichments).where(T.enrichments.c.url.in_(part))).mappings():
                out[row["url"]] = dict(row, next_attempt_at=_aware(row["next_attempt_at"]),
                                       fetched_at=_aware(row["fetched_at"]))
    return out


def _save(store, url: str, platform: str | None, status: str, now: datetime, *, data: dict | None = None,
          error: str | None = None, attempts: int = 0, next_attempt_at: datetime | None = None) -> dict:
    values = dict(url=url, platform=platform, status=status, attempts=attempts, next_attempt_at=next_attempt_at,
                  fetched_at=now if status in ("ok", "gone") else None, data=data, error=error, updated_at=now)
    statement = upsert(T.enrichments).values(**values)
    with store.engine.begin() as conn:
        conn.execute(statement.on_conflict_do_update(
            index_elements=["url"], set_={key: statement.excluded[key] for key in values if key != "url"}))
    return values


def _write_back(store, aliases: CompanyAliases, posting_id: int, cached: dict) -> int | None:
    """Merge current evidence under a brief write lock, after HTTP has completed."""
    p, j = T.postings, T.jobs
    data = cached["data"]
    with store.engine.begin() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        row = conn.execute(select(p.c.parsed, p.c.source, p.c.job_id, p.c.apply_url, p.c.state)
                           .join(j, j.c.job_id == p.c.job_id)
                           .where(p.c.posting_id == posting_id, j.c.archived_at.is_(None),
                                  p.c.parser == "ingest-v1", p.c.state != "archived")).first()
        if row is None or row.apply_url != cached["url"]:
            return None
        parsed = copy.deepcopy(row.parsed or {})
        enrichment = (parsed.get("extras") or {}).get("enrichment") or {}
        recovering = row.state == "normalized" and enrichment.get("url") == cached["url"]
        if not pending_enrichment(parsed) and not recovering:
            return None
        parsed["description_text"] = data["description_text"]
        if data.get("description_html"):
            parsed["description_html"] = data["description_html"]
        for key in ("employment_type", "workplace_type", "req_id"):
            if parsed.get(key) in (None, "", "unknown") and data.get(key) not in (None, "", "unknown"):
                parsed[key] = data[key]
        for key in ("salary", "locations"):
            if not parsed.get(key) and data.get(key):
                parsed[key] = data[key]
        title_filled = parsed.get("title") in (None, "", "unknown", PLACEHOLDER_TITLE) and bool(data.get("title"))
        if title_filled:
            parsed["title"] = data["title"]
        extras = dict(parsed.get("extras") or {})
        country = (data.get("extras") or {}).get("country")
        if isinstance(country, str) and len(country.strip()) == 2 and country.strip().isalpha():
            extras["country"] = country.strip().upper()
        extras["ingestion"] = {**(extras.get("ingestion") or {}), "enrichment_status": "complete"}
        if title_filled:
            extras["ingestion"]["title_observed"] = True
        extras["enrichment"] = {"platform": cached["platform"], "url": cached["url"],
                                "fetched_at": cached["fetched_at"].isoformat() if cached["fetched_at"] else None}
        parsed["extras"] = extras
        norm = normalize_posting(parsed, row.source, aliases, None)
        # Like ingestion, normalized state means the projection still needs refreshing.
        conn.execute(update(p).where(p.c.posting_id == posting_id).values(
            parsed=parsed, title=parsed.get("title"), normalized=norm, state="normalized",
            company_norm=norm["company_norm"], title_norm=norm["title_norm"], employer_key=norm["employer_key"],
            req_id=norm["req_id"], desc_hash=norm["desc_hash"]))
        return row.job_id


def enrich_jobs(store, fetcher, *, limit: int = 200, now: datetime | None = None) -> dict:
    """Fill pending, active, non-excluded ingest postings, newest first.

    Successful cache rows restore descriptions for free. Gone/unsupported rows
    are skipped; failures wait until next_attempt_at. Requests (including failed
    HTTP attempts) are capped at min(limit, 200). Missing metadata is filled
    conservatively, then normalized and projected without changing decisions.
    Excluded jobs may recover an interrupted projection from a successful cache
    entry, but cannot issue fresh requests. Fetch-only adapters cost one request
    per call; Fetcher reports its actual HTTP count and free Ashby cache reads.
    """
    now = _aware(now or utcnow())
    limit = max(0, min(200, limit))
    report = dict.fromkeys(("candidates", "fetched", "ok", "gone", "failed", "unsupported", "from_cache",
                            "waiting", "skipped_limit", "jobs_enriched"), 0)
    rows = candidates(store)
    report["candidates"] = len(rows)
    if not rows:
        return report
    registry = default_registry()
    aliases = CompanyAliases(registry.companies(), store.load_aliases())
    cache = _cache(store, {r["apply_url"] for r in rows})
    touched: set[int] = set()
    for row in rows:
        url = row["apply_url"]
        cached = cache.get(url)
        if cached and cached["status"] == "ok":
            if (job := _write_back(store, aliases, row["posting_id"], cached)) is not None:
                touched.add(job)
                report["from_cache"] += 1
            continue
        if cached and cached["status"] in ("unsupported", "gone"):
            continue
        if cached and cached["status"] == "failed" and cached["next_attempt_at"] and cached["next_attempt_at"] > now:
            report["waiting"] += 1
            continue
        if row["excluded"]:
            continue  # Recovery eligibility must never authorize a fresh employer request.
        target = target_for(url)
        if target is None:
            cache[url] = _save(store, url, None, "unsupported", now, error="no public job API for this link")
            report["unsupported"] += 1
            continue
        counted = isinstance(getattr(fetcher, "request_count", None), int) and callable(getattr(fetcher, "request_needed", None))
        cost = fetcher.request_needed(target) if counted else 1
        if report["fetched"] + cost > limit:
            report["skipped_limit"] += 1
            continue
        before = fetcher.request_count if counted else 0
        result = fetcher.fetch(target)
        report["fetched"] += fetcher.request_count - before if counted else 1
        if result.status == "ok" and isinstance(result.posting, CommonPosting):
            if not result.posting.description_text.strip():
                cache[url] = _save(store, url, target.platform, "unsupported", now,
                                   error="employer record has no description")
                report["unsupported"] += 1
                continue
            data = result.posting.model_dump(mode="json", include=FIELDS)
            # Retain the employer's structured geography without replacing import provenance.
            data["extras"] = {"country": result.posting.extras.get("country")}
            cache[url] = _save(store, url, target.platform, "ok", now, data=data)
            if (job := _write_back(store, aliases, row["posting_id"], cache[url])) is not None:
                touched.add(job)
            report["ok"] += 1
        elif result.status == "gone":
            cache[url] = _save(store, url, target.platform, "gone", now, error=result.error)
            report["gone"] += 1
        else:
            attempts = max(0, (cached or {}).get("attempts", 0)) + 1
            # Bound the exponent before multiplication; long-lived failures cannot overflow.
            wait = timedelta(minutes=min(24 * 60, 30 * 2 ** min(attempts - 1, 6)))
            cache[url] = _save(store, url, target.platform, "failed", now, error=result.error or "invalid fetch result",
                               attempts=attempts, next_attempt_at=now + wait)
            report["failed"] += 1
    if touched:
        # Acquire the write lock before reloading current evidence so concurrent
        # ingestion cannot be hidden by a stale projection or cleared marker.
        with store.transaction(immediate=True):
            items = {}
            for part in chunks(sorted(touched)):
                items.update(load_items(store, registry, job_ids=part))
            refresh_job_projection(store, items, touched)
            for part in chunks(sorted(touched)):
                store._tx.execute(update(T.postings).where(T.postings.c.job_id.in_(part),
                                  T.postings.c.state == "normalized", ENRICHED, ~PENDING).values(state="linked"))
    report["jobs_enriched"] = len(touched)
    return report

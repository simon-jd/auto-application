"""Sponsorship-first job digests and deduplicated operational alerts.

The worker supplies its first-run cutoff as ``since`` to leave the old backlog
for browsing. Title-only jobs wait two hours for enrichment. Only accepted
Discord messages mark jobs notified; failed batches stay pending.
"""

from datetime import datetime, timedelta, timezone
import hashlib

from sqlalchemy import func, select

from auto_app.core.db import tables as T
from auto_app.core.discord import MAX_EMBEDS, DiscordError, redact_webhook_secrets
from auto_app.core.models import utcnow
from auto_app.screen.store import chunks

OPEN_STATES = ("not_evaluated", "recommended")
ORDER = {"offers": 0, "unknown": 1, "no": 2}
BADGE = {"offers": "🟢 sponsors visas", "unknown": "⚪ sponsorship not stated", "no": "🟡 no sponsorship",
         "citizenship_required": "⛔ citizenship required"}
COLOR = {"offers": 0x2ECC71, "unknown": 0x95A5A6, "no": 0xF1C40F, "citizenship_required": 0xE74C3C}
READY_AFTER = timedelta(hours=2)
ALERT_EVERY = timedelta(hours=12)


def _when(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    except ValueError:
        return None
    if not isinstance(parsed, datetime):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def register_sources(store, state: dict, now: datetime) -> list[str]:
    """Baseline every source the worker hasn't seen yet; returns the newly registered names.

    state["source_since"][source] is None for sources that already had postings before notify_since (they
    follow it, including a later --notify-since), else the time the worker first saw the source: that
    feed's existing jobs — imported before this moment — are for browsing, not pushing.
    """
    registry = state.setdefault("source_since", {})
    since = _when(state.get("notify_since")) or _when(now)
    with store.engine.connect() as conn:
        rows = conn.execute(select(T.postings.c.source, func.min(T.postings.c.first_seen_at))
                            .group_by(T.postings.c.source).order_by(T.postings.c.source)).all()
    added = []
    for source, earliest in rows:
        if source in registry:
            continue
        earliest = _when(earliest)
        registry[source] = None if earliest is not None and earliest < since else _when(now).isoformat()
        added.append(source)
    return added


def _announceable(job: dict, since: datetime | None, source_since: dict | None) -> bool:
    first = _when(job.get("first_seen_at")) or datetime.min.replace(tzinfo=timezone.utc)
    if since is not None and first < since:
        return False
    if source_since is None:  # no registry: a direct caller, or state written before registries existed
        return True
    for source in job.get("sources") or []:
        if source in source_since:
            cutoff = _when(source_since[source]) if source_since[source] else since
            if cutoff is None or first >= cutoff:
                return True
    return False  # every source is unregistered or was added after this job appeared


def pending(store, limit: int, since: datetime | None = None, now: datetime | None = None,
            source_since: dict | None = None, *, job_id: int | None = None) -> tuple[list[dict], int]:
    """Return ready jobs up to the limit and the number of ready jobs left over."""
    now, since = _when(now or utcnow()), _when(since)
    jobs = [job for job in store.jobs(include_excluded=False, job_id=job_id)
            if job["state"] in OPEN_STATES and not job.get("notified_at")
            and _announceable(job, since, source_since)]
    s, jd, p = T.job_screenings, T.judgments, T.postings
    verdicts, reasons, links = {}, {}, {}
    with store.engine.connect() as conn:
        for part in chunks([job["job_id"] for job in jobs]):
            for row in conn.execute(select(s.c.job_id, s.c.outcome, s.c.signals).where(s.c.job_id.in_(part))):
                verdicts[row.job_id] = (row.outcome, row.signals or {})
            for row in conn.execute(select(jd.c.job_id, jd.c.reason).where(
                    jd.c.job_id.in_(part), jd.c.recommend.is_(True), jd.c.reason.is_not(None))
                    .order_by(jd.c.created_at)):
                reasons[row.job_id] = row.reason
            for row in conn.execute(select(p.c.job_id, p.c.apply_url).where(
                    p.c.job_id.in_(part), p.c.apply_url.is_not(None)).order_by(p.c.posting_id)):
                if row.apply_url:
                    links.setdefault(row.job_id, row.apply_url)
    ready = []
    for job in jobs:
        outcome, signals = verdicts.get(job["job_id"], (None, {}))
        if outcome != "kept":
            continue
        described = bool(signals.get("has_description"))
        if not described and now - (_when(job.get("first_seen_at")) or now) < READY_AFTER:
            continue
        recommended = job["state"] == "recommended"
        ready.append({**job, "sponsorship": signals.get("sponsorship", "unknown"),
                      "years_required": signals.get("years_required"), "title_only": not described,
                      "recommended": recommended, "reason": reasons.get(job["job_id"]) if recommended else None,
                      "link": job.get("canonical_url") or links.get(job["job_id"])})
    ready.sort(key=lambda job: _when(job.get("posted_at")) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    ready.sort(key=lambda job: not job["recommended"])
    ready.sort(key=lambda job: ORDER.get(job["sponsorship"], 3))
    limit = max(0, limit)
    return ready[:limit], max(0, len(ready) - limit)


def _clip(text: str, limit: int) -> str:
    return text.encode("utf-16-le")[:limit * 2].decode("utf-16-le", errors="ignore")


def _where(locations: list[str]) -> str:
    if not locations:
        return "not stated"
    return locations[0] + (f" +{len(locations) - 1}" if len(locations) > 1 else "")


def embed(job: dict) -> dict:
    sponsorship = job.get("sponsorship", "unknown")
    fields = [{"name": "Where", "value": _clip(_where(job.get("locations") or []), 100), "inline": True},
              {"name": "Visa", "value": BADGE.get(sponsorship, BADGE["unknown"]), "inline": True},
              {"name": "Posted", "value": (job.get("posted_at") or "not stated")[:10], "inline": True}]
    notes = []
    if job.get("recommended"):
        notes.append("⭐ your filter recommends it" + (f": {_clip(job['reason'], 250)}" if job.get("reason") else ""))
    if job.get("years_required"):
        notes.append(f"asks for {job['years_required']}+ years of experience")
    if job.get("title_only"):
        notes.append("title only — open the link for the description")
    if notes:
        fields.append({"name": "Notes", "value": _clip("\n".join(notes), 400)})
    result = {"title": _clip(f"#{job['job_id']} {job.get('title') or 'Untitled'} — {job.get('company') or '?'}", 256),
              "color": COLOR.get(sponsorship, COLOR["unknown"]), "fields": fields,
              "footer": {"text": f"job {job['job_id']}"}}
    if job.get("link"):
        result["url"] = job["link"]
    return result


def batches(store, *, limit: int, since: datetime | None, now: datetime,
            source_since: dict | None = None) -> tuple[list[list[dict]], int]:
    """Plan Discord-sized messages without putting the same link twice in one message."""
    jobs, more = pending(store, limit, since, now, source_since)
    result, offset = [], 0
    while offset < len(jobs):
        batch, links = [], set()
        for job in jobs[offset:offset + MAX_EMBEDS]:
            if job.get("link") and job["link"] in links:
                break
            batch.append(job)
            links.add(job.get("link"))
        result.append(batch)
        offset += len(batch)
    return result, more


def mark_notified(store, jobs: list[dict], now: datetime) -> None:
    store.set_notified([job["job_id"] for job in jobs], now)


def send_digest(store, discord, *, limit: int = 30, since: datetime | None = None,
                now: datetime | None = None, source_since: dict | None = None) -> dict:
    now = now or utcnow()
    plan, more = batches(store, limit=limit, since=since, now=now, source_since=source_since)
    total = sum(len(batch) for batch in plan)
    sent = 0
    for batch in plan:
        header = f"**{total + more} new job(s)** — visa sponsorship first" if sent == 0 else None
        try:
            discord.post(header, [embed(job) for job in batch])
        except DiscordError as exc:
            return {"sent": sent, "waiting": total - sent + more,
                    "error": redact_webhook_secrets(str(exc))}
        mark_notified(store, batch, now)
        sent += len(batch)
    if more:
        try:
            discord.post(f"…and {more} more — they come in the next digest, or ask in chat.")
        except DiscordError:
            pass
    return {"sent": sent, "waiting": more}


def _problems(report: dict, ingest: dict) -> dict[str, str]:
    problems = {}
    for name, step in (report.get("steps") or {}).items():
        if step.get("status") == "error":
            name = redact_webhook_secrets(str(name))
            error = redact_webhook_secrets(str(step.get("error") or ""))
            fingerprint = hashlib.sha256(error.encode()).hexdigest()[:16]
            problems[f"{name}:{fingerprint}"] = f"worker step `{name}` failed: {error[:300]}"
    if (ingest or {}).get("consecutive_failures", 0) >= 3:
        problems["ingest:failing"] = (f"the feed worker failed {ingest['consecutive_failures']} rounds in a row "
                                      f"(`auto-app ingest status`)")
    return problems


def send_alerts(discord, state: dict, report: dict, ingest: dict, now: datetime) -> list[str]:
    if discord is None:
        return []
    now = _when(now)
    sent_at = state.setdefault("alerts", {})
    for key, value in list(sent_at.items()):
        last = _when(value)
        if last is None or now - last > 2 * ALERT_EVERY:
            del sent_at[key]
    sent = []
    for key, message in _problems(report, ingest).items():
        last = _when(sent_at.get(key))
        if last and now - last < ALERT_EVERY:
            continue
        try:
            discord.post(f"⚠️ {message}"[:2000])
        except DiscordError:
            break
        sent_at[key] = now.isoformat()
        sent.append(key)
    return sent


UPDATE_ICONS = {"submitted": "✅ submitted", "needs_input": "🙋 needs you", "action_needed": "⛔ blocked",
                "failed": "❌ failed", "unsupported": "⏭ skipped"}


def handoff_updates(store, after_attempt_id: int) -> list[dict]:
    """Read a bounded page of external-agent outcomes, including retained application history."""
    if type(after_attempt_id) is not int or after_attempt_id < 0:
        raise ValueError("invalid handoff notification cursor")
    a, ap, j, p = T.attempts, T.applications, T.jobs, T.postings
    query = (select(a.c.attempt_id, ap.c.job_id, a.c.outcome, a.c.reason.label("note"), p.c.title, p.c.company)
             .select_from(a.join(ap, ap.c.application_id == a.c.application_id)
                          .outerjoin(j, j.c.job_id == ap.c.job_id)
                          .outerjoin(p, p.c.posting_id == j.c.primary_posting_id))
             .where(a.c.attempt_id > after_attempt_id, a.c.applier == "external-agent")
             .order_by(a.c.attempt_id).limit(50))
    with store.engine.connect() as conn:
        return [dict(row) for row in conn.execute(query).mappings()]


def handoff_batches(store, after_attempt_id: int) -> list[tuple[str, int, int]]:
    """Text, last attempt id, and count per Discord-sized message."""
    result, lines, cursor = [], [], after_attempt_id
    for item in handoff_updates(store, after_attempt_id):
        line = (f"{UPDATE_ICONS.get(item['outcome'], '?')}: #{item['job_id']} "
                f"{_clip(item['title'] or '?', 180)} — {_clip(item['company'] or '?', 120)}")
        if item["note"]:
            line += " — " + _clip(redact_webhook_secrets(item["note"]), 200)
        if lines and len(("\n".join([*lines, line])).encode("utf-16-le")) // 2 > 1900:
            result.append(("\n".join(lines), cursor, len(lines)))
            lines = []
        lines.append(line)
        cursor = item["attempt_id"]
    if lines:
        result.append(("\n".join(lines), cursor, len(lines)))
    return result


def send_handoff_updates(store, discord, state: dict) -> dict:
    sent = 0
    for content, cursor, count in handoff_batches(store, state.get("handoff_cursor", 0)):
        try:
            discord.post(content)
        except DiscordError as exc:
            return {"sent": sent, "error": redact_webhook_secrets(str(exc))}
        state["handoff_cursor"] = cursor
        sent += count
    return {"sent": sent}


def summary_text(summary: dict, queue_size: int | None) -> str:
    week = ", ".join(f"{kind} {count}" for kind, count in sorted((summary.get("by_kind") or {}).items()))
    pipeline = ", ".join(f"{kind} {count}" for kind, count in sorted((summary.get("active_pipeline") or {}).items()))
    lines = [f"📊 Weekly: {week or 'no new events'}", f"Applied so far: {summary.get('applied_total', 0)}"]
    if pipeline:
        lines.append(f"In progress: {pipeline}")
    if queue_size is not None:
        lines.append(f"Waiting in the application queue: {queue_size}")
    return "\n".join(lines)

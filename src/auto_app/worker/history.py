"""The owner's record with a job's company, for the digest card: applied, wanted and skipped jobs there, the
30-day submission count against the handoff's company limit, and jobs with the same title.

Computed from the database (decisions on jobs, company_norm and title_norm), not by a model. Companies are matched by
company_norm, with the handoff's company_groups ("amazon web services" is "amazon"), as the queue's company limit
counts them. A job's own row is never part of its history.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow

SHOWN = 3  # applied jobs listed by title on the card


def _aware(value):
    if value is None:
        return None
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def company_history(store, job_ids, guard, now=None) -> dict[int, dict]:
    """{job_id: {"applied", "wanted", "skipped", "recent_applied", "limit", "window_days", "same_title"}}."""
    now = _aware(now or utcnow())
    groups = guard.company_keys()
    key = lambda norm: groups.get(norm or "", norm or "")  # noqa: E731
    j, p = T.jobs, T.postings
    with store.engine.connect() as conn:
        targets = {r.job_id: r for r in conn.execute(select(j.c.job_id, j.c.company_norm, j.c.title_norm)
                                                     .where(j.c.job_id.in_(list(job_ids))))}
        decided = conn.execute(select(j.c.job_id, j.c.company_norm, j.c.title_norm, j.c.decision, j.c.decided_at,
                                      p.c.title)
                               .select_from(j.outerjoin(p, p.c.posting_id == j.c.primary_posting_id))
                               .where(j.c.decision.is_not(None))).all()
    by_company: dict[str, list] = {}
    for row in decided:
        by_company.setdefault(key(row.company_norm), []).append(row)
    window = timedelta(days=guard.company_window_days)
    history = {}
    for job_id in job_ids:
        target = targets.get(job_id)
        rows = [r for r in by_company.get(key(target.company_norm), []) if r.job_id != job_id] if target else []
        rows.sort(key=lambda r: _aware(r.decided_at) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

        def item(r):
            return {"job_id": r.job_id, "title": r.title, "at": _aware(r.decided_at).isoformat() if r.decided_at else None}

        applied = [item(r) for r in rows if r.decision == "applied"]
        history[job_id] = {
            "applied": applied,
            "wanted": [item(r) for r in rows if r.decision == "want_to_apply"],
            "skipped": sum(r.decision == "not_applied" for r in rows),
            "recent_applied": sum(1 for r in rows if r.decision == "applied" and r.decided_at
                                  and now - _aware(r.decided_at) <= window),
            "limit": guard.company_limit, "window_days": guard.company_window_days,
            "same_title": [r.job_id for r in rows if target and target.title_norm and r.title_norm == target.title_norm
                           and r.decision in ("applied", "want_to_apply")],
        }
    return history


def history_text(history: dict) -> str:
    """The card's lines, in the owner's language."""
    applied, wanted, skipped = history["applied"], history["wanted"], history["skipped"]
    if not applied and not wanted and not skipped:
        return "没有投过这家公司"
    lines = []
    if applied:
        titles = "；".join(f"{a['title'] or '?'}（{(a['at'] or '')[:10]}）" for a in applied[:SHOWN])
        more = f" 等 {len(applied)} 个" if len(applied) > SHOWN else ""
        lines.append(f"已投 {len(applied)}：{titles}{more}")
    counts = [f"想投 {len(wanted)}" if wanted else "", f"不投 {skipped}" if skipped else ""]
    if any(counts):
        lines.append(" · ".join(c for c in counts if c))
    lines.append(f"{history['window_days']} 天内已投 {history['recent_applied']} / 上限 {history['limit']}")
    if history["same_title"]:
        lines.append("⚠ 投过或想投同名岗位：" + "、".join(f"#{i}" for i in history["same_title"]))
    return "\n".join(lines)

# src/auto_app/worker/round.py
"""One round of the Linux worker (docs/linux-worker.md): screen → enrich → screen again → Discord.

No model or agent is called here: model filtering is the user's own `auto-app filter run`, when their
quota allows. Steps are isolated: a failing step is reported and the next ones still run, so a Discord
outage never stops enrichment. Nothing here applies to a job.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path

from auto_app.core.models import utcnow
from auto_app.core.discord import redact_webhook_secrets

STEPS = ("screen", "enrich", "notify", "summary", "health")


@dataclass
class WorkerOptions:
    screening_path: Path
    enrich_limit: int = 200
    digest_limit: int = 30
    steps: tuple[str, ...] = STEPS
    min_free_gb: float = 2.0

    def __post_init__(self):
        if not self.steps or any(s not in STEPS for s in self.steps):
            raise ValueError("steps must contain only: " + ", ".join(STEPS))
        if type(self.min_free_gb) not in (int, float) or not math.isfinite(self.min_free_gb) or self.min_free_gb < 0:
            raise ValueError("min_free_gb must be a finite nonnegative number")
        if type(self.enrich_limit) is not int or not 0 <= self.enrich_limit <= 200:
            raise ValueError("enrich_limit must be between 0 and 200")
        if type(self.digest_limit) is not int or not 0 <= self.digest_limit <= 30:
            raise ValueError("digest_limit must be between 0 and 30")


def run_round(store, opts: WorkerOptions, *, fetcher, discord, state: dict, now: datetime | None = None,
              save_state=None) -> dict:
    """fetcher: enrich.fetch.Fetcher or None (no enrich step); discord: core.discord.Discord or None.
    `state` is the worker's saved state, updated in place; save_state() persists it mid-round."""
    from auto_app.enrich.service import enrich_jobs
    from auto_app.screen import store as screening
    from auto_app.screen.rules import load_rules

    now = now or utcnow()
    state.setdefault("notify_since", now.isoformat())  # the first round: today's backlog is not pushed
    report = {"status": "ok", "started_at": now.isoformat(), "steps": {}}

    def step(name, work):
        try:
            result = work()
        except Exception as exc:  # reported; the next steps still run
            result = {"status": "error", "error": redact_webhook_secrets(f"{type(exc).__name__}: {exc}")[:400]}
        report["steps"][name] = result
        return result

    def screen():
        from auto_app.worker.digest import register_sources

        # Baseline feeds seen for the first time (their existing jobs are never pushed), so the
        # Discord bot, which only reads this state, gates new feeds as the worker's own digest does.
        register_sources(store, state, now)
        return {"status": "ok", **screening.screen_jobs(store, load_rules(opts.screening_path), now=now)}

    if "screen" in opts.steps:
        step("screen", screen)
    if "enrich" in opts.steps and fetcher is not None:
        enriched = step("enrich", lambda: {"status": "ok",
                                           **enrich_jobs(store, fetcher, limit=opts.enrich_limit, now=now)})
        interrupted = enriched.get("status") == "error"
        changed = sum(enriched.get(key, 0) for key in ("ok", "from_cache", "gone"))
        if enriched.get("failed", 0) >= 3 and changed == 0:
            # Keep the alert fingerprint stable as the failure count changes.
            enriched.update(status="error", error="Every employer request failed this round; retry scheduled")
        if "screen" in opts.steps and (changed or enriched.get("jobs_enriched") or interrupted):
            # Interrupted enrichment can save new facts before losing its counters.
            # New descriptions and gone/404 results both change screening facts.
            step("rescreen", screen)
    elif "enrich" in opts.steps:
        report["steps"]["enrich"] = {"status": "skipped", "reason": "no enrichment transport"}
    if "notify" in opts.steps:
        if any(report["steps"].get(name, {}).get("status") == "error" for name in ("screen", "rescreen")):
            report["steps"]["notify"] = {"status": "skipped", "reason": "screening failed; awaiting a current verdict"}
        else:
            step("notify", lambda: _notify(store, discord, opts, state, now))
        step("handoff", lambda: _handoff(store, discord, state))
    if "summary" in opts.steps:
        step("summary", lambda: _summary(store, discord, state, now, save_state))
    if "health" in opts.steps:
        step("health", lambda: _health(store, opts))
    if any(s["status"] == "error" for s in report["steps"].values()):
        report["status"] = "partial"
    return report


def _notify(store, discord, opts: WorkerOptions, state: dict, now: datetime) -> dict:
    from auto_app.worker.digest import register_sources, send_digest

    if discord is None:
        return {"status": "skipped", "reason": "no Discord webhook configured (AUTO_APP_DISCORD_WEBHOOK_URL)"}
    register_sources(store, state, now)
    result = send_digest(store, discord, limit=opts.digest_limit,
                         since=datetime.fromisoformat(state["notify_since"]), now=now,
                         source_since=state["source_since"])
    return {"status": "error" if result.get("error") else "ok", **result}


def _handoff(store, discord, state: dict) -> dict:
    from auto_app.worker.digest import send_handoff_updates

    if discord is None:
        return {"status": "skipped", "reason": "no Discord webhook configured"}
    result = send_handoff_updates(store, discord, state)
    return {"status": "error" if result.get("error") else "ok", **result}


def _summary(store, discord, state: dict, now: datetime, save_state=None) -> dict:
    from auto_app.handoff.service import HandoffService
    from auto_app.track.events import weekly_summary
    from auto_app.worker.digest import summary_text

    if discord is None:
        return {"status": "skipped", "reason": "no Discord webhook configured"}
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    last = state.get("summary_at")
    if last:
        previous = datetime.fromisoformat(last)
        previous = previous.replace(tzinfo=timezone.utc) if previous.tzinfo is None else previous
        if now - previous < timedelta(days=7):
            return {"status": "skipped", "reason": "not due"}
    queue_size = None
    try:
        queue_size = HandoffService(store.db_path, preferences_path=store.paths.preferences).queue(now=now)["total"]
    except ValueError:
        pass  # An invalid optional handoff policy does not hide the application summary.
    text = summary_text(weekly_summary(store, now - timedelta(days=7)), queue_size)
    # Mark the week sent before posting: if the round's state write later fails, a delivered
    # summary must not be resent every round. A failed post restores the previous mark.
    state["summary_at"] = now.isoformat()
    if save_state is not None:
        save_state()
    try:
        discord.post(text)
    except Exception:
        if last:
            state["summary_at"] = last
        else:
            state.pop("summary_at", None)
        if save_state is not None:
            try:
                save_state()
            except OSError:
                pass  # The round's own state write retries this restore.
        raise
    return {"status": "ok", "sent": True}


def _health(store, opts: WorkerOptions) -> dict:
    import shutil

    free = shutil.disk_usage(Path(store.db_path).parent).free / 2**30
    if free < opts.min_free_gb:
        return {"status": "error", "error": f"database disk free space is below {opts.min_free_gb:g} GB",
                "free_gb": round(free, 2)}
    return {"status": "ok", "free_gb": round(free, 2)}

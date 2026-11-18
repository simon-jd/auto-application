"""Digest cards: the owner's record with the same company (company groups apply), computed from the database."""

from datetime import datetime, timedelta, timezone

from sqlalchemy import update

from auto_app.core.db import tables as T
from auto_app.handoff.guard import Guard
from auto_app.worker.history import company_history, history_text
from tests.imported_jobs import ingest, job_id_of, make_store, record

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def decide(store, job, decision, days_ago):
    with store.engine.begin() as conn:
        conn.execute(update(T.jobs).where(T.jobs.c.job_id == job).values(
            decision=decision, decided_at=NOW - timedelta(days=days_ago)))


def test_history_counts_the_same_company_and_flags_the_same_title(tmp_path):
    store = make_store(tmp_path)
    ingest(store, [record("1", "Software Engineer I", company="Acme"),
                   record("2", "Software Engineer, New Grad", company="Acme",
                          url="https://job-boards.greenhouse.io/acme/jobs/2"),
                   record("3", "Data Engineer", company="Acme", url="https://job-boards.greenhouse.io/acme/jobs/3"),
                   record("4", "ML Engineer", company="Acme", url="https://job-boards.greenhouse.io/acme/jobs/4"),
                   record("5", "Backend Engineer", company="Acme Cloud",
                          url="https://job-boards.greenhouse.io/acmecloud/jobs/5"),
                   record("6", "Platform Engineer", company="Other Co",
                          url="https://job-boards.greenhouse.io/other/jobs/6")])
    ids = {t: job_id_of(store, t) for t in ("Data Engineer", "ML Engineer", "Backend Engineer", "Platform Engineer")}
    old_grad, new = job_id_of(store, "Software Engineer I"), job_id_of(store, "Software Engineer, New Grad")
    with store.engine.begin() as conn:  # a repost of the same role under the same normalized title
        same = conn.execute(T.jobs.select().where(T.jobs.c.job_id == new)).mappings().first()["title_norm"]
        conn.execute(update(T.jobs).where(T.jobs.c.job_id == old_grad).values(title_norm=same))
    decide(store, old_grad, "applied", 40)
    decide(store, ids["Data Engineer"], "applied", 5)
    decide(store, ids["ML Engineer"], "not_applied", 2)
    decide(store, ids["Backend Engineer"], "want_to_apply", 1)
    guard = Guard(company_groups=(("acme cloud", "acme"),))
    history = company_history(store, [new, ids["Platform Engineer"]], guard, now=NOW)
    store.engine.dispose()
    acme = history[new]
    assert [a["job_id"] for a in acme["applied"]] == [ids["Data Engineer"], old_grad]  # newest first
    assert [w["job_id"] for w in acme["wanted"]] == [ids["Backend Engineer"]]  # through the company group
    assert acme["skipped"] == 1 and acme["recent_applied"] == 1 and acme["limit"] == 3
    assert acme["same_title"] == [old_grad]
    text = history_text(acme)
    assert "已投 2" in text and "Data Engineer" in text and "想投 1" in text and "不投 1" in text
    assert "30 天内已投 1 / 上限 3" in text and f"#{old_grad}" in text and "同名岗位" in text
    assert history[ids["Platform Engineer"]]["applied"] == []
    assert history_text(history[ids["Platform Engineer"]]) == "没有投过这家公司"


def test_the_card_shows_the_history_field():
    from auto_app.worker.digest import embed

    card = embed({"job_id": 7, "title": "SWE", "company": "Acme", "sponsorship": "offers",
                  "history": {"applied": [], "wanted": [], "skipped": 0, "recent_applied": 0, "limit": 3,
                              "window_days": 30, "same_title": []}})
    assert {"name": "同公司记录", "value": "没有投过这家公司"} in card["fields"]
    assert all(f["name"] != "同公司记录" for f in embed({"job_id": 7, "title": "SWE", "company": "Acme"})["fields"])

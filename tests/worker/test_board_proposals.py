"""Weekly board proposals: unconnected employer boards offered on Discord, added to feeds.yaml on the owner's click."""

from datetime import datetime, timedelta, timezone

import pytest
import yaml
from sqlalchemy import insert, update

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow
from auto_app.ingest.runner import load_feed_config
from auto_app.worker.board_proposals import BoardProposalError, BoardProposals
from tests.imported_jobs import ingest, job_id_of, make_store, record

NOW = datetime(2026, 11, 19, 9, 0, tzinfo=timezone.utc)
FEEDS = """skip_internships: true
sources:
  # the core feeds
  simplify-newgrad:
    kind: repository
    location: listings.json
"""


@pytest.fixture
def home(tmp_path):
    store = make_store(tmp_path)
    ingest(store, [record("101", "Backend Engineer"), record("102", "Data Engineer"),
                   record("103", "ML Engineer", company="Beta",
                          url="https://jobs.lever.co/beta/6ed76ce8-1111-2222-3333-444455556666"),
                   record("104", "Data Annotator", company="Gamma Annotation Services",
                          url="https://job-boards.greenhouse.io/gammaannotation/jobs/104")])
    with store.engine.begin() as conn:
        for title in ("Backend Engineer", "Data Engineer", "ML Engineer", "Data Annotator"):
            conn.execute(insert(T.job_screenings).values(
                job_id=job_id_of(store, title), rules_version="t", facts_hash="h", outcome="kept", reasons=[],
                signals={"sponsorship": "unknown"}, screened_at=utcnow()))
        conn.execute(update(T.jobs).where(T.jobs.c.job_id == job_id_of(store, "ML Engineer"))
                     .values(decision="want_to_apply", decided_at=NOW))
    store.engine.dispose()
    (tmp_path / "config").mkdir(exist_ok=True)
    (tmp_path / "config" / "feeds.yaml").write_text(FEEDS)
    (tmp_path / "config" / "listings.json").write_text("[]")
    return tmp_path


def proposals(home):
    return BoardProposals(home / "data" / "auto_app.db")


def test_generate_ranks_wanted_companies_first_and_leaves_out_vendors(home):
    made = proposals(home).generate(now=NOW)
    assert [(p["platform"], p["board"]) for p in made] == [("lever", "beta"), ("greenhouse", "acme")]
    assert made[0]["wanted"] == 1 and made[1]["kept_jobs"] == 2
    assert "Data Engineer" in made[1]["examples"] and made[1]["board_page"] == "https://job-boards.greenhouse.io/acme"
    assert proposals(home).generate(now=NOW) == []  # nothing is proposed twice
    assert [p["board"] for p in proposals(home).to_post()] == ["beta", "acme"]


def test_adding_appends_a_validated_feed_and_keeps_the_comments(home):
    store = proposals(home)
    [beta, acme] = store.generate(now=NOW)
    added = store.decide(acme["proposal_id"], "add", now=NOW)
    assert added == {"proposal_id": acme["proposal_id"], "decision": "added", "feed": "board-greenhouse-acme"}
    text = (home / "config" / "feeds.yaml").read_text()
    assert "# the core feeds" in text
    feed = load_feed_config(home / "config" / "feeds.yaml")["board-greenhouse-acme"]
    assert feed["format"] == "greenhouse-board" and feed["every_hours"] == 6
    assert (home / "config" / f"feeds.yaml.bak-before-board-proposals-{NOW:%Y%m%d}").read_text() == FEEDS
    with pytest.raises(BoardProposalError, match="already"):
        store.decide(acme["proposal_id"], "skip", now=NOW)
    assert store.decide(beta["proposal_id"], "skip", now=NOW)["decision"] == "skipped"
    assert "beta" not in (home / "config" / "feeds.yaml").read_text()
    assert store.to_post() == [] or all(p["decision"] is None for p in store.to_post())


def test_a_board_already_in_feeds_is_not_proposed_and_a_taken_name_gets_a_suffix(home):
    path = home / "config" / "feeds.yaml"
    path.write_text(FEEDS + "  board-greenhouse-acme:\n    kind: repository\n    location: listings.json\n")
    store = proposals(home)
    made = store.generate(now=NOW)
    assert [p["board"] for p in made] == ["beta", "acme"]  # the name is taken but the board is not configured
    acme = made[1]
    assert store.decide(acme["proposal_id"], "add", now=NOW)["feed"] == "board-greenhouse-acme-2"
    assert "board-greenhouse-acme-2" in yaml.safe_load(path.read_text())["sources"]
    path.write_text(FEEDS + "  x:\n    kind: repository\n    format: lever-board\n    company: Beta\n"
                            "    location: https://api.lever.co/v0/postings/BETA?mode=json\n")
    fresh = home / "data" / "auto_app.db"
    with make_store(home).engine.begin() as conn:
        conn.execute(T.board_proposals.delete())
    assert [p["board"] for p in BoardProposals(fresh).generate(now=NOW)] == ["acme"]  # beta configured as BETA


def test_due_once_a_week():
    assert BoardProposals.due(None, NOW)
    assert not BoardProposals.due((NOW - timedelta(days=6)).isoformat(), NOW)
    assert BoardProposals.due((NOW - timedelta(days=7)).isoformat(), NOW)


def test_bad_decisions_are_refused(home):
    store = proposals(home)
    [beta, _] = store.generate(now=NOW)
    with pytest.raises(BoardProposalError):
        store.decide(beta["proposal_id"], "maybe")
    with pytest.raises(BoardProposalError, match="not found"):
        store.decide(999999, "add")

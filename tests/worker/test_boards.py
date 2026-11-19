"""Recommendations count kept jobs once per board and emit usable YAML."""

import pytest
import json
from sqlalchemy import insert, update
import yaml

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow
from tests.imported_jobs import ingest, job_id_of, make_store, record


@pytest.fixture
def store(tmp_path):
    result = make_store(tmp_path)
    ingest(result, [record("101", "Backend Engineer"), record("102", "Data Engineer"),
                    record("103", "ML Engineer", url="https://jobs.lever.co/beta/6ed76ce8-1111-2222-3333-444455556666")])
    # A repository copy must not inflate the board's job count.
    ingest(result, [record("101-copy", "Backend Engineer", url="https://job-boards.greenhouse.io/acme/jobs/101")],
           source="second-repository")
    with result.engine.begin() as conn:
        for title, sponsorship in (("Backend Engineer", "offers"), ("Data Engineer", "unknown"), ("ML Engineer", "no")):
            conn.execute(insert(T.job_screenings).values(
                job_id=job_id_of(result, title), rules_version="t", facts_hash="h", outcome="kept", reasons=[],
                signals={"sponsorship": sponsorship}, screened_at=utcnow()))
    yield result
    result.engine.dispose()


def test_boards_rank_by_sponsoring_then_unique_kept_jobs(store):
    from auto_app.worker.boards import suggest_boards
    boards = suggest_boards(store, configured=set(), top=10, min_jobs=1)
    assert [(b["platform"], b["board"], b["kept_jobs"], b["sponsoring_jobs"]) for b in boards] == [
        ("greenhouse", "acme", 2, 1), ("lever", "beta", 1, 0)]
    assert boards[0]["location"] == "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
    assert boards[0]["source"] == "board-greenhouse-acme" and boards[0]["company"] == "Acme"


def test_configured_boards_and_small_boards_are_marked_or_dropped(store):
    from auto_app.worker.boards import suggest_boards
    boards = suggest_boards(store, configured={"https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"},
                            top=10, min_jobs=2)
    assert [(b["board"], b["configured"]) for b in boards] == [("acme", True)]


def test_only_kept_active_jobs_contribute(store):
    from auto_app.worker.boards import suggest_boards
    with store.engine.begin() as conn:
        conn.execute(update(T.job_screenings).where(T.job_screenings.c.job_id == job_id_of(store, "Backend Engineer"))
                     .values(outcome="excluded"))
        conn.execute(update(T.jobs).where(T.jobs.c.job_id == job_id_of(store, "ML Engineer"))
                     .values(archived_at=utcnow()))
    boards = suggest_boards(store, configured=set(), top=1, min_jobs=1)
    assert [(b["board"], b["kept_jobs"], b["sponsoring_jobs"]) for b in boards] == [("acme", 1, 0)]


def test_yaml_roundtrips_company_punctuation_and_skips_configured(store):
    from auto_app.worker.boards import as_yaml, suggest_boards
    boards = suggest_boards(store, configured={"https://api.lever.co/v0/postings/beta?mode=json"}, min_jobs=1)
    boards[0]["company"] = "O'Reilly: Research #1"
    data = yaml.safe_load("sources:\n" + as_yaml(boards))
    assert set(data["sources"]) == {"board-greenhouse-acme"}
    assert data["sources"]["board-greenhouse-acme"] == {
        "kind": "repository", "format": "greenhouse-board", "company": "O'Reilly: Research #1",
        "location": "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"}


@pytest.mark.parametrize("platform,board", [("greenhouse", "../bad"), ("lever", "a?b"), ("unknown", "acme")])
def test_board_locations_reject_unsupported_or_unsafe_identifiers(platform, board):
    from auto_app.worker.boards import board_location
    with pytest.raises(ValueError):
        board_location(platform, board)


def test_european_lever_link_recommends_its_regional_list_api(store):
    from auto_app.worker.boards import suggest_boards
    ingest(store, [record("eu", "Europe Engineer", company="Euro",
                          url="https://jobs.eu.lever.co/euro/91eed659-1111-2222-3333-444455556666")])
    with store.engine.begin() as conn:
        conn.execute(insert(T.job_screenings).values(
            job_id=job_id_of(store, "Europe Engineer"), rules_version="t", facts_hash="h", outcome="kept",
            reasons=[], signals={"sponsorship": "offers"}, screened_at=utcnow()))
    board = next(b for b in suggest_boards(store, configured=set(), min_jobs=1) if b["board"] == "euro")
    assert board["location"] == "https://api.eu.lever.co/v0/postings/euro?mode=json"
    assert board["source"] == "board-lever-euro-eu"


def test_country_hidden_jobs_do_not_drive_board_suggestions(store):
    from auto_app.worker.boards import suggest_boards

    ingest(store, [record("999", "Toronto Engineer", locations=["Toronto, Canada"],
                          url="https://job-boards.greenhouse.io/canadian/jobs/999")], source="canadian-feed")
    job_id = job_id_of(store, "Toronto Engineer")
    with store.engine.begin() as conn:
        conn.execute(insert(T.job_screenings).values(job_id=job_id, rules_version="t", facts_hash="h",
                     outcome="kept", reasons=[], signals={"sponsorship": "offers"}, screened_at=utcnow()))
    store.paths.preferences.parent.mkdir(parents=True, exist_ok=True)
    store.paths.preferences.write_text(json.dumps({"exclude_countries": ["Canada"]}))
    assert "canadian" not in {b["board"] for b in suggest_boards(store, configured=set(), min_jobs=1)}


@pytest.mark.parametrize("board, location", [
    ("rivianvw.tech", "https://api.ashbyhq.com/posting-api/job-board/rivianvw.tech?includeCompensation=true"),
    ("spectral labs", "https://api.ashbyhq.com/posting-api/job-board/spectral%20labs?includeCompensation=true"),
])
def test_ashby_board_names_with_dots_or_spaces_have_a_list_api(board, location):
    from auto_app.worker.boards import board_location
    assert board_location("ashby", board) == location


@pytest.mark.parametrize("platform, board", [("greenhouse", "rivianvw.tech"), ("lever", "spectral labs"),
                                             ("ashby", "../bad"), ("ashby", "a?b")])
def test_dots_and_spaces_stay_invalid_outside_ashby(platform, board):
    from auto_app.worker.boards import board_location
    with pytest.raises(ValueError):
        board_location(platform, board)


def test_boards_with_dotted_or_spaced_ashby_names_do_not_crash_suggestions(store):
    from auto_app.worker.boards import as_yaml, suggest_boards
    ingest(store, [record("a1", "Platform Engineer", company="Rivian VW",
                          url="https://jobs.ashbyhq.com/rivianvw.tech/91eed659-1111-2222-3333-444455556666"),
                   record("a2", "Robotics Engineer", company="Spectral",
                          url="https://jobs.ashbyhq.com/Spectral%20Labs/91eed659-1111-2222-3333-444455557777")],
           source="ashby-feed")
    with store.engine.begin() as conn:
        for title in ("Platform Engineer", "Robotics Engineer"):
            conn.execute(insert(T.job_screenings).values(
                job_id=job_id_of(store, title), rules_version="t", facts_hash="h", outcome="kept", reasons=[],
                signals={"sponsorship": "offers"}, screened_at=utcnow()))
    boards = {b["board"]: b for b in suggest_boards(store, configured=set(), min_jobs=1)}
    assert boards["rivianvw.tech"]["source"] == "board-ashby-rivianvw-tech"
    assert boards["spectral labs"]["location"] == (
        "https://api.ashbyhq.com/posting-api/job-board/spectral%20labs?includeCompensation=true")
    data = yaml.safe_load("sources:\n" + as_yaml(list(boards.values())))
    assert data["sources"]["board-ashby-spectral-labs"]["format"] == "ashby-board"


def test_a_board_configured_with_other_letter_case_or_encoding_counts_as_configured(store):
    # board-ashby-roadrunner was configured as ".../job-board/Roadrunner"; the suggestion listed "roadrunner" as new.
    from auto_app.worker.boards import suggest_boards
    boards = suggest_boards(store, configured={"https://boards-api.greenhouse.io/v1/boards/ACME/jobs?content=true/"},
                            top=10, min_jobs=2)
    assert [(b["board"], b["configured"]) for b in boards] == [("acme", True)]

"""jobq: the small JSON command the Grok Bot and Claude Code use to read jobs, record decisions and hand off."""

import io
import json
import os
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import exc as sa_exc, select, update

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow
from auto_app.ingest.scheduler import _write_state
from auto_app.jobq.cli import main
from auto_app.screen.rules import ScreeningRules
from auto_app.screen.store import screen_jobs
from auto_app.worker import loop
from tests.imported_jobs import ingest, job_id_of, make_store, record

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    ingest(store, [record("101", "Backend Engineer", sponsorship="Offers Sponsorship"),
                   record("102", "Data Engineer", sponsorship="Does Not Offer Sponsorship"),
                   record("103", "ML Engineer", company="Beta",
                          url="https://jobs.lever.co/beta/6ed76ce8-1111-2222-3333-444455556666"),
                   record("104", "Cleared Software Engineer", sponsorship="U.S. Citizenship is Required")])
    screen_jobs(store, ScreeningRules())
    now = utcnow()
    with store.engine.begin() as conn:
        conn.execute(update(T.jobs).values(first_seen_at=now - timedelta(hours=3)))
    titles = ("Backend Engineer", "Data Engineer", "ML Engineer", "Cleared Software Engineer")
    ids = {t: job_id_of(store, t) for t in titles}
    store.engine.dispose()
    _write_state(tmp_path / "data" / "auto_app.db", {"notify_since": (now - timedelta(hours=4)).isoformat()}, loop.STATE)
    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    return tmp_path, ids


def run(capsys, *args):
    code = main(list(args))
    return code, json.loads(capsys.readouterr().out)


def test_new_shows_kept_jobs_sponsors_first_and_hides_clearance_jobs(home, capsys):
    _, ids = home
    code, out = run(capsys, "new")
    assert code == 0 and out["more"] == 0
    assert out["jobs"][0]["job_id"] == ids["Backend Engineer"]
    assert {j["job_id"] for j in out["jobs"]} == {ids["Backend Engineer"], ids["Data Engineer"], ids["ML Engineer"]}
    assert run(capsys, "new")[1] == out  # reading never marks anything


def test_new_is_empty_before_the_worker_has_a_notify_cutoff(home, capsys):
    root, _ = home
    (root / "data" / ("auto_app.db" + loop.STATE)).unlink()
    assert run(capsys, "new") == (0, {"jobs": [], "more": 0})


def test_seen_marks_listed_jobs_and_reports_unknown_ids(home, capsys):
    _, ids = home
    code, out = run(capsys, "seen", str(ids["Backend Engineer"]), "999999")
    assert code == 0 and out == {"marked": [ids["Backend Engineer"]], "unknown": [999999]}
    assert ids["Backend Engineer"] not in {j["job_id"] for j in run(capsys, "new")[1]["jobs"]}


def test_search_and_show_expose_the_visa_signal(home, capsys):
    _, ids = home
    assert [i["job_id"] for i in run(capsys, "search", "--sponsorship", "offers")[1]["items"]] == [ids["Backend Engineer"]]
    assert ids["Cleared Software Engineer"] not in {i["job_id"] for i in run(capsys, "search")[1]["items"]}
    code, job = run(capsys, "show", str(ids["Data Engineer"]))
    assert code == 0 and job["screening"]["signals"]["sponsorship"] == "no" and job["events"] == []
    assert run(capsys, "show", "999999") == (1, {"status": "error", "error": "job #999999 not found"})


def keys_anywhere(value) -> set:
    """The keys of every JSON object inside `value`, at any depth."""
    if isinstance(value, dict):
        return set(value).union(*(keys_anywhere(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(keys_anywhere(item) for item in value))
    return set()


def test_show_leaves_out_import_records_and_their_raw_payloads(tmp_path, monkeypatch, capsys):
    # A mailbox import keeps the e-mail's message id, subject and raw links in its ingest payload; job-alert links can
    # carry per-recipient tracking or sign-in tokens, and spec §6.1 lists no import data among show's fields.
    from auto_app.ingest.models import IngestRecord, ParsedFeed
    from auto_app.ingest.service import IngestService
    from auto_app.query.service import QueryService

    link = "https://click.example.com/track?u=alex&token=s3cret-sign-in"
    store = make_store(tmp_path)
    IngestService(store).ingest("job-alerts", ParsedFeed([IngestRecord(
        external_id="email:alert-1", url="https://job-boards.greenhouse.io/acme/jobs/501", title="Platform Engineer",
        company="Acme", metadata={"message_id": "<alert-1@mail.example.com>", "subject": "New jobs for Alex",
                                  "date": "2026-10-19", "original_url": link, "original_urls": [link]})]),
        kind="email", origin="mailbox")
    job_id = job_id_of(store, "Platform Engineer")
    store.engine.dispose()
    with QueryService(tmp_path / "data" / "auto_app.db") as service:  # the service itself does return them
        assert service.job(job_id)["ingestion"]["entries"][0]["payload"]["metadata"]["original_url"] == link
    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    code, job = run(capsys, "show", str(job_id))
    assert code == 0 and (job["job_id"], job["title"], job["company"]) == (job_id, "Platform Engineer", "Acme")
    assert "ingestion" not in job and "payload" not in keys_anywhere(job)
    printed = json.dumps(job)
    assert "s3cret" not in printed and "alert-1@mail" not in printed and "New jobs for Alex" not in printed
    # What apply_kit.py reads is still there.
    assert {"locations", "canonical_url", "posting_details", "parsed", "screening", "events"} <= set(job)


def test_want_and_skip_need_a_reason_and_feed_the_queue(home, capsys):
    _, ids = home
    with pytest.raises(SystemExit) as exc:
        main(["want", str(ids["ML Engineer"])])
    assert exc.value.code == 2
    assert run(capsys, "want", str(ids["ML Engineer"]), "--reason", "confirmed by me")[1] == \
        {"job_id": ids["ML Engineer"], "status": "want_to_apply", "changed": True}
    assert run(capsys, "skip", str(ids["Data Engineer"]), "--reason", "not interested")[1]["status"] == "not_applied"
    assert [i["job_id"] for i in run(capsys, "queue")[1]["items"]] == [ids["ML Engineer"]]


def test_claim_packet_record_round_trip_uses_the_fixed_agent(home, capsys):
    root, ids = home
    run(capsys, "want", str(ids["ML Engineer"]), "--reason", "confirmed by me")
    claim = run(capsys, "claim", str(ids["ML Engineer"]))[1]
    packet = run(capsys, "packet", claim["token"])[1]
    assert packet["job"]["job_id"] == ids["ML Engineer"]
    assert (packet["profile"], packet["answers"], packet["resumes"]) == ({}, {}, [])
    store = make_store(root)
    with store.engine.connect() as conn:
        assert conn.execute(select(T.handoff_claims.c.agent)).scalar() == "grok-bot"
    store.engine.dispose()
    code, out = run(capsys, "record", claim["token"], "needs_user", "--note", "email verification")
    assert code == 0 and out["status"] == "needs_input"
    assert run(capsys, "record", claim["token"], "failed")[0] == 1  # the claim was consumed


def test_release_and_account_metadata(home, capsys):
    _, ids = home
    run(capsys, "want", str(ids["ML Engineer"]), "--reason", "confirmed by me")
    token = run(capsys, "claim", str(ids["ML Engineer"]))[1]["token"]
    assert run(capsys, "release", token)[1] == {"job_id": ids["ML Engineer"], "released": True}
    assert run(capsys, "account", "acme.example.com", "alex@example.com", "--existing")[1]["created_by_agent"] is False


def test_a_claim_token_can_be_passed_as_a_plain_argument_even_if_the_first_draw_starts_with_a_dash(home, capsys,
                                                                                                    monkeypatch):
    from types import SimpleNamespace

    from auto_app.handoff import service as handoff

    draws = iter(["-" + "A" * 31, "B" * 32])
    monkeypatch.setattr(handoff, "secrets", SimpleNamespace(token_urlsafe=lambda nbytes: next(draws)))
    _, ids = home
    run(capsys, "want", str(ids["ML Engineer"]), "--reason", "confirmed by me")
    token = run(capsys, "claim", str(ids["ML Engineer"]))[1]["token"]
    assert not token.startswith("-")  # argparse would read it as an option
    assert run(capsys, "packet", token)[1]["job"]["job_id"] == ids["ML Engineer"]
    code, out = run(capsys, "record", token, "needs_user", "--note", "email verification")
    assert code == 0 and out["status"] == "needs_input"


@pytest.mark.parametrize("args", [
    ["--db", "x.db", "new"], ["new", "--db", "x.db"], ["show", "1", "--preferences", "p.json"],
    ["new", "--limit", "51"], ["seen"], ["want", "1", "--reason", "line one\nline two"],
    ["want", "1", "--reason", "   "], ["packet", "not a token!"], ["record", "x" * 20, "maybe"],
    ["search", "--sponsorship", "citizenship_required"], ["seen", *[str(i) for i in range(1, 202)]]])
def test_rejects_paths_bad_text_and_unknown_values(home, args):
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2


def test_missing_home_or_database_is_a_json_error(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AUTO_APP_HOME", raising=False)
    code, out = run(capsys, "queue")
    assert code == 2 and "AUTO_APP_HOME" in out["error"]
    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    code, out = run(capsys, "show", "1")
    assert code == 1 and out["status"] == "error" and "database" in out["error"]
    assert not (tmp_path / "data" / "auto_app.db").exists()


def test_seen_lists_each_job_once_in_the_order_given(home, capsys):
    _, ids = home
    ml, backend = ids["ML Engineer"], ids["Backend Engineer"]
    code, out = run(capsys, "seen", str(ml), "999999", str(backend), str(ml), "999998", "999999")
    assert code == 0 and out == {"marked": [ml, backend], "unknown": [999999, 999998]}


def test_seen_knows_the_same_jobs_as_the_store(home, capsys):
    # DbStore.job(id) is the reference for "known": it also finds archived and rule-excluded jobs.
    root, ids = home
    store = make_store(root)
    with store.engine.begin() as conn:
        conn.execute(update(T.jobs).where(T.jobs.c.job_id == ids["Data Engineer"]).values(archived_at=utcnow()))
    asked = [999999, *ids.values(), ids["ML Engineer"], 999998]
    known = [i for i in dict.fromkeys(asked) if store.job(i) is not None]
    store.engine.dispose()
    assert known == list(ids.values())
    assert run(capsys, "seen", *map(str, asked)) == (0, {"marked": known, "unknown": [999999, 999998]})
    store = make_store(root)
    assert all(store.job(i)["notified_at"] for i in known)
    store.engine.dispose()


def test_limits_filters_and_cursors_reach_the_services(home, capsys):
    _, ids = home
    code, out = run(capsys, "new", "--limit", "1")
    assert code == 0 and len(out["jobs"]) == 1 and out["more"] == 2
    page = run(capsys, "search", "--limit", "1")[1]
    assert page["returned"] == 1 and page["total"] == 3 and page["next_cursor"]
    after = run(capsys, "search", "--limit", "1", "--cursor", page["next_cursor"])[1]
    assert after["items"][0]["job_id"] < page["items"][0]["job_id"]
    assert [i["job_id"] for i in run(capsys, "search", "--company", "beta")[1]["items"]] == [ids["ML Engineer"]]
    assert [i["job_id"] for i in run(capsys, "search", "--query", "data engineer")[1]["items"]] == [ids["Data Engineer"]]
    assert run(capsys, "search", "--state", "recommended")[1]["total"] == 0
    assert run(capsys, "search", "--state", "not_evaluated")[1]["total"] == 3


@pytest.mark.parametrize("args", [
    ["show", "0"], ["show", "9223372036854775808"], ["show", "1.5"], ["show", "\u0663"],  # the last is an Arabic-Indic digit three
    ["new", "--limit", "0"], ["queue", "--limit", "101"], ["search", "--limit", "201"],
    ["want", "1", "--reason", "x" * 501], ["search", "--query", "x" * 201],
    ["search", "--state", "archived"], ["packet", "a" * 15], ["packet", "a" * 129], ["packet", "a" * 20 + "\n"],
    ["record", "a" * 20, "submitted", "--note", "bell\a"]])
def test_rejects_out_of_range_numbers_overlong_text_and_malformed_tokens(home, args):
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 2


def test_importing_jobq_loads_no_service_or_heavy_runtime(tmp_path):
    # Services load inside their commands; a fresh interpreter catches an accidental module-level import.
    script = """
import sys
import auto_app.jobq.cli
heavy = ('playwright', 'textual', 'anthropic', 'sqlalchemy', 'auto_app.cli', 'auto_app.handoff.service')
assert not any(m in sys.modules for m in heavy), [m for m in heavy if m in sys.modules]
"""
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env, text=True, capture_output=True,
                            timeout=30)
    assert result.returncode == 0, result.stderr


# ---- pre-review fixes: long cursors, database and file errors, Unicode control characters ---------------------

@pytest.fixture
def nowhere(tmp_path, monkeypatch):
    # An AUTO_APP_HOME without a database: an argument that should have been refused but slips through fails
    # harmlessly ("database missing") instead of running against a real runtime.
    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))


@pytest.mark.parametrize("phrase", [("distributed systems " * 10).strip(), "数据工程师" * 40], ids=["ascii", "cjk"])
def test_a_long_cursor_from_a_long_query_is_accepted_on_the_next_page(tmp_path, monkeypatch, capsys, phrase):
    store = make_store(tmp_path)
    ingest(store, [record("301", f"{phrase} one"), record("302", f"{phrase} two")])
    store.engine.dispose()
    monkeypatch.setenv("AUTO_APP_HOME", str(tmp_path))
    search = ["search", "--query", phrase, "--source", "simplify-newgrad", "--company", "acme", "--limit", "1"]
    first = run(capsys, *search)[1]
    assert first["total"] == 2 and [i["title"] for i in first["items"]] == [f"{phrase} two"]
    assert len(first["next_cursor"]) > 500  # longer than the old cap
    code, second = run(capsys, *search, "--cursor", first["next_cursor"])
    assert code == 0 and [i["title"] for i in second["items"]] == [f"{phrase} one"] and second["next_cursor"] is None


def test_the_cursor_cap_is_4096_characters(home, capsys):
    code, out = run(capsys, "search", "--cursor", "x" * 4096)
    assert code == 1 and "cursor" in out["error"]  # past the length check; the service refuses the made-up cursor
    with pytest.raises(SystemExit) as exc:
        main(["search", "--cursor", "x" * 4097])
    assert exc.value.code == 2


@pytest.mark.parametrize("error, message", [
    (sqlite3.OperationalError("database is locked"), "database error (OperationalError); retry"),
    (sa_exc.OperationalError("UPDATE jobs SET notified_at=?", ("s3cret-parameter",),
                             sqlite3.OperationalError("database is locked")), "database error (OperationalError); retry"),
    (sa_exc.ArgumentError("SELECT s3cret FROM jobs"), "database error (ArgumentError); retry"),
    (PermissionError(13, "Permission denied", "/runtime/profile/profile.yaml"), "file error (PermissionError); retry")],
    ids=["sqlite", "sqlalchemy", "sqlalchemy-other", "file"])
def test_database_and_file_errors_are_one_generic_json_error(home, capsys, monkeypatch, error, message):
    from auto_app.core.db.store import DbStore

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(DbStore, "set_notified", fail)
    _, ids = home
    # run() parses all of stdout as one JSON object; the message holds the class name only (no SQL, parameters, paths).
    assert run(capsys, "seen", str(ids["ML Engineer"])) == (1, {"status": "error", "error": message})


def test_other_exceptions_still_raise(home, monkeypatch):
    from auto_app.handoff.service import HandoffService

    def fail(*args, **kwargs):
        raise RuntimeError("a bug, not a database failure")

    monkeypatch.setattr(HandoffService, "queue", fail)
    with pytest.raises(RuntimeError):
        main(["queue"])


@pytest.mark.parametrize("prefix", [["want", "1", "--reason"], ["record", "a" * 20, "failed", "--note"],
                                    ["search", "--query"]], ids=["reason", "note", "query"])
@pytest.mark.parametrize("text", ["one\x85two", "one\u2028two", "one\u2029two", "one\x80two", "one\x9f", "\x85", "\u2028"])
def test_rejects_unicode_control_characters_and_line_separators(nowhere, prefix, text):
    with pytest.raises(SystemExit) as exc:
        main([*prefix, text])
    assert exc.value.code == 2


def test_ordinary_unicode_text_is_kept_and_surrounding_whitespace_is_trimmed(home, capsys):
    root, ids = home
    reason = "用户确认 – café 😀"
    code, out = run(capsys, "want", str(ids["ML Engineer"]), "--reason", f"  {reason}\u2028\n")
    assert code == 0 and out["changed"] is True
    store = make_store(root)
    assert store.reviews(ids["ML Engineer"])[-1]["reason"] == reason
    store.engine.dispose()


# ---- the claim token on stdin -----------------------------------------------------------------------------
# sudo logs the whole command line, so `jobq packet <token>` wrote the token into /var/log/auth.log, readable by
# every member of `adm` (found in the 2026-10-21 end-to-end run). The token can now arrive on stdin instead.

def _stdin(monkeypatch, text):
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def test_packet_record_and_release_read_the_token_from_stdin(home, capsys, monkeypatch):
    _, ids = home
    run(capsys, "want", str(ids["ML Engineer"]), "--reason", "confirmed by me")
    token = run(capsys, "claim", str(ids["ML Engineer"]))[1]["token"]
    _stdin(monkeypatch, token + "\n")
    assert run(capsys, "packet")[1]["job"]["job_id"] == ids["ML Engineer"]  # no argument at all
    _stdin(monkeypatch, token)  # and with an explicit "-", without a trailing newline
    assert run(capsys, "packet", "-")[1]["job"]["job_id"] == ids["ML Engineer"]
    _stdin(monkeypatch, f"  {token}  \n")  # surrounding blanks are stripped
    assert run(capsys, "record", "-", "submitted", "--note", "stdin")[0] == 0

    run(capsys, "want", str(ids["Backend Engineer"]), "--reason", "confirmed by me")
    token = run(capsys, "claim", str(ids["Backend Engineer"]))[1]["token"]
    _stdin(monkeypatch, token + "\n")
    assert run(capsys, "release")[0] == 0
    assert ids["Backend Engineer"] in {i["job_id"] for i in run(capsys, "queue")[1]["items"]}


def test_the_token_argument_still_works(home, capsys):
    _, ids = home
    run(capsys, "want", str(ids["ML Engineer"]), "--reason", "confirmed by me")
    token = run(capsys, "claim", str(ids["ML Engineer"]))[1]["token"]
    assert run(capsys, "packet", token)[1]["job"]["job_id"] == ids["ML Engineer"]


@pytest.mark.parametrize("text, message", [
    ("", "no claim token on stdin"),
    ("   \n", "no claim token on stdin"),
    ("not a token!\n", "not a claim token"),
    ("x" * 300 + "\n", "too long"),
])
def test_a_bad_token_on_stdin_is_a_usage_error(home, capsys, monkeypatch, text, message):
    _stdin(monkeypatch, text)
    code, out = run(capsys, "packet")
    assert code == 2 and message in out["error"]


def test_without_stdin_the_error_says_how_to_pipe_the_token(home, capsys, monkeypatch):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", Tty(""))
    code, out = run(capsys, "packet")
    assert code == 2 and "pipe it in" in out["error"]


def test_the_wrapper_passes_stdin_through(home, capsys, tmp_path):
    """The server wrapper must not swallow stdin: it is how the token now arrives."""
    assert "exec" in (ROOT / "deploy" / "jobq").read_text()  # exec keeps the same stdin
    assert "<" not in (ROOT / "deploy" / "jobq").read_text().split("exec", 1)[1]


# ---- who ran the attempt, and a cap on repeated failures ---------------------------------------------------

def test_the_attempt_records_the_agent_that_ran_it(home, capsys):
    root, ids = home
    run(capsys, "want", str(ids["ML Engineer"]), "--reason", "confirmed by me")
    token = run(capsys, "claim", str(ids["ML Engineer"]))[1]["token"]
    assert run(capsys, "record", token, "failed", "--note", "form would not load")[0] == 0
    store = make_store(root)
    with store.engine.connect() as conn:
        # The claim is gone after an outcome, so the attempt is the only lasting trace of who ran it.
        assert conn.execute(select(T.attempts.c.applier)).scalars().all() == ["grok-bot"]
        assert conn.execute(select(T.handoff_claims.c.job_id)).scalars().all() == []
    store.engine.dispose()


def test_repeated_failures_park_the_job_instead_of_cycling(home, capsys):
    from auto_app.handoff.service import FAILED_ATTEMPT_LIMIT

    _, ids = home
    job = ids["ML Engineer"]
    run(capsys, "want", str(job), "--reason", "confirmed by me")
    for _ in range(FAILED_ATTEMPT_LIMIT - 1):
        token = run(capsys, "claim", str(job))[1]["token"]
        assert run(capsys, "record", token, "failed")[1]["status"] == "failed"
        # Below the cap the job comes straight back, which is what makes a cap necessary.
        assert [i["job_id"] for i in run(capsys, "queue")[1]["items"]] == [job]
    token = run(capsys, "claim", str(job))[1]["token"]
    code, out = run(capsys, "record", token, "failed")  # the FAILED_ATTEMPT_LIMIT-th failure parks it
    assert code == 0 and out["status"] == "action_needed"
    queue = run(capsys, "queue")[1]
    assert queue["items"] == [] and queue["blocked"]["by_reason"] == {"waiting_for_you": 1}

"""`jobq`: read new jobs, record the user's decisions and run the application handoff, as one JSON command.

It only calls the existing services. It takes no database or file-path arguments: the runtime is
$AUTO_APP_HOME (the server's wrapper script pins it), and a missing database is reported, never created.
stdout is one JSON object, except that `kit ID FILE` and `guide` print the file itself (so a PDF can be redirected
into a file); an error is JSON either way. Exit status 0 is success, 1 a failed request (a database or file error is
reported by its class name only), 2 bad arguments or configuration.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

from auto_app.query.service import STATES, QueryService

AGENT = "grok-bot"  # fixed, so a caller can't claim jobs under another name
MAX_ID = 9223372036854775807  # SQLite's largest integer
SEEN_LIMIT = 200
OUTCOMES = ("submitted", "needs_user", "blocked", "failed", "skipped")
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,128}")
STDIN_TOKEN = "-"  # `jobq packet -` (or no argument at all) reads the token from stdin
MAX_TOKEN_LINE = 256  # bytes read from stdin while looking for the token
FORBIDDEN_CATEGORIES = ("Cc", "Zl", "Zp")  # control characters (C0, DEL, C1), line and paragraph separators
MAX_QUESTIONS_STDIN = 64 * 1024  # bytes of question JSON read from stdin (the service refuses more than this anyway)
# The same names as HandoffService's KIT_FILES and KIT_GUIDES (a test checks it), kept here so that --help and
# argument checks do not import the service.
KIT_FILES = ("job.json", "fields.json", "jd.txt", "resume.pdf", "cover_letter.pdf", "answers.md", "message.txt")
GUIDES = ("cheatsheet", "readme")


class _StartupError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


# ---- argument types: every check that can fail here fails through argparse (exit status 2) ----------------

def bounded(low: int, high: int):
    """A whole number from low to high, in plain ASCII digits."""
    def parse(value: str) -> int:
        if value.isascii() and value.isdigit() and len(value) <= len(str(high)) and low <= int(value) <= high:
            return int(value)
        raise argparse.ArgumentTypeError(f"must be a whole number from {low} to {high}")
    return parse


positive_int = bounded(1, MAX_ID)


def plain_text(limit: int):
    """One line of text, trimmed, 1 to `limit` characters, with no control characters or line breaks."""
    def parse(value: str) -> str:
        text = value.strip()
        if not text or len(text) > limit:
            raise argparse.ArgumentTypeError(f"must be 1 to {limit} characters")
        if any(unicodedata.category(c) in FORBIDDEN_CATEGORIES for c in text):
            raise argparse.ArgumentTypeError("must be one line, without control characters or line breaks")
        return text
    return parse


def token(value: str) -> str:
    if value == STDIN_TOKEN:  # the token follows on stdin
        return value
    if not TOKEN_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError("not a claim token")
    return value


def _claim_token(args) -> str:
    """The claim token: from stdin unless it was given as an argument.

    sudo logs the whole command line, so a token passed as an argument lands in /var/log/auth.log, which every
    member of `adm` can read (found in the 2026-10-21 end-to-end run). A token is a capability: it reads the
    packet and records the outcome. Passing it on stdin keeps it out of that log and out of the process list.
    The argument still works, so existing callers do not break.
    """
    value = getattr(args, "token", None)
    if value is not None and value != STDIN_TOKEN:
        return value
    if sys.stdin is None or sys.stdin.isatty():
        raise _StartupError(2, "no claim token: pipe it in, for example "
                               "`printf %s \"$token\" | jobq packet`, or pass it as an argument "
                               "(which puts it in the sudo log)")
    line = sys.stdin.readline(MAX_TOKEN_LINE + 1)
    if len(line) > MAX_TOKEN_LINE:
        raise _StartupError(2, "the claim token on stdin is too long")
    line = line.strip()
    if not line:
        raise _StartupError(2, "no claim token on stdin")
    if not TOKEN_PATTERN.fullmatch(line):
        raise _StartupError(2, "what arrived on stdin is not a claim token")
    return line


def _questions_json():
    """The open questions, as a JSON list, from stdin after the token line (or all of stdin with a TOKEN argument)."""
    if sys.stdin is None or sys.stdin.isatty():
        raise _StartupError(2, "no questions: pipe the token line and then the JSON list, for example "
                               "`printf '%s\\n%s' \"$token\" \"$json\" | jobq questions`")
    text = sys.stdin.read(MAX_QUESTIONS_STDIN + 1)
    if len(text.encode()) > MAX_QUESTIONS_STDIN:
        raise _StartupError(2, "the questions on stdin are too long (at most 64 KiB)")
    if not text.strip():
        raise _StartupError(2, "no questions on stdin after the token")
    try:
        return json.loads(text)
    except ValueError:
        raise _StartupError(2, "the questions on stdin are not valid JSON") from None


def _owner_only(command: str) -> None:
    """Refuse a step that only the owner's side may take when jobq runs through sudo, which is how grokbot reaches it.

    sudo always sets SUDO_USER and its env_reset keeps the caller from clearing it; Claude Code reaches jobq as
    postedism, without sudo (on the server, or over ssh).
    """
    if os.environ.get("SUDO_USER"):
        raise ValueError(f"`{command}` is the owner's step (Claude Code as postedism, without sudo); "
                         "it is refused through sudo")


class _SeenIds(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if len(values) > SEEN_LIMIT:
            parser.error(f"argument {self.metavar}: at most {SEEN_LIMIT} job numbers at a time")
        setattr(namespace, self.dest, values)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="jobq", description="Read new jobs, record the user's decisions and run the application handoff.",
        epilog='Prints one JSON object, except that `kit ID FILE` and `guide` print the file itself. Exit status: '
               '0 success; 1 the request failed ({"status": "error", "error": "..."}); 2 bad arguments or '
               'configuration.')
    commands = root.add_subparsers(dest="command", required=True, metavar="COMMAND")
    text = plain_text(500)  # reasons, notes, an account's site and email
    short = plain_text(200)  # search filters
    cursor = plain_text(4096)  # a cursor carries the search's filters, so it can be long

    def command(name, run, summary):
        sub = commands.add_parser(name, help=summary, description=summary)
        sub.set_defaults(run=run)
        return sub

    def job_number(sub):
        sub.add_argument("job_id", metavar="ID", type=positive_int, help="The job number.")

    def claim_token(sub):
        sub.add_argument("token", metavar="TOKEN", nargs="?", default=None, type=token,
                         help="The token `claim` returned. Leave it out (or pass -) to read it from stdin "
                              "instead, which keeps it out of the sudo log and the process list.")

    def note(sub, about):
        sub.add_argument("--note", type=text, help=about)

    new = command("new", _cmd_new, "List jobs the user has not been told about yet, visa sponsorship first.")
    new.add_argument("--limit", type=bounded(1, 50), default=20, metavar="N", help="How many, 1 to 50 (default 20).")

    seen = command("seen", _cmd_seen, "Mark jobs as told to the user, so `new` stops listing them.")
    seen.add_argument("ids", metavar="ID", nargs="+", type=positive_int, action=_SeenIds,
                      help=f"Job numbers, at most {SEEN_LIMIT}.")

    search = command("search", _cmd_search, "Search the job library (jobs the user's rules exclude are left out).")
    search.add_argument("--query", type=short, help="Text in the title or company, ignoring case.")
    search.add_argument("--company", type=short, help="Text in the company name, ignoring case.")
    search.add_argument("--source", type=short, help="The source the job was found on.")
    search.add_argument("--state", choices=[state for state in STATES if state != "archived"],
                        help="Only jobs in this state.")
    search.add_argument("--sponsorship", choices=("offers", "no", "unknown"), help="The job's visa-sponsorship signal.")
    search.add_argument("--limit", type=bounded(1, 200), default=50, metavar="N", help="How many, 1 to 200 (default 50).")
    search.add_argument("--cursor", type=cursor, help="The next_cursor of the previous page of this search.")

    show = command("show", _cmd_show, "One job in full, without its import records: description, apply link, "
                                      "screening, visa signal, history.")
    job_number(show)

    want = command("want", _cmd_want, "Record that the user confirmed they want to apply (the job joins the queue).")
    job_number(want)
    want.add_argument("--reason", required=True, type=text, help="How the user confirmed, in one line.")

    skip = command("skip", _cmd_skip, "Record that the user decided not to apply.")
    job_number(skip)
    skip.add_argument("--reason", required=True, type=text, help="Why not, in one line.")

    queue = command("queue", _cmd_queue, "List jobs that may be applied to now, after quotas, claims and cooldowns.")
    queue.add_argument("--limit", type=bounded(1, 100), default=20, metavar="N", help="How many, 1 to 100 (default 20).")

    claim = command("claim", _cmd_claim, "Claim one queued job; returns a token and when the claim ends.")
    job_number(claim)

    packet = command("packet", _cmd_packet, "Everything needed to apply to the claimed job.")
    claim_token(packet)

    record = command("record", _cmd_record, "Record the outcome of an application attempt, once per claim.")
    claim_token(record)
    record.add_argument("outcome", metavar="OUTCOME", choices=OUTCOMES,
                        help="submitted (it went in); needs_user or blocked (the job waits until the user "
                             "requeues it); failed (it can be claimed again); skipped (counts as not applying).")
    note(record, "What happened, in one line.")

    release = command("release", _cmd_release, "Give a claim back without an outcome.")
    claim_token(release)

    hold = command("hold", _cmd_hold, "The form is filled and waits for the user's go: keep the claim for "
                                      "hold_hours (default 24). Claiming the job again gives you a fresh token.")
    claim_token(hold)

    command("held", _cmd_held, "Filled forms waiting for the user's go (held, claim still live), oldest first, so "
                               "they can be approved together.")

    questions = command("questions", _cmd_questions,
                        "Hand back the form's open questions that answers.md does not cover, or the user's change "
                        "requests from the pre-submit check (this uses up the claim; the job waits until the revised "
                        "package is published). stdin: the token line, then a JSON list of "
                        '{"text", "required", "limit", "page", "kind"} ("kind": "question", the default, or "change" '
                        "for the user's words verbatim); with a TOKEN argument, stdin is only the JSON.")
    claim_token(questions)

    command("answers-needed", _cmd_answers_needed, "Jobs waiting for answers, with the questions handed back.")

    answers_ready = command("answers-ready", _cmd_answers_ready,
                            "The owner's step after publishing the answers: the job returns to the queue. "
                            "Refused through sudo.")
    job_number(answers_ready)

    kit = command("kit", _cmd_kit,
                  "The published application packages, no claim token needed: with no ID, the index; with ID, that "
                  "job's job.json; with ID FILE, the file itself on stdout (not JSON), for example "
                  "`jobq kit 14143 resume.pdf > ~/kit/14143/resume.pdf`.")
    kit.add_argument("job_id", metavar="ID", nargs="?", type=positive_int, help="The job number.")
    kit.add_argument("file", metavar="FILE", nargs="?", choices=KIT_FILES,
                     help=f"One of {', '.join(KIT_FILES)}.")

    guide = command("guide", _cmd_guide, "The apply-kit's guide as plain text (not JSON): cheatsheet (answers.md, the "
                                         "default) or readme (README.md).")
    guide.add_argument("which", metavar="WHICH", nargs="?", choices=GUIDES, default="cheatsheet",
                       help="cheatsheet (the default) or readme.")

    account = command("account", _cmd_account, "Note that an account exists on a hiring site (never its password).")
    account.add_argument("site", metavar="SITE", type=text, help="The site's host name.")
    account.add_argument("email", metavar="EMAIL", type=text, help="The address the account uses.")
    account.add_argument("--existing", action="store_true", help="The user made the account, not the agent.")
    note(account, "A short note.")
    return root


# ---- commands: each calls one service and returns what it returned --------------------------------------------

def _cmd_new(args, db: Path, home: Path) -> dict:
    from auto_app.agent.control import ControlService

    return ControlService(db).digest(args.limit)


def _cmd_seen(args, db: Path, home: Path) -> dict:
    from auto_app.core.db.store import DbStore
    from auto_app.settings import Paths

    ids = list(dict.fromkeys(args.ids))
    store = DbStore(db, Paths(home=home), migrate=False)
    try:
        # One scan of the jobs: DbStore.job(id) rescans them all, which is slow with 200 IDs.
        existing = {job["job_id"] for job in store.jobs(archived=None)}
        marked = [i for i in ids if i in existing]
        if marked:
            store.set_notified(marked)
    finally:
        store.engine.dispose()
    return {"marked": marked, "unknown": [i for i in ids if i not in existing]}


def _cmd_search(args, db: Path, home: Path) -> dict:
    with QueryService(db) as service:
        return service.jobs(query=args.query, state=args.state, company=args.company, source=args.source,
                            archived=False, limit=args.limit, cursor=args.cursor, include_excluded=False,
                            sponsorship=args.sponsorship)


def _cmd_show(args, db: Path, home: Path) -> dict:
    with QueryService(db) as service:
        job = service.job(args.job_id)
    if job is None:
        raise ValueError(f"job #{args.job_id} not found")
    # Not the import records: a mailbox import's payload holds the e-mail's message id, subject and raw links, which
    # can carry tracking or sign-in tokens. The job's own fields (description, links, screening, history) stay.
    job.pop("ingestion", None)
    return job


def _decide(args, db: Path, status: str) -> dict:
    from auto_app.agent.service import AgentService

    result = AgentService(db).record_application_status(args.job_id, status, reason=args.reason)
    return {"job_id": args.job_id, "status": result["status"], "changed": result["changed"]}


def _cmd_want(args, db: Path, home: Path) -> dict:
    return _decide(args, db, "want_to_apply")


def _cmd_skip(args, db: Path, home: Path) -> dict:
    return _decide(args, db, "not_applied")


def _cmd_queue(args, db: Path, home: Path) -> dict:
    from auto_app.handoff.service import HandoffService

    return HandoffService(db).queue(args.limit)


def _cmd_claim(args, db: Path, home: Path) -> dict:
    from auto_app.handoff.service import HandoffService

    return HandoffService(db).claim(args.job_id, AGENT)


def _cmd_packet(args, db: Path, home: Path) -> dict:
    from auto_app.handoff.service import HandoffService

    return HandoffService(db).packet(_claim_token(args))


def _cmd_record(args, db: Path, home: Path) -> dict:
    from auto_app.handoff.service import HandoffService

    return HandoffService(db).record_attempt(_claim_token(args), args.outcome, args.note, None)  # no evidence files


def _cmd_release(args, db: Path, home: Path) -> dict:
    from auto_app.handoff.service import HandoffService

    return HandoffService(db).release(_claim_token(args))


def _cmd_hold(args, db: Path, home: Path) -> dict:
    from auto_app.handoff.service import HandoffService

    return HandoffService(db).hold(_claim_token(args))


def _cmd_questions(args, db: Path, home: Path) -> dict:
    from auto_app.handoff.service import HandoffService

    token = _claim_token(args)  # first: when it comes from stdin, it is the first line
    return HandoffService(db).submit_questions(token, _questions_json())


def _cmd_held(args, db: Path, home: Path) -> dict:
    from auto_app.handoff.service import HandoffService

    return HandoffService(db).held_applications()


def _cmd_kit(args, db: Path, home: Path) -> dict | bytes:
    from auto_app.handoff.service import HandoffService

    service = HandoffService(db)
    if args.job_id is None:
        return service.kit_index()
    data = service.kit_file(args.job_id, args.file or "job.json")
    if args.file is not None:
        return data  # printed as it is
    try:
        manifest = json.loads(data)
    except ValueError:
        raise ValueError(f"job #{args.job_id}'s job.json is not valid JSON") from None
    if not isinstance(manifest, dict):
        raise ValueError(f"job #{args.job_id}'s job.json is not a JSON object")
    return manifest


def _cmd_guide(args, db: Path, home: Path) -> bytes:
    from auto_app.handoff.service import HandoffService

    return HandoffService(db).kit_guide(args.which)


def _cmd_answers_needed(args, db: Path, home: Path) -> dict:
    from auto_app.handoff.service import HandoffService

    return HandoffService(db).pending_questions()


def _cmd_answers_ready(args, db: Path, home: Path) -> dict:
    from auto_app.handoff.service import HandoffService

    _owner_only("answers-ready")
    return HandoffService(db).answers_ready(args.job_id)


def _cmd_account(args, db: Path, home: Path) -> dict:
    from auto_app.handoff.service import HandoffService

    return HandoffService(db).record_account(args.site, args.email, not args.existing, args.note)


# ---- startup ------------------------------------------------------------------------------------------------

def _database() -> Path:
    """The runtime's database. Checked before any service is touched: opening a missing one would create it."""
    home = os.environ.get("AUTO_APP_HOME")
    if not home:
        raise _StartupError(2, "AUTO_APP_HOME is not set")
    db = Path(home).expanduser() / "data" / "auto_app.db"
    if not os.path.isfile(db):  # unlike Path.is_file(), False (not an error) when a parent can't be searched
        raise _StartupError(1, "the job database is missing: $AUTO_APP_HOME/data/auto_app.db is not a regular file")
    return db


def _print(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, default=str))


def _storage_errors() -> tuple[type[Exception], ...]:
    from sqlalchemy.exc import SQLAlchemyError  # imported only once something has failed, so --help stays light

    return sqlite3.Error, SQLAlchemyError, OSError


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        db = _database()
        result = args.run(args, db, db.parent.parent)
    except _StartupError as exc:
        _print({"status": "error", "error": str(exc)})
        return exc.code
    except ValueError as exc:  # QueryError, AgentError, HandoffError and ControlError are all ValueErrors
        _print({"status": "error", "error": str(exc)})
        return 1
    except _storage_errors() as exc:  # e.g. "database is locked" while the worker writes; anything else still raises
        # Only the class is reported: the message can hold SQL, its parameters or file paths.
        kind = "file" if isinstance(exc, OSError) else "database"
        _print({"status": "error", "error": f"{kind} error ({type(exc).__name__}); retry"})
        return 1
    if isinstance(result, bytes):  # `kit ID FILE` and `guide`: the file itself, byte for byte
        sys.stdout.flush()
        sys.stdout.buffer.write(result)
        sys.stdout.buffer.flush()
        return 0
    _print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

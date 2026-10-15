"""Small JSON query client. Does not load the application CLI, secrets or migrations."""

import argparse
import json
import os
import sys
from pathlib import Path

from auto_app.query.service import QueryError, QueryService


def _default_db() -> Path:
    home = os.environ.get("AUTO_APP_HOME")
    return (Path(home).expanduser() if home else Path(__file__).resolve().parents[3]) / "data" / "auto_app.db"


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Read the job database without changing it. JSON output by default.")
    root.add_argument("--db", type=Path, default=_default_db(), help="Existing database (defaults to AUTO_APP_HOME/data/auto_app.db).")
    root.add_argument("--json", action="store_true", help="JSON output (the default).")
    root.add_argument("--preferences", type=Path, help="Country preferences JSON (defaults to config/preferences.json beside the database's runtime home).")
    commands = root.add_subparsers(dest="command", required=True)
    context = commands.add_parser("context", help="Counts, source health and available query capabilities.")
    jobs = commands.add_parser("jobs", help="Page through jobs; legacy recommendation states are preserved.")
    jobs.add_argument("--query", help="Title/company search.")
    jobs.add_argument("--state", help="Existing state, e.g. recommended or want_to_apply.")
    jobs.add_argument("--company")
    jobs.add_argument("--source")
    archive = jobs.add_mutually_exclusive_group()
    archive.add_argument("--archived", action="store_true", help="Only archived jobs.")
    archive.add_argument("--all", action="store_true", help="Include active and archived jobs.")
    jobs.add_argument("--limit", type=int, default=50)
    jobs.add_argument("--cursor", help="Continue the same query using the returned next_cursor.")
    jobs.add_argument("--include-excluded", action="store_true", help="Audit jobs hidden by country preferences or screening rules.")
    jobs.add_argument("--sponsorship", choices=["offers", "no", "unknown", "citizenship_required"],
                      help="Only jobs with this visa-sponsorship signal from screening.")
    job = commands.add_parser("job", help="Job, postings, judgments and personal review history.")
    job.add_argument("job_id", type=int)
    serve = commands.add_parser("serve", help="Expose this read API (localhost by default).")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8766)
    for command in (context, jobs, job, serve):
        command.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "serve":
        import uvicorn

        from auto_app.query.api import create_app

        hosts = {f"127.0.0.1:{args.port}", f"localhost:{args.port}", f"[::1]:{args.port}"}
        if args.host not in {"0.0.0.0", "::"}:
            host = f"[{args.host}]" if ":" in args.host else args.host
            hosts.add(f"{host}:{args.port}")
        uvicorn.run(create_app(args.db, preferences_path=args.preferences, port=args.port, allowed_hosts=hosts),
                    host=args.host, port=args.port)
        return 0
    try:
        with QueryService(args.db, preferences_path=args.preferences) as service:
            if args.command == "context":
                result = service.context()
            elif args.command == "jobs":
                result = service.jobs(query=args.query, state=args.state, company=args.company,
                                      source=args.source, archived=None if args.all else args.archived,
                                      limit=args.limit, cursor=args.cursor, include_excluded=args.include_excluded, sponsorship=args.sponsorship)
            else:
                result = service.job(args.job_id)
                if result is None:
                    print(f"Job {args.job_id} not found.", file=sys.stderr)
                    return 1
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except QueryError as exc:
        print(f"Query failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

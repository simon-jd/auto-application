"""Retrieve and import repository snapshots and saved email messages."""

import glob
from pathlib import Path
import re
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
import yaml

from auto_app.core.db import tables as T
from auto_app.core.models import utcnow
from auto_app.ingest.email import EMAIL_PARSER_VERSION, parse_email
from auto_app.ingest.reader import read_feed
from auto_app.ingest.repository import parse_repository, validate_repository_format
from auto_app.ingest.repository_paged import feed_options, paged_board
from auto_app.ingest.service import IngestService
from auto_app.process.normalize import NORMALIZER_VERSION

READ_ERRORS = (OSError, ValueError, httpx.HTTPError)
COUNTERS = ("records", "created", "updated", "unchanged", "restored", "inactive", "missing", "jobs_created",
            "skipped_internships")


def validate_source(source: str) -> None:
    if not isinstance(source, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", source):
        raise ValueError("source must contain lowercase letters, numbers, underscores or hyphens")


def resolve_location(location: str, base: Path | None = None) -> str:
    if not isinstance(location, str) or not location.strip():
        raise ValueError("location must be a nonempty file path or HTTP(S) URL")
    parsed = urlparse(location)
    if parsed.scheme:
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("location must be a file path or absolute HTTP(S) URL")
        return location
    path = Path(location).expanduser()
    if not path.is_absolute() and base is not None:
        path = base / path
    return str(path.resolve())


def _check_kind(store, source: str, kind: str) -> dict:
    validate_source(source)
    checkpoint = store.source_health(source).get("ingest", {})
    if checkpoint.get("kind") not in (None, kind):
        raise ValueError(f"{source} is already used by a different source kind")
    with store.engine.connect() as conn:
        kinds = set(conn.execute(select(T.ingest_entries.c.kind).where(
            T.ingest_entries.c.source == source)).scalars())
    if kinds - {kind}:
        raise ValueError(f"{source} is already used by a different source kind")
    return checkpoint


def run_repository(store, source: str, location: str, client: httpx.Client | None = None,
                   *, format: str = "simplify", company: str | None = None,
                   skip_internships: bool = False, options: dict | None = None) -> dict:
    """`options`: a paged board feed's validated options (repository_paged.feed_options); None reads with
    the format's defaults."""
    validate_repository_format(format)
    if company is not None and (not isinstance(company, str) or not company.strip()):
        raise ValueError("company must be a nonempty string")
    company = company.strip() if company else None
    board = paged_board(format)
    location = board.board_url(location) if board is not None else resolve_location(location)
    previous = _check_kind(store, source, "repository")
    checkpoint = previous if (previous.get("location") == location
                              and previous.get("format", "simplify") == format
                              and previous.get("company") == company) else {}
    if board is not None:
        # A paged search: no HTTP validators, so every run reads and compares a fresh snapshot.
        result = board.read(location, client=client, **(options or {}))
    else:
        result = read_feed(location, etag=checkpoint.get("etag"),
                           last_modified=checkpoint.get("last_modified"), client=client)
    service = IngestService(store)
    if result.not_modified:
        if not checkpoint.get("revision") or not (checkpoint.get("etag") or checkpoint.get("last_modified")):
            raise ValueError("received HTTP 304 without a valid conditional snapshot of this origin")
        report = service.checked_unchanged(source)
        report.update(kind="repository", origin=location, revision=checkpoint["revision"], status="ok", issues=[])
    else:
        parsed = parse_repository(result.content, format=format, company=company)
        # Even a complete snapshot commits before linking finishes. Invalidate
        # the old validators before any import writes so an interruption and
        # upstream rollback cannot turn a stale 304 into a consistency claim.
        # Failed reads/parses leave the previous snapshot untouched above.
        store.update_health(source, ingest={
            "kind": "repository", "location": previous.get("location") or location,
            "format": previous.get("format", "simplify"),
            "company": previous.get("company"),
            "revision": previous.get("revision"), "etag": None, "last_modified": None,
        })
        report = service.ingest(source, parsed, kind="repository", origin=location,
                                revision=result.revision, complete_snapshot=not parsed.issues,
                                skip_internships=skip_internships)
        report["status"] = "partial" if parsed.issues else "ok"
    if report["status"] == "ok":
        store.update_health(source, ingest={
            "kind": "repository", "location": location,
            "format": format,
            "company": company,
            "etag": result.etag, "last_modified": result.last_modified,
            "revision": result.revision or checkpoint.get("revision"),
        })
    report["format"] = format
    return report


def _status(reports: list[dict]) -> str:
    if not reports or all(report["status"] == "ok" for report in reports):
        return "ok"
    return "error" if all(report["status"] == "error" for report in reports) else "partial"


def run_email(store, source: str, paths: list[Path], *, skip_internships: bool = False) -> dict:
    checkpoint = _check_kind(store, source, "email")
    if not paths:
        raise ValueError("at least one saved email file is required")
    reports, errors, issues = [], [], []
    saved_files = dict(checkpoint.get("files", {}))
    checkpoint_changed = False
    service = IngestService(store)
    for path in dict.fromkeys(Path(path).expanduser().resolve() for path in paths):
        try:
            result = read_feed(str(path))
            saved = saved_files.get(str(path))
            if (saved and saved.get("revision") == result.revision
                    and saved.get("normalizer_version") == NORMALIZER_VERSION
                    and saved.get("email_parser_version") == EMAIL_PARSER_VERSION):
                report = {"source": source, "kind": "email", "origin": str(path),
                          "revision": result.revision, "status": "ok", "issues": [],
                          **{key: 0 for key in COUNTERS}, "records": saved["records"],
                          "unchanged": saved["records"], "inactive": saved["inactive"],
                          "not_modified": True}
            else:
                parsed = parse_email(result.content)
                if saved:
                    # Invalidate before partial writes: a later rollback to the old
                    # file bytes must retry rather than reuse a stale checkpoint.
                    saved_files.pop(str(path))
                    store.update_health(source, ingest={"kind": "email", "files": saved_files})
                report = service.ingest(source, parsed, kind="email", origin=str(path),
                                        revision=result.revision, complete_snapshot=False,
                                        skip_internships=skip_internships)
                report["status"] = "partial" if parsed.issues else "ok"
                issues.extend({**issue, "path": str(path)} for issue in parsed.issues)
                if not parsed.issues:
                    saved_files[str(path)] = {"revision": result.revision, "normalizer_version": NORMALIZER_VERSION,
                                              "email_parser_version": EMAIL_PARSER_VERSION,
                                              "records": report["records"], "inactive": report["inactive"]}
                    checkpoint_changed = True
        except READ_ERRORS as exc:
            report = {"path": str(path), "status": "error", "error": str(exc)}
            errors.append(report)
        reports.append(report)
    if checkpoint_changed:
        store.update_health(source, ingest={"kind": "email", "files": saved_files})
    return {"source": source, "kind": "email", "status": _status(reports), "files": reports,
            "errors": errors, "issues": issues,
            **{key: sum(report.get(key, 0) for report in reports) for key in COUNTERS}}


def load_feed_config(config_path: Path) -> dict[str, dict]:
    """Validate configuration structure before opening a database; resolve relative inputs."""
    config_path = Path(config_path).expanduser().resolve()
    data = yaml.safe_load(config_path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        raise ValueError("feed config must contain a sources mapping")
    # Leave out rows whose own title or employment type says internship; a source may override it.
    skip_default = data.get("skip_internships", False)
    if not isinstance(skip_default, bool):
        raise ValueError("skip_internships must be a boolean")
    sources = {}
    for source, definition in data["sources"].items():
        validate_source(source)
        if not isinstance(definition, dict):
            raise ValueError(f"{source}: source definition must be a mapping")
        enabled = definition.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"{source}: enabled must be a boolean")
        if not enabled:
            continue
        skip = definition.get("skip_internships", skip_default)
        if not isinstance(skip, bool):
            raise ValueError(f"{source}: skip_internships must be a boolean")
        kind = definition.get("kind")
        if kind == "repository":
            format = definition.get("format", "simplify")
            validate_repository_format(format)
            company = definition.get("company")
            if company is not None and (not isinstance(company, str) or not company.strip()):
                raise ValueError(f"{source}: company must be a nonempty string")
            try:
                options = feed_options(definition, format=format)
                if options is not None:
                    board = paged_board(format)
                    if company is None and board.company_required:
                        raise ValueError(f"company is required for {format}: "
                                         f"{board.label}'s list never names the employer")
                    location = board.board_url(definition.get("location"))
                else:
                    location = resolve_location(definition.get("location"), config_path.parent)
            except ValueError as exc:
                raise ValueError(f"{source}: {exc}") from exc
            sources[source] = {"kind": kind, "format": format, "company": company.strip() if company else None,
                               "location": location, "skip_internships": skip}
            if options is not None:
                sources[source]["options"] = options
        elif kind == "email":
            paths = definition.get("paths")
            if not isinstance(paths, list) or not paths or any(not isinstance(p, str) or not p.strip() for p in paths):
                raise ValueError(f"{source}: email paths must be a nonempty list of file paths or glob patterns")
            resolved = []
            for pattern in paths:
                path = Path(pattern).expanduser()
                resolved.append(str(path if path.is_absolute() else config_path.parent / path))
            sources[source] = {"kind": kind, "paths": resolved, "skip_internships": skip}
        elif kind == "mailbox":
            from auto_app.ingest.mailbox import validate_mailbox_config
            sources[source] = validate_mailbox_config({**definition, "skip_internships": skip}, config_path.parent)
        else:
            raise ValueError(f"{source}: kind must be repository, email or mailbox")
    return sources


def run_config(store, config_path: Path, client: httpx.Client | None = None) -> dict:
    sources = load_feed_config(config_path)
    reports = {}
    for source, definition in sources.items():
        started = utcnow().isoformat()
        try:
            if definition["kind"] == "repository":
                report = run_repository(store, source, definition["location"], client=client,
                                        format=definition["format"], company=definition.get("company"),
                                        skip_internships=definition["skip_internships"],
                                        options=definition.get("options"))
            elif definition["kind"] == "mailbox":
                from auto_app.ingest.mailbox import run_mailbox
                report = run_mailbox(store, source, definition, client=client)
            else:
                paths = []
                for pattern in definition["paths"]:
                    # Preserve unmatched patterns so they become explicit file errors.
                    paths.extend(Path(p) for p in (sorted(glob.glob(pattern, recursive=True)) or [pattern]))
                report = run_email(store, source, paths, skip_internships=definition["skip_internships"])
        except READ_ERRORS as exc:
            report = {"source": source, "kind": definition["kind"], "status": "error", "error": str(exc)}
        reports[source] = report
        previous = store.source_health(source).get("ingest_run", {})
        progress = {
            "status": report["status"], "last_run_at": started, "finished_at": utcnow().isoformat(),
            "last_ok_at": started if report["status"] == "ok" else previous.get("last_ok_at"),
            **{key: report[key] for key in (*COUNTERS, "has_more", "messages_processed", "messages_skipped", "messages_gone",
                                          "messages_pending", "messages_deferred")
               if key in report},
        }
        if report.get("error"):
            progress["error"] = report["error"]
        elif report["status"] != "ok":
            errors = report.get("errors") or report.get("issues") or []
            if errors:
                progress["error"] = errors[0].get("error") or errors[0].get("reason") or "Some inputs failed"
            elif report.get("messages_pending"):
                progress["error"] = "Unresolved messages remain in the durable retry queue"
        store.update_health(source, status=report["status"], ingest_run=progress)
    return {"status": _status(list(reports.values())), "sources": reports}

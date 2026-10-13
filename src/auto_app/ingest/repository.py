"""Select a repository format and parse snapshots without network or storage."""

import json
from datetime import datetime, timezone
from urllib.parse import urlparse

from auto_app.ingest.models import IngestRecord, ParsedFeed


REPOSITORY_FORMATS = ("simplify", "speedyapply", "applyguy", "ricsign", "jobright")


def validate_repository_format(format: str) -> None:
    if format not in REPOSITORY_FORMATS:
        raise ValueError("repository format must be one of: " + ", ".join(REPOSITORY_FORMATS))


def parse_repository(content: bytes, *, format: str = "simplify") -> ParsedFeed:
    """Use explicit source format; existing configurations default to Simplify."""
    validate_repository_format(format)
    if format == "simplify":
        return _parse_simplify(content)
    if format in ("applyguy", "ricsign"):
        from auto_app.ingest.repository_json import parse_applyguy, parse_ricsign
        return (parse_applyguy if format == "applyguy" else parse_ricsign)(content)
    from auto_app.ingest.repository_markdown import parse_jobright, parse_speedyapply
    return (parse_speedyapply if format == "speedyapply" else parse_jobright)(content)


def _parse_simplify(content: bytes) -> ParsedFeed:
    """Parse a listing array, retaining source flags and reporting bad rows.

    Row issue indexes are zero based. A document error raises rather than looking
    like an empty successful snapshot; callers must treat row issues as partial.
    """
    try:
        rows = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("repository feed must contain valid JSON") from exc
    if not isinstance(rows, list):
        raise ValueError("repository feed must be a JSON array of listing objects")

    result = ParsedFeed()
    seen: set[str] = set()
    for index, row in enumerate(rows):
        try:
            record = _record(row)
            if record.external_id in seen:
                raise ValueError(f"duplicate id {record.external_id!r}")
        except ValueError as exc:
            result.issues.append({"index": index, "reason": str(exc)})
            continue
        seen.add(record.external_id)
        result.records.append(record)
    return result


def _text(row: dict, key: str, *, required: bool = False) -> str | None:
    value = row.get(key)
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    value = value.strip() if value is not None else None
    if required and not value:
        raise ValueError(f"{key} must be a nonempty string")
    return value or None


def _record(row) -> IngestRecord:
    if not isinstance(row, dict):
        raise ValueError("listing must be an object")
    external_id = _text(row, "id", required=True)
    url = _text(row, "url", required=True)
    try:
        parts = urlparse(url)
        valid_url = parts.scheme in {"http", "https"} and bool(parts.hostname)
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError("url must be an absolute HTTP(S) URL")
    for flag in ("active", "is_visible"):
        if not isinstance(row.get(flag), bool):
            raise ValueError(f"{flag} must be a boolean")

    locations = row.get("locations", [])
    if not isinstance(locations, list) or any(not isinstance(value, str) for value in locations):
        raise ValueError("locations must be a list of strings")
    posted_at = None
    timestamp = row.get("date_posted")
    if timestamp is not None:
        if type(timestamp) not in (int, float):
            raise ValueError("date_posted must be Unix epoch seconds")
        try:
            posted_at = datetime.fromtimestamp(timestamp, timezone.utc)
        except (ValueError, OverflowError, OSError) as exc:
            raise ValueError("date_posted is outside the supported timestamp range") from exc

    return IngestRecord(
        external_id=external_id,
        url=url,
        title=_text(row, "title"),
        company=_text(row, "company_name"),
        locations=[value.strip() for value in locations if value.strip()],
        description=_text(row, "description_text") or _text(row, "description") or "",
        posted_at=posted_at,
        employment_type=_text(row, "employment_type") or "unknown",
        active=row["active"] and row["is_visible"],
        metadata={"upstream": row},
    )

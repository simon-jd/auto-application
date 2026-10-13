"""Parse provider-specific public repository JSON without network or storage."""

import json
import re
from datetime import datetime, timezone

from auto_app.ingest.models import IngestRecord, ParsedFeed
from auto_app.ingest.urls import canonical_url


def parse_applyguy(content: bytes) -> ParsedFeed:
    """Read ApplyGuy's active-only export; missing rows do not imply closure."""
    return _records(_document(content), _applyguy_record)


def parse_ricsign(content: bytes) -> ParsedFeed:
    """Read the version 1 feed, keeping explicit closure and eligibility evidence."""
    document = _document(content)
    if type(document.get("api_version")) is not int or document["api_version"] != 1:
        raise ValueError("Ricsign feed api_version must be 1")
    return _records(document, _ricsign_record)


def _document(content: bytes) -> dict:
    try:
        document = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("repository feed must contain valid JSON") from exc
    if not isinstance(document, dict) or not isinstance(document.get("jobs"), list):
        raise ValueError("repository feed must be an object with a jobs array")
    return document


def _records(document: dict, build) -> ParsedFeed:
    result = ParsedFeed()
    seen = set()
    for index, row in enumerate(document["jobs"]):
        try:
            if not isinstance(row, dict):
                raise ValueError("listing must be an object")
            record = build(row, document)
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


def _url(row: dict, key: str) -> str:
    value = canonical_url(_text(row, key, required=True))
    if value is None:
        raise ValueError(f"{key} must be an absolute HTTP(S) URL")
    return value


def _posted_at(row: dict, key: str, metadata: dict) -> datetime | None:
    value = _text(row, key)
    if value is None:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            # Midnight is a storage convention, not an observed publication time.
            posted = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            precision = "day"
        else:
            posted = datetime.fromisoformat(value)
            if posted.tzinfo is None:
                raise ValueError("timestamp timezone is required")
            posted = posted.astimezone(timezone.utc)
            precision = "timestamp"
    except ValueError as exc:
        raise ValueError(f"{key} must be an ISO date or a timestamp with timezone") from exc
    metadata["posted_at_precision"] = precision
    return posted


def _metadata(row: dict, document: dict, *, row_observations: set[str], feed_observations: set[str]) -> dict:
    metadata = {"upstream": {key: value for key, value in row.items() if key not in row_observations}}
    feed = {key: value for key, value in document.items() if key not in feed_observations | {"jobs"}}
    if feed:
        metadata["feed"] = feed
    # The ingestion service excludes this key from content hashes while retaining
    # it in observation payloads. Refresh clocks do not change the underlying job.
    metadata["source_observation"] = {
        **{key: value for key, value in document.items() if key in feed_observations},
        **{key: value for key, value in row.items() if key in row_observations},
    }
    return metadata


def _applyguy_record(row: dict, document: dict) -> IngestRecord:
    location = _text(row, "location")
    metadata = _metadata(row, document, row_observations={"age"}, feed_observations={"updatedAt"})
    return IngestRecord(
        external_id=_text(row, "id", required=True),
        url=_url(row, "listingUrl"),
        title=_text(row, "title"),
        company=_text(row, "company"),
        locations=[location] if location else [],
        posted_at=_posted_at(row, "posted", metadata),
        active=True,
        metadata=metadata,
    )


def _ricsign_record(row: dict, document: dict) -> IngestRecord:
    active = row.get("active")
    if active is not None and not isinstance(active, bool):
        raise ValueError("active must be a boolean or null")
    link_status = _text(row, "link_status")
    if link_status and link_status.lower() in {"dead", "closed"}:
        active = False
    locations = row.get("locations", [])
    if not isinstance(locations, list) or any(not isinstance(value, str) for value in locations):
        raise ValueError("locations must be a list of strings")
    metadata = _metadata(
        row, document, row_observations={"last_verified"}, feed_observations={"generated_at", "count"},
    )
    return IngestRecord(
        external_id=_text(row, "uid", required=True),
        url=_url(row, "apply_url"),
        title=_text(row, "title"),
        company=_text(row, "company_name"),
        locations=[value.strip() for value in locations if value.strip()],
        posted_at=_posted_at(row, "posted_at", metadata),
        active=active,
        metadata=metadata,
    )

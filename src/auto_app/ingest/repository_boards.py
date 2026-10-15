"""Employer board snapshots, parsed with the collectors' platform parsers.

Document errors fail the read; malformed rows become partial-snapshot issues so
an absent posting is never closed merely because its row could not be parsed.
"""

import json

from auto_app.core.models import RawPosting
from auto_app.ingest.models import IngestRecord, ParsedFeed
from auto_app.ingest.urls import canonical_url
from auto_app.platforms.ashby import AshbySource
from auto_app.platforms.greenhouse import GreenhouseSource
from auto_app.platforms.lever import LeverSource
from auto_app.sources.parsing import ParseError

BOARD_FORMATS = {"greenhouse-board": GreenhouseSource, "lever-board": LeverSource, "ashby-board": AshbySource}


def _rows(format: str, document) -> list:
    if format == "lever-board":
        if not isinstance(document, list):
            raise ValueError("a Lever board feed must be a JSON array")
        return document
    if not isinstance(document, dict) or not isinstance(document.get("jobs"), list):
        raise ValueError("a board feed must be an object with a jobs array")
    return document["jobs"]


def parse_board(content: bytes, *, format: str, company: str | None = None) -> ParsedFeed:
    if format not in BOARD_FORMATS:
        raise ValueError("unsupported employer board format")
    if company is not None and (not isinstance(company, str) or not company.strip()):
        raise ValueError("company must be a nonempty string")
    company = company.strip() if company else None
    try:
        document = json.loads(content)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("board feed must contain valid JSON") from exc
    source = BOARD_FORMATS[format]()
    result, seen = ParsedFeed(), set()
    for index, row in enumerate(_rows(format, document)):
        try:
            if not isinstance(row, dict):
                raise ValueError("job must be an object")
            if format == "ashby-board" and row.get("isListed") is False:
                continue
            external_id = row.get("id")
            if not (isinstance(external_id, str) and external_id.strip()
                    or format == "greenhouse-board" and type(external_id) is int and external_id > 0):
                raise ValueError("job needs a nonempty id")
            job = source.to_job(row)
            posting = source.parse(RawPosting(source=format, source_job_id=job.id, kind="api", posting={},
                                              capture={}, card={}, record=row, company_hint=company))
            url = canonical_url(posting.url or job.url or "")
            if not url or not (posting.title or "").strip():
                raise ValueError("job needs an absolute HTTP(S) URL and a title")
            if job.id in seen:
                raise ValueError("duplicate job id")
        except (ValueError, KeyError, TypeError, AttributeError, OverflowError, OSError, ParseError) as exc:
            # Pydantic/platform diagnostics can include the input row, including
            # URL credentials. Report the type without echoing untrusted values.
            result.issues.append({"index": index, "reason": f"Invalid board job ({type(exc).__name__})"})
            continue
        seen.add(job.id)
        result.records.append(IngestRecord(
            external_id=job.id, url=url, title=posting.title.strip(), company=posting.company or company,
            locations=posting.locations, description=posting.description_text or "", posted_at=posting.posted_at,
            employment_type=posting.employment_type, active=True,
            metadata={"upstream": {"platform": source.platform, "board_job_id": job.id,
                                   "req_id": posting.req_id,
                                   # Lever's structured country: location text is often just "Remote".
                                   "country": (posting.extras or {}).get("country")}}))
    return result

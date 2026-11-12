"""Small parser contracts independent of retrieval and database storage."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class IngestRecord:
    external_id: str
    url: str
    title: str | None = None
    company: str | None = None
    locations: list[str] = field(default_factory=list)
    description: str = ""
    posted_at: datetime | None = None
    employment_type: str = "unknown"
    active: bool | None = None
    metadata: dict = field(default_factory=dict)
    listing_url: str | None = None  # the listing site's page when url is the employer's link


@dataclass
class ParsedFeed:
    records: list[IngestRecord] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)


@dataclass
class ReadResult:
    content: bytes | None
    revision: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False

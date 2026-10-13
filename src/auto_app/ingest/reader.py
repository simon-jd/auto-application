"""Read one local or public HTTP feed without parsing or persisting it."""

import hashlib
from pathlib import Path
from urllib.parse import urlparse

import httpx

from auto_app.ingest.models import ReadResult


def read_feed(
    location: str,
    etag: str | None = None,
    last_modified: str | None = None,
    client: httpx.Client | None = None,
) -> ReadResult:
    """Read bytes with a content revision, or signal an HTTP 304 explicitly."""
    parsed = urlparse(location)
    if not parsed.scheme:
        content = Path(location).expanduser().read_bytes()
        return ReadResult(content=content, revision=_revision(content))
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"unsupported feed URL scheme: {parsed.scheme}")
    if not parsed.hostname:
        raise ValueError("feed URL must have a hostname")
    if client is None:
        with httpx.Client(timeout=30, follow_redirects=True) as owned:
            return read_feed(location, etag=etag, last_modified=last_modified, client=owned)

    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    response = client.get(location, headers=headers, follow_redirects=True)
    if response.status_code == 304:
        return ReadResult(
            content=None,
            etag=response.headers.get("etag") or etag,
            last_modified=response.headers.get("last-modified") or last_modified,
            not_modified=True,
        )
    response.raise_for_status()
    content = response.content
    return ReadResult(
        content=content,
        revision=_revision(content),
        etag=response.headers.get("etag"),
        last_modified=response.headers.get("last-modified"),
    )


def _revision(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()

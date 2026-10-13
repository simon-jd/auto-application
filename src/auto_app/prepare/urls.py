"""Strict, canonical Ashby application targets shared by service and browser."""

import re
from urllib.parse import urlsplit


_PATH = re.compile(r"/([A-Za-z0-9][A-Za-z0-9_-]*)/"
                   r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
                   r"(?:/application)?/?")


def normalize_ashby_url(value: str) -> str:
    """Drop query/fragment tracking while rejecting ambiguous authorities/paths."""
    error = "application_url must be an HTTPS jobs.ashbyhq.com employer/UUID Ashby URL"
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError(error)
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ValueError(error) from exc
    if parsed.scheme != "https" or parsed.netloc.lower() != "jobs.ashbyhq.com":
        raise ValueError(error)
    matched = _PATH.fullmatch(parsed.path)
    if not matched:
        raise ValueError(error)
    return f"https://jobs.ashbyhq.com/{matched[1]}/{matched[2].lower()}/application"

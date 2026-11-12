"""URL normalization shared by ingestion parsers."""

import re
from html import unescape
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit


_TRACKING_PARAMS = {
    "gclid", "dclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "gh_src",
    "trk", "trkemail", "li_fat_id", "jr_id",  # jr_id: Jobright's marker on the employer links it hands out
}
_LINKEDIN_TRACKING_PARAMS = {"trackingid", "refid", "lipi", "midtoken", "midsig", "eid", "otptoken"}
_LINKEDIN_HOSTS = {"linkedin.com", "www.linkedin.com", "m.linkedin.com"}
_REDIRECT_PARAMS = {"url", "target", "joburl"}
_LINKEDIN_JOB = re.compile(r"^/(?:comm/)?jobs/view/(?:[^/]*-)?(\d+)/?$")


def _http_url(url: str):
    try:
        parsed = urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username or parsed.password or any(char.isspace() for char in parsed.netloc):
            return None
        parsed.port  # Reject malformed ports along with malformed host brackets.
        return parsed
    except ValueError:
        return None


def canonical_url(url: str) -> str | None:
    """Normalize an absolute HTTP URL without discarding unknown query fields.

    Explicit redirect destinations can be extracted locally; opaque tracking URLs
    are never resolved over the network. Unknown parameters may identify a job.
    """
    current = unescape(url.strip())
    seen = set()
    for _ in range(5):
        parsed = _http_url(current)
        if parsed is None:
            return None
        seen.add(current)
        target = None
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key.lower() not in _REDIRECT_PARAMS:
                continue
            # Email providers sometimes percent-encode the destination twice.
            for _ in range(3):
                if _http_url(value):
                    target = value
                    break
                decoded = unquote(value)
                if decoded == value:
                    break
                value = decoded
            if target:
                break
        if not target or target in seen:
            break
        current = target

    parsed = _http_url(current)
    if parsed is None:
        return None
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    if port and (parsed.scheme.lower(), port) not in {("https", 443), ("http", 80)}:
        host += f":{port}"
    path = parsed.path.rstrip("/") or "/"
    tracking_params = _TRACKING_PARAMS
    if parsed.hostname.lower() in _LINKEDIN_HOSTS:
        tracking_params = tracking_params | _LINKEDIN_TRACKING_PARAMS
        match = _LINKEDIN_JOB.fullmatch(path)
        if match:
            host = "www.linkedin.com"
            path = f"/jobs/view/{match.group(1)}"
    query = [
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in tracking_params
        # Other source= values can select a job board, so only Jobright's own referral is dropped.
        and not (key.lower() == "source" and value.lower() == "jobright")
    ]
    return urlunsplit((parsed.scheme.lower(), host, path, urlencode(sorted(query)), ""))

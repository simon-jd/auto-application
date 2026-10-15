"""Incoming Discord webhook delivery, without exposing the credential in diagnostics.

The caller obtains the URL from AUTO_APP_DISCORD_WEBHOOK_URL. Success requires
Discord's created-message receipt (wait=true); unconfirmed jobs remain pending.
"""

import logging
import math
import re
import time
from urllib.parse import urlsplit

import httpx

WEBHOOK_ENV = "AUTO_APP_DISCORD_WEBHOOK_URL"
MAX_EMBEDS = 5
MAX_TRIES = 3
MAX_RETRY_AFTER = 30
_WEBHOOK_PATH = re.compile(r"/api/(?:v\d+/)?webhooks/[0-9]+/[A-Za-z0-9_-]+")
_WEBHOOK_SECRET = re.compile(r"https?://(?:[^\s/@]+@)?(?:[\w-]+\.)?discord(?:app)?\.com"
                             r"(?::\d+)?/api/(?:v\d+/)?webhooks/[^\s<>\"']+", re.IGNORECASE)


class DiscordError(RuntimeError):
    pass


def redact_webhook_secrets(value: str) -> str:
    """Remove webhook paths/tokens from error text before it is saved or logged."""
    return _WEBHOOK_SECRET.sub("[Discord webhook redacted]", value)


class _RedactWebhookLogs(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        redacted = redact_webhook_secrets(message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


_LOG_FILTER = _RedactWebhookLogs()


def _units(text: str) -> int:
    # Conservative for Discord's Unicode limits, including supplementary emoji.
    return len(text.encode("utf-16-le")) // 2


def _validate_embeds(embeds: list[dict]) -> None:
    if len(embeds) > MAX_EMBEDS:
        raise DiscordError(f"Discord messages allow at most {MAX_EMBEDS} job embeds")
    total = 0
    for embed in embeds:
        fields = embed.get("fields") or []
        if len(fields) > 25:
            raise DiscordError("Discord embed has too many fields")
        strings = [(embed.get("title", ""), 256), (embed.get("description", ""), 4096),
                   ((embed.get("footer") or {}).get("text", ""), 2048),
                   ((embed.get("author") or {}).get("name", ""), 256)]
        strings.extend((field.get(key, ""), limit) for field in fields
                       for key, limit in (("name", 256), ("value", 1024)))
        for value, limit in strings:
            length = _units(value)
            if length > limit:
                raise DiscordError("Discord embed text exceeds a field limit")
            total += length
    if total > 6000:
        raise DiscordError("Discord embeds exceed the total message text limit")


def _retry_after(response: httpx.Response) -> float:
    try:
        body = response.json()
    except ValueError:
        body = {}
    value = body.get("retry_after") if isinstance(body, dict) else None
    if value is None:
        value = response.headers.get("Retry-After", 1)
    try:
        delay = float(value)
    except (TypeError, ValueError, OverflowError):
        return 1.0
    return delay if math.isfinite(delay) and delay >= 0 else 1.0


class Discord:
    def __init__(self, webhook_url: str, client: httpx.Client | None = None, sleep=time.sleep):
        try:
            url = urlsplit(webhook_url)
            valid = (url.scheme == "https" and url.hostname in {"discord.com", "discordapp.com"}
                     and not url.username and not url.password and url.port in {None, 443}
                     and not url.query and not url.fragment and _WEBHOOK_PATH.fullmatch(url.path))
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise DiscordError(f"{WEBHOOK_ENV} must be a Discord incoming webhook URL")
        self.url, self.sleep = webhook_url, sleep
        self._owns_client = client is None
        self.client = client if client is not None else httpx.Client(timeout=15, follow_redirects=False)
        # HTTPX logs full request URLs at INFO even when the sender never logs them.
        logging.getLogger("httpx").addFilter(_LOG_FILTER)

    def close(self) -> None:
        """Release the owned HTTP client; injected clients remain caller-owned."""
        if self._owns_client:
            self.client.close()

    def post(self, content: str | None = None, embeds: list[dict] | None = None) -> None:
        body: dict = {"allowed_mentions": {"parse": []}}
        if content:
            body["content"] = content.encode("utf-16-le")[:4000].decode("utf-16-le", errors="ignore")
        if embeds:
            _validate_embeds(embeds)
            body["embeds"] = embeds
        if len(body) == 1:
            raise DiscordError("Discord message must contain content or embeds")
        self._post(self.client, body)

    def _post(self, client: httpx.Client, body: dict) -> None:
        for attempt in range(MAX_TRIES):
            try:
                response = client.post(self.url, params={"wait": "true"}, json=body, follow_redirects=False)
            except httpx.HTTPError as exc:
                if attempt == MAX_TRIES - 1:
                    raise DiscordError(f"Discord unreachable: {type(exc).__name__}") from None
                self.sleep(2 ** attempt)
                continue
            if response.status_code == 429:
                delay = _retry_after(response)
                if delay > MAX_RETRY_AFTER or attempt == MAX_TRIES - 1:
                    raise DiscordError("Discord rate-limited the webhook; retry in a later round")
                self.sleep(delay)
                continue
            if response.is_success:
                try:
                    receipt = response.json()
                except ValueError:
                    receipt = None
                message_id = receipt.get("id") if isinstance(receipt, dict) else None
                if response.status_code == 200 and isinstance(message_id, str) and message_id.isdecimal():
                    return
                raise DiscordError("Discord did not confirm a created message")
            # Response bodies and HTTPError messages can echo the secret URL.
            raise DiscordError(f"Discord rejected the message: HTTP {response.status_code}")

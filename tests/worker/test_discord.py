"""Webhook delivery needs a receipt; diagnostics never expose its credential."""

import json
import logging

import httpx
import pytest

from auto_app.core.discord import Discord, DiscordError

URL = "https://discord.com/api/webhooks/1/secret-token"
RECEIPT = {"id": "123", "channel_id": "456", "webhook_id": "1"}


def discord(handler, slept=None, *, follow_redirects=False):
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=follow_redirects)
    return Discord(URL, client=client, sleep=(slept if slept is not None else []).append)


def test_rate_limits_are_waited_out_and_nobody_is_pinged():
    calls, slept = [], []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={"retry_after": 0.5}) if len(calls) == 1 else httpx.Response(200, json=RECEIPT)

    discord(handler, slept).post("hello", [{"title": "x"}])
    body = json.loads(calls[0].content)
    assert slept == [0.5] and len(calls) == 2
    assert body["allowed_mentions"] == {"parse": []}
    assert body["embeds"] == [{"title": "x"}]
    assert calls[0].url.params["wait"] == "true"


def test_rejections_raise_without_leaking_response_or_webhook_url():
    with pytest.raises(DiscordError) as failure:
        discord(lambda request: httpx.Response(400, text=f"bad embed {URL} secret-token")).post("hello")
    assert "secret-token" not in str(failure.value) and "400" in str(failure.value)


@pytest.mark.parametrize("url", [
    "https://example.com/hook", "http://discord.com/api/webhooks/1/token",
    "https://discord.com.evil.test/api/webhooks/1/token", "https://discord.com@evil.test/api/webhooks/1/token",
    "https://user:password@discord.com/api/webhooks/1/token", "https://discord.com:8443/api/webhooks/1/token",
    "https://discord.com/api/webhooks/", "https://discord.com/api/webhooks/1/../token",
    "https://discord.com/api/webhooks/1/token?wait=false", "https://discord.com/api/webhooks/1/token#secret",
])
def test_only_valid_discord_webhook_urls_are_accepted(url):
    with pytest.raises(DiscordError) as failure:
        Discord(url)
    assert url not in str(failure.value)


@pytest.mark.parametrize("response", [
    httpx.Response(204), httpx.Response(200, json={}), httpx.Response(200, json=[]),
    httpx.Response(200, text="not JSON"), httpx.Response(200, json={"id": "not-a-message"}),
])
def test_unconfirmed_success_is_not_delivery(response):
    with pytest.raises(DiscordError, match="confirm"):
        discord(lambda request: response).post("hello")


def test_http_errors_are_bounded_and_do_not_expose_the_request():
    calls, slept = [], []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError(f"failure at {URL}", request=request)

    with pytest.raises(DiscordError) as failure:
        discord(handler, slept).post("hello")
    assert len(calls) == 3 and slept == [1, 2]
    assert "ConnectError" in str(failure.value) and "secret-token" not in str(failure.value)


def test_rate_limit_retries_are_bounded():
    calls, slept = [], []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={"retry_after": 0.5})

    with pytest.raises(DiscordError, match="rate-limit"):
        discord(handler, slept).post("hello")
    assert len(calls) == 3 and slept == [0.5, 0.5]


def test_long_retry_delay_defers_instead_of_retrying_before_discord_allows():
    calls, slept = [], []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={"retry_after": 120})

    with pytest.raises(DiscordError, match="rate-limit"):
        discord(handler, slept).post("hello")
    assert len(calls) == 1 and slept == []


@pytest.mark.parametrize("body", [[], {}, {"retry_after": None}, {"retry_after": "NaN"},
                                  {"retry_after": "Infinity"}, {"retry_after": -1}])
def test_malformed_retry_delays_are_safe_and_bounded(body):
    slept = []
    with pytest.raises(DiscordError):
        discord(lambda request: httpx.Response(429, json=body), slept).post("hello")
    assert slept == [1.0, 1.0]


def test_retry_after_header_is_used_when_body_has_no_delay():
    calls, slept = [], []

    def handler(request):
        calls.append(request)
        return (httpx.Response(429, headers={"Retry-After": "0.75"}, text="not JSON")
                if len(calls) == 1 else httpx.Response(200, json=RECEIPT))

    discord(handler, slept).post("hello")
    assert slept == [0.75]


def test_redirects_are_never_followed_even_with_an_injected_client():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://example.com/collect"})

    with pytest.raises(DiscordError, match="302"):
        discord(handler, follow_redirects=True).post("hello")
    assert len(calls) == 1


def test_request_logs_do_not_expose_webhook_tokens(caplog):
    with caplog.at_level(logging.INFO, logger="httpx"):
        discord(lambda request: httpx.Response(200, json=RECEIPT)).post("hello")
    assert "secret-token" not in caplog.text


@pytest.mark.parametrize("embeds", [
    [{"title": "x"}] * 6,
    [{"description": "x" * 2000}] * 4,
    [{"title": "x" * 257}],
])
def test_oversized_embeds_are_rejected_instead_of_silently_dropping_jobs(embeds):
    calls = []
    sender = discord(lambda request: calls.append(request) or httpx.Response(200, json=RECEIPT))
    with pytest.raises(DiscordError):
        sender.post(embeds=embeds)
    assert calls == []


def test_close_releases_the_owned_client(monkeypatch):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=RECEIPT)))
    monkeypatch.setattr("auto_app.core.discord.httpx.Client", lambda **kwargs: client)
    sender = Discord(URL)
    sender.close()
    assert client.is_closed


def test_close_does_not_close_an_injected_client():
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=RECEIPT))) as client:
        sender = Discord(URL, client=client)
        sender.close()
        assert not client.is_closed

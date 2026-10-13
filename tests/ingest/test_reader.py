"""Feed retrieval uses HTTP transport doubles and temporary local files only."""

import hashlib

import httpx
import pytest

from auto_app.ingest.reader import read_feed


URL = "https://raw.githubusercontent.com/Example/jobs/dev/listings.json"


def test_local_bytes_and_http_body_have_same_content_revision(tmp_path):
    content = b'[{"id": "one"}]\n'
    path = tmp_path / "jobs.json"
    path.write_bytes(content)
    local = read_feed(str(path), etag='"old"')
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content))) as client:
        remote = read_feed(URL, client=client)
    assert local.content == remote.content == content
    assert local.revision == remote.revision == "sha256:" + hashlib.sha256(content).hexdigest()
    assert local.etag is None and local.last_modified is None
    assert not local.not_modified and not remote.not_modified


def test_http_records_server_validators_and_does_not_close_injected_client():
    def handler(request):
        return httpx.Response(200, content=b"[]", headers={
            "ETag": '"snapshot-2"', "Last-Modified": "Thu, 01 Oct 2026 00:00:00 GMT",
        })

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = read_feed(URL, client=client)
        assert not client.is_closed
    assert result.etag == '"snapshot-2"'
    assert result.last_modified == "Thu, 01 Oct 2026 00:00:00 GMT"
    assert result.content == b"[]" and not result.not_modified


def test_conditional_http_304_keeps_prior_validators_without_fake_empty_body():
    def handler(request):
        assert request.headers["if-none-match"] == '"snapshot-1"'
        assert request.headers["if-modified-since"] == "Thu, 01 Oct 2026 00:00:00 GMT"
        return httpx.Response(304)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = read_feed(URL, etag='"snapshot-1"',
                           last_modified="Thu, 01 Oct 2026 00:00:00 GMT", client=client)
    assert result.not_modified
    assert result.content is None and result.revision is None
    assert result.etag == '"snapshot-1"'
    assert result.last_modified == "Thu, 01 Oct 2026 00:00:00 GMT"


def test_304_can_refresh_server_validator():
    with httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(304, headers={"ETag": 'W/"snapshot-1"'}),
    )) as client:
        result = read_feed(URL, etag='"snapshot-1"', client=client)
    assert result.etag == 'W/"snapshot-1"' and result.not_modified


@pytest.mark.parametrize("status", [403, 404, 429, 500])
def test_http_errors_are_not_returned_as_successful_snapshot(status):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status))) as client:
        with pytest.raises(httpx.HTTPStatusError):
            read_feed(URL, client=client)


def test_transport_error_propagates():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.ConnectError):
            read_feed(URL, client=client)


def test_missing_local_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_feed(str(tmp_path / "missing.json"))


def test_unsupported_remote_scheme_raises_clear_error():
    with pytest.raises(ValueError, match="scheme"):
        read_feed("ftp://example.com/jobs.json")

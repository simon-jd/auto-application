"""Authenticated HTTP MCP against disposable data and an in-process ASGI client."""

import os

import pytest
from starlette.testclient import TestClient

from tests.screening_db import screening_db  # noqa: F401

TOKEN = "t" * 40
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def app(db):
    from auto_app.agent.auth import PublicAuth
    from auto_app.agent.mcp_server import create_mcp_server

    auth = PublicAuth(TOKEN, "https://testserver")
    return create_mcp_server(db, auth=auth).streamable_http_app(
        json_response=True, transport_security=auth.transport_security(8769))


def test_requests_without_the_token_are_rejected(screening_db):
    with TestClient(app(screening_db)) as client:
        for authorization in (None, "Bearer " + "x" * 40):
            headers = {**HEADERS, **({"Authorization": authorization} if authorization else {})}
            assert client.post("/mcp", json=INIT, headers=headers).status_code == 401
        right = client.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": f"Bearer {TOKEN}"})
        assert right.status_code == 200, right.text


def test_unknown_hosts_are_rejected_even_with_the_token(screening_db):
    with TestClient(app(screening_db), base_url="https://evil.example") as client:
        response = client.post("/mcp", json=INIT, headers={**HEADERS, "Authorization": f"Bearer {TOKEN}"})
        assert response.status_code in (400, 403, 421)


def test_token_files_must_be_private_and_long(tmp_path):
    from auto_app.agent.auth import load_token

    path = tmp_path / "mcp-token"
    path.write_text(TOKEN + "\n")
    path.chmod(0o600)
    assert load_token(path) == TOKEN
    path.chmod(0o644)
    with pytest.raises(ValueError, match="chmod 600"):
        load_token(path)
    path.write_text("short")
    path.chmod(0o600)
    with pytest.raises(ValueError, match="32"):
        load_token(path)


def test_token_symlinks_and_invalid_encoding_are_rejected_without_content(tmp_path):
    from auto_app.agent.auth import load_token

    path = tmp_path / "token"
    path.write_bytes(b"fictional-secret-\xff")
    path.chmod(0o600)
    with pytest.raises(ValueError) as error:
        load_token(path)
    assert "fictional-secret" not in str(error.value)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        load_token(link)


@pytest.mark.parametrize("url", ["http://example.com", "https://user:secret@example.com", "https://bad host",
    "https://example.com/path", "https://example.com/#secret", "https://example.com:bad",
    "https://example.com:99999", "https://example.com?secret=yes", "https://example.com\n"])
def test_public_url_rejects_unsafe_shapes_without_echoing_input(url):
    from auto_app.agent.auth import PublicAuth

    with pytest.raises(ValueError) as error:
        PublicAuth(TOKEN, url)
    assert "secret" not in str(error.value)


def test_the_cli_requires_both_auth_options(tmp_path, capsys):
    from auto_app.agent.cli import main

    assert main(["serve", "--db", str(tmp_path / "x.db"), "--transport", "streamable-http",
                 "--auth-token-file", str(tmp_path / "t")]) == 2
    assert "go together" in capsys.readouterr().err
    assert not (tmp_path / "x.db").exists()


async def test_static_verifier_checks_token_and_hides_it_in_repr():
    from auto_app.agent.auth import PublicAuth

    auth = PublicAuth(TOKEN, "https://example.com")
    assert TOKEN not in repr(auth)
    assert await auth.verifier().verify_token("wrong") is None
    assert (await auth.verifier().verify_token(TOKEN)).scopes == ["jobs"]

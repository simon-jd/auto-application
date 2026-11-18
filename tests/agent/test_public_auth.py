"""Authenticated HTTP MCP against disposable data and an in-process ASGI client."""

import os
import subprocess
import sys

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


@pytest.mark.parametrize(("args", "reason"), [
    (["--transport", "streamable-http"], "--handoff-tools"),
    (["--handoff-tools"], "--transport streamable-http"),
    (["--handoff-tools", "--transport", "streamable-http", "--host", "localhost"], "127.0.0.1"),
    (["--handoff-tools", "--transport", "streamable-http", "--host", "::1"], "127.0.0.1"),
    (["--handoff-tools", "--transport", "streamable-http", "--host", "127.0.0.2"], "127.0.0.1"),
    (["--handoff-tools", "--transport", "streamable-http", "--public-url", "https://testserver"],
     "cannot be combined"),
    (["--handoff-tools", "--transport", "streamable-http", "--auth-token-file", "unused-token"],
     "cannot be combined"),
])
def test_loopback_handoff_opt_in_rejects_other_configurations(tmp_path, args, reason):
    result = subprocess.run([sys.executable, "-m", "auto_app.agent.cli", "serve", "--db",
                             str(tmp_path / "missing.db"), "--allow-unauthenticated-loopback-handoff", *args],
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 2
    assert reason in result.stderr and "unrecognized arguments" not in result.stderr


# The SDK's own default for 127.0.0.1 already allows any port on 127.0.0.1, localhost and [::1], so only the
# localhost, other-port and localhost-Origin rows fail if the opt-in's exact pinning is removed.
LOOPBACK_PINNING = [
    ({"Host": "external.example:8767"}, 421),
    ({"Host": "localhost:8767"}, 421),
    ({"Host": "127.0.0.1:9999"}, 421),
    ({"Host": "[::1]:8767"}, 421),
    ({"Origin": "https://external.example"}, 403),
    ({"Origin": "http://localhost:8767"}, 403),
    ({"Origin": "http://127.0.0.1:9999"}, 403),
    ({}, 200),
    ({"Origin": "http://127.0.0.1:8767"}, 200),
]


def test_loopback_handoff_pins_the_exact_host_and_origin(screening_db, monkeypatch):
    from mcp.server import MCPServer

    from auto_app.agent.cli import main

    started = []

    def run(server, *, transport, host, port, **kwargs):
        assert (transport, host, port) == ("streamable-http", "127.0.0.1", 8767)
        app = server.streamable_http_app(host=host, **kwargs)
        with TestClient(app, base_url=f"http://{host}:{port}") as client:
            codes = [(extra, client.post("/mcp", json=INIT, headers={**HEADERS, **extra}).status_code)
                     for extra, _ in LOOPBACK_PINNING]
        assert codes == LOOPBACK_PINNING
        security = kwargs["transport_security"]
        assert (security.allowed_hosts, security.allowed_origins) == (["127.0.0.1:8767"], ["http://127.0.0.1:8767"])
        started.append(True)

    monkeypatch.setattr(MCPServer, "run", run)
    assert main(["serve", "--db", str(screening_db), "--transport", "streamable-http",
                 "--handoff-tools", "--allow-unauthenticated-loopback-handoff"]) == 0
    assert started == [True]


def test_handoff_tools_with_authentication_still_start_through_the_cli(screening_db, tmp_path, monkeypatch):
    from mcp.server import MCPServer

    from auto_app.agent.auth import PublicAuth
    from auto_app.agent.cli import main

    token_file = tmp_path / "mcp-token"
    token_file.write_text(TOKEN + "\n")
    token_file.chmod(0o600)
    started = []

    def run(server, *, transport, host, port, **kwargs):
        assert (transport, host, port) == ("streamable-http", "127.0.0.1", 8767)
        # The authenticated listener's transport security, not the loopback opt-in's exact pinning.
        assert kwargs["transport_security"] == PublicAuth(TOKEN, "https://testserver").transport_security(8767)
        app = server.streamable_http_app(host=host, **kwargs)
        with TestClient(app, base_url="https://testserver") as client:
            assert client.post("/mcp", json=INIT, headers=HEADERS).status_code == 401
            authorized = {**HEADERS, "Authorization": f"Bearer {TOKEN}"}
            init = client.post("/mcp", json=INIT, headers=authorized)
            assert init.status_code == 200, init.text
            session = {**authorized, "mcp-session-id": init.headers["mcp-session-id"],
                       "mcp-protocol-version": init.json()["result"]["protocolVersion"]}
            client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=session)
            listed = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=session)
            assert listed.status_code == 200, listed.text
            assert "claim_application" in {tool["name"] for tool in listed.json()["result"]["tools"]}
        started.append(True)

    monkeypatch.setattr(MCPServer, "run", run)
    assert main(["serve", "--db", str(screening_db), "--transport", "streamable-http", "--handoff-tools",
                 "--auth-token-file", str(token_file), "--public-url", "https://testserver"]) == 0
    assert started == [True]


async def test_static_verifier_checks_token_and_hides_it_in_repr():
    from auto_app.agent.auth import PublicAuth

    auth = PublicAuth(TOKEN, "https://example.com")
    assert TOKEN not in repr(auth)
    assert await auth.verifier().verify_token("wrong") is None
    assert (await auth.verifier().verify_token(TOKEN)).scopes == ["jobs"]

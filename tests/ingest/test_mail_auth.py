"""OAuth protocol tests use synthetic credentials and in-memory HTTP only."""

import base64
import hashlib
import io
import json
import stat
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from typer.testing import CliRunner
import typer


def credential_file(tmp_path, **overrides):
    path = tmp_path / "credentials.json"
    data = {"provider": "gmail", "client_id": "test-client", "client_secret": "test-secret",
            "access_token": "old-access", "refresh_token": "old-refresh",
            "expires_at": 900, "account_key": "same-account"}
    data.update(overrides)
    path.write_text(json.dumps(data))
    return path


def test_valid_token_needs_no_network_or_credential_write(tmp_path, monkeypatch):
    from auto_app.ingest import mail_auth
    monkeypatch.setattr(mail_auth.time, "time", lambda: 1000)
    path = credential_file(tmp_path, expires_at=5000)
    before = path.read_bytes()
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("unexpected refresh"))) as client:
        assert mail_auth.access_token(path, provider="gmail", client=client) == "old-access"
    assert path.read_bytes() == before


@pytest.mark.parametrize("provider,endpoint", [
    ("gmail", "https://oauth2.googleapis.com/token"),
    ("outlook", "https://login.microsoftonline.com/common/oauth2/v2.0/token"),
])
def test_refresh_rotation_is_saved_atomically_without_changing_account(tmp_path, monkeypatch, provider, endpoint):
    from auto_app.ingest import mail_auth
    monkeypatch.setattr(mail_auth.time, "time", lambda: 1000)
    path = credential_file(tmp_path, provider=provider)

    def handler(request):
        assert str(request.url) == endpoint and request.method == "POST"
        form = parse_qs(request.content.decode())
        assert form["grant_type"] == ["refresh_token"]
        assert form["refresh_token"] == ["old-refresh"]
        if provider == "outlook":
            assert "client_secret" not in form
            assert form["scope"] == ["offline_access https://graph.microsoft.com/Mail.Read"]
        else:
            assert form["client_secret"] == ["test-secret"]
        return httpx.Response(200, json={"access_token": "new-access", "refresh_token": "rotated-refresh",
                                        "expires_in": 3600, "token_type": "Bearer"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert mail_auth.access_token(path, provider=provider, client=client) == "new-access"
        assert not client.is_closed
    saved = json.loads(path.read_text())
    assert saved["access_token"] == "new-access" and saved["refresh_token"] == "rotated-refresh"
    assert saved["expires_at"] == 4600 and saved["account_key"] == "same-account"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [path]


def test_google_refresh_without_replacement_preserves_refresh_token(tmp_path, monkeypatch):
    from auto_app.ingest import mail_auth
    monkeypatch.setattr(mail_auth.time, "time", lambda: 1000)
    path = credential_file(tmp_path)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        "access_token": "new-access", "expires_in": 3600, "token_type": "Bearer",
    }))) as client:
        mail_auth.access_token(path, provider="gmail", client=client)
    assert json.loads(path.read_text())["refresh_token"] == "old-refresh"


@pytest.mark.parametrize("response", [
    httpx.Response(400, json={"error": "invalid_grant", "error_description": "secret-old-refresh"}),
    httpx.Response(500, text="secret-old-refresh"),
    httpx.Response(200, json={"access_token": "secret-new-access", "expires_in": "not-a-number"}),
])
def test_failed_refresh_preserves_file_and_redacts_response(tmp_path, monkeypatch, response):
    from auto_app.ingest import mail_auth
    monkeypatch.setattr(mail_auth.time, "time", lambda: 1000)
    path = credential_file(tmp_path)
    before = path.read_bytes()
    with httpx.Client(transport=httpx.MockTransport(lambda _: response)) as client:
        with pytest.raises(mail_auth.MailAuthError) as error:
            mail_auth.access_token(path, provider="gmail", client=client)
    assert "secret-" not in str(error.value) and "old-refresh" not in str(error.value)
    assert path.read_bytes() == before


def test_network_error_does_not_echo_sensitive_exception(tmp_path):
    from auto_app.ingest import mail_auth
    path = credential_file(tmp_path)
    def handler(request):
        raise httpx.ConnectError("secret-old-refresh", request=request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(mail_auth.MailAuthError, match="network") as error:
            mail_auth.access_token(path, provider="gmail", client=client)
    assert "secret-" not in str(error.value)


def test_wrong_provider_is_rejected_before_using_a_valid_token(tmp_path):
    from auto_app.ingest import mail_auth
    path = credential_file(tmp_path, expires_at=9e12)
    with pytest.raises(mail_auth.MailAuthError, match="provider"):
        mail_auth.access_token(path, provider="outlook")


@pytest.mark.parametrize("query", ["code=secret-code&state=wrong", "code=secret-code", "state=expected",
                                    "state=expected&error=access_denied", "state=expected&state=wrong&code=secret-code",
                                    "state=%E4%B8%AD%E6%96%87&code=secret-code"])
def test_callback_rejects_invalid_state_missing_code_and_denial_without_echo(query):
    from auto_app.ingest import mail_auth
    with pytest.raises(mail_auth.MailAuthError) as error:
        mail_auth._callback_code("/?" + query, "expected")
    assert "secret-code" not in str(error.value)


def test_callback_decodes_only_matching_state():
    from auto_app.ingest import mail_auth
    assert mail_auth._callback_code("/?state=expected&code=abc%2B123", "expected") == "abc+123"


def fake_callback_server(monkeypatch, mail_auth, announced, *, denied=False):
    """Replace only the socket boundary; run the real HTTP callback handler."""
    class Socket:
        def __init__(self, request):
            self.input = io.BytesIO(request)
            self.output = io.BytesIO()
        def makefile(self, *args, **kwargs):
            return self.input
        def sendall(self, data):
            self.output.write(data)
    class Server:
        def __init__(self, address, handler):
            assert address[0] == "127.0.0.1"
            self.server_address = ("127.0.0.1", address[1] or 43210)
            self.handler = handler
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def handle_request(self):
            auth_url = next(message for message in announced if message.startswith("https://accounts.google.com/"))
            state = parse_qs(urlparse(auth_url).query)["state"][0]
            tail = "error=access_denied" if denied else "code=synthetic-code"
            sock = Socket(f"GET /?state={state}&{tail} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
            self.handler(sock, ("127.0.0.1", 50000), self)
            assert b"synthetic-code" not in sock.output.getvalue()
    monkeypatch.setattr(mail_auth, "HTTPServer", Server)


def test_gmail_authorization_pkce_readonly_offline_and_new_account_key(tmp_path, monkeypatch):
    from auto_app.ingest import mail_auth
    monkeypatch.setattr(mail_auth.time, "time", lambda: 1000)
    client_path = tmp_path / "desktop.json"
    client_path.write_text(json.dumps({"installed": {"client_id": "test-client", "client_secret": "test-secret"}}))
    credentials = credential_file(tmp_path)
    announced = []
    fake_callback_server(monkeypatch, mail_auth, announced)
    monkeypatch.setattr(mail_auth.webbrowser, "open", lambda _: pytest.fail("no-browser ignored"))
    def handler(request):
        form = parse_qs(request.content.decode())
        auth_url = next(line for line in announced if line.startswith("https://accounts.google.com/"))
        query = parse_qs(urlparse(auth_url).query)
        assert query["scope"] == ["https://www.googleapis.com/auth/gmail.readonly"]
        assert query["access_type"] == ["offline"] and query["prompt"] == ["consent"]
        assert query["code_challenge_method"] == ["S256"]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
        assert query["code_challenge"] == [challenge]
        assert form["redirect_uri"] == query["redirect_uri"] == ["http://127.0.0.1:43210/"]
        assert form["code"] == ["synthetic-code"] and form["grant_type"] == ["authorization_code"]
        return httpx.Response(200, json={"access_token": "secret-access", "refresh_token": "secret-refresh",
                                        "expires_in": 3600, "token_type": "Bearer"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = mail_auth.authorize_gmail(client_path, credentials, no_browser=True, client=client, announce=announced.append)
    assert result == credentials
    data = json.loads(credentials.read_text())
    assert data["provider"] == "gmail" and data["account_key"] != "same-account"
    assert data["expires_at"] == 4600 and stat.S_IMODE(credentials.stat().st_mode) == 0o600
    assert "secret-access" not in " ".join(announced) and "secret-refresh" not in " ".join(announced)


def test_gmail_denial_preserves_old_credentials_without_token_exchange(tmp_path, monkeypatch):
    from auto_app.ingest import mail_auth
    path = tmp_path / "desktop.json"
    path.write_text(json.dumps({"installed": {"client_id": "test-client"}}))
    credentials = credential_file(tmp_path)
    before = credentials.read_bytes()
    announced = []
    fake_callback_server(monkeypatch, mail_auth, announced, denied=True)
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("denial exchanged a code"))) as client:
        with pytest.raises(mail_auth.MailAuthError, match="denied"):
            mail_auth.authorize_gmail(path, credentials, no_browser=True, client=client, announce=announced.append)
    assert credentials.read_bytes() == before


def test_gmail_authorization_timeout_closes_listener_without_saving_tokens(tmp_path, monkeypatch):
    from auto_app.ingest import mail_auth
    path = tmp_path / "desktop.json"
    path.write_text(json.dumps({"installed": {"client_id": "test-client"}}))
    announced = []
    fake_callback_server(monkeypatch, mail_auth, announced)
    credentials = tmp_path / "credentials.json"
    with pytest.raises(mail_auth.MailAuthError, match="timed out"):
        mail_auth.authorize_gmail(path, credentials, no_browser=True, announce=announced.append, timeout=0)
    assert not credentials.exists()


def test_gmail_requires_desktop_client_before_opening_listener(tmp_path, monkeypatch):
    from auto_app.ingest import mail_auth
    path = tmp_path / "web.json"
    path.write_text(json.dumps({"web": {"client_id": "test-client", "client_secret": "secret-client"}}))
    monkeypatch.setattr(mail_auth, "HTTPServer", lambda *args: pytest.fail("invalid client started a listener"))
    with pytest.raises(mail_auth.MailAuthError, match="Desktop app"):
        mail_auth.authorize_gmail(path, tmp_path / "credentials.json")


def test_failed_atomic_replace_keeps_previous_credentials_and_cleans_temporary(tmp_path, monkeypatch):
    from auto_app.ingest import mail_auth
    path = credential_file(tmp_path)
    before = path.read_bytes()
    def reject_replace(*args):
        raise OSError("synthetic permission error")
    monkeypatch.setattr(mail_auth.os, "replace", reject_replace)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        "access_token": "secret-new-access", "refresh_token": "secret-new-refresh", "expires_in": 3600,
    }))) as client:
        with pytest.raises(mail_auth.MailAuthError, match="Cannot save"):
            mail_auth.access_token(path, provider="gmail", client=client)
    assert path.read_bytes() == before and list(tmp_path.iterdir()) == [path]


def fake_time(monkeypatch, mail_auth):
    value = [0.0]
    delays = []
    monkeypatch.setattr(mail_auth.time, "monotonic", lambda: value[0])
    monkeypatch.setattr(mail_auth.time, "time", lambda: 1000 + value[0])
    def sleep(delay):
        delays.append(delay)
        value[0] += delay
    monkeypatch.setattr(mail_auth.time, "sleep", sleep)
    return delays


def test_outlook_device_flow_waits_handles_slowdown_and_stores_offline_tokens(tmp_path, monkeypatch):
    from auto_app.ingest import mail_auth
    delays = fake_time(monkeypatch, mail_auth)
    announced = []
    polls = iter(["authorization_pending", "slow_down", None])
    def handler(request):
        form = parse_qs(request.content.decode())
        assert form["client_id"] == ["test-client"] and "client_secret" not in form
        if request.url.path.endswith("/devicecode"):
            assert form["scope"] == ["offline_access https://graph.microsoft.com/Mail.Read"]
            return httpx.Response(200, json={"device_code": "secret-device", "user_code": "ABCD-EFGH",
                "verification_uri": "https://microsoft.com/devicelogin", "expires_in": 900, "interval": 5,
                "message": "untrusted secret-device body"})
        assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:device_code"]
        assert form["device_code"] == ["secret-device"]
        error = next(polls)
        if error:
            return httpx.Response(400, json={"error": error, "error_description": "secret-details"})
        return httpx.Response(200, json={"access_token": "secret-access", "refresh_token": "secret-refresh",
                                        "expires_in": 3600, "token_type": "Bearer"})
    path = tmp_path / "new" / "outlook.json"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert mail_auth.authorize_outlook("test-client", path, client=client, announce=announced.append) == path
    assert delays == [5, 5, 10]
    data = json.loads(path.read_text())
    assert data["provider"] == "outlook" and data["tenant"] == "common" and data["account_key"]
    assert data["expires_at"] == 4620
    output = " ".join(announced)
    assert "ABCD-EFGH" in output and "https://microsoft.com/devicelogin" in output
    assert "secret-" not in output


@pytest.mark.parametrize("failure", ["authorization_declined", "expired_token", "bad_verification_code"])
def test_outlook_terminal_errors_stop_polling_without_credentials(tmp_path, monkeypatch, failure):
    from auto_app.ingest import mail_auth
    fake_time(monkeypatch, mail_auth)
    def handler(request):
        if request.url.path.endswith("/devicecode"):
            return httpx.Response(200, json={"device_code": "secret-device", "user_code": "ABCD-EFGH",
                "verification_uri": "https://microsoft.com/devicelogin", "expires_in": 900, "interval": 5})
        return httpx.Response(400, json={"error": failure, "error_description": "secret-details"})
    path = tmp_path / "credentials.json"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(mail_auth.MailAuthError) as error:
            mail_auth.authorize_outlook("test-client", path, client=client, announce=lambda _: None)
    assert not path.exists() and "secret-" not in str(error.value)


def test_device_flow_expires_locally_without_extra_poll(tmp_path, monkeypatch):
    from auto_app.ingest import mail_auth
    delays = fake_time(monkeypatch, mail_auth)
    def handler(request):
        if request.url.path.endswith("/devicecode"):
            return httpx.Response(200, json={"device_code": "secret-device", "user_code": "ABCD-EFGH",
                "verification_uri": "https://microsoft.com/devicelogin", "expires_in": 7, "interval": 5})
        return httpx.Response(400, json={"error": "authorization_pending"})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(mail_auth.MailAuthError, match="expired"):
            mail_auth.authorize_outlook("test-client", tmp_path / "outlook.json", client=client, announce=lambda _: None)
    assert delays == [5, 2]


def test_auth_cli_keeps_failure_output_redacted_and_requires_explicit_paths(tmp_path, monkeypatch):
    from auto_app.ingest import auth_cli, mail_auth
    app = typer.Typer()
    auth_cli.register(app)
    def reject(*args, **kwargs):
        raise mail_auth.MailAuthError("authorization denied; run ingest auth gmail again")
    monkeypatch.setattr(auth_cli, "authorize_gmail", reject)
    result = CliRunner().invoke(app, ["auth", "gmail", "--client", str(tmp_path / "client.json"),
                                    "--credentials", str(tmp_path / "tokens.json"), "--no-browser", "--port", "8088"])
    assert result.exit_code == 1 and "authorization denied" in result.output
    missing = CliRunner().invoke(app, ["auth", "outlook", "--client-id", "test-client"])
    assert missing.exit_code == 2 and "credentials" in missing.output

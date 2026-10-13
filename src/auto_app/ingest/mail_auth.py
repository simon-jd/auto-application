"""Read-only mailbox OAuth without storing or displaying tokens in the job library."""

import base64
from contextlib import nullcontext
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urlparse
import uuid
import webbrowser

import httpx

GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
OUTLOOK_SCOPE = "offline_access https://graph.microsoft.com/Mail.Read"
GMAIL_TOKEN_URL = "https://oauth2.googleapis.com/token"


class MailAuthError(ValueError):
    """An actionable OAuth error that never contains response bodies or tokens."""


def _read_json(path: Path, *, purpose: str) -> dict:
    try:
        data = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise MailAuthError(f"Cannot read {purpose} JSON at {path}; run ingest auth to configure mailbox access.") from None
    if not isinstance(data, dict):
        raise MailAuthError(f"Invalid {purpose} JSON; expected an object.")
    return data


def _save(path: Path, data: dict) -> None:
    """Replace the token file only after the complete refresh succeeded."""
    path = path.expanduser()
    temporary = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(data, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except OSError:
        raise MailAuthError(f"Cannot save mailbox credentials at {path}; check its directory permissions.") from None
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _client(client):
    return nullcontext(client) if client is not None else httpx.Client(timeout=30, follow_redirects=False)


def _post(client, url: str, data: dict) -> tuple[int, dict]:
    try:
        response = client.post(url, data=data)
    except httpx.HTTPError:
        raise MailAuthError("OAuth network request failed; check connectivity and try again.") from None
    try:
        payload = response.json()
    except (ValueError, UnicodeError):
        raise MailAuthError(f"OAuth service returned an unreadable response (HTTP {response.status_code}); try again.") from None
    if not isinstance(payload, dict):
        raise MailAuthError("OAuth service returned an invalid response; try again.")
    return response.status_code, payload


def _oauth_error(payload: dict, status: int, provider: str) -> MailAuthError:
    # Provider error descriptions can echo submitted credentials. Only map known codes.
    code = payload.get("error")
    if code in ("invalid_grant", "expired_token"):
        reason = "authorization expired or was revoked"
    elif code in ("access_denied", "authorization_declined"):
        reason = "authorization denied"
    elif code in ("invalid_client", "unauthorized_client"):
        reason = "OAuth client configuration was rejected"
    elif code == "bad_verification_code":
        reason = "device authorization code was rejected"
    elif code in ("invalid_scope", "consent_required"):
        reason = "mail read permission was not granted"
    else:
        reason = f"OAuth request failed (HTTP {status})"
    return MailAuthError(f"{reason}; check the app registration and run ingest auth {provider} again.")


def _positive_number(value, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise MailAuthError(f"OAuth response has invalid {label}; try authorization again.") from None
    if isinstance(value, bool) or not math.isfinite(number) or number <= 0:
        raise MailAuthError(f"OAuth response has invalid {label}; try authorization again.")
    return number


def _tokens(payload: dict, previous: dict) -> dict:
    token = payload.get("access_token")
    refresh = payload.get("refresh_token") or previous.get("refresh_token")
    if not isinstance(token, str) or not token or not isinstance(refresh, str) or not refresh:
        raise MailAuthError("OAuth did not return offline access credentials; run ingest auth again and grant consent.")
    expiry = _positive_number(payload.get("expires_in"), "token lifetime")
    return {**previous, "access_token": token, "refresh_token": refresh, "expires_at": time.time() + expiry}


def _outlook_base(tenant: str) -> str:
    if not isinstance(tenant, str) or not re.fullmatch(r"[A-Za-z0-9.-]+", tenant):
        raise MailAuthError("Invalid Outlook tenant; use common, consumers, organizations or your tenant ID.")
    return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0"


def access_token(credentials_path: Path, *, provider: str, client=None) -> str:
    """Return a cached token or refresh it, persisting rotated refresh tokens."""
    if provider not in ("gmail", "outlook"):
        raise MailAuthError("Unsupported mailbox provider; use gmail or outlook.")
    credentials = _read_json(credentials_path, purpose="mailbox credentials")
    if credentials.get("provider") != provider:
        raise MailAuthError(f"Credential provider does not match {provider}; select the correct credential file.")
    try:
        expires = float(credentials.get("expires_at", 0))
    except (ValueError, TypeError):
        expires = 0
    token = credentials.get("access_token")
    if isinstance(token, str) and token and math.isfinite(expires) and expires > time.time() + 60:
        return token
    client_id, refresh = credentials.get("client_id"), credentials.get("refresh_token")
    if not isinstance(client_id, str) or not client_id or not isinstance(refresh, str) or not refresh:
        raise MailAuthError(f"Missing offline credentials; run ingest auth {provider} again.")
    form = {"client_id": client_id, "grant_type": "refresh_token", "refresh_token": refresh}
    if provider == "gmail":
        endpoint = GMAIL_TOKEN_URL
        if credentials.get("client_secret"):
            form["client_secret"] = credentials["client_secret"]
    else:
        endpoint = _outlook_base(credentials.get("tenant", "common")) + "/token"
        form["scope"] = OUTLOOK_SCOPE
    with _client(client) as session:
        status, payload = _post(session, endpoint, form)
    if status != 200 or payload.get("error"):
        raise _oauth_error(payload, status, provider)
    updated = _tokens(payload, credentials)
    _save(credentials_path, updated)
    return updated["access_token"]


def _callback_code(path: str, expected_state: str) -> str:
    values = parse_qs(urlparse(path).query)
    states = values.get("state", [])
    if len(states) != 1 or not secrets.compare_digest(states[0].encode(), expected_state.encode()):
        raise MailAuthError("OAuth callback state did not match; run ingest auth gmail again.")
    if values.get("error"):
        raise MailAuthError("Gmail authorization denied; run ingest auth gmail again when ready.")
    codes = values.get("code", [])
    if len(codes) != 1 or not codes[0]:
        raise MailAuthError("Gmail callback did not contain an authorization code; try again.")
    return codes[0]


def _callback_handler(state: str, result: dict):
    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if urlparse(self.path).path != "/":
                self.send_error(404)
                return
            try:
                result["code"] = _callback_code(self.path, state)
                status, message = 200, b"Mailbox authorization received. Return to the terminal."
            except MailAuthError as exc:
                result["error"] = exc
                status, message = 400, b"Mailbox authorization failed. Return to the terminal and try again."
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            self.wfile.write(message)

        def log_message(self, format, *args):
            # BaseHTTPRequestHandler logs callback URLs, including authorization codes.
            pass

    return CallbackHandler


def authorize_gmail(client_path: Path, credentials_path: Path, *, no_browser: bool = False,
                    port: int = 0, client=None, announce=print, timeout: float = 300) -> Path:
    """Authorize a Google Desktop app using PKCE and a temporary loopback listener."""
    config = _read_json(client_path, purpose="Google Desktop client").get("installed")
    if not isinstance(config, dict) or not isinstance(config.get("client_id"), str) or not config["client_id"]:
        raise MailAuthError("Use a downloaded Google OAuth client JSON with application type Desktop app.")
    if not 0 <= port <= 65535:
        raise MailAuthError("Loopback port must be between 0 and 65535.")
    verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    result = {}
    try:
        server = HTTPServer(("127.0.0.1", port), _callback_handler(state, result))
    except OSError:
        raise MailAuthError("Cannot start the local OAuth callback listener; choose another --port.") from None
    with server:
        redirect_uri = f"http://127.0.0.1:{server.server_address[1]}/"
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": config["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
            "scope": GMAIL_SCOPE, "access_type": "offline", "prompt": "consent",
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        })
        announce("Open this URL in a browser on this computer to authorize Gmail read-only access:")
        announce(url)
        if no_browser:
            announce("For SSH, forward the chosen --port to this computer before opening the URL.")
        else:
            try:
                webbrowser.open(url)
            except webbrowser.Error:
                announce("The browser could not be opened; open the URL above manually.")
        deadline = time.monotonic() + timeout
        while not result:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MailAuthError("Gmail authorization timed out; run ingest auth gmail again.")
            server.timeout = min(1, remaining)
            server.handle_request()
    if result.get("error"):
        raise result["error"]
    form = {"client_id": config["client_id"], "code": result["code"], "code_verifier": verifier,
            "grant_type": "authorization_code", "redirect_uri": redirect_uri}
    if config.get("client_secret"):
        form["client_secret"] = config["client_secret"]
    with _client(client) as session:
        status, payload = _post(session, GMAIL_TOKEN_URL, form)
    if status != 200 or payload.get("error"):
        raise _oauth_error(payload, status, "gmail")
    credentials = {"provider": "gmail", "client_id": config["client_id"], "account_key": str(uuid.uuid4())}
    if config.get("client_secret"):
        credentials["client_secret"] = config["client_secret"]
    _save(credentials_path, _tokens(payload, credentials))
    return credentials_path.expanduser()


def authorize_outlook(client_id: str, credentials_path: Path, *, tenant: str = "common",
                      client=None, announce=print) -> Path:
    """Authorize Microsoft public-client mail access from any browser using a device code."""
    if not client_id.strip():
        raise MailAuthError("Outlook authorization requires the registered application's client ID.")
    endpoint = _outlook_base(tenant)
    with _client(client) as session:
        status, device = _post(session, endpoint + "/devicecode", {"client_id": client_id, "scope": OUTLOOK_SCOPE})
        if status != 200 or device.get("error"):
            raise _oauth_error(device, status, "outlook")
        if not all(isinstance(device.get(key), str) and device[key] for key in ("device_code", "user_code", "verification_uri")):
            raise MailAuthError("Outlook returned an incomplete device authorization response; try again.")
        interval = _positive_number(device.get("interval", 5), "poll interval")
        deadline = time.monotonic() + _positive_number(device.get("expires_in"), "device lifetime")
        announce(f"Open {device['verification_uri']} and enter code: {device['user_code']}")
        announce("Authorize read-only mail access; this terminal will wait for completion.")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MailAuthError("Outlook device authorization expired; run ingest auth outlook again.")
            time.sleep(min(interval, remaining))
            if time.monotonic() >= deadline:
                raise MailAuthError("Outlook device authorization expired; run ingest auth outlook again.")
            status, payload = _post(session, endpoint + "/token", {
                "client_id": client_id, "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device["device_code"],
            })
            if status == 200 and not payload.get("error"):
                break
            if payload.get("error") == "authorization_pending":
                continue
            if payload.get("error") == "slow_down":
                interval += 5
                continue
            raise _oauth_error(payload, status, "outlook")
    credentials = {"provider": "outlook", "client_id": client_id, "tenant": tenant,
                   "account_key": str(uuid.uuid4())}
    _save(credentials_path, _tokens(payload, credentials))
    return credentials_path.expanduser()

"""Bearer-protected second MCP listener; public traffic arrives through a tunnel."""

import hmac
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings

SCOPE = "jobs"


def _validate_token(token: str) -> None:
    if not isinstance(token, str) or not 32 <= len(token) <= 4096 or not token.isascii() or any(
        not 33 <= ord(c) <= 126 for c in token
    ):
        raise ValueError("the MCP token must be 32–4096 printable ASCII characters without spaces")


def load_token(path: Path) -> str:
    """Read the same regular file whose permissions were checked; never echo its contents."""
    try:
        fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("the MCP token file must be a regular private file (chmod 600)")
            raw = stream.read(4098)
        token = raw.decode("ascii").strip()
    except (OSError, UnicodeError):
        raise ValueError("Cannot read the private MCP token file") from None
    _validate_token(token)
    return token


class StaticTokenVerifier:
    def __init__(self, token: str):
        _validate_token(token)
        self._token = token.encode()

    async def verify_token(self, token: str) -> AccessToken | None:
        if hmac.compare_digest(token.encode(), self._token):
            return AccessToken(token=token, client_id="owner", scopes=[SCOPE])
        return None


@dataclass(frozen=True)
class PublicAuth:
    token: str = field(repr=False)
    public_url: str

    def __post_init__(self):
        _validate_token(self.token)
        try:
            u = urlsplit(self.public_url)
            valid = (u.scheme == "https" and u.hostname and u.path in ("", "/") and not u.query
                     and not u.fragment and u.username is None and u.password is None
                     and not any(c.isspace() or ord(c) < 33 for c in self.public_url)
                     and re.fullmatch(r"[A-Za-z0-9.-]+", u.hostname)
                     and all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
                             for label in u.hostname.split("."))
                     and (u.port is None or 1 <= u.port <= 65535))
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ValueError("--public-url must be https://<host> with an optional port and no path or credentials")
        object.__setattr__(self, "public_url", f"https://{u.netloc.lower()}")

    @property
    def host(self) -> str:
        return urlsplit(self.public_url).netloc

    @property
    def settings(self) -> AuthSettings:
        return AuthSettings(issuer_url=self.public_url, resource_server_url=f"{self.public_url}/mcp",
                            required_scopes=[SCOPE], validate_token_resource=False)

    def verifier(self) -> StaticTokenVerifier:
        return StaticTokenVerifier(self.token)

    def transport_security(self, port: int) -> TransportSecuritySettings:
        return TransportSecuritySettings(
            allowed_hosts=[self.host, f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"],
            allowed_origins=[self.public_url])

"""Installed-app OAuth for Gmail.

Google requires a Desktop OAuth client from the user's own Cloud project.
``login`` opens the consent page, catches the loopback redirect, and stores
a refresh token in the data directory. Later commands refresh the access
token on their own.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# Mail plus Calendar (list calendars, view/create/edit/delete events).
# Existing accounts must run ``gmail-tui login`` again after this expands.
SCOPES = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
)
SCOPE = " ".join(SCOPES)
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

_FILE = "oauth.json"


class OAuthError(Exception):
    pass


@dataclass(frozen=True)
class StoredAuth:
    client_id: str
    client_secret: str
    refresh_token: str
    access_token: str
    expires_at: float


@dataclass(frozen=True)
class Token:
    access_token: str
    refresh_token: str
    expires_at: float


class PendingLogin:
    """Local redirect listener for one authorization code."""

    def __init__(
        self,
        url: str,
        redirect_uri: str,
        verifier: str,
        state: str,
        server: HTTPServer,
        holder: _Callback,
    ) -> None:
        self.url = url
        self.redirect_uri = redirect_uri
        self.verifier = verifier
        self.state = state
        self._server = server
        self._holder = holder

    @classmethod
    def start(cls, client_id: str) -> PendingLogin:
        holder = _Callback()
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(32)
        server = HTTPServer(("127.0.0.1", 0), _handler(state, holder))
        port = server.server_address[1]
        redirect_uri = f"http://127.0.0.1:{port}"
        query = urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": SCOPE,
                "code_challenge": _challenge(verifier),
                "code_challenge_method": "S256",
                "state": state,
                "access_type": "offline",
                "prompt": "consent",
            }
        )
        return cls(f"{AUTH_URL}?{query}", redirect_uri, verifier, state, server, holder)

    def wait(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline and not self._holder.code and not self._holder.error:
                self._server.timeout = max(0.1, deadline - time.monotonic())
                self._server.handle_request()
        finally:
            self.close()
        if self._holder.error:
            raise OAuthError(self._holder.error)
        if not self._holder.code:
            raise OAuthError("timed out waiting for Google. Open the link and approve access")
        return self._holder.code

    def close(self) -> None:
        self._server.server_close()


def read_client_file(path: Path) -> tuple[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OAuthError(f"cannot read {path}: {exc}") from exc
    block = data.get("installed") or data.get("web") or data
    if not isinstance(block, dict):
        raise OAuthError(f"{path} has no client_id and client_secret")
    client_id = str(block.get("client_id") or "")
    client_secret = str(block.get("client_secret") or "")
    if not client_id or not client_secret:
        raise OAuthError(f"{path} has no client_id and client_secret")
    return client_id, client_secret


def load(root: Path) -> StoredAuth | None:
    path = root / _FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OAuthError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise OAuthError(f"cannot read {path}: expected an object")
    return StoredAuth(
        client_id=str(data.get("client_id") or ""),
        client_secret=str(data.get("client_secret") or ""),
        refresh_token=str(data.get("refresh_token") or ""),
        access_token=str(data.get("access_token") or ""),
        expires_at=float(data.get("expires_at") or 0),
    )


def store_tokens(root: Path, client_id: str, client_secret: str, token: Token) -> None:
    previous = load(root)
    refresh = token.refresh_token or (previous.refresh_token if previous else "")
    _write(
        root,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh,
            "access_token": token.access_token,
            "expires_at": token.expires_at,
        },
    )


def exchange_code(
    *,
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    verifier: str,
    token_url: str = TOKEN_URL,
    timeout: float = 60,
) -> Token:
    return _post_token(
        token_url,
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        },
        timeout,
    )


def refresh_access_token(stored: StoredAuth, *, token_url: str = TOKEN_URL, timeout: float = 60) -> Token:
    token = _post_token(
        token_url,
        {
            "grant_type": "refresh_token",
            "refresh_token": stored.refresh_token,
            "client_id": stored.client_id,
            "client_secret": stored.client_secret,
        },
        timeout,
    )
    if not token.refresh_token:
        return Token(token.access_token, stored.refresh_token, token.expires_at)
    return token


def current_access_token(
    root: Path,
    *,
    token_url: str = TOKEN_URL,
    skew: float = 60,
    force: bool = False,
    allow_expired: bool = False,
) -> str | None:
    stored = load(root)
    if stored is None:
        return None
    if (
        not force
        and stored.access_token
        and (allow_expired or stored.expires_at > time.time() + skew)
    ):
        return stored.access_token
    if not stored.refresh_token or not stored.client_id or not stored.client_secret:
        return stored.access_token or None
    token = refresh_access_token(stored, token_url=token_url)
    store_tokens(root, stored.client_id, stored.client_secret, token)
    return token.access_token


def _post_token(url: str, form: dict[str, str], timeout: float) -> Token:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(form).encode(),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise OAuthError(_error_text(exc)) from exc
    except urllib.error.URLError as exc:
        raise OAuthError(f"token request failed: {exc.reason}") from exc
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise OAuthError("token response had no access_token")
    expires_in = int(payload.get("expires_in") or 3600)
    return Token(
        access_token=str(payload["access_token"]),
        refresh_token=str(payload.get("refresh_token") or ""),
        expires_at=time.time() + expires_in,
    )


def _error_text(exc: urllib.error.HTTPError) -> str:
    detail = exc.read().decode("utf-8", errors="replace")
    try:
        parsed = json.loads(detail)
    except json.JSONDecodeError:
        return detail[:300] or f"token HTTP {exc.code}"
    if isinstance(parsed, dict):
        message = parsed.get("error_description") or parsed.get("error")
        if message:
            return str(message)
    return detail[:300] or f"token HTTP {exc.code}"


def _write(root: Path, payload: dict[str, object]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    path = root / _FILE
    temporary = path.with_suffix(".json.tmp")
    encoded = (json.dumps(payload, indent=2) + "\n").encode()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, encoded)
    finally:
        os.close(fd)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class _Callback:
    def __init__(self) -> None:
        self.code: str | None = None
        self.error: str | None = None


def _handler(expected_state: str, holder: _Callback) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path not in ("", "/"):
                self._send(404, "Not found")
                return
            query = urllib.parse.parse_qs(parsed.query)
            state = (query.get("state") or [""])[0]
            error = (query.get("error") or [""])[0]
            code = (query.get("code") or [""])[0]
            if not state and not code and not error:
                self._send(404, "Not found")
                return
            if state != expected_state:
                holder.error = "OAuth state did not match. Run gmail-tui login again"
                self._send(400, "Sign-in failed. Return to the terminal.")
                return
            if error:
                holder.error = "Google denied access" if error == "access_denied" else error
                self._send(400, "Sign-in was cancelled. Return to the terminal.")
                return
            if not code:
                self._send(404, "Not found")
                return
            holder.code = code
            self._send(200, "Signed in. You can close this tab and return to the terminal.")

        def log_message(self, fmt: str, *args: object) -> None:
            return

        def _send(self, status: int, message: str) -> None:
            body = (
                "<!DOCTYPE html><meta charset=\"utf-8\"><title>gmail-tui</title>"
                f"<p>{message}</p>"
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler

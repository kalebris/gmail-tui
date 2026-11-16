"""Loopback OAuth: consent link, redirect, and stored refresh token."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from gmail_tui.oauth import (
    OAuthError,
    PendingLogin,
    Token,
    current_access_token,
    exchange_code,
    load,
    read_client_file,
    store_tokens,
)


class OAuthTests(unittest.TestCase):
    def test_client_json_reads_installed_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "client.json"
            path.write_text(
                json.dumps({"installed": {"client_id": "id.apps.googleusercontent.com", "client_secret": "secret"}}),
                encoding="utf-8",
            )
            self.assertEqual(read_client_file(path), ("id.apps.googleusercontent.com", "secret"))

    def test_login_link_catches_the_redirect_and_stores_a_refresh_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tokens = _TokenServer()
            pending = PendingLogin.start("client-id")
            query = urllib.parse.parse_qs(urllib.parse.urlparse(pending.url).query)
            self.assertEqual(query["client_id"], ["client-id"])
            self.assertEqual(
                query["scope"],
                [
                    "https://www.googleapis.com/auth/gmail.modify "
                    "https://www.googleapis.com/auth/calendar"
                ],
            )
            self.assertEqual(query["code_challenge_method"], ["S256"])
            self.assertEqual(query["access_type"], ["offline"])
            self.assertEqual(query["prompt"], ["consent"])
            self.assertEqual(query["redirect_uri"], [pending.redirect_uri])
            box: dict[str, object] = {}

            def wait() -> None:
                try:
                    box["code"] = pending.wait(3)
                except Exception as exc:  # pragma: no cover - assertion below
                    box["error"] = exc

            thread = threading.Thread(target=wait)
            thread.start()
            _wait_until_listening(pending.redirect_uri + "/")
            redirect = pending.redirect_uri + "/?" + urllib.parse.urlencode(
                {"code": "auth-code", "state": pending.state}
            )
            page = _fetch(redirect).decode()
            self.assertIn("Signed in", page)
            thread.join(3)
            self.assertEqual(box.get("code"), "auth-code")
            token = exchange_code(
                code="auth-code",
                client_id="client-id",
                client_secret="client-secret",
                redirect_uri=pending.redirect_uri,
                verifier=pending.verifier,
                token_url=tokens.url,
            )
            store_tokens(root, "client-id", "client-secret", token)
            mode = stat.S_IMODE((root / "oauth.json").stat().st_mode)
            self.assertEqual(mode, 0o600)
            self.assertEqual(current_access_token(root, token_url=tokens.url), "ya29.one")
            self.assertEqual(tokens.calls, 1)
            stored = load(root)
            assert stored is not None
            expired = Token(stored.access_token, stored.refresh_token, 0)
            store_tokens(root, stored.client_id, stored.client_secret, expired)
            self.assertEqual(current_access_token(root, token_url=tokens.url), "ya29.two")
            self.assertEqual(tokens.calls, 2)
            # Still unexpired, but force asks Google again after a bad bearer.
            self.assertEqual(current_access_token(root, token_url=tokens.url), "ya29.two")
            self.assertEqual(tokens.calls, 2)
            self.assertEqual(
                current_access_token(root, token_url=tokens.url, force=True),
                "ya29.three",
            )
            self.assertEqual(tokens.calls, 3)
            body = urllib.parse.parse_qs(tokens.bodies[0])
            self.assertEqual(body["grant_type"], ["authorization_code"])
            self.assertEqual(body["code"], ["auth-code"])
            self.assertEqual(body["code_verifier"], [pending.verifier])
            refresh = urllib.parse.parse_qs(tokens.bodies[1])
            self.assertEqual(refresh["grant_type"], ["refresh_token"])
            self.assertEqual(refresh["refresh_token"], ["refresh-token"])
            tokens.stop()

    def test_wrong_state_is_rejected(self) -> None:
        pending = PendingLogin.start("client-id")
        box: dict[str, object] = {}

        def wait() -> None:
            try:
                pending.wait(3)
            except Exception as exc:
                box["error"] = exc

        thread = threading.Thread(target=wait)
        thread.start()
        _wait_until_listening(pending.redirect_uri + "/")
        _fetch(pending.redirect_uri + "/?" + urllib.parse.urlencode({"code": "stolen", "state": "nope"}))
        thread.join(3)
        self.assertIsInstance(box.get("error"), OAuthError)
        self.assertIn("state", str(box.get("error")))

    def test_login_without_a_client_explains_the_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            for name in ("GMAIL_TUI_CLIENT_ID", "GMAIL_TUI_CLIENT_SECRET", "GMAIL_TUI_TOKEN"):
                env.pop(name, None)
            result = subprocess.run(
                [sys.executable, "-m", "gmail_tui", "--data", str(Path(tmp) / "mail"), "login", "--no-browser"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("client-json", result.stderr)
            self.assertIn("Desktop", result.stderr)


def _wait_until_listening(url: str) -> None:
    deadline = time.time() + 2
    while time.time() < deadline:
        try:
            _fetch(url)
            return
        except OSError:
            time.sleep(0.02)
    raise AssertionError("login redirect did not start listening")


def _fetch(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        exc.close()
        return body


class _TokenServer:
    def __init__(self) -> None:
        handler = self._handler()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_address[1]}/token"
        self.calls = 0
        self.bodies: list[str] = []
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(2)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                owner.bodies.append(self.rfile.read(length).decode())
                owner.calls += 1
                access = {1: "ya29.one", 2: "ya29.two"}.get(owner.calls, "ya29.three")
                refresh = "refresh-token" if owner.calls == 1 else ""
                payload = json.dumps(
                    {"access_token": access, "refresh_token": refresh, "expires_in": 3600, "token_type": "Bearer"}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, fmt: str, *args: object) -> None:
                return

        return Handler


if __name__ == "__main__":
    unittest.main()

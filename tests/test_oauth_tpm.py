"""TPM-sealed oauth.json.age via age + age-plugin-tpm (mocked tools)."""

from __future__ import annotations

import json
import os
import select
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from gmail_tui import oauth, oauth_tpm
from gmail_tui.oauth import OAuthError, Token, current_access_token, load, store_tokens
from gmail_tui.settings import parse


class OAuthTpmTests(unittest.TestCase):
    def tearDown(self) -> None:
        oauth_tpm.reset()
        oauth.clear_cache()

    def test_settings_parse_oauth_tpm(self) -> None:
        settings = parse("oauth-tpm yes\noauth-tpm-identity /tmp/id\n")
        self.assertTrue(settings.oauth_tpm)
        self.assertEqual(settings.oauth_tpm_identity, Path("/tmp/id"))
        self.assertFalse(parse("oauth-tpm no\n").oauth_tpm)

    def test_store_and_load_use_age_file_not_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "account"
            identity = Path(tmp) / "oauth-tpm.identity"
            identity.write_text(
                "# Recipient: age1tag1testrecipient\nAGE-PLUGIN-TPM-1TEST\n",
                encoding="utf-8",
            )
            sealed: dict[str, bytes] = {}

            def fake_encrypt(payload: bytes, *, dest: Path) -> None:
                sealed["payload"] = payload
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"age-ciphertext:" + payload)
                os.chmod(dest, 0o600)

            def fake_decrypt(path: Path) -> bytes:
                raw = path.read_bytes()
                self.assertTrue(raw.startswith(b"age-ciphertext:"))
                return raw[len(b"age-ciphertext:") :]

            oauth_tpm.configure(enabled=True, identity=identity, pin="1234")
            with mock.patch.object(oauth_tpm, "encrypt_bytes", side_effect=fake_encrypt):
                with mock.patch.object(oauth_tpm, "decrypt_bytes", side_effect=fake_decrypt):
                    with mock.patch.object(
                        oauth_tpm, "recipient_from_identity", return_value="age1tag1testrecipient"
                    ):
                        store_tokens(
                            root,
                            "client-id",
                            "client-secret",
                            Token("ya29.access", "refresh-token", 9_999_999_999),
                        )
                        self.assertTrue((root / "oauth.json.age").exists())
                        self.assertFalse((root / "oauth.json").exists())
                        mode = stat.S_IMODE((root / "oauth.json.age").stat().st_mode)
                        self.assertEqual(mode, 0o600)
                        oauth.clear_cache()
                        stored = load(root)
            assert stored is not None
            self.assertEqual(stored.refresh_token, "refresh-token")
            self.assertEqual(stored.client_id, "client-id")
            self.assertIn(b"refresh-token", sealed["payload"])

    def test_plaintext_migrates_on_first_tpm_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "account"
            root.mkdir()
            plain = {
                "client_id": "id",
                "client_secret": "secret",
                "refresh_token": "refresh",
                "access_token": "ya29",
                "expires_at": 9_999_999_999,
            }
            (root / "oauth.json").write_text(json.dumps(plain), encoding="utf-8")
            identity = Path(tmp) / "id"
            identity.write_text("# Recipient: age1tag1x\nAGE-PLUGIN-TPM-1X\n", encoding="utf-8")

            def fake_encrypt(payload: bytes, *, dest: Path) -> None:
                dest.write_bytes(b"AGE:" + payload)
                os.chmod(dest, 0o600)

            def fake_decrypt(path: Path) -> bytes:
                return path.read_bytes()[4:]

            oauth_tpm.configure(enabled=True, identity=identity, pin="pin")
            with mock.patch.object(oauth_tpm, "encrypt_bytes", side_effect=fake_encrypt):
                with mock.patch.object(oauth_tpm, "decrypt_bytes", side_effect=fake_decrypt):
                    stored = load(root)
            assert stored is not None
            self.assertEqual(stored.refresh_token, "refresh")
            self.assertTrue((root / "oauth.json.age").exists())
            self.assertFalse((root / "oauth.json").exists())

    def test_token_refresh_rewrites_age_without_reprompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity = Path(tmp) / "id"
            identity.write_text("# Recipient: age1tag1x\nAGE-PLUGIN-TPM-1X\n", encoding="utf-8")
            writes = {"n": 0}

            def fake_encrypt(payload: bytes, *, dest: Path) -> None:
                writes["n"] += 1
                dest.write_bytes(payload)
                os.chmod(dest, 0o600)

            def fake_decrypt(path: Path) -> bytes:
                return path.read_bytes()

            oauth_tpm.configure(enabled=True, identity=identity, pin="pin")
            with mock.patch.object(oauth_tpm, "encrypt_bytes", side_effect=fake_encrypt):
                with mock.patch.object(oauth_tpm, "decrypt_bytes", side_effect=fake_decrypt):
                    store_tokens(
                        root,
                        "id",
                        "secret",
                        Token("ya29.old", "refresh", 0),
                    )
                    # Expired access token → refresh path calls store_tokens again.
                    with mock.patch(
                        "gmail_tui.oauth.refresh_access_token",
                        return_value=Token("ya29.one", "refresh", 9_999_999_999),
                    ):
                        access = current_access_token(root)
            self.assertEqual(access, "ya29.one")
            self.assertGreaterEqual(writes["n"], 2)

    def test_wrong_pin_surfaces_as_oauth_error(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "oauth.json.age"
            path.write_bytes(b"ciphertext")
            identity = Path(tmp) / "id"
            identity.write_text("# Recipient: age1tag1x\nAGE-PLUGIN-TPM-1X\n", encoding="utf-8")
            oauth_tpm.configure(enabled=True, identity=identity, pin="bad")

            with mock.patch.object(oauth_tpm.shutil, "which", return_value="/bin/true"):
                with mock.patch(
                    "gmail_tui.oauth_tpm.subprocess.run",
                    side_effect=subprocess.CalledProcessError(
                        1, ["age"], stderr=b"TPM_RC_AUTH_FAIL"
                    ),
                ):
                    with self.assertRaises(OAuthError) as caught:
                        oauth_tpm.decrypt_bytes(path)
            self.assertIn("PIN", str(caught.exception))

    def test_pin_entry_shows_a_star_per_character(self) -> None:
        import pty

        master, slave = pty.openpty()
        found: dict[str, str] = {}

        def run() -> None:
            found["pin"] = oauth_tpm._read_pin("PIN: ", fd=slave)
            found["again"] = oauth_tpm._read_pin("Again: ", fd=slave)

        thread = threading.Thread(target=run)
        thread.start()
        try:
            shown = _read_until(master, b"PIN: ")
            os.write(master, b"ab\x7f12\nsecond\n")
            shown += _read_until(master, b"Again: ")
            thread.join(2)
            self.assertFalse(thread.is_alive())
            while True:
                ready, _, _ = select.select([master], [], [], 0.1)
                if not ready:
                    break
                shown += os.read(master, 1024)
        finally:
            os.close(master)
            os.close(slave)
        self.assertEqual(found["pin"], "a12")
        self.assertEqual(found["again"], "second")
        self.assertIn(b"**\x08 \x08**", shown)
        self.assertIn(b"******", shown)
        self.assertNotIn(b"a12", shown)
        self.assertNotIn(b"second", shown)


def _read_until(fd: int, marker: bytes) -> bytes:
    buf = b""
    deadline = time.monotonic() + 2
    while marker not in buf:
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {marker!r} in {buf!r}")
        ready, _, _ = select.select([fd], [], [], 0.1)
        if ready:
            buf += os.read(fd, 1024)
    return buf


if __name__ == "__main__":
    unittest.main()

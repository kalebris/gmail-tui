"""TPM-backed OAuth token files via age + age-plugin-tpm.

When ``oauth-tpm yes`` is set in the config, each account's tokens are stored
as ``oauth.json.age`` encrypted to a TPM-bound age recipient. Decrypting asks
for the TPM PIN once per process (or reads ``AGE_TPM_PIN``). Encrypting only
needs the public recipient, so token refreshes do not re-prompt.
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import sys
from pathlib import Path

from gmail_tui.oauth import OAuthError

_FILE = "oauth.json"
_AGE_FILE = "oauth.json.age"
_DEFAULT_IDENTITY_NAME = "oauth-tpm.identity"

# Process-wide policy set from the user config.
_enabled = False
_identity: Path | None = None
_pin: str | None = None
_recipient: str | None = None


def encrypted_path(root: Path) -> Path:
    return root / _AGE_FILE


def plaintext_path(root: Path) -> Path:
    return root / _FILE


def default_identity_path(config_file: Path | None = None) -> Path:
    if config_file is not None:
        return config_file.parent / _DEFAULT_IDENTITY_NAME
    from gmail_tui.settings import config_dir

    return config_dir() / _DEFAULT_IDENTITY_NAME


def enabled() -> bool:
    return _enabled


def configure(*, enabled: bool, identity: Path | None = None, pin: str | None = None) -> None:
    """Turn TPM storage on or off for this process."""
    global _enabled, _identity, _pin, _recipient
    _enabled = enabled
    _identity = identity
    _pin = pin
    _recipient = None


def reset() -> None:
    """Tests only: clear TPM policy."""
    configure(enabled=False)


def prepare(identity: Path, *, prompt: bool = True) -> None:
    """Enable TPM storage, creating the identity if needed, and keep the PIN."""
    pin = _pin
    if pin is None:
        pin = _resolve_pin(create=not identity.exists(), prompt=prompt)
    recipient = ensure_identity(identity, pin)
    configure(enabled=True, identity=identity, pin=pin)
    global _recipient
    _recipient = recipient


def ensure_identity(identity: Path, pin: str) -> str:
    """Return the age recipient for ``identity``, generating it when missing."""
    if identity.exists():
        return recipient_from_identity(identity)
    _require_tools()
    identity.parent.mkdir(parents=True, exist_ok=True)
    temporary = identity.with_suffix(identity.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()
    env = os.environ.copy()
    env["AGE_TPM_PIN"] = pin
    try:
        subprocess.run(
            ["age-plugin-tpm", "--generate", "-p", "-o", str(temporary)],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
    except FileNotFoundError as exc:
        raise OAuthError(
            "age-plugin-tpm not found on PATH (install age-plugin-tpm for oauth-tpm)"
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip() or str(exc)
        raise OAuthError(f"cannot create TPM age identity: {detail}") from exc
    os.chmod(temporary, 0o600)
    temporary.replace(identity)
    os.chmod(identity, 0o600)
    return recipient_from_identity(identity)


def recipient_from_identity(identity: Path) -> str:
    try:
        text = identity.read_text(encoding="utf-8")
    except OSError as exc:
        raise OAuthError(f"cannot read {identity}: {exc}") from exc
    for line in text.splitlines():
        if line.startswith("# Recipient:"):
            recipient = line.split(":", 1)[1].strip()
            if recipient:
                return recipient
    # Fall back to converting the identity blob.
    _require_tools()
    try:
        result = subprocess.run(
            ["age-plugin-tpm", "--convert", "-o", "-", str(identity)],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise OAuthError(
            "age-plugin-tpm not found on PATH (install age-plugin-tpm for oauth-tpm)"
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip() or str(exc)
        raise OAuthError(f"cannot read TPM recipient from {identity}: {detail}") from exc
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("age1"):
            return line
    raise OAuthError(f"{identity} has no age recipient")


def encrypt_bytes(payload: bytes, *, dest: Path) -> None:
    recipient = _recipient or (
        recipient_from_identity(_identity) if _identity is not None else ""
    )
    if not recipient:
        raise OAuthError("TPM oauth storage is not configured")
    _require_tools()
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_suffix(dest.suffix + ".tmp")
    try:
        subprocess.run(
            ["age", "-r", recipient, "-o", str(temporary)],
            input=payload,
            check=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise OAuthError("age not found on PATH (install age for oauth-tpm)") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", errors="replace").strip() or str(exc)
        raise OAuthError(f"cannot encrypt oauth tokens: {detail}") from exc
    os.chmod(temporary, 0o600)
    temporary.replace(dest)
    os.chmod(dest, 0o600)


def decrypt_bytes(path: Path) -> bytes:
    if _identity is None:
        raise OAuthError("TPM oauth storage is not configured")
    _require_tools()
    env = os.environ.copy()
    if _pin:
        env["AGE_TPM_PIN"] = _pin
    try:
        result = subprocess.run(
            ["age", "--decrypt", "-i", str(_identity), str(path)],
            check=True,
            capture_output=True,
            env=env,
        )
    except FileNotFoundError as exc:
        raise OAuthError("age not found on PATH (install age for oauth-tpm)") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", errors="replace").strip() or str(exc)
        if "AUTH_FAIL" in detail or "PIN" in detail.upper() or "auth" in detail.lower():
            raise OAuthError("wrong TPM PIN (or TPM refused the unwrap)") from exc
        raise OAuthError(f"cannot decrypt {path}: {detail}") from exc
    return result.stdout


def migrate_plaintext(root: Path, payload: bytes) -> None:
    """Write ``oauth.json.age`` and remove the plaintext file when present."""
    encrypt_bytes(payload, dest=encrypted_path(root))
    plain = plaintext_path(root)
    if plain.exists():
        try:
            plain.unlink()
        except OSError:
            pass


def has_tokens(root: Path) -> bool:
    return encrypted_path(root).exists() or plaintext_path(root).exists()


def unlock_roots(roots: list[Path]) -> None:
    """Decrypt every account's tokens once so later loads hit the cache."""
    from gmail_tui import oauth

    for root in roots:
        if not has_tokens(root):
            continue
        oauth.load(root)


def _resolve_pin(*, create: bool, prompt: bool) -> str:
    env = os.environ.get("AGE_TPM_PIN")
    if env is not None and env != "":
        return env
    if not prompt:
        raise OAuthError("TPM PIN required (set AGE_TPM_PIN)")
    if not sys.stdin.isatty():
        raise OAuthError("TPM PIN required (set AGE_TPM_PIN or run on a TTY)")
    label = "Choose a TPM PIN for oauth storage: " if create else "TPM PIN for oauth: "
    pin = getpass.getpass(label)
    if create:
        again = getpass.getpass("Confirm TPM PIN: ")
        if pin != again:
            raise OAuthError("PIN confirmation did not match")
    if not pin:
        raise OAuthError("empty TPM PIN")
    return pin


def _require_tools() -> None:
    missing = [name for name in ("age", "age-plugin-tpm") if shutil.which(name) is None]
    if missing:
        raise OAuthError(
            "oauth-tpm needs "
            + " and ".join(missing)
            + " on PATH (Nix: age + age-plugin-tpm)"
        )

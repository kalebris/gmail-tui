"""Account directories under one data root.

Every mailbox lives in ``<data>/accounts/<email>/`` (index, blobs, oauth).
Older installs that kept the first account in ``<data>/`` itself are moved
there automatically the next time accounts are discovered.
"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from gmail_tui.store import Store

_ACCOUNT_FILES = (
    "index.sqlite",
    "index.sqlite-wal",
    "index.sqlite-shm",
    "oauth.json",
)


@dataclass
class Account:
    name: str
    root: Path
    inbox: int = 0
    unread: int = 0
    local_messages: int = 0


def account_root(data: Path, email: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "@._-" else "_" for ch in email.strip()) or "account"
    path = Path(data) / "accounts" / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_account_dir(data: Path, email: str) -> Path:
    """Directory for ``email`` under ``data`` (always ``accounts/<email>/``)."""
    data = Path(data)
    migrate_legacy_root(data)
    wanted = email.strip().casefold()
    if not wanted:
        return account_root(data, "primary")
    for account in discover(data):
        if account.name.casefold() == wanted:
            return account.root
    return account_root(data, email)


def arrange(accounts: list[Account], order: Sequence[str]) -> list[Account]:
    """Put ``order`` first, left to right. Any other mailbox keeps its place after those."""
    if not order:
        return list(accounts)
    rank: dict[str, int] = {}
    for index, email in enumerate(order):
        rank.setdefault(email.casefold(), index)
    placed = sorted(
        enumerate(accounts),
        key=lambda item: (
            0 if item[1].name.casefold() in rank else 1,
            rank.get(item[1].name.casefold(), 0),
            item[0],
        ),
    )
    return [account for _index, account in placed]


def discover(data: Path) -> list[Account]:
    data = Path(data)
    migrate_legacy_root(data)
    roots: list[Path] = []
    extra = data / "accounts"
    if extra.is_dir():
        children = sorted((path for path in extra.iterdir() if path.is_dir()), key=lambda path: path.name)
        for child in children:
            if child.name.startswith((".", "_")):
                continue
            if _is_account(child):
                roots.append(child)
    if not roots:
        return []
    return _dedupe([_describe(root) for root in roots])


def cli_store_dir(data: Path, order: Sequence[str] = ()) -> Path:
    """Mailbox used by one-off commands (``list``, ``sync``, …).

    ``--data`` may point at the shared data root or directly at one
    ``accounts/<email>/`` folder.
    """
    data = Path(data)
    if data.parent.name == "accounts" and _is_account(data):
        return data
    accounts = arrange(discover(data), order)
    if accounts:
        return accounts[0].root
    # Fresh tree: keep CLI init/list working before the first login.
    return account_root(data, "primary")


def migrate_legacy_root(data: Path) -> Path | None:
    """Move a mailbox stored in ``data/`` itself into ``accounts/<email>/``."""
    data = Path(data)
    if not _is_account(data):
        return None
    store = Store.open(data)
    try:
        email = store.account_email().strip() or "primary"
        local = int(store.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
    finally:
        store.close()
    dest = account_root(data, email)
    if dest.resolve() == data.resolve():
        return None
    if _is_account(dest):
        existing = Store.open(dest)
        try:
            kept = int(existing.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        finally:
            existing.close()
        # Prefer the copy with more mail; park the other under _legacy_root.
        if local > kept:
            backup = data / "_legacy_root" / dest.name
            backup.mkdir(parents=True, exist_ok=True)
            _move_account_files(dest, backup)
            _move_account_files(data, dest)
        else:
            backup = data / "_legacy_root" / "data-root"
            backup.mkdir(parents=True, exist_ok=True)
            _move_account_files(data, backup)
        return dest
    _move_account_files(data, dest)
    return dest


def _move_account_files(source: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name in _ACCOUNT_FILES:
        src = source / name
        if src.exists() or src.is_symlink():
            target = dest / name
            if target.exists() or target.is_symlink():
                target.unlink()
            shutil.move(str(src), str(target))
    blobs = source / "blobs"
    if blobs.is_dir():
        target = dest / "blobs"
        if target.exists():
            for child in blobs.iterdir():
                shutil.move(str(child), str(target / child.name))
            blobs.rmdir()
        else:
            shutil.move(str(blobs), str(target))


def _is_account(root: Path) -> bool:
    return (root / "index.sqlite").exists() or (root / "oauth.json").exists()


def _describe(root: Path) -> Account:
    # Skip inbox_counts and COUNT(*) here: those scans are repeated when
    # MailSession opens, and large mailboxes made TUI startup wait twice.
    # File size is only used to prefer the richer copy when deduping tabs.
    store = Store.open(root)
    try:
        name = store.account_email() or root.name
        db = root / "index.sqlite"
        local = db.stat().st_size if db.is_file() else 0
        return Account(name, root, 0, 0, local)
    finally:
        store.close()


def _dedupe(accounts: list[Account]) -> list[Account]:
    """One tab per address. Prefer the copy with more local mail."""
    best: dict[str, Account] = {}
    order: list[str] = []
    for account in accounts:
        key = account.name.strip().casefold() or str(account.root)
        previous = best.get(key)
        if previous is None:
            best[key] = account
            order.append(key)
            continue
        if account.local_messages > previous.local_messages:
            best[key] = account
    return [best[key] for key in order]

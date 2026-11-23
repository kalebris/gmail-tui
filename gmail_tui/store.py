"""Private SQLite replica of Gmail metadata, plus the body-cache index.

The database is the only local authority for labels, threads, the history
cursor, and the push queue. Message bytes live under ``blobs/`` and can be
deleted and fetched again. There is no maildir and no notmuch database.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISREG
from typing import Iterator

from gmail_tui.gmail import HistoryRecord, IncomingMessage, Label
from gmail_tui.search import ParsedQuery
from gmail_tui.textutil import Attachment, MimePart, parse_rfc822, read_attachment

_SCHEMA = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
_GMAIL_ID = re.compile(r"[A-Za-z0-9_-]+")
# Leave a small WAL in place so the next write does not recreate the file,
# but never keep the hundreds of megabytes that stall every reader.
_JOURNAL_LIMIT = 8 * 1024 * 1024
_WAL_CHECKPOINT_BYTES = 4 * 1024 * 1024
_WRITE_BATCH = 25
# Parsed bodies kept so stepping through a burst does not reread each file.
_PARSE_CACHE_LIMIT = 64
# Folders whose bodies wait until that folder is open (or a message is).
# Overridden from config via ``configure_on_demand``.
_ON_DEMAND_LABELS: frozenset[str] = frozenset({"TRASH", "SPAM"})


def configure_on_demand(labels: tuple[str, ...] | list[str]) -> None:
    """Set which label ids defer background body downloads."""
    global _ON_DEMAND_LABELS
    _ON_DEMAND_LABELS = frozenset(str(label).strip() for label in labels if str(label).strip())


def on_demand_labels() -> frozenset[str]:
    return _ON_DEMAND_LABELS


def defers_body(
    labels: list[str] | set[str] | tuple[str, ...], open_label: str | None
) -> bool:
    """True when this message's body should wait for its on-demand folder."""
    return _defers_body(labels, open_label)


class MessageNotFound(Exception):
    """The local replica has no row for this Gmail message id."""


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.isolation_level = None
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL").fetchone()
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA journal_size_limit={_JOURNAL_LIMIT}")
    conn.execute("PRAGMA wal_autocheckpoint=1000")
    return conn


@dataclass(frozen=True)
class ThreadCursor:
    latest_internal_date_ms: int
    thread_id: int


@dataclass(frozen=True)
class MessageCursor:
    internal_date_ms: int
    message_id: int


@dataclass(frozen=True)
class MessageSummary:
    message_id: int
    gmail_id: str
    from_addr: str
    to_addr: str
    subject: str
    snippet: str
    internal_date_ms: int
    label_names: tuple[str, ...]
    has_attachment: bool
    unread: bool
    thread_id: int = 0
    thread_count: int = 1
    thread_role: str = ""
    expanded: bool = False
    row_key: str = ""
    member_ids: tuple[str, ...] = ()
    message_id_header: str = ""
    in_reply_to: str = ""
    references_header: str = ""


@dataclass(frozen=True)
class MessagePreview:
    gmail_id: str
    from_addr: str
    to_addr: str
    cc_addr: str
    subject: str
    snippet: str
    internal_date_ms: int
    label_names: tuple[str, ...]
    has_attachment: bool
    body_text: str
    body_cached: bool
    attachments: tuple[Attachment, ...] = ()
    mime_parts: tuple[MimePart, ...] = ()
    mime_complete: bool = False


@dataclass(frozen=True)
class LabelInfo:
    gmail_id: str
    name: str
    type: str
    unread: int
    total: int = 0


@dataclass(frozen=True)
class ThreadSummary:
    thread_id: int
    gmail_thread_id: str
    latest_message_gmail_id: str
    subject: str
    from_addr: str
    snippet: str
    latest_internal_date_ms: int
    unread_count: int
    starred: bool
    message_count: int
    label_ids: tuple[str, ...]


@dataclass(frozen=True)
class SearchHit:
    gmail_id: str
    thread_gmail_id: str
    subject: str
    from_addr: str
    snippet: str
    internal_date_ms: int
    label_ids: tuple[str, ...]
    body_cached: bool


@dataclass(frozen=True)
class QueueItem:
    id: int
    seq: int
    gmail_id: str
    label: str
    op: str


@dataclass(frozen=True)
class PendingChange:
    gmail_id: str
    subject: str
    op: str
    label: str


@dataclass(frozen=True)
class HeldChange:
    queue_id: int
    gmail_id: str
    subject: str
    op: str
    label: str


class Store:
    def __init__(self, root: Path, conn: sqlite3.Connection) -> None:
        self.root = root
        # One connection per thread. The screen keeps reading while sync is
        # inside a write; WAL allows that, a shared connection does not.
        self._local = threading.local()
        self._local.conn = conn
        self._conns_lock = threading.Lock()
        self._conns = [conn]
        self.label_epoch = 0
        self._preview_key: tuple | None = None
        self._preview_value: MessagePreview | None = None
        # Parsed attachments and MIME parts, keyed by message and body stamp.
        # An attachment used to force a fresh read of the raw file on every visit.
        self._mime_cache: OrderedDict[
            tuple[str, tuple[int, int] | None],
            tuple[tuple[Attachment, ...], tuple[MimePart, ...]],
        ] = OrderedDict()

    @property
    def conn(self) -> sqlite3.Connection:
        current = getattr(self._local, "conn", None)
        if current is not None:
            return current
        opened = _connect(self.root / "index.sqlite")
        with self._conns_lock:
            self._conns.append(opened)
        self._local.conn = opened
        return opened

    @classmethod
    def open(cls, root: Path | str) -> Store:
        path = Path(root)
        (path / "blobs").mkdir(parents=True, exist_ok=True)
        conn = _connect(path / "index.sqlite")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            conn.executescript(_SCHEMA)
            conn.execute("PRAGMA user_version=8")
        elif version < 8:
            if version < 2:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
                if "has_attachment" not in columns:
                    conn.execute(
                        "ALTER TABLE messages ADD COLUMN has_attachment INTEGER NOT NULL DEFAULT 0"
                    )
            if version < 3:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS label_creates (
                      local_id TEXT PRIMARY KEY,
                      name TEXT NOT NULL
                    )
                    """
                )
            queue_columns = {row[1] for row in conn.execute("PRAGMA table_info(mutation_queue)")}
            if version < 4 and "confirmed_history_id" not in queue_columns:
                conn.execute("ALTER TABLE mutation_queue ADD COLUMN confirmed_history_id TEXT")
            state_columns = {row[1] for row in conn.execute("PRAGMA table_info(sync_state)")}
            if "bootstrap_listed" not in state_columns:
                conn.execute(
                    "ALTER TABLE sync_state ADD COLUMN bootstrap_listed INTEGER NOT NULL DEFAULT 0"
                )
                conn.execute(
                    """
                    UPDATE sync_state
                    SET bootstrap_listed = (SELECT COUNT(*) FROM messages)
                    WHERE bootstrap_phase IN ('inbox', 'all')
                    """
                )
            state_columns = {row[1] for row in conn.execute("PRAGMA table_info(sync_state)")}
            if "messages_total" not in state_columns:
                conn.execute(
                    "ALTER TABLE sync_state ADD COLUMN messages_total INTEGER NOT NULL DEFAULT 0"
                )
            message_columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
            if "in_reply_to" not in message_columns:
                conn.execute("ALTER TABLE messages ADD COLUMN in_reply_to TEXT NOT NULL DEFAULT ''")
            if "references_header" not in message_columns:
                conn.execute(
                    "ALTER TABLE messages ADD COLUMN references_header TEXT NOT NULL DEFAULT ''"
                )
            if version < 8:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS snoozed (
                      gmail_id TEXT PRIMARY KEY,
                      until_ms INTEGER NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS snoozed_until ON snoozed(until_ms)"
                )
            conn.execute("PRAGMA user_version=8")
        store = cls(path, conn)
        store.replay_pending()
        return store

    def close(self) -> None:
        with self._conns_lock:
            conns = list(self._conns)
            self._conns.clear()
        for conn in conns:
            conn.close()
        self._local.conn = None

    def checkpoint(self) -> None:
        """Fold a large WAL back into the database without blocking readers.

        A multi-hundred-megabyte WAL makes every status-line read wait. PASSIVE
        checkpoint copies what it can and returns; readers keep going.
        """
        wal = self.root / "index.sqlite-wal"
        try:
            size = wal.stat().st_size
        except OSError:
            return
        if size < _WAL_CHECKPOINT_BYTES:
            return
        self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    @contextmanager
    def _tx(self) -> Iterator[None]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def open_label(self) -> str:
        return self._state()["open_label"]

    def set_open_label(self, label: str) -> None:
        with self._tx():
            self.conn.execute("UPDATE sync_state SET open_label=? WHERE id=1", (label,))

    def history_id(self) -> str | None:
        return self._state()["history_id"]

    def bootstrap_phase(self) -> str:
        return self._state()["bootstrap_phase"]

    def bootstrap_history_id(self) -> str | None:
        return self._state()["bootstrap_history_id"]

    def page_token(self) -> str | None:
        return self._state()["bootstrap_page_token"]

    def listed_count(self) -> int:
        return int(self._state()["bootstrap_listed"] or 0)

    def note_listed(self, count: int) -> None:
        if count <= 0:
            return
        with self._tx():
            self.conn.execute(
                "UPDATE sync_state SET bootstrap_listed = bootstrap_listed + ? WHERE id=1",
                (count,),
            )

    def stage_message(self, message: IncomingMessage, schedule: str) -> None:
        """Store one message immediately so the inbox can show it before the page ends."""
        with self._tx():
            thread_id = self._upsert_snapshot(message, schedule)
            self._rebuild_thread(thread_id)

    def note_inbox(self, gmail_id: str) -> bool:
        """Record that Gmail currently lists this message in the inbox.

        A queued removal wins, so a message the user just archived stays archived.
        """
        row = self._message_row(gmail_id)
        if row is None:
            return False
        if self._pending_map(row["id"]).get("INBOX") == "remove":
            return False
        with self._tx():
            self._set_membership(row["id"], "INBOX", row["internal_date"] or 0, present=True)
            if row["body_state"] != "present":
                self._mark_pending_now(row["id"])
            self._rebuild_thread(row["thread_id"])
        return True

    def retain_inbox(self, seen: set[str], after_id: int = 0, limit: int = _WRITE_BATCH) -> int | None:
        """Drop INBOX from one batch of messages Gmail did not list.

        Scans local ids above ``after_id``. Returns the next id to pass in,
        or None when the scan is finished. One call stays small so the screen
        can read the index while the inbox list is being reconciled.
        """
        rows = self.conn.execute(
            """
            SELECT m.id, m.gmail_id, m.thread_id, m.internal_date
            FROM messages m
            JOIN message_labels ml ON ml.message_id = m.id
            JOIN labels l ON l.id = ml.label_id
            WHERE l.gmail_id = 'INBOX' AND m.id > ?
            ORDER BY m.id
            LIMIT ?
            """,
            (after_id, limit),
        ).fetchall()
        if not rows:
            return None
        threads: set[int] = set()
        with self._tx():
            for row in rows:
                if row["gmail_id"] in seen:
                    continue
                if self._pending_map(row["id"]).get("INBOX") == "add":
                    continue
                self._set_membership(row["id"], "INBOX", row["internal_date"] or 0, present=False)
                threads.add(row["thread_id"])
            for thread_id in threads:
                self._rebuild_thread(thread_id)
        if len(rows) < limit:
            return None
        return int(rows[-1]["id"])

    def next_inbox_bodies(self, limit: int = 8) -> list[str]:
        return self._missing_in_label("INBOX", limit)

    def next_priority_bodies(self, open_label: str, limit: int = 8) -> list[str]:
        """Bodies for the open folder, then inbox — before the rest of the mailbox."""
        if limit <= 0:
            return []
        found: list[str] = []
        seen: set[str] = set()
        for label in self._priority_labels(open_label):
            for gmail_id in self._missing_in_label(
                label, limit - len(found), open_label=open_label
            ):
                if gmail_id in seen:
                    continue
                seen.add(gmail_id)
                found.append(gmail_id)
                if len(found) >= limit:
                    return found
        return found

    def _priority_labels(self, open_label: str) -> list[str]:
        labels: list[str] = []
        chosen = (open_label or "").strip()
        if chosen and chosen != "INBOX":
            labels.append(chosen)
        labels.append("INBOX")
        return labels

    def _missing_in_label(
        self, label: str, limit: int, *, open_label: str | None = None
    ) -> list[str]:
        if limit <= 0:
            return []
        defer = _body_defer_sql(
            "m", open_label if open_label is not None else self.open_label()
        )
        rows = self.conn.execute(
            f"""
            SELECT m.gmail_id
            FROM messages m
            JOIN message_labels ml ON ml.message_id = m.id
            JOIN labels l ON l.id = ml.label_id
            WHERE l.gmail_id = ? AND m.body_state != 'present'
              {defer}
            ORDER BY m.internal_date DESC, m.id DESC
            LIMIT ?
            """,
            (label, limit),
        ).fetchall()
        return [row[0] for row in rows]

    def next_inbox_body(self) -> str | None:
        row = self.conn.execute(
            """
            SELECT m.gmail_id
            FROM messages m
            JOIN message_labels ml ON ml.message_id = m.id
            JOIN labels l ON l.id = ml.label_id
            WHERE l.gmail_id = 'INBOX' AND m.body_state != 'present'
            ORDER BY m.internal_date DESC, m.id DESC
            LIMIT 1
            """
        ).fetchone()
        return None if row is None else row[0]

    def inbox_body_counts(self) -> tuple[int, int]:
        row = self.conn.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN m.body_state = 'present' THEN 1 ELSE 0 END), 0),
              COUNT(*)
            FROM messages m
            JOIN message_labels ml ON ml.message_id = m.id
            JOIN labels l ON l.id = ml.label_id
            WHERE l.gmail_id = 'INBOX'
            """
        ).fetchone()
        return int(row[0]), int(row[1])

    def resyncing(self) -> bool:
        return bool(self._state()["resyncing"])

    def messages_total(self) -> int:
        return int(self._state()["messages_total"] or 0)

    def set_messages_total(self, total: int) -> None:
        if total <= 0:
            return
        with self._tx():
            self.conn.execute(
                "UPDATE sync_state SET messages_total=? WHERE id=1",
                (total,),
            )

    def set_profile(self, email: str, history_id: str, messages_total: int = 0) -> None:
        current = self.account_email()
        if current and email and current.casefold() != email.casefold():
            raise ValueError(f"this mailbox is {current}, but the Gmail token is for {email}")
        with self._tx():
            self.conn.execute(
                """
                UPDATE sync_state
                SET email=?, bootstrap_history_id=?, messages_total=?,
                    bootstrap_phase='inbox',
                    bootstrap_page_token=NULL, bootstrap_listed=0
                WHERE id=1
                """,
                (email or current, history_id, messages_total),
            )

    def account_email(self) -> str:
        return str(self._state()["email"] or "")

    def set_email(self, email: str) -> None:
        current = self.account_email()
        if current and email and current.casefold() != email.casefold():
            raise ValueError(f"this mailbox is {current}, but the Gmail token is for {email}")
        with self._tx():
            self.conn.execute("UPDATE sync_state SET email=? WHERE id=1", (email,))

    def inbox_counts(self) -> tuple[int, int]:
        """Inbox size and, of those, how many are unread. Trash is left out."""
        row = self.conn.execute(
            """
            SELECT COUNT(*),
                   COALESCE(SUM(CASE WHEN EXISTS (
                     SELECT 1 FROM message_labels um
                     JOIN labels ul ON ul.id = um.label_id
                     WHERE um.message_id = ml.message_id AND ul.gmail_id = 'UNREAD'
                   ) THEN 1 ELSE 0 END), 0)
            FROM message_labels ml
            JOIN labels l ON l.id = ml.label_id
            WHERE l.gmail_id = 'INBOX'
              AND NOT EXISTS (
                SELECT 1 FROM message_labels tm
                JOIN labels tl ON tl.id = tm.label_id
                WHERE tm.message_id = ml.message_id AND tl.gmail_id = 'TRASH'
              )
            """
        ).fetchone()
        return int(row[0] or 0), int(row[1] or 0)

    def advance_phase(self, phase: str) -> None:
        with self._tx():
            self.conn.execute(
                """
                UPDATE sync_state
                SET bootstrap_phase=?, bootstrap_page_token=NULL, bootstrap_listed=0
                WHERE id=1
                """,
                (phase,),
            )

    def begin_resync(self) -> None:
        """Drop the history cursor and rebuild metadata. Existing rows stay until prune."""
        with self._tx():
            self.conn.execute(
                """
                UPDATE sync_state
                SET resync_gen = resync_gen + 1,
                    resyncing = 1,
                    bootstrap_phase = 'need_profile',
                    bootstrap_page_token = NULL,
                    bootstrap_listed = 0,
                    history_id = NULL,
                    history_page_token = NULL,
                    history_page_start = NULL
                WHERE id=1
                """
            )

    def finish_bootstrap(self) -> None:
        with self._tx():
            self.conn.execute(
                """
                UPDATE sync_state
                SET bootstrap_phase='done',
                    resyncing=0,
                    bootstrap_page_token=NULL,
                    history_id=COALESCE(history_id, bootstrap_history_id)
                WHERE id=1
                """
            )

    def set_history_id(self, history_id: str) -> None:
        with self._tx():
            self.conn.execute(
                """
                UPDATE sync_state
                SET history_id=?, history_page_token=NULL, history_page_start=NULL
                WHERE id=1
                """,
                (history_id,),
            )

    def replace_labels(self, labels: list[Label]) -> None:
        with self._tx():
            seen: list[str] = []
            for label in labels:
                if not label.id:
                    continue
                seen.append(label.id)
                self.conn.execute(
                    """
                    INSERT INTO labels (gmail_id, name, type) VALUES (?, ?, ?)
                    ON CONFLICT(gmail_id) DO UPDATE SET name=excluded.name, type=excluded.type
                    """,
                    (label.id, label.name or label.id, label.type or ""),
                )
            if not seen:
                return
            for local_id in self._pending_label_ids():
                if local_id not in seen:
                    seen.append(local_id)
            placeholders = ",".join("?" for _ in seen)
            thread_ids = [
                row[0]
                for row in self.conn.execute(
                    f"""
                    SELECT DISTINCT m.thread_id
                    FROM message_labels ml
                    JOIN messages m ON m.id = ml.message_id
                    JOIN labels l ON l.id = ml.label_id
                    WHERE l.gmail_id NOT IN ({placeholders})
                    """,
                    seen,
                )
            ]
            self.conn.execute(f"DELETE FROM labels WHERE gmail_id NOT IN ({placeholders})", seen)
            for thread_id in thread_ids:
                self._rebuild_thread(thread_id)

    def list_threads(
        self,
        label: str,
        cursor: ThreadCursor | None = None,
        limit: int = 50,
    ) -> list[ThreadSummary]:
        label_id = self._label_pk_read(label)
        if label_id is None:
            return []
        limit = max(1, min(limit, 500))
        sql = """
            SELECT t.id AS thread_id, t.gmail_id, t.subject, t.from_addr, t.snippet,
                   tl.latest_internal_date, t.unread_count, t.starred, t.message_count,
                   t.latest_message_gmail_id, t.label_ids_csv
            FROM thread_labels tl
            JOIN threads t ON t.id = tl.thread_id
            WHERE tl.label_id = ?
        """
        params: list[object] = [label_id]
        if cursor is not None:
            sql += " AND (tl.latest_internal_date, tl.thread_id) < (?, ?)"
            params.extend([cursor.latest_internal_date_ms, cursor.thread_id])
        sql += " ORDER BY tl.latest_internal_date DESC, tl.thread_id DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [_thread_summary(row) for row in rows]

    def list_mail_threads(
        self,
        label: str | None,
        cursor: ThreadCursor | None = None,
        limit: int = 100,
        *,
        newest_first: bool = True,
    ) -> list[MessageSummary]:
        """One row per conversation that meets ``label``. ``label`` None is all mail.

        The row stands for the newest saved message in the conversation. Messages
        filed under other labels still count, so the list can open the whole thread.
        """
        limit = max(1, min(limit, 500))
        hide_trash = label != "TRASH"
        page = self._mail_thread_page(label, cursor, limit, newest_first, hide_trash)
        if not page:
            return []
        members = self._member_rows([row["thread_id"] for row in page])
        message_ids = [item["id"] for group in members.values() for item in group]
        names = self._label_names_for(message_ids)
        summaries: list[MessageSummary] = []
        for row in page:
            visible = [
                item
                for item in members.get(row["thread_id"], [])
                if not (hide_trash and item["trashed"])
            ]
            if label:
                in_label = [item for item in visible if label in item["label_ids"]]
                if not in_label:
                    continue
            if not visible:
                continue
            newest = max(visible, key=lambda item: (item["internal_date"], item["id"]))
            acted = in_label if label else visible
            count = len(visible)
            label_names: list[str] = []
            seen: set[str] = set()
            for item in visible:
                for name in names.get(item["id"], ()):
                    if name not in seen:
                        seen.add(name)
                        label_names.append(name)
            summaries.append(
                MessageSummary(
                    message_id=newest["id"],
                    gmail_id=newest["gmail_id"],
                    from_addr=newest["from_addr"],
                    to_addr=newest["to_addr"],
                    subject=newest["subject"],
                    snippet=newest["snippet"],
                    internal_date_ms=newest["internal_date"],
                    label_names=tuple(label_names),
                    has_attachment=any(item["has_attachment"] for item in visible),
                    unread=any(item["unread"] for item in visible),
                    thread_id=row["thread_id"],
                    thread_count=count,
                    thread_role="head" if count > 1 else "",
                    row_key=f"t:{row['thread_id']}" if count > 1 else newest["gmail_id"],
                    member_ids=tuple(str(item["gmail_id"]) for item in acted) if count > 1 else (),
                )
            )
        return summaries

    def messages_in_thread(self, thread_id: int, *, hide_trash: bool = True) -> list[MessageSummary]:
        """Every saved message in the conversation, oldest first, from any label."""
        self._backfill_reply_headers(thread_id)
        visible = [
            item
            for item in self._member_rows([thread_id]).get(thread_id, [])
            if not (hide_trash and item["trashed"])
        ]
        names = self._label_names_for([item["id"] for item in visible])
        return [_member_summary(item, names) for item in visible]

    def _mail_thread_page(
        self,
        label: str | None,
        cursor: ThreadCursor | None,
        limit: int,
        newest_first: bool,
        hide_trash: bool,
    ) -> list[sqlite3.Row]:
        visible = f"NOT {self._label_exists('m', 'TRASH')}" if hide_trash else "1"
        sort_expr = f"""(
            SELECT MAX(m.internal_date) FROM messages m
            WHERE m.thread_id = t.id AND {visible}
        )"""
        compare = "<" if newest_first else ">"
        direction = "DESC" if newest_first else "ASC"
        params: list[object] = []
        if label:
            label_id = self._label_pk_read(label)
            if label_id is None:
                return []
            sql = f"""
                SELECT t.id AS thread_id, {sort_expr} AS sort_date
                FROM thread_labels tl
                JOIN threads t ON t.id = tl.thread_id
                WHERE tl.label_id = ?
                AND EXISTS (
                  SELECT 1 FROM messages m
                  JOIN message_labels ml ON ml.message_id = m.id AND ml.label_id = tl.label_id
                  WHERE m.thread_id = t.id AND {visible}
                )
            """
            params.append(label_id)
        else:
            sql = f"""
                SELECT t.id AS thread_id, {sort_expr} AS sort_date
                FROM threads t
                WHERE {sort_expr} IS NOT NULL
            """
        if cursor is not None:
            sql += f" AND ({sort_expr}, t.id) {compare} (?, ?)"
            params.extend([cursor.latest_internal_date_ms, cursor.thread_id])
        sql += f" ORDER BY {sort_expr} {direction}, t.id {direction} LIMIT ?"
        params.append(limit)
        return list(self.conn.execute(sql, params))

    def _member_rows(self, thread_ids: list[int]) -> dict[int, list[dict[str, object]]]:
        if not thread_ids:
            return {}
        placeholders = ",".join("?" for _ in thread_ids)
        rows = self.conn.execute(
            f"""
            SELECT m.id, m.thread_id, m.gmail_id, m.from_addr, m.to_addr, m.subject,
                   m.snippet, m.internal_date, m.has_attachment, m.message_id_header,
                   m.in_reply_to, m.references_header,
                   {self._label_exists("m", "UNREAD")} AS unread,
                   {self._label_exists("m", "TRASH")} AS trashed
            FROM messages m
            WHERE m.thread_id IN ({placeholders})
            ORDER BY m.internal_date ASC, m.id ASC
            """,
            thread_ids,
        ).fetchall()
        message_ids = [row["id"] for row in rows]
        labels: dict[int, list[str]] = {}
        if message_ids:
            marks = ",".join("?" for _ in message_ids)
            for label_row in self.conn.execute(
                f"""
                SELECT ml.message_id, l.gmail_id
                FROM message_labels ml
                JOIN labels l ON l.id = ml.label_id
                WHERE ml.message_id IN ({marks})
                """,
                message_ids,
            ):
                labels.setdefault(label_row["message_id"], []).append(label_row["gmail_id"])
        grouped: dict[int, list[dict[str, object]]] = {}
        for row in rows:
            grouped.setdefault(row["thread_id"], []).append(
                {
                    "id": row["id"],
                    "thread_id": row["thread_id"],
                    "gmail_id": row["gmail_id"],
                    "from_addr": row["from_addr"],
                    "to_addr": row["to_addr"],
                    "subject": row["subject"],
                    "snippet": row["snippet"],
                    "internal_date": row["internal_date"],
                    "has_attachment": bool(row["has_attachment"]),
                    "message_id_header": row["message_id_header"] or "",
                    "in_reply_to": row["in_reply_to"] or "",
                    "references_header": row["references_header"] or "",
                    "unread": bool(row["unread"]),
                    "trashed": bool(row["trashed"]),
                    "label_ids": labels.get(row["id"], []),
                }
            )
        return grouped

    def _backfill_reply_headers(self, thread_id: int) -> None:
        rows = self.conn.execute(
            """
            SELECT id, body_state, body_path, in_reply_to, references_header,
                   message_id_header
            FROM messages
            WHERE thread_id = ?
            """,
            (thread_id,),
        ).fetchall()
        updates: list[tuple[str, str, str, int]] = []
        for row in rows:
            if (
                row["in_reply_to"]
                and row["references_header"]
                and row["message_id_header"]
            ):
                continue
            if row["body_state"] != "present" or not row["body_path"]:
                continue
            path = self.root / str(row["body_path"])
            try:
                parsed = parse_rfc822(path.read_bytes())
            except OSError:
                continue
            updates.append(
                (
                    parsed.in_reply_to.strip(),
                    parsed.references.strip() or "-",
                    parsed.message_id_header.strip(),
                    row["id"],
                )
            )
        if not updates:
            return
        with self._tx():
            for in_reply_to, references, message_id, message_pk in updates:
                self.conn.execute(
                    """
                    UPDATE messages SET
                      in_reply_to=CASE WHEN in_reply_to != '' THEN in_reply_to ELSE ? END,
                      references_header=CASE WHEN references_header != '' THEN references_header ELSE ? END,
                      message_id_header=CASE WHEN message_id_header != '' THEN message_id_header ELSE ? END
                    WHERE id=?
                    """,
                    (in_reply_to, references, message_id, message_pk),
                )

    def list_label_info(self) -> list[LabelInfo]:
        # The mail list hides Trash except inside Trash. The badge has to do
        # the same, or Unread can count letters the list will not show.
        # One grouped scan beats a correlated COUNT per label on large mailboxes.
        rows = self.conn.execute(
            """
            WITH trashed AS (
              SELECT ml.message_id AS id
              FROM message_labels ml
              JOIN labels l ON l.id = ml.label_id AND l.gmail_id = 'TRASH'
            ),
            unread AS (
              SELECT ml.message_id AS id
              FROM message_labels ml
              JOIN labels l ON l.id = ml.label_id AND l.gmail_id = 'UNREAD'
            ),
            totals AS (
              SELECT ml.label_id AS label_id,
                     COUNT(*) AS total,
                     SUM(CASE WHEN u.id IS NOT NULL THEN 1 ELSE 0 END) AS unread
              FROM message_labels ml
              JOIN labels lab ON lab.id = ml.label_id
              LEFT JOIN trashed t ON t.id = ml.message_id
              LEFT JOIN unread u ON u.id = ml.message_id
              WHERE lab.gmail_id = 'TRASH' OR t.id IS NULL
              GROUP BY ml.label_id
            )
            SELECT l.gmail_id, l.name, l.type,
                   COALESCE(totals.total, 0) AS total,
                   COALESCE(totals.unread, 0) AS unread
            FROM labels l
            LEFT JOIN totals ON totals.label_id = l.id
            ORDER BY l.name
            """
        ).fetchall()
        return [
            LabelInfo(
                gmail_id=row["gmail_id"],
                name=row["name"],
                type=row["type"] or "",
                unread=row["unread"],
                total=row["total"],
            )
            for row in rows
        ]

    def mailbox_counts(self) -> tuple[int, int]:
        """Messages outside Trash, and how many of those are unread."""
        visible = "NOT " + self._label_exists("m", "TRASH")
        total = int(
            self.conn.execute(f"SELECT COUNT(*) FROM messages m WHERE {visible}").fetchone()[0] or 0
        )
        unread = int(
            self.conn.execute(
                f"""
                SELECT COUNT(*)
                FROM messages m
                WHERE {visible}
                  AND {self._label_exists("m", "UNREAD")}
                """
            ).fetchone()[0]
            or 0
        )
        return total, unread

    def list_messages(
        self,
        label: str | None,
        cursor: MessageCursor | None = None,
        limit: int = 100,
        *,
        newest_first: bool = True,
    ) -> list[MessageSummary]:
        """Messages in one label. ``label`` None lists the whole mailbox."""
        limit = max(1, min(limit, 500))
        compare = "<" if newest_first else ">"
        direction = "DESC" if newest_first else "ASC"
        params: list[object] = []
        if label:
            label_id = self._label_pk_read(label)
            if label_id is None:
                return []
            sql = """
                SELECT m.id AS message_id, m.gmail_id, m.from_addr, m.to_addr, m.subject,
                       m.snippet, m.internal_date, m.has_attachment,
                       EXISTS (
                         SELECT 1 FROM message_labels um
                         JOIN labels u ON u.id = um.label_id
                         WHERE um.message_id = m.id AND u.gmail_id = 'UNREAD'
                       ) AS unread
                FROM message_labels ml
                JOIN messages m ON m.id = ml.message_id
                WHERE ml.label_id = ?
            """
            params.append(label_id)
            if label != "TRASH":
                sql += " AND NOT " + self._label_exists("m", "TRASH")
            if cursor is not None:
                sql += f" AND (ml.internal_date, ml.message_id) {compare} (?, ?)"
                params.extend([cursor.internal_date_ms, cursor.message_id])
            sql += f" ORDER BY ml.internal_date {direction}, ml.message_id {direction} LIMIT ?"
        else:
            sql = """
                SELECT m.id AS message_id, m.gmail_id, m.from_addr, m.to_addr, m.subject,
                       m.snippet, m.internal_date, m.has_attachment,
                       EXISTS (
                         SELECT 1 FROM message_labels um
                         JOIN labels u ON u.id = um.label_id
                         WHERE um.message_id = m.id AND u.gmail_id = 'UNREAD'
                       ) AS unread
                FROM messages m
                WHERE NOT """ + self._label_exists("m", "TRASH") + """
            """
            if cursor is not None:
                sql += f" AND (m.internal_date, m.id) {compare} (?, ?)"
                params.extend([cursor.internal_date_ms, cursor.message_id])
            sql += f" ORDER BY m.internal_date {direction}, m.id {direction} LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        names = self._label_names_for([row["message_id"] for row in rows])
        return [
            MessageSummary(
                message_id=row["message_id"],
                gmail_id=row["gmail_id"],
                from_addr=row["from_addr"],
                to_addr=row["to_addr"],
                subject=row["subject"],
                snippet=row["snippet"],
                internal_date_ms=row["internal_date"],
                label_names=names.get(row["message_id"], ()),
                has_attachment=bool(row["has_attachment"]),
                unread=bool(row["unread"]),
            )
            for row in rows
        ]

    def preview_message(self, gmail_id: str, *, mime: bool = False) -> MessagePreview | None:
        row = self._preview_row(gmail_id)
        if row is None:
            return None
        names = str(row["label_names"] or "")
        label_names = tuple(sorted(names.split("\x1f"))) if names else ()
        cached = False
        stamp: tuple[int, int] | None = None
        path: Path | None = None
        if row["body_state"] == "present" and row["body_path"]:
            candidate = self.root / str(row["body_path"])
            try:
                info = candidate.stat()
            except OSError:
                info = None
            if info is not None and S_ISREG(info.st_mode):
                cached = True
                stamp = (info.st_mtime_ns, info.st_size)
                path = candidate
        body_text = str(row["body_text"]) if cached else ""
        attachments: tuple[Attachment, ...] = ()
        mime_parts: tuple[MimePart, ...] = ()
        has_attachment = bool(row["has_attachment"])
        identity = (
            gmail_id,
            stamp,
            str(row["subject"]),
            str(row["from_addr"]),
            str(row["snippet"]),
            label_names,
            cached,
            has_attachment,
            len(body_text),
        )
        if identity == self._preview_key and self._preview_value is not None:
            if self._preview_value.mime_complete or not mime:
                return self._preview_value
        mime_complete = False
        mime_key = (gmail_id, stamp)
        cached_parse = self._cached_parse(mime_key)
        # The raw file is read once per stamp. Later moves reuse the parts and
        # the attachment list; the attachment flag is not a reason to reread.
        if cached_parse is not None:
            attachments, mime_parts = cached_parse
            mime_complete = True
            if not body_text.strip():
                recovered = "\n".join(part.text for part in mime_parts if part.text.strip())
                if recovered.strip():
                    body_text = recovered
        elif path is not None:
            try:
                parsed = parse_rfc822(path.read_bytes())
            except OSError:
                parsed = None
            if parsed is not None:
                attachments = parsed.attachments
                mime_parts = parsed.mime_parts
                mime_complete = True
                self._store_parse(mime_key, attachments, mime_parts)
                if not body_text.strip() and parsed.text.strip():
                    body_text = parsed.text
        if cached and not mime_parts and body_text:
            encoded = body_text.encode("utf-8", errors="replace")
            mime_parts = (MimePart(1, "text/plain", len(encoded), body_text, source=body_text),)
        preview = MessagePreview(
            gmail_id=str(row["gmail_id"]),
            from_addr=str(row["from_addr"]),
            to_addr=str(row["to_addr"]),
            cc_addr=str(row["cc_addr"]),
            subject=str(row["subject"]),
            snippet=str(row["snippet"]),
            internal_date_ms=int(row["internal_date"]),
            label_names=label_names,
            has_attachment=has_attachment,
            body_text=body_text,
            body_cached=cached,
            attachments=attachments,
            mime_parts=mime_parts,
            mime_complete=mime_complete,
        )
        self._preview_key = identity
        self._preview_value = preview
        return preview

    def _cached_parse(
        self, key: tuple[str, tuple[int, int] | None]
    ) -> tuple[tuple[Attachment, ...], tuple[MimePart, ...]] | None:
        found = self._mime_cache.get(key)
        if found is None:
            return None
        self._mime_cache.move_to_end(key)
        return found

    def _store_parse(
        self,
        key: tuple[str, tuple[int, int] | None],
        attachments: tuple[Attachment, ...],
        mime_parts: tuple[MimePart, ...],
    ) -> None:
        self._mime_cache[key] = (attachments, mime_parts)
        self._mime_cache.move_to_end(key)
        while len(self._mime_cache) > _PARSE_CACHE_LIMIT:
            self._mime_cache.popitem(last=False)

    def explain_list(self, label: str, cursor: ThreadCursor | None = None) -> str:
        label_id = self._label_pk_read(label)
        if label_id is None:
            return ""
        sql = """
            SELECT t.id
            FROM thread_labels tl
            JOIN threads t ON t.id = tl.thread_id
            WHERE tl.label_id = ?
        """
        params: list[object] = [label_id]
        if cursor is not None:
            sql += " AND (tl.latest_internal_date, tl.thread_id) < (?, ?)"
            params.extend([cursor.latest_internal_date_ms, cursor.thread_id])
        sql += " ORDER BY tl.latest_internal_date DESC, tl.thread_id DESC LIMIT 50"
        rows = self.conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
        return " ".join(" ".join(str(cell) for cell in row) for row in rows)

    def already_visited(self, gmail_id: str) -> bool:
        row = self.conn.execute(
            "SELECT has_metadata, resync_gen FROM messages WHERE gmail_id=?",
            (gmail_id,),
        ).fetchone()
        if row is None or not row["has_metadata"]:
            return False
        return row["resync_gen"] == self._state()["resync_gen"]

    def has_metadata(self, gmail_id: str) -> bool:
        row = self.conn.execute(
            "SELECT has_metadata FROM messages WHERE gmail_id=?",
            (gmail_id,),
        ).fetchone()
        return bool(row and row["has_metadata"])

    def commit_metadata_page(
        self,
        messages: list[tuple[IncomingMessage, str]],
        next_token: str | None,
    ) -> None:
        pieces = [
            messages[start : start + _WRITE_BATCH]
            for start in range(0, len(messages), _WRITE_BATCH)
        ] or [[]]
        for index, piece in enumerate(pieces):
            last = index == len(pieces) - 1
            with self._tx():
                threads: set[int] = set()
                for message, schedule in piece:
                    threads.add(self._upsert_snapshot(message, schedule))
                for thread_id in threads:
                    self._rebuild_thread(thread_id)
                if last:
                    self.conn.execute(
                        "UPDATE sync_state SET bootstrap_page_token=? WHERE id=1",
                        (next_token,),
                    )

    def apply_history_page(
        self,
        records: list[HistoryRecord],
        headers: dict[str, IncomingMessage | None],
        *,
        start: str,
        next_page_token: str | None,
        finished_history_id: str | None,
    ) -> list[Path]:
        """Apply one history page and advance the cursor in the same transaction."""
        paths: list[Path] = []
        pieces = [
            records[start : start + _WRITE_BATCH]
            for start in range(0, len(records), _WRITE_BATCH)
        ] or [[]]
        for index, piece in enumerate(pieces):
            last = index == len(pieces) - 1
            with self._tx():
                threads: set[int] = set()
                for record in piece:
                    touched, removed = self._apply_record(record, headers)
                    threads.update(touched)
                    paths.extend(removed)
                for thread_id in threads:
                    if self._thread_exists(thread_id):
                        self._rebuild_thread(thread_id)
                if last:
                    self._note_history_resume(start, next_page_token, finished_history_id)
        return paths

    def prune_unseen(self, limit: int = _WRITE_BATCH) -> list[Path]:
        """Delete one batch of messages left from an older resync. Empty means done."""
        gen = self._state()["resync_gen"]
        rows = self.conn.execute(
            """
            SELECT gmail_id FROM messages
            WHERE resync_gen < ?
              AND id NOT IN (SELECT message_id FROM mutation_queue)
            LIMIT ?
            """,
            (gen, limit),
        ).fetchall()
        paths: list[Path] = []
        if not rows:
            return paths
        with self._tx():
            for row in rows:
                paths.extend(self._delete_message_in_tx(row["gmail_id"]))
        return paths

    def upsert_metadata(self, message: IncomingMessage, schedule: str) -> None:
        with self._tx():
            thread_id = self._upsert_snapshot(message, schedule)
            self._rebuild_thread(thread_id)

    def apply_local_labels(
        self,
        gmail_id: str,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> None:
        """Update the replica immediately and queue the intent for Gmail."""
        self.apply_local_labels_batch([(gmail_id, add, remove)])

    def apply_local_labels_batch(
        self,
        changes: list[tuple[str, list[str] | None, list[str] | None]],
    ) -> None:
        """Apply many local label intents in one transaction."""
        if not changes:
            return
        threads: set[int] = set()
        with self._tx():
            for gmail_id, add, remove in changes:
                row = self._message_row(gmail_id)
                if row is None:
                    raise MessageNotFound(gmail_id)
                for label in add or []:
                    self._ensure_label(label)
                    self._set_membership(row["id"], label, row["internal_date"], present=True)
                    self._queue_op(row["id"], label, "add")
                for label in remove or []:
                    self._ensure_label(label)
                    self._set_membership(row["id"], label, row["internal_date"], present=False)
                    self._queue_op(row["id"], label, "remove")
                threads.add(row["thread_id"])
            for thread_id in threads:
                self._rebuild_thread(thread_id)

    def undo_labels(self, gmail_id: str, add: list[str], remove: list[str]) -> None:
        """Reverse one label change. An unsent intent is dropped instead of inverted on Gmail."""
        with self._tx():
            row = self._message_row(gmail_id)
            if row is None:
                return
            for label in add:
                self._undo_label(row["id"], row["internal_date"], label, "add")
            for label in remove:
                self._undo_label(row["id"], row["internal_date"], label, "remove")
            self._rebuild_thread(row["thread_id"])

    def _undo_label(self, message_pk: int, internal_date: int, label: str, original: str) -> None:
        queued = self.conn.execute(
            """
            SELECT id, op, confirmed_history_id
            FROM mutation_queue
            WHERE message_id=? AND label_gmail_id=?
            """,
            (message_pk, label),
        ).fetchone()
        if queued is not None and queued["confirmed_history_id"] is None and queued["op"] == original:
            self.conn.execute("DELETE FROM mutation_queue WHERE id=?", (queued["id"],))
            self._set_membership(message_pk, label, internal_date, present=original != "add")
            return
        inverse = "remove" if original == "add" else "add"
        self._ensure_label(label)
        self._set_membership(message_pk, label, internal_date, present=inverse == "add")
        if queued is not None:
            self.conn.execute("DELETE FROM mutation_queue WHERE id=?", (queued["id"],))
        self._queue_op(message_pk, label, inverse)

    def queue_groups(self) -> dict[str, list[QueueItem]]:
        rows = self.conn.execute(
            """
            SELECT q.id, q.seq, q.op, q.label_gmail_id, m.gmail_id
            FROM mutation_queue q
            JOIN messages m ON m.id = q.message_id
            WHERE q.confirmed_history_id IS NULL
            ORDER BY q.id
            """
        ).fetchall()
        grouped: dict[str, list[QueueItem]] = {}
        for row in rows:
            grouped.setdefault(row["gmail_id"], []).append(
                QueueItem(
                    id=row["id"],
                    seq=row["seq"],
                    gmail_id=row["gmail_id"],
                    label=row["label_gmail_id"],
                    op=row["op"],
                )
            )
        return grouped

    def queue_counts(self) -> tuple[int, int]:
        """Return ``(accepted, waiting)`` label changes still in the queue.

        Accepted changes were sent to Gmail and stay until history catches up.
        Waiting changes have not been sent yet.
        """
        row = self.conn.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN confirmed_history_id IS NULL THEN 0 ELSE 1 END), 0),
              COALESCE(SUM(CASE WHEN confirmed_history_id IS NULL THEN 1 ELSE 0 END), 0)
            FROM mutation_queue
            """
        ).fetchone()
        return int(row[0]), int(row[1])

    def has_pending_mutations(self) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM mutation_queue WHERE confirmed_history_id IS NULL LIMIT 1"
            ).fetchone()
            is not None
        )

    def schedule_snooze(self, gmail_id: str, until_ms: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO snoozed(gmail_id, until_ms) VALUES (?, ?)",
            (gmail_id, until_ms),
        )

    def clear_snooze(self, gmail_id: str) -> None:
        self.conn.execute("DELETE FROM snoozed WHERE gmail_id=?", (gmail_id,))

    def due_snooze_ids(self, now_ms: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT gmail_id FROM snoozed WHERE until_ms <= ? ORDER BY until_ms, gmail_id",
            (now_ms,),
        ).fetchall()
        return [str(row["gmail_id"]) for row in rows]

    def snooze_until_ms(self, gmail_id: str) -> int | None:
        row = self.conn.execute(
            "SELECT until_ms FROM snoozed WHERE gmail_id=?", (gmail_id,)
        ).fetchone()
        return int(row["until_ms"]) if row is not None else None

    def mutation_pending(self, item_id: int, seq: int) -> bool:
        row = self.conn.execute(
            """
            SELECT 1 FROM mutation_queue
            WHERE id=? AND seq=? AND confirmed_history_id IS NULL
            """,
            (item_id, seq),
        ).fetchone()
        return row is not None

    def replay_pending(self) -> None:
        """Put every queued label change back onto the local message.

        A restart can import an older Gmail snapshot after the change was
        sent. The queue row is what makes the local inbox match that change.
        """
        rows = self.conn.execute(
            """
            SELECT m.id AS message_id, m.thread_id, m.internal_date,
                   q.label_gmail_id, q.op
            FROM mutation_queue q
            JOIN messages m ON m.id = q.message_id
            ORDER BY q.seq
            """
        ).fetchall()
        if not rows:
            return
        threads: set[int] = set()
        with self._tx():
            for row in rows:
                self._set_membership(
                    row["message_id"],
                    row["label_gmail_id"],
                    row["internal_date"],
                    present=row["op"] == "add",
                )
                threads.add(row["thread_id"])
            for thread_id in threads:
                self._rebuild_thread(thread_id)

    def pending_mutations(self) -> list[tuple[str, str, str]]:
        items: list[tuple[str, str, str]] = []
        for group in self.queue_groups().values():
            for item in group:
                items.append((item.gmail_id, item.label, item.op))
        return items

    def pending_changes(self) -> list[PendingChange]:
        rows = self.conn.execute(
            """
            SELECT m.gmail_id, m.subject, q.op,
                   COALESCE(l.name, q.label_gmail_id) AS label
            FROM mutation_queue q
            JOIN messages m ON m.id = q.message_id
            LEFT JOIN labels l ON l.gmail_id = q.label_gmail_id
            WHERE q.confirmed_history_id IS NULL
            ORDER BY q.id
            """
        ).fetchall()
        return [
            PendingChange(
                gmail_id=row["gmail_id"],
                subject=row["subject"] or "",
                op=row["op"],
                label=row["label"],
            )
            for row in rows
        ]

    def held_changes(self) -> list[HeldChange]:
        """Label changes Gmail already accepted, still waiting for history."""
        rows = self.conn.execute(
            """
            SELECT q.id, m.gmail_id, m.subject, q.op,
                   COALESCE(l.name, q.label_gmail_id) AS label
            FROM mutation_queue q
            JOIN messages m ON m.id = q.message_id
            LEFT JOIN labels l ON l.gmail_id = q.label_gmail_id
            WHERE q.confirmed_history_id IS NOT NULL
            ORDER BY q.id
            """
        ).fetchall()
        return [
            HeldChange(
                queue_id=row["id"],
                gmail_id=row["gmail_id"],
                subject=row["subject"] or "",
                op=row["op"],
                label=row["label"],
            )
            for row in rows
        ]

    def release_held(self, queue_id: int) -> bool:
        """Drop one accepted queue row. The label change itself stays in place."""
        with self._tx():
            cursor = self.conn.execute(
                """
                DELETE FROM mutation_queue
                WHERE id=? AND confirmed_history_id IS NOT NULL
                """,
                (queue_id,),
            )
            return cursor.rowcount > 0

    def rebase_and_snapshot(self, gmail_id: str, remote_labels: list[str]) -> list[QueueItem]:
        with self._tx():
            row = self._message_row(gmail_id)
            if row is None:
                raise MessageNotFound(gmail_id)
            self._reconcile(row["id"], row["internal_date"], remote_labels)
            self._rebuild_thread(row["thread_id"])
        groups = self.queue_groups()
        return groups.get(gmail_id, [])

    def complete_push(
        self,
        gmail_id: str,
        items: list[QueueItem],
        remote_labels: list[str],
        remote_history_id: str | None = None,
    ) -> None:
        with self._tx():
            row = self._message_row(gmail_id)
            if row is None:
                for item in items:
                    self.conn.execute(
                        "DELETE FROM mutation_queue WHERE id=? AND seq=?",
                        (item.id, item.seq),
                    )
                return
            for item in items:
                if remote_history_id:
                    self.conn.execute(
                        """
                        UPDATE mutation_queue
                        SET confirmed_history_id=?
                        WHERE id=? AND seq=?
                        """,
                        (remote_history_id, item.id, item.seq),
                    )
                else:
                    self.conn.execute(
                        "DELETE FROM mutation_queue WHERE id=? AND seq=?",
                        (item.id, item.seq),
                    )
            self._reconcile(row["id"], row["internal_date"], remote_labels)
            self._rebuild_thread(row["thread_id"])
        self.label_epoch += 1

    def complete_batch_push(self, items_by_message: dict[str, list[QueueItem]]) -> None:
        """Record label changes Gmail accepted in one batchModify call.

        A batch response carries no per-message label snapshot, so the local
        membership set when the intent was queued stays authoritative. Rows
        are held against the local history cursor until the next history page
        commits, like the per-message path, so a stale snapshot cannot flip a
        label back between the push and the next poll.
        """
        confirmed = self.history_id() or None
        with self._tx():
            threads: set[int] = set()
            for gmail_id, items in items_by_message.items():
                row = self._message_row(gmail_id)
                if row is not None:
                    threads.add(row["thread_id"])
                for item in items:
                    if confirmed:
                        self.conn.execute(
                            """
                            UPDATE mutation_queue
                            SET confirmed_history_id=?
                            WHERE id=? AND seq=?
                            """,
                            (confirmed, item.id, item.seq),
                        )
                    else:
                        self.conn.execute(
                            "DELETE FROM mutation_queue WHERE id=? AND seq=?",
                            (item.id, item.seq),
                        )
            for thread_id in threads:
                self._rebuild_thread(thread_id)
        self.label_epoch += 1

    def save_attachment(self, gmail_id: str, index: int, destination: Path) -> Path:
        """Write one saved attachment to ``destination`` and return that path."""
        row = self._message_row(gmail_id)
        if row is None or not row["body_path"]:
            raise FileNotFoundError(gmail_id)
        found = read_attachment((self.root / str(row["body_path"])).read_bytes(), index)
        if found is None:
            raise FileNotFoundError(str(index))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(found[1])
        return destination

    def message_source(self, gmail_id: str) -> bytes | None:
        """The saved RFC822 message exactly as Gmail sent it. None when not on disk."""
        if not self.body_readable(gmail_id):
            return None
        row = self._message_row(gmail_id)
        assert row is not None
        try:
            return (self.root / str(row["body_path"])).read_bytes()
        except OSError:
            return None

    def attachment_bytes(self, gmail_id: str, index: int) -> bytes | None:
        """The bytes of one saved attachment. None when the body is not on disk."""
        row = self._message_row(gmail_id)
        if row is None or not row["body_path"]:
            return None
        found = read_attachment((self.root / str(row["body_path"])).read_bytes(), index)
        if found is None:
            return None
        return found[1]

    def write_body(self, message: IncomingMessage, raw: bytes, body_text: str) -> None:
        relative = self._blob_name(message.gmail_id)
        destination = self.root / relative
        temporary = destination.with_suffix(".eml.tmp")
        temporary.write_bytes(raw)
        temporary.replace(destination)
        with self._tx():
            row = self._message_row(message.gmail_id)
            if row is None:
                thread_id = self._upsert_snapshot(message, "unchanged")
                row = self._message_row(message.gmail_id)
                assert row is not None
                display_changed = True
            else:
                thread_id = row["thread_id"]
                display_changed = self._fill_headers(row["id"], message, metadata_complete=True)
            self.conn.execute(
                """
                UPDATE messages
                SET body_state='present', body_path=?,
                    has_attachment=CASE WHEN ? IS NULL THEN has_attachment ELSE ? END
                WHERE id=?
                """,
                (
                    relative,
                    None if message.has_attachment is None else int(message.has_attachment),
                    None if message.has_attachment is None else int(message.has_attachment),
                    row["id"],
                ),
            )
            if message.label_ids:
                display_changed = (
                    self._reconcile(
                        row["id"], row["internal_date"] or message.internal_date_ms, message.label_ids
                    )
                    or display_changed
                )
            # One index update, with the body included. A second write used to
            # index the old text and then the new text.
            self._write_text(row["id"], body_text)
            if display_changed:
                self._rebuild_thread(thread_id)

    def delete_message(self, gmail_id: str) -> list[Path]:
        with self._tx():
            return self._delete_message_in_tx(gmail_id)

    def mark_body_missing(self, gmail_id: str) -> None:
        with self._tx():
            row = self._message_row(gmail_id)
            if row is None:
                raise MessageNotFound(gmail_id)
            self.conn.execute(
                """
                UPDATE messages SET body_state='missing', body_path=NULL WHERE id=?
                """,
                (row["id"],),
            )
            self._set_body_text(row["id"], "")
        path = self._blob_file(gmail_id)
        if path.exists():
            path.unlink()

    def pending_body_ids(self, open_label: str) -> list[str]:
        defer = _body_defer_sql("m", open_label)
        rows = self.conn.execute(
            f"""
            SELECT m.gmail_id
            FROM messages m
            WHERE m.body_state = 'pending_now'
              {defer}
            ORDER BY EXISTS (
                SELECT 1
                FROM message_labels ml
                JOIN labels l ON l.id = ml.label_id
                WHERE ml.message_id = m.id AND l.gmail_id = ?
            ) DESC, m.internal_date DESC, m.id DESC
            """,
            (open_label,),
        ).fetchall()
        return [row[0] for row in rows]

    def missing_body_count(self) -> int:
        defer = _body_defer_sql("m", self.open_label())
        row = self.conn.execute(
            f"""
            SELECT COUNT(*) FROM messages m
            WHERE m.body_state = 'missing'
              {defer}
            """
        ).fetchone()
        return int(row[0])

    def body_cache_counts(self) -> tuple[int, int]:
        """Bodies already saved, and every message that caching walks."""
        defer = _body_defer_sql("m", self.open_label())
        row = self.conn.execute(
            f"""
            SELECT
              COALESCE(SUM(CASE WHEN m.body_state = 'present' THEN 1 ELSE 0 END), 0),
              COUNT(*)
            FROM messages m
            WHERE 1=1
              {defer}
            """
        ).fetchone()
        return int(row[0]), int(row[1])

    def missing_body_ids(self, limit: int = 1, *, open_label: str | None = None) -> list[str]:
        """Unsaved messages: open folder, then inbox, then newest first.

        Label membership and date are both indexed. Sorting every missing row
        on each lookup walked the whole mailbox. On-demand folders wait until
        that folder is open.
        """
        if limit <= 0:
            return []
        chosen = open_label if open_label is not None else self.open_label()
        found: list[str] = []
        skip: set[str] = set()
        for label in self._priority_labels(chosen):
            defer = _body_defer_sql("m", chosen)
            rows = self.conn.execute(
                f"""
                SELECT m.gmail_id
                FROM message_labels ml
                JOIN labels l ON l.id = ml.label_id
                JOIN messages m ON m.id = ml.message_id
                WHERE l.gmail_id = ? AND m.body_state = 'missing'
                  {defer}
                ORDER BY ml.internal_date DESC, ml.message_id DESC
                LIMIT ?
                """,
                (label, limit - len(found)),
            ).fetchall()
            for row in rows:
                if row[0] in skip:
                    continue
                skip.add(row[0])
                found.append(row[0])
                if len(found) >= limit:
                    return found
        need = limit - len(found)
        defer = _body_defer_sql("m", chosen)
        rest = self.conn.execute(
            f"""
            SELECT m.gmail_id FROM messages m
            WHERE m.body_state = 'missing'
              {defer}
            ORDER BY m.internal_date DESC, m.id DESC
            LIMIT ?
            """,
            (need + len(skip),),
        ).fetchall()
        for row in rest:
            if row[0] in skip:
                continue
            found.append(row[0])
            if len(found) >= limit:
                break
        return found

    def body_readable(self, gmail_id: str) -> bool:
        row = self._message_row(gmail_id)
        if row is None or row["body_state"] != "present" or not row["body_path"]:
            return False
        return (self.root / row["body_path"]).is_file()

    def is_idle(self) -> bool:
        state = self._state()
        if state["bootstrap_phase"] != "done":
            return False
        defer = _body_defer_sql("m", self.open_label())
        pending = self.conn.execute(
            f"""
            SELECT 1 FROM messages m
            WHERE m.body_state='pending_now'
              {defer}
            LIMIT 1
            """
        ).fetchone()
        if pending is not None:
            return False
        queued = self.conn.execute(
            "SELECT 1 FROM mutation_queue WHERE confirmed_history_id IS NULL LIMIT 1"
        ).fetchone()
        return queued is None

    def message_label_keys(self, gmail_id: str) -> set[str]:
        rows = self.conn.execute(
            """
            SELECT l.gmail_id, l.name
            FROM messages m
            JOIN message_labels ml ON ml.message_id = m.id
            JOIN labels l ON l.id = ml.label_id
            WHERE m.gmail_id = ?
            """,
            (gmail_id,),
        ).fetchall()
        keys: set[str] = set()
        for row in rows:
            keys.add(row[0])
            keys.add(row[1])
            keys.add(row[0].lower())
            keys.add(row[1].lower())
        return keys

    def message_label_ids(self, gmail_id: str) -> set[str]:
        rows = self.conn.execute(
            """
            SELECT l.gmail_id
            FROM messages m
            JOIN message_labels ml ON ml.message_id = m.id
            JOIN labels l ON l.id = ml.label_id
            WHERE m.gmail_id = ?
            """,
            (gmail_id,),
        ).fetchall()
        return {row[0] for row in rows}

    def search_local(self, parsed: ParsedQuery, limit: int) -> list[SearchHit]:
        if parsed.fts is None and parsed.unread is None and parsed.starred is None and parsed.label is None:
            return []
        clauses = ["1=1"]
        params: list[object] = []
        source = "messages m"
        if parsed.fts is not None:
            source = "message_fts JOIN messages m ON m.id = message_fts.rowid"
            clauses.append("message_fts MATCH ?")
            params.append(parsed.fts)
        self._append_filters(clauses, params, parsed, "m")
        sql = f"""
            SELECT m.gmail_id, t.gmail_id AS thread_gmail_id, m.subject, m.from_addr,
                   m.snippet, m.internal_date, m.body_state,
                   (
                     SELECT group_concat(l.gmail_id, ' ')
                     FROM message_labels ml
                     JOIN labels l ON l.id = ml.label_id
                     WHERE ml.message_id = m.id
                   ) AS label_ids
            FROM {source}
            JOIN threads t ON t.id = m.thread_id
            WHERE {' AND '.join(clauses)}
            ORDER BY m.internal_date DESC, m.id DESC
            LIMIT ?
        """
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [_search_hit(row) for row in rows]

    def search_messages(
        self,
        parsed: ParsedQuery,
        limit: int = 100,
        *,
        newest_first: bool = True,
    ) -> list[MessageSummary]:
        """Messages matching a parsed query, across every label."""
        if parsed.fts is None and parsed.unread is None and parsed.starred is None and parsed.label is None:
            return []
        limit = max(1, min(limit, 200))
        clauses = ["1=1"]
        params: list[object] = []
        source = "messages m"
        if parsed.fts is not None:
            source = "message_fts JOIN messages m ON m.id = message_fts.rowid"
            clauses.append("message_fts MATCH ?")
            params.append(parsed.fts)
        self._append_filters(clauses, params, parsed, "m")
        direction = "DESC" if newest_first else "ASC"
        sql = f"""
            SELECT m.id AS message_id, m.gmail_id, m.from_addr, m.to_addr, m.subject,
                   m.snippet, m.internal_date, m.has_attachment,
                   EXISTS (
                     SELECT 1 FROM message_labels um
                     JOIN labels u ON u.id = um.label_id
                     WHERE um.message_id = m.id AND u.gmail_id = 'UNREAD'
                   ) AS unread
            FROM {source}
            WHERE {' AND '.join(clauses)}
            ORDER BY m.internal_date {direction}, m.id {direction}
            LIMIT ?
        """
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        names = self._label_names_for([row["message_id"] for row in rows])
        return [_summary(row, names) for row in rows]

    def summaries_for(self, gmail_ids: list[str]) -> list[MessageSummary]:
        if not gmail_ids:
            return []
        placeholders = ",".join("?" for _ in gmail_ids)
        rows = self.conn.execute(
            f"""
            SELECT m.id AS message_id, m.gmail_id, m.from_addr, m.to_addr, m.subject,
                   m.snippet, m.internal_date, m.has_attachment,
                   EXISTS (
                     SELECT 1 FROM message_labels um
                     JOIN labels u ON u.id = um.label_id
                     WHERE um.message_id = m.id AND u.gmail_id = 'UNREAD'
                   ) AS unread
            FROM messages m
            WHERE m.gmail_id IN ({placeholders})
            """,
            gmail_ids,
        ).fetchall()
        names = self._label_names_for([row["message_id"] for row in rows])
        by_id = {row["gmail_id"]: _summary(row, names) for row in rows}
        return [by_id[gmail_id] for gmail_id in gmail_ids if gmail_id in by_id]

    def search_hit(self, gmail_id: str) -> SearchHit | None:
        row = self.conn.execute(
            """
            SELECT m.gmail_id, t.gmail_id AS thread_gmail_id, m.subject, m.from_addr,
                   m.snippet, m.internal_date, m.body_state,
                   (
                     SELECT group_concat(l.gmail_id, ' ')
                     FROM message_labels ml
                     JOIN labels l ON l.id = ml.label_id
                     WHERE ml.message_id = m.id
                   ) AS label_ids
            FROM messages m
            JOIN threads t ON t.id = m.thread_id
            WHERE m.gmail_id = ?
            """,
            (gmail_id,),
        ).fetchone()
        if row is None:
            return None
        return _search_hit(row)

    def _preview_row(self, gmail_id: str) -> sqlite3.Row | None:
        """Everything the reader needs for one message, in one statement.

        Opening a row used to take four statements and stat the body file
        twice. Label names travel with the row so j/k and the message a
        delete reveals touch SQLite once.
        """
        return self.conn.execute(
            """
            SELECT m.gmail_id, m.subject, m.from_addr, m.to_addr, m.cc_addr,
                   m.snippet, m.internal_date, m.body_state, m.body_path,
                   m.has_attachment,
                   COALESCE(mt.body_text, '') AS body_text,
                   (
                     SELECT group_concat(l.name, char(31))
                     FROM (
                       SELECT l.name AS name
                       FROM message_labels ml
                       JOIN labels l ON l.id = ml.label_id
                       WHERE ml.message_id = m.id AND l.gmail_id != 'UNREAD'
                     ) l
                   ) AS label_names
            FROM messages m
            LEFT JOIN message_text mt ON mt.message_id = m.id
            WHERE m.gmail_id = ?
            """,
            (gmail_id,),
        ).fetchone()

    def opened_message(self, gmail_id: str) -> dict[str, object] | None:
        row = self.conn.execute(
            """
            SELECT m.gmail_id, t.gmail_id AS thread_gmail_id, m.subject, m.from_addr,
                   m.to_addr, m.cc_addr, m.message_id_header, m.snippet,
                   m.internal_date, m.body_state,
                   COALESCE(mt.body_text, '') AS body_text,
                   (
                     SELECT group_concat(l.gmail_id, ' ')
                     FROM message_labels ml
                     JOIN labels l ON l.id = ml.label_id
                     WHERE ml.message_id = m.id
                   ) AS label_ids
            FROM messages m
            JOIN threads t ON t.id = m.thread_id
            LEFT JOIN message_text mt ON mt.message_id = m.id
            WHERE m.gmail_id = ?
            """,
            (gmail_id,),
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    def message_id_for(self, gmail_id: str) -> str:
        """Message-ID header for replies; backfills from the cached .eml when needed."""
        row = self.conn.execute(
            """
            SELECT id, message_id_header, body_state, body_path, thread_id
            FROM messages WHERE gmail_id = ?
            """,
            (gmail_id,),
        ).fetchone()
        if row is None:
            return ""
        existing = (row["message_id_header"] or "").strip()
        if existing:
            return existing
        if row["thread_id"]:
            self._backfill_reply_headers(int(row["thread_id"]))
            again = self.conn.execute(
                "SELECT message_id_header FROM messages WHERE id=?",
                (row["id"],),
            ).fetchone()
            if again and (again["message_id_header"] or "").strip():
                return str(again["message_id_header"]).strip()
        if row["body_state"] != "present" or not row["body_path"]:
            return ""
        path = self.root / str(row["body_path"])
        try:
            parsed = parse_rfc822(path.read_bytes())
        except OSError:
            return ""
        token = parsed.message_id_header.strip()
        if not token:
            return ""
        with self._tx():
            self.conn.execute(
                """
                UPDATE messages SET message_id_header=?
                WHERE id=? AND message_id_header = ''
                """,
                (token, row["id"]),
            )
        return token

    def _apply_record(
        self,
        record: HistoryRecord,
        headers: dict[str, IncomingMessage | None],
    ) -> tuple[set[int], list[Path]]:
        threads: set[int] = set()
        paths: list[Path] = []
        for message in record.messages_added:
            if not message.id:
                continue
            header = headers.get(message.id)
            if header is None and message.id in headers:
                paths.extend(self._delete_message_in_tx(message.id))
                continue
            threads.add(self._insert_history_message(message, header))
        for change in record.labels_added:
            threads.add(self._apply_label_delta(change.message, change.label_ids, added=True, headers=headers))
        for change in record.labels_removed:
            threads.add(self._apply_label_delta(change.message, change.label_ids, added=False, headers=headers))
        for message in record.messages_deleted:
            paths.extend(self._delete_message_in_tx(message.id))
        return threads, paths

    def _insert_history_message(self, message, header: IncomingMessage | None) -> int:
        existing = self._message_row(message.id)
        labels = list(message.label_ids)
        if not labels and header is not None:
            labels = list(header.label_ids)
        schedule = _body_schedule(labels, self.open_label())
        if existing is None:
            snapshot = header or IncomingMessage(
                gmail_id=message.id,
                thread_id=message.thread_id or message.id,
                label_ids=labels,
            )
            snapshot.label_ids = labels
            return self._upsert_snapshot(snapshot, schedule)
        self._fill_headers(existing["id"], header, metadata_complete=header is not None)
        self._apply_delta(existing["id"], existing["internal_date"], labels, [])
        if schedule == "pending_now":
            self._mark_pending_now(existing["id"])
        self.conn.execute(
            "UPDATE messages SET resync_gen=? WHERE id=?",
            (self._state()["resync_gen"], existing["id"]),
        )
        return existing["thread_id"]

    def _apply_label_delta(self, message, label_ids: list[str], *, added: bool, headers) -> int:
        row = self._message_row(message.id)
        if row is None:
            header = headers.get(message.id)
            if header is None and message.id in headers:
                return 0
            created = header or IncomingMessage(
                gmail_id=message.id,
                thread_id=message.thread_id or message.id,
                label_ids=label_ids if added else [],
            )
            if added:
                created.label_ids = label_ids
            labels = list(created.label_ids)
            if added and "INBOX" in label_ids:
                schedule = _body_schedule(labels, self.open_label())
            else:
                schedule = "missing"
            thread_id = self._upsert_snapshot(created, schedule)
            return thread_id
        if added:
            self._apply_delta(row["id"], row["internal_date"], label_ids, [])
            if "INBOX" in label_ids and not _defers_body(label_ids, self.open_label()):
                self._mark_pending_now(row["id"])
        else:
            self._apply_delta(row["id"], row["internal_date"], [], label_ids)
        return row["thread_id"]

    def _upsert_snapshot(self, message: IncomingMessage, schedule: str) -> int:
        _check_gmail_id(message.gmail_id)
        thread_pk = self._ensure_thread(message.thread_id or message.gmail_id)
        gen = self._state()["resync_gen"]
        existing = self._message_row(message.gmail_id)
        if existing is None:
            body_state = "pending_now" if schedule == "pending_now" else "missing"
            cursor = self.conn.execute(
                """
                INSERT INTO messages (
                  thread_id, gmail_id, history_id, internal_date, snippet, subject,
                  from_addr, to_addr, cc_addr, message_id_header, in_reply_to,
                  references_header, size_estimate,
                  body_state, has_metadata, has_attachment, resync_gen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    thread_pk,
                    message.gmail_id,
                    message.history_id,
                    message.internal_date_ms,
                    message.snippet,
                    message.subject,
                    message.from_addr,
                    message.to_addr,
                    message.cc_addr,
                    message.message_id_header,
                    message.in_reply_to,
                    message.references_header,
                    message.size_estimate,
                    body_state,
                    1 if message.has_attachment else 0,
                    gen,
                ),
            )
            message_pk = int(cursor.lastrowid)
        else:
            message_pk = existing["id"]
            if existing["thread_id"] != thread_pk:
                thread_pk = existing["thread_id"]
            self._fill_headers(message_pk, message, metadata_complete=True)
            self.conn.execute("UPDATE messages SET resync_gen=? WHERE id=?", (gen, message_pk))
            if schedule == "pending_now" and existing["body_state"] != "present":
                self._mark_pending_now(message_pk)
        internal_date = message.internal_date_ms or (
            existing["internal_date"] if existing is not None else 0
        )
        self._reconcile(message_pk, internal_date, message.label_ids)
        self._write_text(message_pk)
        self._set_attachment(message_pk, message.has_attachment)
        return thread_pk

    def _fill_headers(self, message_pk: int, message: IncomingMessage | None, *, metadata_complete: bool) -> bool:
        """Store headers. True when the thread row's subject, sender, or date changed."""
        if message is None:
            return False
        current = self.conn.execute("SELECT * FROM messages WHERE id=?", (message_pk,)).fetchone()
        if current is None:
            return False
        display_changed = bool(
            (message.snippet and message.snippet != current["snippet"])
            or (message.subject and message.subject != current["subject"])
            or (message.from_addr and message.from_addr != current["from_addr"])
            or (message.internal_date_ms > 0 and message.internal_date_ms != current["internal_date"])
        )
        self.conn.execute(
            """
            UPDATE messages SET
              history_id=COALESCE(?, history_id),
              internal_date=CASE WHEN ? > 0 THEN ? ELSE internal_date END,
              snippet=CASE WHEN ? != '' THEN ? ELSE snippet END,
              subject=CASE WHEN ? != '' THEN ? ELSE subject END,
              from_addr=CASE WHEN ? != '' THEN ? ELSE from_addr END,
              to_addr=CASE WHEN ? != '' THEN ? ELSE to_addr END,
              cc_addr=CASE WHEN ? != '' THEN ? ELSE cc_addr END,
              message_id_header=CASE WHEN ? != '' THEN ? ELSE message_id_header END,
              in_reply_to=CASE WHEN ? != '' THEN ? ELSE in_reply_to END,
              references_header=CASE WHEN ? != '' THEN ? ELSE references_header END,
              size_estimate=COALESCE(?, size_estimate),
              has_metadata=CASE WHEN ? THEN 1 ELSE has_metadata END,
              has_attachment=CASE WHEN ? IS NULL THEN has_attachment ELSE ? END
            WHERE id=?
            """,
            (
                message.history_id,
                message.internal_date_ms,
                message.internal_date_ms,
                message.snippet,
                message.snippet,
                message.subject,
                message.subject,
                message.from_addr,
                message.from_addr,
                message.to_addr,
                message.to_addr,
                message.cc_addr,
                message.cc_addr,
                message.message_id_header,
                message.message_id_header,
                message.in_reply_to,
                message.in_reply_to,
                message.references_header,
                message.references_header,
                message.size_estimate,
                1 if metadata_complete else 0,
                None if message.has_attachment is None else int(message.has_attachment),
                None if message.has_attachment is None else int(message.has_attachment),
                message_pk,
            ),
        )
        return display_changed

    def _reconcile(self, message_pk: int, internal_date: int, remote_labels: list[str]) -> bool:
        pending = self._pending_map(message_pk)
        desired = set(remote_labels)
        for label, op in pending.items():
            if op == "add":
                desired.add(label)
            else:
                desired.discard(label)
        current = self._membership(message_pk)
        changed = False
        for label in desired - current:
            self._set_membership(message_pk, label, internal_date, present=True)
            changed = True
        for label in current - desired:
            self._set_membership(message_pk, label, internal_date, present=False)
            changed = True
        return changed

    def _apply_delta(self, message_pk: int, internal_date: int, added: list[str], removed: list[str]) -> None:
        pending = self._pending_map(message_pk)
        for label in added:
            if pending.get(label) == "remove":
                continue
            self._set_membership(message_pk, label, internal_date, present=True)
        for label in removed:
            if pending.get(label) == "add":
                continue
            self._set_membership(message_pk, label, internal_date, present=False)

    def _set_membership(self, message_pk: int, label: str, internal_date: int, *, present: bool) -> None:
        label_pk = self._ensure_label(label)
        if present:
            self.conn.execute(
                """
                INSERT INTO message_labels (message_id, label_id, internal_date)
                VALUES (?, ?, ?)
                ON CONFLICT(message_id, label_id) DO UPDATE SET internal_date=excluded.internal_date
                """,
                (message_pk, label_pk, internal_date),
            )
        else:
            self.conn.execute(
                "DELETE FROM message_labels WHERE message_id=? AND label_id=?",
                (message_pk, label_pk),
            )

    def _mark_pending_now(self, message_pk: int) -> None:
        self.conn.execute(
            """
            UPDATE messages
            SET body_state='pending_now'
            WHERE id=? AND body_state != 'present'
            """,
            (message_pk,),
        )

    def _queue_op(self, message_pk: int, label: str, op: str) -> None:
        seq = self.conn.execute(
            "UPDATE sync_state SET queue_seq = queue_seq + 1 WHERE id=1 RETURNING queue_seq"
        ).fetchone()[0]
        self.conn.execute(
            """
            INSERT INTO mutation_queue (message_id, label_gmail_id, op, seq)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(message_id, label_gmail_id) DO UPDATE SET op=excluded.op, seq=excluded.seq
            """,
            (message_pk, label, op, seq),
        )

    def _pending_map(self, message_pk: int) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT label_gmail_id, op FROM mutation_queue WHERE message_id=?",
            (message_pk,),
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def _membership(self, message_pk: int) -> set[str]:
        rows = self.conn.execute(
            """
            SELECT l.gmail_id
            FROM message_labels ml
            JOIN labels l ON l.id = ml.label_id
            WHERE ml.message_id=?
            """,
            (message_pk,),
        ).fetchall()
        return {row[0] for row in rows}

    def _write_text(self, message_pk: int, body_text: str | None = None) -> None:
        row = self.conn.execute("SELECT * FROM messages WHERE id=?", (message_pk,)).fetchone()
        if row is None:
            return
        existing = self.conn.execute(
            "SELECT body_text FROM message_text WHERE message_id=?",
            (message_pk,),
        ).fetchone()
        if body_text is None:
            body = existing["body_text"] if existing is not None else ""
        else:
            body = body_text
        values = (
            row["subject"],
            row["from_addr"],
            row["to_addr"],
            row["cc_addr"],
            row["snippet"],
            body,
            message_pk,
        )
        if existing is None:
            self.conn.execute(
                """
                INSERT INTO message_text (
                  subject, from_addr, to_addr, cc_addr, snippet, body_text, message_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        else:
            self.conn.execute(
                """
                UPDATE message_text
                SET subject=?, from_addr=?, to_addr=?, cc_addr=?, snippet=?, body_text=?
                WHERE message_id=?
                """,
                values,
            )

    def _set_body_text(self, message_pk: int, body_text: str) -> None:
        self.conn.execute(
            "UPDATE message_text SET body_text=? WHERE message_id=?",
            (body_text, message_pk),
        )

    def _set_attachment(self, message_pk: int, has_attachment: bool | None) -> None:
        if has_attachment is None:
            return
        self.conn.execute(
            "UPDATE messages SET has_attachment=? WHERE id=?",
            (1 if has_attachment else 0, message_pk),
        )

    def _label_names_for(self, message_ids: list[int]) -> dict[int, tuple[str, ...]]:
        if not message_ids:
            return {}
        placeholders = ",".join("?" for _ in message_ids)
        rows = self.conn.execute(
            f"""
            SELECT ml.message_id, l.name
            FROM message_labels ml
            JOIN labels l ON l.id = ml.label_id
            WHERE ml.message_id IN ({placeholders}) AND l.gmail_id != 'UNREAD'
            ORDER BY l.name
            """,
            message_ids,
        ).fetchall()
        grouped: dict[int, list[str]] = {}
        for row in rows:
            grouped.setdefault(row["message_id"], []).append(row["name"])
        return {message_id: tuple(names) for message_id, names in grouped.items()}

    def _rebuild_thread(self, thread_id: int) -> None:
        count = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE thread_id=?",
            (thread_id,),
        ).fetchone()[0]
        if count == 0:
            self.conn.execute("DELETE FROM threads WHERE id=?", (thread_id,))
            return
        latest = self.conn.execute(
            """
            SELECT * FROM messages
            WHERE thread_id=?
            ORDER BY internal_date DESC, id DESC
            LIMIT 1
            """,
            (thread_id,),
        ).fetchone()
        unread = self.conn.execute(
            """
            SELECT COUNT(*) FROM messages m
            JOIN message_labels ml ON ml.message_id = m.id
            JOIN labels l ON l.id = ml.label_id
            WHERE m.thread_id=? AND l.gmail_id='UNREAD'
            """,
            (thread_id,),
        ).fetchone()[0]
        starred = self.conn.execute(
            """
            SELECT COUNT(*) FROM messages m
            JOIN message_labels ml ON ml.message_id = m.id
            JOIN labels l ON l.id = ml.label_id
            WHERE m.thread_id=? AND l.gmail_id='STARRED'
            """,
            (thread_id,),
        ).fetchone()[0]
        label_rows = self.conn.execute(
            """
            SELECT l.gmail_id
            FROM message_labels ml
            JOIN labels l ON l.id = ml.label_id
            WHERE ml.message_id=?
            ORDER BY l.gmail_id
            """,
            (latest["id"],),
        ).fetchall()
        self.conn.execute(
            """
            UPDATE threads SET
              snippet=?, subject=?, from_addr=?, latest_internal_date=?,
              latest_message_gmail_id=?, unread_count=?, starred=?,
              message_count=?, label_ids_csv=?
            WHERE id=?
            """,
            (
                latest["snippet"],
                latest["subject"],
                latest["from_addr"],
                latest["internal_date"],
                latest["gmail_id"],
                unread,
                1 if starred else 0,
                count,
                " ".join(row[0] for row in label_rows),
                thread_id,
            ),
        )
        self.conn.execute("DELETE FROM thread_labels WHERE thread_id=?", (thread_id,))
        self.conn.execute(
            """
            INSERT INTO thread_labels (thread_id, label_id, latest_internal_date)
            SELECT ?, ml.label_id, MAX(m.internal_date)
            FROM messages m
            JOIN message_labels ml ON ml.message_id = m.id
            WHERE m.thread_id = ?
            GROUP BY ml.label_id
            """,
            (thread_id, thread_id),
        )

    def _delete_message_in_tx(self, gmail_id: str) -> list[Path]:
        row = self._message_row(gmail_id)
        if row is None:
            return []
        path = self.root / row["body_path"] if row["body_path"] else None
        thread_id = row["thread_id"]
        self.conn.execute("DELETE FROM message_text WHERE message_id=?", (row["id"],))
        self.conn.execute("DELETE FROM mutation_queue WHERE message_id=?", (row["id"],))
        self.conn.execute("DELETE FROM message_labels WHERE message_id=?", (row["id"],))
        self.conn.execute("DELETE FROM messages WHERE id=?", (row["id"],))
        self._rebuild_thread(thread_id)
        return [path] if path is not None else []

    def _ensure_thread(self, gmail_thread_id: str) -> int:
        _check_gmail_id(gmail_thread_id)
        row = self.conn.execute("SELECT id FROM threads WHERE gmail_id=?", (gmail_thread_id,)).fetchone()
        if row is not None:
            return int(row[0])
        cursor = self.conn.execute("INSERT INTO threads (gmail_id) VALUES (?)", (gmail_thread_id,))
        return int(cursor.lastrowid)

    def create_user_label(self, name: str) -> str:
        """Insert a user label that Gmail has not assigned an id to yet.

        The returned id is local. The next push creates the label on Gmail
        and rewrites the queue to the id Gmail returns.
        """
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("label name is empty")
        existing = self._label_by_name(cleaned)
        if existing is not None:
            return existing
        local_id = "local" + secrets.token_hex(8)
        with self._tx():
            self.conn.execute(
                "INSERT INTO labels (gmail_id, name, type) VALUES (?, ?, 'user')",
                (local_id, cleaned),
            )
            self.conn.execute(
                "INSERT INTO label_creates (local_id, name) VALUES (?, ?)",
                (local_id, cleaned),
            )
        return local_id

    def pending_label_name(self, local_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT name FROM label_creates WHERE local_id=?",
            (local_id,),
        ).fetchone()
        return str(row[0]) if row is not None else None

    def adopt_label_id(self, local_id: str, gmail_id: str, name: str) -> None:
        """Replace a provisional label id with the id Gmail assigned."""
        _check_gmail_id(gmail_id)
        with self._tx():
            if local_id != gmail_id:
                local = self.conn.execute(
                    "SELECT id FROM labels WHERE gmail_id=?",
                    (local_id,),
                ).fetchone()
                existing = self.conn.execute(
                    "SELECT id FROM labels WHERE gmail_id=?",
                    (gmail_id,),
                ).fetchone()
                if local is not None and existing is not None and int(existing[0]) != int(local[0]):
                    self._merge_label(int(local[0]), int(existing[0]))
                    self.conn.execute(
                        "UPDATE labels SET name=?, type='user' WHERE id=?",
                        (name, int(existing[0])),
                    )
                elif local is not None:
                    self.conn.execute(
                        "UPDATE labels SET gmail_id=?, name=?, type='user' WHERE id=?",
                        (gmail_id, name, int(local[0])),
                    )
                self.conn.execute(
                    """
                    UPDATE OR IGNORE mutation_queue
                    SET label_gmail_id=?
                    WHERE label_gmail_id=?
                    """,
                    (gmail_id, local_id),
                )
                self.conn.execute(
                    "DELETE FROM mutation_queue WHERE label_gmail_id=?",
                    (local_id,),
                )
            self.conn.execute("DELETE FROM label_creates WHERE local_id=?", (local_id,))
            thread_ids = [
                int(row[0])
                for row in self.conn.execute(
                    """
                    SELECT DISTINCT m.thread_id
                    FROM message_labels ml
                    JOIN messages m ON m.id = ml.message_id
                    JOIN labels l ON l.id = ml.label_id
                    WHERE l.gmail_id=?
                    """,
                    (gmail_id,),
                )
            ]
            for thread_id in thread_ids:
                self._rebuild_thread(thread_id)

    def _pending_label_ids(self) -> list[str]:
        return [str(row[0]) for row in self.conn.execute("SELECT local_id FROM label_creates")]

    def _label_by_name(self, name: str) -> str | None:
        needle = name.casefold()
        for row in self.conn.execute("SELECT gmail_id, name FROM labels"):
            if str(row["name"]).casefold() == needle or str(row["gmail_id"]).casefold() == needle:
                return str(row["gmail_id"])
        return None

    def _merge_label(self, local_pk: int, existing_pk: int) -> None:
        self.conn.execute(
            "UPDATE OR IGNORE message_labels SET label_id=? WHERE label_id=?",
            (existing_pk, local_pk),
        )
        self.conn.execute("DELETE FROM message_labels WHERE label_id=?", (local_pk,))
        self.conn.execute(
            "UPDATE OR IGNORE thread_labels SET label_id=? WHERE label_id=?",
            (existing_pk, local_pk),
        )
        self.conn.execute("DELETE FROM thread_labels WHERE label_id=?", (local_pk,))
        self.conn.execute("DELETE FROM labels WHERE id=?", (local_pk,))

    def _ensure_label(self, gmail_id: str) -> int:
        row = self.conn.execute("SELECT id FROM labels WHERE gmail_id=?", (gmail_id,)).fetchone()
        if row is not None:
            return int(row[0])
        cursor = self.conn.execute(
            "INSERT INTO labels (gmail_id, name, type) VALUES (?, ?, '')",
            (gmail_id, gmail_id),
        )
        return int(cursor.lastrowid)

    def _label_pk_read(self, gmail_id: str) -> int | None:
        row = self.conn.execute("SELECT id FROM labels WHERE gmail_id=?", (gmail_id,)).fetchone()
        if row is not None:
            return int(row[0])
        row = self.conn.execute(
            "SELECT id FROM labels WHERE lower(name)=lower(?)",
            (gmail_id,),
        ).fetchone()
        return int(row[0]) if row is not None else None

    def _message_row(self, gmail_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM messages WHERE gmail_id=?", (gmail_id,)).fetchone()

    def _thread_exists(self, thread_id: int) -> bool:
        if thread_id == 0:
            return False
        row = self.conn.execute("SELECT 1 FROM threads WHERE id=?", (thread_id,)).fetchone()
        return row is not None

    def _note_history_resume(self, start: str, page_token: str | None, history_id: str | None) -> None:
        if history_id:
            self.conn.execute(
                """
                UPDATE sync_state
                SET history_id=?, history_page_token=NULL, history_page_start=NULL
                WHERE id=1
                """,
                (history_id,),
            )
            self._drop_confirmed_queue(history_id)
        else:
            self.conn.execute(
                """
                UPDATE sync_state
                SET history_page_start=?, history_page_token=?
                WHERE id=1
                """,
                (start, page_token),
            )

    def _drop_confirmed_queue(self, history_id: str) -> None:
        rows = self.conn.execute(
            """
            SELECT id, confirmed_history_id
            FROM mutation_queue
            WHERE confirmed_history_id IS NOT NULL
            """
        ).fetchall()
        for row in rows:
            if _history_reached(history_id, row["confirmed_history_id"]):
                self.conn.execute("DELETE FROM mutation_queue WHERE id=?", (row["id"],))

    def clear_history_page(self) -> None:
        """Drop an in-flight history page so the next pull starts from the committed cursor."""
        with self._tx():
            self.conn.execute(
                """
                UPDATE sync_state
                SET history_page_token=NULL, history_page_start=NULL
                WHERE id=1
                """
            )

    def history_resume(self) -> tuple[str | None, str | None]:
        state = self._state()
        return state["history_page_start"], state["history_page_token"]

    def _state(self) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM sync_state WHERE id=1").fetchone()
        if row is None:
            raise RuntimeError("sync_state row is missing")
        return row

    def _append_filters(
        self,
        clauses: list[str],
        params: list[object],
        parsed: ParsedQuery,
        alias: str,
    ) -> None:
        if parsed.label:
            clauses.append(
                f"""
                EXISTS (
                  SELECT 1 FROM message_labels ml
                  JOIN labels l ON l.id = ml.label_id
                  WHERE ml.message_id = {alias}.id
                    AND (l.gmail_id = ? OR lower(l.name) = lower(?))
                )
                """
            )
            params.extend([parsed.label, parsed.label])
        if parsed.unread is True:
            clauses.append(self._label_exists(alias, "UNREAD"))
        elif parsed.unread is False:
            clauses.append("NOT " + self._label_exists(alias, "UNREAD"))
        if parsed.starred is True:
            clauses.append(self._label_exists(alias, "STARRED"))
        elif parsed.starred is False:
            clauses.append("NOT " + self._label_exists(alias, "STARRED"))
        if (parsed.label or "").upper() != "TRASH":
            clauses.append("NOT " + self._label_exists(alias, "TRASH"))

    @staticmethod
    def _label_exists(alias: str, gmail_id: str) -> str:
        return f"""
            EXISTS (
              SELECT 1 FROM message_labels ml
              JOIN labels l ON l.id = ml.label_id
              WHERE ml.message_id = {alias}.id AND l.gmail_id = '{gmail_id}'
            )
        """

    def _blob_name(self, gmail_id: str) -> str:
        _check_gmail_id(gmail_id)
        return f"blobs/{gmail_id}.eml"

    def _blob_file(self, gmail_id: str) -> Path:
        return self.root / self._blob_name(gmail_id)


def _history_reached(cursor: str, confirmed: str) -> bool:
    try:
        return int(cursor) >= int(confirmed)
    except ValueError:
        return cursor == confirmed


def _defers_body(labels: list[str] | set[str] | tuple[str, ...], open_label: str | None) -> bool:
    """True when on-demand folder bodies should wait for that folder (or an open)."""
    deferred = _ON_DEMAND_LABELS.intersection(labels)
    if not deferred:
        return False
    return (open_label or "") not in deferred


def _body_schedule(labels: list[str], open_label: str | None) -> str:
    return "missing" if _defers_body(labels, open_label) else "pending_now"


def _body_defer_sql(alias: str, open_label: str | None) -> str:
    """SQL that skips on-demand folders unless that folder is open."""
    allowed = (open_label or "").upper()
    blocked = sorted(label for label in _ON_DEMAND_LABELS if label != allowed)
    if not blocked:
        return ""
    labels = ", ".join("'" + label.replace("'", "''") + "'" for label in blocked)
    return f"""
    AND NOT EXISTS (
      SELECT 1 FROM message_labels ml
      JOIN labels l ON l.id = ml.label_id
      WHERE ml.message_id = {alias}.id AND l.gmail_id IN ({labels})
    )
    """


def _check_gmail_id(gmail_id: str) -> None:
    if not gmail_id or _GMAIL_ID.fullmatch(gmail_id) is None:
        raise ValueError(f"refusing message id {gmail_id!r}")


def _thread_summary(row: sqlite3.Row) -> ThreadSummary:
    csv = row["label_ids_csv"] or ""
    return ThreadSummary(
        thread_id=row["thread_id"],
        gmail_thread_id=row["gmail_id"],
        latest_message_gmail_id=row["latest_message_gmail_id"],
        subject=row["subject"],
        from_addr=row["from_addr"],
        snippet=row["snippet"],
        latest_internal_date_ms=row["latest_internal_date"],
        unread_count=row["unread_count"],
        starred=bool(row["starred"]),
        message_count=row["message_count"],
        label_ids=tuple(part for part in csv.split(" ") if part),
    )


def _member_summary(item: dict[str, object], names: dict[int, tuple[str, ...]]) -> MessageSummary:
    message_id = int(item["id"])
    return MessageSummary(
        message_id=message_id,
        gmail_id=str(item["gmail_id"]),
        from_addr=str(item["from_addr"]),
        to_addr=str(item["to_addr"]),
        subject=str(item["subject"]),
        snippet=str(item["snippet"]),
        internal_date_ms=int(item["internal_date"]),
        label_names=names.get(message_id, ()),
        has_attachment=bool(item["has_attachment"]),
        unread=bool(item["unread"]),
        thread_id=int(item["thread_id"]),
        message_id_header=str(item["message_id_header"]),
        in_reply_to=str(item["in_reply_to"]),
        references_header=str(item["references_header"]),
        row_key=str(item["gmail_id"]),
    )


def _summary(row: sqlite3.Row, names: dict[int, tuple[str, ...]]) -> MessageSummary:
    return MessageSummary(
        message_id=row["message_id"],
        gmail_id=row["gmail_id"],
        from_addr=row["from_addr"],
        to_addr=row["to_addr"],
        subject=row["subject"],
        snippet=row["snippet"],
        internal_date_ms=row["internal_date"],
        label_names=names.get(row["message_id"], ()),
        has_attachment=bool(row["has_attachment"]),
        unread=bool(row["unread"]),
    )


def _search_hit(row: sqlite3.Row) -> SearchHit:
    csv = row["label_ids"] or ""
    return SearchHit(
        gmail_id=row["gmail_id"],
        thread_gmail_id=row["thread_gmail_id"],
        subject=row["subject"],
        from_addr=row["from_addr"],
        snippet=row["snippet"],
        internal_date_ms=row["internal_date"],
        label_ids=tuple(part for part in str(csv).split(" ") if part),
        body_cached=row["body_state"] == "present",
    )

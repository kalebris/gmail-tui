"""Local index: WAL, keyset list, FTS5, and the mutation queue."""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from gmail_tui.gmail import IncomingMessage, Label
from gmail_tui.search import parse_query
from gmail_tui.store import Store, ThreadCursor


def _message(
    gmail_id: str,
    thread_id: str,
    date: int,
    subject: str,
    labels: list[str],
    sender: str = "Ada <ada@example.com>",
) -> IncomingMessage:
    return IncomingMessage(
        gmail_id=gmail_id,
        thread_id=thread_id,
        label_ids=labels,
        internal_date_ms=date,
        subject=subject,
        from_addr=sender,
        to_addr="me@gmail.com",
        snippet=subject,
    )


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(self.tmp.name)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_wal_and_private_layout(self) -> None:
        mode = self.store.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")
        root = Path(self.tmp.name)
        self.assertTrue((root / "index.sqlite").is_file())
        self.assertTrue((root / "blobs").is_dir())
        self.assertFalse((root / "cur").exists())
        self.assertFalse((root / "new").exists())

    def test_keyset_pagination_uses_label_index(self) -> None:
        self.store.upsert_metadata(_message("m1", "t1", 1000, "old", ["INBOX"]), "missing")
        self.store.upsert_metadata(_message("m2", "t2", 3000, "new", ["INBOX", "UNREAD"]), "missing")
        self.store.upsert_metadata(_message("m3", "t3", 2000, "mid", ["INBOX"]), "missing")
        self.store.upsert_metadata(_message("m4", "t4", 4000, "other", ["Label_9"]), "missing")

        plan = self.store.explain_list("INBOX")
        self.assertIn("thread_labels_list", plan)

        first = self.store.list_threads("INBOX", limit=2)
        self.assertEqual([row.subject for row in first], ["new", "mid"])
        self.assertIsInstance(first[0].thread_id, int)
        self.assertIsInstance(first[0].gmail_thread_id, str)
        self.assertTrue(first[0].unread_count)

        cursor = ThreadCursor(first[-1].latest_internal_date_ms, first[-1].thread_id)
        second = self.store.list_threads("INBOX", cursor, limit=2)
        self.assertEqual([row.subject for row in second], ["old"])
        paged = self.store.explain_list("INBOX", cursor)
        self.assertIn("thread_labels_list", paged)

    def test_the_open_folder_uses_the_label_index(self) -> None:
        self.store.upsert_metadata(_message("m1", "t1", 1000, "old", ["INBOX"]), "missing")
        self.store.upsert_metadata(_message("m2", "t2", 3000, "new", ["INBOX"]), "missing")
        plan = self.store.explain_mail_threads("INBOX")
        self.assertIn("thread_labels_list", plan)
        self.assertNotIn("MAX", plan)
        page = self.store.list_mail_threads("INBOX", limit=1)
        self.assertEqual(page[0].subject, "new")
        cursor = ThreadCursor(page[0].sort_ms, page[0].thread_id)
        rest = self.store.list_mail_threads("INBOX", cursor, limit=1)
        self.assertEqual(rest[0].subject, "old")
        whole = self.store.explain_mail_threads(None)
        self.assertIn("threads_recent", whole)

    def test_equal_dates_are_ordered_by_thread_id(self) -> None:
        self.store.upsert_metadata(_message("m1", "t1", 5000, "first", ["INBOX"]), "missing")
        self.store.upsert_metadata(_message("m2", "t2", 5000, "second", ["INBOX"]), "missing")
        page = self.store.list_threads("INBOX", limit=1)
        self.assertEqual(page[0].subject, "second")
        cursor = ThreadCursor(page[0].latest_internal_date_ms, page[0].thread_id)
        rest = self.store.list_threads("INBOX", cursor, limit=1)
        self.assertEqual(rest[0].subject, "first")

    def test_missing_bodies_come_from_the_inbox_before_newer_mail(self) -> None:
        self.store.replace_labels(
            [
                Label("INBOX", "INBOX", "system"),
                Label("Label_1", "Work", "user"),
            ]
        )
        self.store.upsert_metadata(_message("old", "t1", 1_000, "old inbox", ["INBOX"]), "missing")
        self.store.upsert_metadata(_message("new", "t2", 9_000, "new archive", ["Label_1"]), "missing")
        self.assertEqual(self.store.missing_body_ids(1), ["old"])
        self.assertEqual(self.store.missing_body_ids(2), ["old", "new"])

    def test_contact_addresses_come_from_from_and_to(self) -> None:
        self.store.set_profile("me@example.com", "1", 2)
        self.store.upsert_metadata(
            _message("in", "t1", 2_000, "hello", ["INBOX"], sender="Ada <ada@example.com>"),
            "present",
        )
        self.store.conn.execute(
            "UPDATE messages SET to_addr=? WHERE gmail_id=?",
            ("Me <me@example.com>, Bea <bea@example.com>", "in"),
        )
        self.store.upsert_metadata(
            _message("out", "t2", 3_000, "sent", ["SENT"], sender="me@example.com"),
            "present",
        )
        self.store.conn.execute(
            "UPDATE messages SET to_addr=? WHERE gmail_id=?",
            ("Cy <cy@example.com>", "out"),
        )
        self.assertEqual(
            self.store.contact_addresses(),
            ["ada@example.com", "bea@example.com", "cy@example.com"],
        )
        self.assertEqual(self.store.contact_addresses(), ["ada@example.com", "bea@example.com", "cy@example.com"])

    def test_a_locked_store_does_not_fail_thread_header_backfill(self) -> None:
        message = _message("m1", "t1", 1_000, "Hi", ["INBOX"])
        self.store.upsert_metadata(message, "missing")
        self.store.write_body(
            message,
            b"Subject: Hi\r\nMessage-ID: <m@x>\r\nIn-Reply-To: <p@x>\r\nReferences: <p@x>\r\n\r\nbody\r\n",
            "body",
        )
        self.store.conn.execute(
            "UPDATE messages SET in_reply_to='', references_header='', message_id_header=''"
        )
        thread_id = int(self.store.conn.execute("SELECT thread_id FROM messages").fetchone()[0])
        other = sqlite3.connect(Path(self.tmp.name) / "index.sqlite")
        other.execute("BEGIN IMMEDIATE")
        try:
            started = time.monotonic()
            shown = self.store.messages_in_thread(thread_id)
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual([item.gmail_id for item in shown], ["m1"])
            stored = self.store.conn.execute("SELECT in_reply_to FROM messages").fetchone()[0]
            self.assertEqual(stored, "")
        finally:
            other.execute("ROLLBACK")
            other.close()
        self.store.messages_in_thread(thread_id)
        stored = self.store.conn.execute("SELECT in_reply_to FROM messages").fetchone()[0]
        self.assertEqual(stored, "<p@x>")

    def test_a_locked_store_does_not_fail_the_open_folder_write(self) -> None:
        other = sqlite3.connect(Path(self.tmp.name) / "index.sqlite")
        other.execute("BEGIN IMMEDIATE")
        try:
            started = time.monotonic()
            self.store.set_open_label("INBOX")
            self.assertLess(time.monotonic() - started, 0.1)
            self.store.set_open_label("Label_9")
            self.assertEqual(self.store.open_label(), "INBOX")
        finally:
            other.execute("ROLLBACK")
            other.close()
        self.store.set_open_label("Label_9")
        self.assertEqual(self.store.open_label(), "Label_9")

    def test_open_folder_outranks_inbox_then_the_rest(self) -> None:
        self.store.replace_labels(
            [
                Label("INBOX", "INBOX", "system"),
                Label("Label_1", "Work", "user"),
                Label("Label_2", "Other", "user"),
            ]
        )
        self.store.upsert_metadata(_message("inbox", "t1", 5_000, "inbox", ["INBOX"]), "missing")
        self.store.upsert_metadata(_message("work", "t2", 1_000, "work", ["Label_1"]), "missing")
        self.store.upsert_metadata(_message("other", "t3", 9_000, "other", ["Label_2"]), "missing")
        self.store.set_open_label("Label_1")
        self.assertEqual(self.store.next_priority_bodies("Label_1", 3), ["work", "inbox"])
        self.assertEqual(
            self.store.missing_body_ids(3, open_label="Label_1"),
            ["work", "inbox", "other"],
        )
        self.assertEqual(self.store.missing_body_ids(1, open_label="Label_1"), ["work"])
        self.assertEqual(self.store.next_priority_bodies("INBOX", 2), ["inbox"])

    def test_trash_bodies_wait_until_trash_is_open(self) -> None:
        from gmail_tui.store import configure_on_demand

        configure_on_demand(("TRASH", "SPAM"))
        try:
            self.store.replace_labels(
                [
                    Label("INBOX", "INBOX", "system"),
                    Label("TRASH", "TRASH", "system"),
                    Label("SPAM", "SPAM", "system"),
                    Label("Label_1", "Work", "user"),
                ]
            )
            self.store.upsert_metadata(_message("keep", "t1", 5_000, "keep", ["INBOX"]), "missing")
            self.store.upsert_metadata(_message("bin", "t2", 9_000, "trashed", ["TRASH"]), "missing")
            self.store.upsert_metadata(_message("junk", "t3", 8_000, "spam", ["SPAM"]), "missing")
            self.store.set_open_label("INBOX")
            self.assertEqual(self.store.missing_body_ids(5), ["keep"])
            self.assertEqual(self.store.next_priority_bodies("INBOX", 5), ["keep"])
            self.assertEqual(self.store.missing_body_count(), 1)
            self.store.set_open_label("TRASH")
            self.assertEqual(self.store.next_priority_bodies("TRASH", 5)[0], "bin")
            self.assertEqual(self.store.missing_body_ids(5, open_label="TRASH"), ["bin", "keep"])
            self.assertNotIn("junk", self.store.missing_body_ids(5, open_label="TRASH"))
        finally:
            configure_on_demand(("TRASH", "SPAM"))

    def test_local_label_change_is_visible_and_queued(self) -> None:
        self.store.upsert_metadata(_message("m1", "t1", 1000, "hello", ["INBOX", "UNREAD"]), "missing")
        self.store.apply_local_labels("m1", add=["STARRED"], remove=["INBOX"])
        self.assertEqual(self.store.message_label_ids("m1"), {"UNREAD", "STARRED"})
        self.assertEqual(self.store.list_threads("INBOX"), [])
        starred = self.store.list_threads("STARRED")
        self.assertEqual(len(starred), 1)
        self.assertTrue(starred[0].starred)
        self.assertIn(("m1", "STARRED", "add"), self.store.pending_mutations())
        self.assertIn(("m1", "INBOX", "remove"), self.store.pending_mutations())

    def test_a_batched_push_holds_rows_until_history_catches_up(self) -> None:
        self.store.set_history_id("500")
        for index in range(3):
            self.store.upsert_metadata(
                _message(f"m{index}", f"t{index}", 1_000 + index, f"mail {index}", ["INBOX"]),
                "missing",
            )
        self.store.apply_local_labels_batch(
            [(f"m{index}", ["TRASH"], ["INBOX"]) for index in range(3)]
        )
        groups = self.store.queue_groups()
        self.assertEqual(sorted(groups), ["m0", "m1", "m2"])
        self.store.complete_batch_push(groups)
        self.assertEqual(self.store.queue_groups(), {})
        self.assertFalse(self.store.has_pending_mutations())
        # Held, not lost: the change must outlive a history page that was
        # written before Gmail applied it.
        self.assertEqual(len(self.store.held_changes()), 6)
        self.store.apply_history_page(
            [],
            {},
            start="500",
            next_page_token=None,
            finished_history_id="520",
        )
        self.assertEqual(self.store.held_changes(), [])
        for index in range(3):
            self.assertEqual(self.store.message_label_ids(f"m{index}"), {"TRASH"})

    def test_a_batched_push_without_a_cursor_drops_its_rows(self) -> None:
        self.store.upsert_metadata(_message("m1", "t1", 1_000, "mail", ["INBOX"]), "missing")
        self.store.apply_local_labels_batch([("m1", ["TRASH"], ["INBOX"])])
        self.store.complete_batch_push(self.store.queue_groups())
        self.assertEqual(self.store.queue_groups(), {})
        self.assertEqual(self.store.held_changes(), [])

    def test_fts_indexes_headers_then_body_and_drops_on_delete(self) -> None:
        self.store.upsert_metadata(_message("m1", "t1", 1000, "Hello there", ["INBOX"]), "missing")
        parsed = parse_query("Hello")
        self.assertEqual([hit.gmail_id for hit in self.store.search_local(parsed, 10)], ["m1"])
        self.store.write_body(
            _message("m1", "t1", 1000, "Hello there", ["INBOX"]),
            b"Subject: Hello there\r\n\r\nplatypus lives here\r\n",
            "platypus lives here",
        )
        body_hits = self.store.search_local(parse_query("platypus"), 10)
        self.assertEqual([hit.gmail_id for hit in body_hits], ["m1"])
        self.assertTrue(body_hits[0].body_cached)
        self.assertTrue((Path(self.tmp.name) / "blobs" / "m1.eml").is_file())
        self.store.delete_message("m1")
        self.assertEqual(self.store.search_local(parse_query("platypus"), 10), [])
        self.assertEqual(self.store.search_local(parse_query("Hello"), 10), [])

    def test_transaction_rolls_back_the_history_cursor(self) -> None:
        before = self.store.history_id()
        with self.assertRaises(RuntimeError):
            with self.store._tx():
                self.store.conn.execute("UPDATE sync_state SET history_id='999' WHERE id=1")
                raise RuntimeError("boom")
        self.assertEqual(self.store.history_id(), before)

    def test_set_profile_refuses_a_different_address(self) -> None:
        self.store.set_email("bela.toros@gmail.com")
        with self.assertRaises(ValueError):
            self.store.set_profile("kalebsys@gmail.com", "50", 10)
        self.assertEqual(self.store.account_email(), "bela.toros@gmail.com")
        self.store.set_profile("Bela.Toros@gmail.com", "50", 10)
        self.assertEqual(self.store.account_email(), "Bela.Toros@gmail.com")

    def test_rejects_path_like_ids(self) -> None:
        with self.assertRaises(ValueError):
            self.store.upsert_metadata(_message("../etc", "t1", 1, "x", ["INBOX"]), "missing")

    def test_a_writer_does_not_block_a_reader(self) -> None:
        self.store.upsert_metadata(_message("m1", "t1", 1, "Hi", ["INBOX"]), "missing")
        started = threading.Event()
        release = threading.Event()

        def writer() -> None:
            self.store.conn.execute("BEGIN IMMEDIATE")
            started.set()
            release.wait(3)
            self.store.conn.execute("ROLLBACK")

        thread = threading.Thread(target=writer)
        thread.start()
        self.assertTrue(started.wait(2))
        try:
            began = time.monotonic()
            self.assertEqual(self.store.inbox_counts()[0], 1)
            self.assertLess(time.monotonic() - began, 0.5)
        finally:
            release.set()
            thread.join()

    def test_retain_inbox_finishes_across_batches(self) -> None:
        for index in range(3):
            self.store.upsert_metadata(
                _message(f"m{index}", f"t{index}", index + 1, "S", ["INBOX"]),
                "missing",
            )
        nxt = self.store.retain_inbox(set(), after_id=0, limit=2)
        self.assertIsNotNone(nxt)
        self.assertEqual(self.store.inbox_counts()[0], 1)
        done = self.store.retain_inbox({"m2"}, after_id=nxt or 0, limit=2)
        self.assertIsNone(done)
        self.assertEqual(self.store.inbox_counts()[0], 1)

    def test_unread_folder_count_skips_trash(self) -> None:
        self.store.upsert_metadata(_message("m1", "t1", 3000, "inbox", ["INBOX", "UNREAD"]), "missing")
        self.store.upsert_metadata(_message("m2", "t2", 2000, "trashed", ["TRASH", "UNREAD"]), "missing")
        info = {row.gmail_id: row for row in self.store.list_label_info()}
        self.assertEqual((info["UNREAD"].unread, info["UNREAD"].total), (1, 1))
        self.assertEqual((info["TRASH"].unread, info["TRASH"].total), (1, 1))
        self.assertEqual([row.subject for row in self.store.list_mail_threads("UNREAD")], ["inbox"])
        self.assertEqual([row.subject for row in self.store.list_mail_threads("TRASH")], ["trashed"])
        self.assertEqual(self.store.mailbox_counts(), (1, 1))


if __name__ == "__main__":
    unittest.main()

"""Changing messages stays local and cheap.

A long saved body used to spend hundreds of milliseconds wrapping, and a
delete used to recount every label and open another message before the next
one appeared. These bounds fail if either of those comes back.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from gmail_tui.gmail import IncomingMessage, Label, NullGmail
from gmail_tui.links import present_links
from gmail_tui.store import Store
from gmail_tui.sync import BackgroundSync, Engine
from gmail_tui.textutil import html_to_text
from gmail_tui.ui import MailSession, _display_width, layout_reader, render_lines, wrap_lines


def _message(gmail_id: str, thread_id: str, date: int, subject: str, body: str) -> IncomingMessage:
    return IncomingMessage(
        gmail_id=gmail_id,
        thread_id=thread_id,
        label_ids=["INBOX"],
        internal_date_ms=date,
        subject=subject,
        from_addr="Ada <ada@example.com>",
        to_addr="me@gmail.com",
        snippet=subject,
    )


class ChangeSpeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(self.tmp.name)
        self.store.replace_labels([Label("INBOX", "INBOX", "system")])

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _save(self, message: IncomingMessage, body: str) -> None:
        # Raw and stored text match: the reader shows the MIME part, which
        # comes from the raw body once it has been parsed.
        raw = f"Subject: {message.subject}\r\n\r\n{body}\r\n".encode()
        self.store.upsert_metadata(message, "missing")
        self.store.write_body(message, raw, body)

    def test_a_long_line_wraps_inside_a_small_budget(self) -> None:
        # One 80KB line used to take about half a second. Twice that length
        # would take seconds if the wrap walked the tail on every row.
        line = "See https://example.com/" + ("a" * 160_000)
        started = time.perf_counter()
        shown = present_links(line)
        rows, _starts, marks = layout_reader(shown.splitlines(), 40)
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertTrue(rows)
        self.assertTrue(all(_display_width(row) <= 40 for row in rows))
        self.assertTrue(any(mark.links for mark in marks))
        self.assertIn("https://example.com/", "".join(rows))

        wide = "word " * 20_000
        started = time.perf_counter()
        view, _starts = wrap_lines([wide], 72)
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertGreater(len(view), 1)
        self.assertTrue(all(_display_width(row) <= 72 for row in view))

    def test_a_long_html_letter_stays_cheap(self) -> None:
        # Each paragraph used to copy the whole letter again, so a long HTML
        # message held a CPU core for minutes while its body was saved.
        count = 20_000
        html = "<html><body>" + "".join(
            f'<p>Hello {index} <a href="https://ex.com/{index}">notes</a></p>'
            for index in range(count)
        ) + "</body></html>"
        started = time.perf_counter()
        text = html_to_text(html)
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertIn("Hello 0", text)
        self.assertIn("link https://ex.com/0", text)
        self.assertIn(f"Hello {count - 1}", text)

    def test_moving_between_saved_messages_does_not_reread_them(self) -> None:
        body = "stored body " + ("x" * 80_000)
        self._save(_message("m1", "t1", 3_000, "First", body), body)
        self._save(_message("m2", "t2", 2_000, "Second", body), body)
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            self.assertIn("stored body", "\n".join(session.reader_view))
            reads: list[Path] = []
            labels: list[str] = []
            original_read = Path.read_bytes
            original_labels = Store.list_label_info

            def counting_read(path: Path) -> bytes:
                reads.append(path)
                return original_read(path)

            def counting_labels():
                labels.append("labels")
                return original_labels(engine.store)

            Path.read_bytes = counting_read  # type: ignore[method-assign]
            engine.store.list_label_info = counting_labels  # type: ignore[method-assign]
            try:
                started = time.perf_counter()
                session.move(1)
                render_lines(session, 36, 120)
                session.move(-1)
                render_lines(session, 36, 120)
                elapsed = time.perf_counter() - started
            finally:
                Path.read_bytes = original_read  # type: ignore[method-assign]
                del engine.store.list_label_info
            # First visit to m2 may read once for the MIME list; returning to
            # m1 must reuse the cache instead of reading again.
            self.assertEqual(len(reads), 1)
            self.assertEqual(labels, [])
            self.assertLess(elapsed, 0.5)
            self.assertEqual(session.messages[session.message_cursor].gmail_id, "m1")
            self.assertIn("stored body", "\n".join(session.reader_view))
        finally:
            engine.store.close()

    def test_delete_shows_the_next_message_without_extra_work(self) -> None:
        self._save(_message("m1", "t1", 3_000, "Newest", "newest body"), "newest body")
        self._save(_message("m2", "t2", 2_000, "Middle", "middle body"), "middle body")
        self._save(_message("m3", "t3", 1_000, "Oldest", "oldest body"), "oldest body")
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            session.move(1)
            self.assertEqual(session.messages[session.message_cursor].gmail_id, "m2")
            previews: list[str] = []
            labels: list[str] = []
            reads: list[Path] = []
            original_preview = Store.preview_message
            original_labels = Store.list_label_info
            original_read = Path.read_bytes

            def counting_preview(gmail_id: str):
                previews.append(gmail_id)
                return original_preview(engine.store, gmail_id)

            def counting_labels():
                labels.append("labels")
                return original_labels(engine.store)

            def counting_read(path: Path) -> bytes:
                reads.append(path)
                return original_read(path)

            engine.store.preview_message = counting_preview  # type: ignore[method-assign]
            engine.store.list_label_info = counting_labels  # type: ignore[method-assign]
            Path.read_bytes = counting_read  # type: ignore[method-assign]
            try:
                started = time.perf_counter()
                session.delete_selected()
                render_lines(session, 36, 120)
                elapsed = time.perf_counter() - started
            finally:
                Path.read_bytes = original_read  # type: ignore[method-assign]
                del engine.store.preview_message
                del engine.store.list_label_info
            self.assertLess(elapsed, 0.5)
            self.assertEqual(previews, ["m3"])
            self.assertEqual(labels, [])
            # Opening m3 for the first time reads its body for the MIME list.
            self.assertEqual(len(reads), 1)
            self.assertEqual(session.messages[session.message_cursor].gmail_id, "m3")
            self.assertIn("oldest body", "\n".join(session.reader_view))
            self.assertNotIn("newest body", "\n".join(session.reader_view))
            self.assertTrue(engine.store.has_pending_mutations())
        finally:
            engine.store.close()

    def test_a_burst_does_not_rebuild_the_list_while_bodies_save(self) -> None:
        self._save(_message("m1", "t1", 1_000, "Older", "older body"), "older body")
        pending = _message("m2", "t2", 9_000, "Just arrived", "arrived body")
        self.store.upsert_metadata(pending, "missing")
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            session.focus = "list"
            self.assertEqual(session.messages[session.message_cursor].gmail_id, "m2")
            self.assertIn("(body not downloaded)", session.reader)
            job = BackgroundSync(engine)
            job.dirty = True
            session._refreshed_at = time.monotonic()
            labels: list[str] = []
            original = Store.list_label_info

            def counting_labels(store: Store):
                labels.append("labels")
                return original(store)

            engine.store.list_label_info = counting_labels  # type: ignore[method-assign]
            try:
                self.assertFalse(session.observe(job))
                self.assertEqual(labels, [])
                self.assertTrue(job.dirty)
                raw = b"Subject: Just arrived\r\n\r\narrived body\r\n"
                self.store.write_body(pending, raw, "arrived body")
                self.assertTrue(session.observe(job))
            finally:
                del engine.store.list_label_info
            self.assertEqual(labels, [])
            self.assertTrue(job.dirty)
            self.assertIn("arrived body", "\n".join(session.reader_view))
        finally:
            engine.store.close()

    def test_returning_to_an_attachment_does_not_reread_it(self) -> None:
        plain = "plain body"
        self._save(_message("m1", "t1", 1_000, "Plain", plain), plain)
        attached = _message("m2", "t2", 2_000, "File", "see file")
        attached.has_attachment = True
        raw = (
            b"Subject: File\r\n"
            b"MIME-Version: 1.0\r\n"
            b"Content-Type: multipart/mixed; boundary=xyz\r\n"
            b"\r\n"
            b"--xyz\r\n"
            b"Content-Type: text/plain\r\n"
            b"\r\n"
            b"see file\r\n"
            b"--xyz\r\n"
            b"Content-Type: application/pdf\r\n"
            b"Content-Disposition: attachment; filename=a.pdf\r\n"
            b"\r\n"
            + b"%PDF" + (b"x" * 8_000) +
            b"\r\n--xyz--\r\n"
        )
        self.store.upsert_metadata(attached, "missing")
        self.store.write_body(attached, raw, "see file")
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            self.assertEqual(session.messages[0].gmail_id, "m2")
            self.assertIn("a.pdf", "\n".join(session.reader_view))
            reads: list[Path] = []
            original_read = Path.read_bytes

            def counting_read(path: Path) -> bytes:
                reads.append(path)
                return original_read(path)

            Path.read_bytes = counting_read  # type: ignore[method-assign]
            try:
                session.move(1)
                opened_plain = len(reads)
                session.move(-1)
            finally:
                Path.read_bytes = original_read  # type: ignore[method-assign]
            # The plain message may be parsed once. Coming back to the
            # attachment must reuse the cached parts instead of reading again.
            self.assertEqual(len(reads), opened_plain)
            self.assertIn("a.pdf", "\n".join(session.reader_view))
            self.assertIn("see file", "\n".join(session.reader_view))
        finally:
            engine.store.close()

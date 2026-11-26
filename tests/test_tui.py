"""Label tree, message list, and the three-pane screen."""

from __future__ import annotations

import curses
import os
import pty
import select
import subprocess
import sys
import tempfile
import time
import unittest
from email import message_from_bytes
from email.policy import default
from pathlib import Path

from gmail_tui.accounts import Account
from gmail_tui.fake import FakeGmail
from gmail_tui.gmail import GmailError, IncomingMessage, Label, NullGmail, Offline, RateLimited
from gmail_tui.labels import build_label_tree, flatten
from gmail_tui.store import MessageCursor, Store
from gmail_tui.sync import BackgroundSync, Engine
from gmail_tui.textutil import parse_rfc822, preferred_mime_index, reserve_download
from gmail_tui.tui import (
    _alt_action,
    _chgat_segments,
    _close_prompt,
    _csi_alt_letter,
    _edit_prompt,
    _drop_held_keys,
    _is_escape,
    _open_folder_attr,
    _open_message_attr,
    _pane_highlight,
    _popup_covers,
)
from gmail_tui.keys import ALT, defaults
from gmail_tui.ui import (
    Geometry,
    MailSession,
    _display_width,
    _fit_columns,
    _mailboxes,
    _scrollbar_column,
    edit_draft,
    editor_command,
    format_activity,
    format_when,
    geometry,
    help_lines,
    message_row,
    render_lines,
    parse_draft,
    reader_match,
    reply_draft,
    status_bar,
    wrap_lines,
)

_ATTACHMENT = (
    "Subject: Files\r\n"
    "From: Ada <ada@example.com>\r\n"
    "To: me@gmail.com\r\n"
    "MIME-Version: 1.0\r\n"
    "Content-Type: multipart/mixed; boundary=zzz\r\n"
    "\r\n"
    "--zzz\r\n"
    "Content-Type: text/plain\r\n"
    "\r\n"
    "see the file\r\n"
    "--zzz\r\n"
    "Content-Type: application/pdf\r\n"
    "Content-Disposition: attachment; filename=\"note.pdf\"\r\n"
    "\r\n"
    "%PDF\r\n"
    "--zzz--\r\n"
).encode()


class TreeTests(unittest.TestCase):
    def test_user_labels_nest_on_slash(self) -> None:
        from gmail_tui.store import LabelInfo

        rows = [
            LabelInfo("INBOX", "INBOX", "system", 2, 5),
            LabelInfo("CATEGORY_PROMOTIONS", "CATEGORY_PROMOTIONS", "system", 0, 3),
            LabelInfo("Label_1", "Work", "user", 1, 2),
            LabelInfo("Label_2", "Work/Clients", "user", 4, 10),
        ]
        tree = build_label_tree(rows)
        names = [node.name for node in tree]
        self.assertEqual(names[0], "All mail")
        self.assertIn("Inbox", names)
        categories = next(node for node in tree if node.name == "Categories")
        self.assertEqual([child.name for child in categories.children], ["Promotions"])
        work = next(node for node in tree if node.name == "Work")
        self.assertEqual(work.gmail_id, "Label_1")
        self.assertEqual([(child.name, child.gmail_id) for child in work.children], [("Clients", "Label_2")])
        visible = flatten(tree, {categories.key, work.key})
        clients = next(item for item in visible if item.name == "Clients")
        self.assertEqual(clients.depth, 1)
        self.assertEqual(clients.unread, 4)
        self.assertEqual(clients.total, 10)
        # Fold heads use v/>; children start under the arrow (not under the name).
        work_row = next(item for item in visible if item.name == "Work")
        categories_row = next(item for item in visible if item.name == "Categories")
        promotions = next(item for item in visible if item.name == "Promotions")
        self.assertEqual(categories_row.prefix, "v ")
        self.assertEqual(promotions.prefix, "  └ ")
        self.assertEqual(work_row.prefix, "v ")
        self.assertEqual(clients.prefix, "  └ ")
        # Indented under the name; no second spine under the arrow.
        from gmail_tui.store import LabelInfo as _LI
        from gmail_tui.labels import build_label_tree as _build

        nested = _build(
            [
                _LI("INBOX", "INBOX", "system", 0, 1),
                _LI("L1", "archiv", "user", 0, 1),
                _LI("L2", "archiv/trip", "user", 0, 1),
                _LI("L3", "archiv/trip/hotel", "user", 0, 1),
                _LI("L4", "archiv/music", "user", 0, 1),
            ]
        )
        archiv = next(node for node in nested if node.name == "archiv")
        trip = next(node for node in archiv.children if node.name == "trip")
        opened = flatten(nested, {archiv.key, trip.key})
        self.assertEqual(
            [(item.prefix + item.name) for item in opened if item.path.startswith("archiv")],
            [
                "v archiv",
                "  ├ music",
                "  └ trip",
                "    └ hotel",
            ],
        )
        closed_trip = flatten(nested, {archiv.key})
        trip_row = next(item for item in closed_trip if item.name == "trip")
        self.assertEqual(trip_row.prefix, "  └ > ")

    def test_folder_counts_align_total_and_unread(self) -> None:
        from gmail_tui.ui import _folder_count, _folder_count_widths
        from gmail_tui.labels import FlatLabel

        items = [
            FlatLabel("a", 0, "Inbox", "INBOX", "INBOX", 12, False, False, 345),
            FlatLabel("b", 0, "Sent", "SENT", "SENT", 0, False, False, 8),
            FlatLabel("c", 1, "Clients", "Work/Clients", "Label_2", 4, False, False, 40),
        ]
        total_w, unread_w = _folder_count_widths(items)
        badges = [_folder_count(item.total, item.unread, total_w, unread_w) for item in items]
        self.assertEqual(badges[0], " 12 [345]")
        self.assertEqual(badges[1], "  0 [  8]")
        self.assertEqual(badges[2], "  4 [ 40]")
        ends = [badge.index("[") for badge in badges]
        self.assertEqual(len(set(ends)), 1)


class MailboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(self.tmp.name)
        self.store.replace_labels(
            [
                Label("INBOX", "INBOX", "system"),
                Label("UNREAD", "UNREAD", "system"),
                Label("Label_1", "Work", "user"),
                Label("Label_2", "Work/Clients", "user"),
            ]
        )
        self._message("m1", "t1", 3_000, "First", "Ada <ada@example.com>", "me@gmail.com", ["INBOX", "UNREAD"], "first body")
        self._message("m2", "t2", 2_000, "Second", "Bea <bea@example.com>", "ada@example.com", ["INBOX"], "second body")
        attached = IncomingMessage(
            gmail_id="m3",
            thread_id="t3",
            label_ids=["Label_2"],
            internal_date_ms=1_000,
            subject="Files",
            from_addr="Cy <cy@example.com>",
            to_addr="me@gmail.com",
            snippet="see the file",
            has_attachment=True,
        )
        self.store.upsert_metadata(attached, "missing")
        self.store.write_body(attached, _ATTACHMENT, "see the file")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _message(self, gmail_id, thread_id, date, subject, sender, recipient, labels, body) -> None:
        message = IncomingMessage(
            gmail_id=gmail_id,
            thread_id=thread_id,
            label_ids=labels,
            internal_date_ms=date,
            subject=subject,
            from_addr=sender,
            to_addr=recipient,
            snippet=body,
        )
        self.store.upsert_metadata(message, "missing")
        raw = (
            f"Subject: {subject}\r\nFrom: {sender}\r\nTo: {recipient}\r\n\r\n{body}\r\n"
        ).encode()
        self.store.write_body(message, raw, body)

    def test_message_list_has_columns_and_attachment_flag(self) -> None:
        parsed = parse_rfc822(_ATTACHMENT)
        self.assertTrue(parsed.has_attachment)
        self.assertEqual(parsed.attachments[0].filename, "note.pdf")
        self.assertEqual(parsed.attachments[0].content_type, "application/pdf")
        self.assertEqual(parsed.attachments[0].index, 1)
        self.assertIn("see the file", parsed.text)
        inbox = self.store.list_messages("INBOX")
        self.assertEqual([item.subject for item in inbox], ["First", "Second"])
        oldest = self.store.list_messages("INBOX", newest_first=False)
        self.assertEqual([item.subject for item in oldest], ["Second", "First"])
        older = self.store.list_messages(
            "INBOX",
            MessageCursor(oldest[0].internal_date_ms, oldest[0].message_id),
            10,
            newest_first=False,
        )
        self.assertEqual([item.subject for item in older], ["First"])
        self.assertEqual(inbox[0].from_addr, "Ada <ada@example.com>")
        self.assertEqual(inbox[0].to_addr, "me@gmail.com")
        self.assertIn("INBOX", inbox[0].label_names)
        self.assertNotIn("UNREAD", inbox[0].label_names)
        self.assertTrue(inbox[0].unread)
        self.assertFalse(inbox[0].has_attachment)
        page = self.store.list_messages("INBOX", MessageCursor(inbox[0].internal_date_ms, inbox[0].message_id), 10)
        self.assertEqual([item.subject for item in page], ["Second"])
        clients = self.store.list_messages("Label_2")
        self.assertEqual(clients[0].subject, "Files")
        self.assertTrue(clients[0].has_attachment)
        wide = message_row(clients[0], 100)
        self.assertIn("cy@example.com", wide)
        self.assertNotIn("Cy <", wide)
        self.assertIn("me@gmail.com", wide)
        self.assertIn("Files", wide)
        self.assertTrue(wide.startswith("    │ * │"))
        self.assertIn(format_when(clients[0].internal_date_ms), wide)
        self.assertLess(wide.index("*"), wide.index(format_when(clients[0].internal_date_ms)))
        self.assertLess(wide.index(format_when(clients[0].internal_date_ms)), wide.index("cy@example.com"))
        sender = "Ada Lovelace <ada.lovelace@analytical.example>"
        recipient = "Charles Babbage <charles.babbage@analytical.example>"
        long = message_row(
            type(clients[0])(
                message_id=1,
                gmail_id="long",
                from_addr=sender,
                to_addr=recipient,
                subject="Engines",
                snippet="",
                internal_date_ms=1,
                label_names=("Work/Clients",),
                has_attachment=False,
                unread=False,
            ),
            180,
        )
        self.assertIn("ada.lovelace@analytical.example", long)
        self.assertNotIn("Ada Lovelace", long)
        self.assertIn("charles.babbage@analytical.example", long)
        self.assertNotIn("Charles Babbage", long)
        self.assertIn("Work/Clients", long)

    def test_label_column_stays_put_when_earlier_columns_are_wide(self) -> None:
        sample = self.store.list_messages("Label_2")[0]

        width = 120
        variants = [
            ("Hello", "INBOX"),
            ("Hello\u4e16", "INBOX"),
            ("Hello\tthere", "INBOX"),
            ("Hello", "INBOX, projects/solar"),
        ]
        columns = _fit_columns(
            width - 2,
            [
                (sample.from_addr, sample.to_addr, subject, labels)
                for subject, labels in variants
            ],
        )

        def row(subject: str, labels: tuple[str, ...] = ("INBOX",), star: bool = False) -> str:
            return message_row(
                type(sample)(
                    message_id=sample.message_id,
                    gmail_id=sample.gmail_id,
                    from_addr=sample.from_addr,
                    to_addr=sample.to_addr,
                    subject=subject,
                    snippet="",
                    internal_date_ms=sample.internal_date_ms,
                    label_names=labels,
                    has_attachment=star,
                    unread=False,
                ),
                width,
                columns=columns,
            )

        def column(text: str, needle: str) -> int:
            return _display_width(text[: text.index(needle)])

        plain = row("Hello")
        wide = row("Hello\u4e16")
        tabbed = row("Hello\tthere")
        several = row("Hello", ("INBOX", "projects/solar"), True)
        variation = row("Hello\ufe0f")
        invisible = row("\u200eHello\u200bthere\u200d")
        self.assertEqual(column(plain, "INBOX"), column(wide, "INBOX"))
        self.assertEqual(column(plain, "INBOX"), column(tabbed, "INBOX"))
        self.assertEqual(column(plain, "INBOX"), column(several, "INBOX"))
        self.assertEqual(column(plain, "INBOX"), column(variation, "INBOX"))
        self.assertEqual(column(plain, "INBOX"), column(invisible, "INBOX"))
        self.assertEqual(column(plain, "Hello"), column(variation, "Hello"))
        self.assertEqual(column(plain, "Hello"), column(invisible, "Hello"))
        starred = row("Hello", star=True)
        self.assertEqual(column(starred, "*"), column(row("Hello\u4e16", star=True), "*"))
        self.assertEqual(column(starred, "*"), column(several, "*"))

    def test_labels_stay_whole_when_the_subject_has_room(self) -> None:
        sender = "ada@example.com"
        recipient = "me@gmail.com"
        subject = "Hi"
        labels = "INBOX, Work/Clients, projects/solar-install"
        width = 160
        columns = _fit_columns(width, [(sender, recipient, subject, labels)])
        assert columns is not None
        from_w, to_w, subject_w, labels_w = columns
        self.assertEqual(from_w, _display_width(sender))
        self.assertEqual(to_w, _display_width(recipient))
        self.assertEqual(labels_w, _display_width(labels))
        self.assertGreaterEqual(subject_w, len("Subject"))
        sample = self.store.list_messages("Label_2")[0]
        row = message_row(
            type(sample)(
                message_id=sample.message_id,
                gmail_id=sample.gmail_id,
                from_addr=sender,
                to_addr=recipient,
                subject=subject,
                snippet="",
                internal_date_ms=sample.internal_date_ms,
                label_names=tuple(part.strip() for part in labels.split(",")),
                has_attachment=False,
                unread=False,
            ),
            width + 2,
            columns=columns,
        )
        self.assertIn(labels, row)
        self.assertNotIn("~", row[row.index(labels) :])

    def test_address_columns_follow_the_longest_and_subject_gets_the_rest(self) -> None:
        short_from = "Ada"
        long_from = "Beatrice Longname <beatrice.longname@example.com>"
        shown_from = _mailboxes(long_from)
        recipient = "me@gmail.com"
        labels = "INBOX"
        width = 140
        columns = _fit_columns(
            width,
            [
                (short_from, recipient, "Hello", labels),
                (shown_from, recipient, "Quarterly planning notes", labels),
            ],
        )
        assert columns is not None
        from_w, to_w, subject_w, _labels_w = columns
        self.assertEqual(from_w, _display_width(shown_from))
        self.assertEqual(to_w, _display_width(recipient))
        self.assertGreater(subject_w, 20)
        sample = self.store.list_messages("Label_2")[0]

        def built(sender: str, subject: str) -> str:
            return message_row(
                type(sample)(
                    message_id=sample.message_id,
                    gmail_id=sample.gmail_id,
                    from_addr=sender,
                    to_addr=recipient,
                    subject=subject,
                    snippet="",
                    internal_date_ms=sample.internal_date_ms,
                    label_names=(labels,),
                    has_attachment=True,
                    unread=False,
                ),
                width + 2,
                columns=columns,
            )

        brief = built(short_from, "Hello")
        wide = built(long_from, "Quarterly planning notes for the solar installation")
        self.assertEqual(brief.index("Hello"), wide.index("Quarterly"))
        self.assertIn(shown_from, wide)
        self.assertNotIn("Beatrice", wide)
        self.assertIn("Quarterly planning notes for the solar", wide)
        self.assertTrue(brief.startswith("    │ * │"))
        self.assertEqual(_display_width(brief), width + 2)

    def test_from_and_to_columns_keep_only_the_email_address(self) -> None:
        self.assertEqual(_mailboxes("Charles Babbage <charles@example.com>"), "charles@example.com")
        self.assertEqual(
            _mailboxes('"Babbage, Charles" <c@example.com>, Bea <b@y.com>'),
            "c@example.com, b@y.com",
        )
        self.assertEqual(_mailboxes("To"), "To")

    def test_read_flag_toggles_without_autoread_and_steps_to_the_next_unread(self) -> None:
        self._message(
            "m4",
            "t4",
            500,
            "Older",
            "Ada <ada@example.com>",
            "me@gmail.com",
            ["INBOX", "UNREAD"],
            "older body",
        )
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            self.assertEqual([item.gmail_id for item in session.messages], ["m1", "m2", "m4"])
            self.assertTrue(session.messages[0].unread)
            session.load_reader(fetch=True)
            self.assertTrue(self.store.list_messages("INBOX")[0].unread)
            self.assertTrue(message_row(session.messages[0], 140).startswith("  U │"))
            self.assertTrue(message_row(session.messages[1], 140).startswith("    │"))
            session.toggle_unread()
            self.assertFalse(session.messages[0].unread)
            self.assertEqual(session.messages[session.message_cursor].gmail_id, "m4")
            self.assertTrue(session.messages[session.message_cursor].unread)
            session.message_cursor = 1
            session.next_unread()
            self.assertEqual(session.messages[session.message_cursor].gmail_id, "m4")
            self.assertTrue(self.store.list_messages("INBOX")[1].unread is False)
            session.message_cursor = 1
            session.toggle_unread()
            self.assertEqual(session.messages[session.message_cursor].gmail_id, "m2")
            self.assertTrue(session.messages[session.message_cursor].unread)
        finally:
            engine.store.close()

    def test_reply_opens_a_plain_text_composer(self) -> None:
        to, cc, subject, lines = reply_draft(
            from_addr="Ada <ada@example.com>",
            to_addr="me@gmail.com, Bea <bea@example.com>",
            cc_addr="Cy <cy@example.com>",
            subject="Hello",
            body="hello there",
            me="me@gmail.com",
            everyone=False,
        )
        self.assertEqual((to, cc, subject), ("ada@example.com", "", "Re: Hello"))
        self.assertEqual(lines[0], "")
        self.assertEqual(lines[1], "> hello there")
        signed = reply_draft(
            from_addr="Ada <ada@example.com>",
            to_addr="me@gmail.com",
            cc_addr="",
            subject="Hello",
            body="hello there",
            me="me@gmail.com",
            everyone=False,
            signature="-- \nAda\n",
        )[3]
        self.assertEqual(signed[:4], ["", "-- ", "Ada", ""])
        self.assertEqual(signed[4], "> hello there")
        to, cc, subject, _lines = reply_draft(
            from_addr="Ada <ada@example.com>",
            to_addr="me@gmail.com, Bea <bea@example.com>",
            cc_addr="Cy <cy@example.com>",
            subject="Re: Hello",
            body="hello there",
            me="me@gmail.com",
            everyone=True,
        )
        self.assertEqual(to, "ada@example.com")
        self.assertEqual(cc, "bea@example.com, cy@example.com")
        self.assertEqual(subject, "Re: Hello")
        to, cc, _subject, _lines = reply_draft(
            from_addr="Me <me@gmail.com>",
            to_addr="Ada <ada@example.com>, Bea <bea@example.com>",
            cc_addr="",
            subject="Note",
            body="x",
            me="me@gmail.com",
            everyone=True,
        )
        self.assertEqual(to, "ada@example.com")
        self.assertEqual(cc, "bea@example.com")

        self.store.set_email("me@gmail.com")
        note = IncomingMessage(
            gmail_id="m5",
            thread_id="t5",
            label_ids=["INBOX", "UNREAD"],
            internal_date_ms=4_000,
            subject="Re: Hello",
            from_addr="Ada <ada@example.com>",
            to_addr="me@gmail.com, Bea <bea@example.com>",
            cc_addr="Cy <cy@example.com>",
            snippet="hello there",
            message_id_header="<m5@example.com>",
        )
        self.store.upsert_metadata(note, "missing")
        self.store.write_body(note, b"Subject: Re: Hello\r\n\r\nhello there\r\n", "hello there")
        saved_editor = os.environ.get("EDITOR")
        saved_visual = os.environ.get("VISUAL")
        try:
            os.environ["EDITOR"] = "nvim -f"
            os.environ["VISUAL"] = "vi"
            self.assertEqual(editor_command(), ["nvim", "-f"])
            del os.environ["EDITOR"]
            self.assertEqual(editor_command(), ["vi"])
            del os.environ["VISUAL"]
            self.assertEqual(editor_command(), ["vim"])
        finally:
            if saved_editor is None:
                os.environ.pop("EDITOR", None)
            else:
                os.environ["EDITOR"] = saved_editor
            if saved_visual is None:
                os.environ.pop("VISUAL", None)
            else:
                os.environ["VISUAL"] = saved_visual

        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            self.assertEqual(session.messages[0].gmail_id, "m5")
            self.assertTrue(session.messages[0].unread)
            self.assertTrue(session.begin_reply(everyone=False))
            draft = session.draft_text()
            self.assertTrue(draft.startswith("To: ada@example.com\nSubject: Re: Hello\n\n"))
            self.assertNotIn("Cc:", draft)
            self.assertIn("> hello there", draft)
            self.assertTrue(session.messages[0].unread)
            edited, status = edit_draft(draft, lambda path: 0)
            self.assertEqual(status, 0)
            self.assertEqual(edited, draft)
            session.finish_editor(draft, edited, status)
            self.assertEqual(session.prompt, "send")
            self.assertIn("send or save as draft", format_activity(session, 80))
            self.assertIn("> Send", "\n".join(render_lines(session, 24, 90)))
            self.assertIn("Save as draft", "\n".join(render_lines(session, 24, 90)))
            session.prompt_type("n")
            self.assertIsNone(session.prompt)
            self.assertEqual(session.notice, "not sent")
            session.begin_reply(everyone=True)
            draft = session.draft_text()
            self.assertIn("Cc: bea@example.com, cy@example.com\n", draft)
            changed = draft.replace("\n\n", "\n\nHi\n", 1)
            session.finish_editor(draft, changed, 0)
            self.assertEqual(session.prompt, "send")
            self.assertIn("Hi", session.reader)
            kept = list(session.messages)
            session.confirm_send()
            self.assertEqual(session.prompt, "send")
            self.assertIn("couldn't check Sent", session.notice)
            self.assertEqual(session.messages, kept)
            self.assertTrue(session.messages[0].unread)
            session.prompt_type("n")
            self.assertIsNone(session.prompt)
            self.assertEqual(session.notice, "not sent")
            session.finish_editor(draft, changed, 1)
            self.assertIsNone(session.prompt)
            self.assertEqual(session.notice, "not sent")

            def missing(_path: Path) -> int:
                raise OSError("missing")

            edited, status = edit_draft(draft, missing)
            self.assertIsNone(edited)
            session.finish_editor(draft, edited, status)
            self.assertEqual(session.notice, "editor failed")
            # The lost reply left an unconfirmed token. The online half is a
            # first send, so drop that note.
            engine.store.conn.execute("DELETE FROM sent_mail")
            engine.store.conn.commit()
        finally:
            engine.store.close()

        gmail = FakeGmail()
        engine = Engine.open(self.tmp.name, gmail)
        try:
            session = MailSession(engine)
            session.begin_reply(everyone=True)
            draft = session.draft_text()

            def write_reply(path: Path) -> int:
                path.write_text(draft.replace("\n\n", "\n\nHi\n", 1), encoding="utf-8")
                return 0

            edited, status = edit_draft(draft, write_reply)
            session.finish_editor(draft, edited, status)
            self.assertEqual(session.prompt, "send")
            self.assertEqual(gmail.sent, [])
            session.prompt_type("y")
            self.assertEqual(session.notice, "sent")
            self.assertTrue(session.messages[0].unread)
            sent = message_from_bytes(gmail.sent[0], policy=default)
            self.assertEqual(sent["To"], "ada@example.com")
            self.assertEqual(sent["Cc"], "bea@example.com, cy@example.com")
            self.assertEqual(sent["Subject"], "Re: Hello")
            self.assertEqual(sent["In-Reply-To"], "<m5@example.com>")
            self.assertIn("Hi", sent.get_content())
            self.assertIn("> hello there", sent.get_content())
            # After send we keep Gmail's copy so Message-ID is available for later replies.
            stored = engine.store.opened_message("sent-1")
            self.assertIsNotNone(stored)
            self.assertTrue(str(stored["message_id_header"]).strip())
        finally:
            engine.store.close()

    def test_reply_keeps_the_line_written_under_the_subject(self) -> None:
        written = (
            "To: ada@example.com\n"
            "Subject: Re: Hello\n"
            "Hi Ada: thanks for the note\n"
            "See you then\n"
            "\n"
            "> hello there\n"
        )
        to, cc, bcc, subject, body = parse_draft(written)
        self.assertEqual((to, cc, bcc, subject), ("ada@example.com", "", "", "Re: Hello"))
        self.assertEqual(body, "Hi Ada: thanks for the note\nSee you then\n\n> hello there")
        gmail = FakeGmail()
        engine = Engine.open(self.tmp.name, gmail)
        try:
            session = MailSession(engine)
            session.begin_reply(everyone=False)
            session.finish_editor("", written, 0)
            session.confirm_send()
            self.assertEqual(session.notice, "sent")
            sent = message_from_bytes(gmail.sent[0], policy=default)
            self.assertTrue(sent.get_content().startswith("Hi Ada: thanks for the note\n"))
        finally:
            engine.store.close()

    def test_compose_completes_an_address_before_the_editor(self) -> None:
        from gmail_tui.ui import render_lines

        gmail = FakeGmail()
        engine = Engine.open(self.tmp.name, gmail)
        try:
            session = MailSession(engine)
            session._compose_contacts = (
                "ada@example.com",
                "ann@example.com",
                "bea@example.com",
                "cy@example.com",
            )
            session.prompt = "compose"
            session.prompt_cursor = 0
            session.compose_to = "b"
            session.complete_compose_address()
            self.assertEqual(session.compose_to, "bea@example.com")
            self.assertEqual(session.prompt_hint, "")
            session.compose_to = "a"
            session._compose_matches = []
            session.complete_compose_address()
            self.assertEqual(session.compose_to, "ada@example.com")
            self.assertEqual(session.prompt_hint, "1/2")
            session.complete_compose_address()
            self.assertEqual(session.compose_to, "ann@example.com")
            session.compose_to = "ada@example.com"
            session.prompt_cursor = 1
            session.compose_cc = "ada@example.com, c"
            session._compose_matches = []
            session.complete_compose_address()
            self.assertEqual(session.compose_cc, "ada@example.com, cy@example.com")
            session.compose_bcc = "hidden@example.com"
            session.compose_lines = [""]
            opened: list[str] = []

            def launch() -> bool:
                opened.append(session.draft_text())
                return True

            session.begin_external_edit = launch  # type: ignore[method-assign]
            session.prompt_accept()
            self.assertIsNone(session.prompt)
            self.assertEqual(
                opened,
                [
                    "To: ada@example.com\n"
                    "Cc: ada@example.com, cy@example.com\n"
                    "Bcc: hidden@example.com\n"
                    "Subject: \n"
                    "\n"
                ],
            )
            session.prompt = "compose"
            session.prompt_cursor = 2
            painted = "\n".join(render_lines(session, 24, 80))
            self.assertIn("To: ada@example.com", painted)
            self.assertIn("Bcc: hidden@example.com_", painted)
        finally:
            engine.store.close()

    def test_signature_file_is_in_the_new_message(self) -> None:
        gmail = FakeGmail()
        engine = Engine.open(self.tmp.name, gmail)
        try:
            session = MailSession(engine)
            path = Path(self.tmp.name) / "signature"
            path.write_text("-- \nAda\n", encoding="utf-8")
            session.signature_path = path
            self.assertTrue(session.begin_compose())
            self.assertIn("\n\n-- \nAda\n", session.draft_text())
            session.signature_path = path.with_name("missing")
            self.assertTrue(session.begin_compose())
            self.assertNotIn("Ada", session.draft_text())
            self.assertTrue(session.notice.startswith("signature:"))
        finally:
            engine.store.close()

    def test_compose_opens_a_blank_message(self) -> None:
        gmail = FakeGmail()
        engine = Engine.open(self.tmp.name, gmail)
        try:
            session = MailSession(engine)
            self.assertTrue(session.begin_compose())
            draft = session.draft_text()
            self.assertTrue(draft.startswith("To: \nCc: \nSubject: \n\n"))
            filled = (
                "To: ada@example.com\n"
                "Cc: bea@example.com\n"
                "Subject: Hello\n"
                "\n"
                "Hi there\n"
            )

            def write_new(path: Path) -> int:
                path.write_text(filled, encoding="utf-8")
                return 0

            edited, status = edit_draft(draft, write_new)
            session.finish_editor(draft, edited, status)
            self.assertEqual(session.prompt, "send")
            self.assertIn("send or save as draft", format_activity(session, 80))
            session.prompt_type("y")
            self.assertEqual(session.notice, "sent")
            self.assertEqual(len(gmail.sent), 1)
            # A second confirm (queued Enter/click) must not send again.
            session.confirm_send()
            session.send_compose()
            self.assertEqual(len(gmail.sent), 1)
            self.assertTrue(any(call.startswith("messages.send:") for call in gmail.calls))
            self.assertIn("messages.get:sent-1:raw", gmail.calls)
            sent = message_from_bytes(gmail.sent[0], policy=default)
            self.assertEqual(sent["To"], "ada@example.com")
            self.assertEqual(sent["Cc"], "bea@example.com")
            self.assertEqual(sent["Subject"], "Hello")
            self.assertEqual(len(sent["X-Gmail-Tui"]), 64)
            self.assertIsNone(sent["In-Reply-To"])
            self.assertIn("Hi there", sent.get_content())
            # Re-opening the same draft must not send a duplicate either.
            session.finish_editor(filled, filled, 0)
            self.assertEqual(session.prompt, "send")
            session.confirm_send()
            self.assertEqual(len(gmail.sent), 1)
            self.assertIn("already sent", session.notice)
            session.begin_compose()
            session.finish_editor(
                filled,
                filled.replace("Hi there", "Different body"),
                0,
            )
            session.prompt_move(1)
            shown = "\n".join(render_lines(session, 24, 90))
            self.assertIn("> Save as draft", shown)
            session.prompt_accept()
            self.assertEqual(session.notice, "saved draft")
            self.assertEqual(len(gmail.drafts), 1)
            self.assertTrue(any(call.startswith("drafts.create:") for call in gmail.calls))
            stored = engine.store.opened_message("draft-1")
            self.assertIsNotNone(stored)
            self.assertIn("DRAFT", stored["label_ids"])
        finally:
            engine.store.close()

    def test_offline_send_does_not_allow_blind_retry(self) -> None:
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            session.begin_compose()
            filled = (
                "To: ada@example.com\n"
                "Cc: \n"
                "Subject: Hello\n"
                "\n"
                "Hi there\n"
            )
            calls: list[int] = []
            real_send = engine.gmail.send_message

            def send(raw: bytes, thread_id: str):
                calls.append(1)
                raise Offline("no Gmail token")

            engine.gmail.send_message = send  # type: ignore[method-assign]
            session.finish_editor("", filled, 0)
            session.confirm_send()
            self.assertEqual(session.prompt, "send")
            self.assertIn("couldn't check Sent", session.notice)
            self.assertFalse(session._sending)
            session.confirm_send()
            self.assertEqual(calls, [1])
            self.assertEqual(session.prompt, "send")
            engine.gmail.send_message = real_send  # type: ignore[method-assign]
        finally:
            engine.store.close()

    def test_rate_limited_send_is_not_offered_again(self) -> None:
        gmail = FakeGmail()
        engine = Engine.open(self.tmp.name, gmail)
        try:
            original = gmail.send_message

            def limited(raw: bytes, thread_id: str):
                gmail.send_message = original  # type: ignore[method-assign]
                raise RateLimited("rateLimitExceeded")

            gmail.send_message = limited  # type: ignore[method-assign]
            session = MailSession(engine)
            session.begin_compose()
            filled = (
                "To: ada@example.com\n"
                "Cc: \n"
                "Subject: Hello\n"
                "\n"
                "Hi there\n"
            )
            session.finish_editor("", filled, 0)
            session.confirm_send()
            self.assertEqual(gmail.sent, [])
            self.assertEqual(session.prompt, "send")
            self.assertIn("rateLimitExceeded", session.notice)
            self.assertFalse(session._sending)
            session.confirm_send()
            self.assertEqual(len(gmail.sent), 1)
            self.assertEqual(session.notice, "sent")
        finally:
            engine.store.close()

    def test_a_rejected_send_is_not_offered_again(self) -> None:
        gmail = FakeGmail()
        engine = Engine.open(self.tmp.name, gmail)

        original = gmail.send_message

        def rejected(raw: bytes, thread_id: str):
            gmail.send_message = original  # type: ignore[method-assign]
            raise GmailError("Gmail HTTP 500")

        gmail.send_message = rejected  # type: ignore[method-assign]
        try:
            session = MailSession(engine)
            session.begin_compose()
            filled = (
                "To: ada@example.com\n"
                "Cc: \n"
                "Subject: Hello\n"
                "\n"
                "Hi there\n"
            )
            session.finish_editor("", filled, 0)
            session.confirm_send()
            self.assertEqual(gmail.sent, [])
            self.assertEqual(session.prompt, "send")
            self.assertIn("not in Sent", session.notice)
            self.assertFalse(session._sending)
            session.confirm_send()
            self.assertEqual(len(gmail.sent), 1)
            self.assertEqual(session.notice, "sent")
        finally:
            engine.store.close()

    def test_editor_runs_in_another_terminal(self) -> None:
        from gmail_tui.terminal import editor_argv

        path = Path(self.tmp.name) / "draft.eml"
        sentinel = Path(str(path) + ".done")
        saved = os.environ.get("GMAIL_TUI_TERMINAL")
        os.environ["GMAIL_TUI_TERMINAL"] = "alacritty -e"
        try:
            argv = editor_argv(path, sentinel, ["vim"])
        finally:
            if saved is None:
                os.environ.pop("GMAIL_TUI_TERMINAL", None)
            else:
                os.environ["GMAIL_TUI_TERMINAL"] = saved
        self.assertEqual(argv[:2], ["alacritty", "-e"])
        self.assertIn("vim", argv)
        self.assertEqual(argv[-1], str(path))
        self.assertIn(str(sentinel), argv)

        gmail = FakeGmail()
        engine = Engine.open(self.tmp.name, gmail)
        try:
            session = MailSession(engine)
            session.begin_compose()
            session.compose_thread = "thread-9"
            session.compose_reply_to = "<m@x>"

            def launch(draft, done, _editor):
                draft.write_text(
                    "To: ada@example.com\nSubject: Hi\n\nHello\n",
                    encoding="utf-8",
                )
                done.write_text("0", encoding="ascii")
                return None

            self.assertTrue(session.begin_external_edit(launch=launch))
            draft_path = session.external.path
            session.compose_thread = "nope"
            self.assertTrue(session.poll_external_edit())
            self.assertEqual(session.compose_thread, "thread-9")
            self.assertEqual(session.compose_reply_to, "<m@x>")
            self.assertEqual(session.prompt, "send")
            self.assertEqual(session.compose_to, "ada@example.com")
            self.assertFalse(draft_path.exists())

            def hold(_draft, _done, _editor):
                return None

            session.prompt_cancel()
            session.begin_compose()
            self.assertTrue(session.begin_external_edit(launch=hold))
            self.assertFalse(session.begin_external_edit(launch=hold))
            self.assertEqual(session.notice, "already writing a message")
            session.forget_external_edit()
            self.assertEqual(session.notice, "editor closed")
            self.assertIsNone(session.external)
        finally:
            engine.store.close()

    def test_mouse_drag_copies_the_cells_it_covers(self) -> None:
        from gmail_tui.ui import mark_spans, selected_text

        lines = ["hello world", "second line"]
        self.assertEqual(selected_text(lines, 0, 6, 1, 5), "world\nsecond")
        self.assertEqual(selected_text(lines, 1, 5, 0, 6), "world\nsecond")
        spans = mark_spans((0, 6), (1, 5))
        self.assertEqual(spans[0][0], 0)
        self.assertEqual(spans[0][1], 6)
        self.assertEqual(spans[-1], (1, 0, 6))

    def test_mouse_drag_updates_the_mark_while_held(self) -> None:
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            session.mark_press(2, 4)
            self.assertTrue(session.marking)
            self.assertEqual(session.mark_end, (2, 4))
            session.mark_drag(3, 10)
            self.assertEqual(session.mark_start, (2, 4))
            self.assertEqual(session.mark_end, (3, 10))
            text = session.finish_mark(3, 12)
            self.assertFalse(session.marking)
            self.assertEqual(session.mark_end, (3, 12))
            self.assertIsInstance(text, str)
        finally:
            engine.store.close()

    def test_drag_repaints_only_rows_under_the_old_and_new_mark(self) -> None:
        from unittest import mock

        from gmail_tui import tui

        class Screen:
            def __init__(self) -> None:
                self.rows: list[int] = []

            def getmaxyx(self):
                return (30, 80)

            def addnstr(self, y, *_args):
                self.rows.append(y)

            def chgat(self, *_args):
                pass

            def refresh(self):
                pass

        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            session.painted = [f"row {y}" for y in range(29)]
            session.mark_press(10, 2)
            session.mark_rows = (10, 10)
            session.mark_drag(12, 5)
            screen = Screen()
            with mock.patch.object(curses, "putp", lambda _b: None):
                tui._paint_mark(screen, session, 0)
            self.assertEqual(set(screen.rows), {10, 11, 12})
            self.assertEqual(session.mark_rows, (10, 12))

            session.mark_drag(10, 7)
            screen.rows.clear()
            with mock.patch.object(curses, "putp", lambda _b: None):
                tui._paint_mark(screen, session, 0)
            # Rows 11 and 12 still carry the old highlight and must be rewritten.
            self.assertEqual(set(screen.rows), {10, 11, 12})
            self.assertEqual(session.mark_rows, (10, 10))
        finally:
            engine.store.close()

    def test_queued_drag_motion_is_applied_before_painting(self) -> None:
        from unittest import mock

        from gmail_tui import tui

        class Screen:
            def __init__(self, keys) -> None:
                self.keys = list(keys)

            def timeout(self, _ms):
                pass

            def get_wch(self):
                if not self.keys:
                    raise curses.error
                return self.keys.pop(0)

        motion = getattr(curses, "REPORT_MOUSE_POSITION", 0)
        events = iter([(0, 9, 4, 0, motion), (0, 20, 6, 0, motion)])
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            session.mark_press(2, 3)
            pending: list = []
            screen = Screen([curses.KEY_MOUSE, curses.KEY_MOUSE, "j"])
            with mock.patch.object(curses, "getmouse", lambda: next(events)):
                chosen = tui._drain_motion(screen, session, pending)
            self.assertIsNone(chosen)
            self.assertEqual(session.mark_end, (6, 20))
            self.assertEqual(pending, ["j"])
        finally:
            engine.store.close()

    def test_plain_text_is_shown_and_html_keeps_its_shape(self) -> None:
        both = (
            "Subject: Both\r\n"
            "MIME-Version: 1.0\r\n"
            "Content-Type: multipart/alternative; boundary=alt\r\n"
            "\r\n"
            "--alt\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "PLAIN BODY\r\n"
            "--alt\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "\r\n"
            "<p>HTML BODY</p>\r\n"
            "--alt--\r\n"
        ).encode()
        parsed = parse_rfc822(both)
        self.assertIn("PLAIN BODY", parsed.text)
        self.assertNotIn("HTML BODY", parsed.text)

        empty_plain = (
            "Subject: Empty plain\r\n"
            "MIME-Version: 1.0\r\n"
            "Content-Type: multipart/alternative; boundary=alt\r\n"
            "\r\n"
            "--alt\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "\r\n"
            "\r\n"
            "--alt\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "\r\n"
            "<p>From the HTML part</p>\r\n"
            "--alt--\r\n"
        ).encode()
        self.assertEqual(parse_rfc822(empty_plain).text, "From the HTML part")

        odd_charset = (
            "Subject: Odd\r\n"
            "Content-Type: text/html; charset=x-unknown\r\n"
            "Content-Transfer-Encoding: base64\r\n"
            "\r\n"
            "PHA+SGVsbG8gdW5rbm93bjwvcD4=\r\n"
        ).encode()
        self.assertIn("Hello unknown", parse_rfc822(odd_charset).text)

        html_only = (
            "Subject: Page\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "\r\n"
            "<html><body><p>Hello there</p>"
            "<p>See <a href=\"https://example.com/a\">the notes</a>.</p>"
            "<ul><li>One</li><li>Two</li></ul>"
            "<table><tr><td>Left</td><td>Right</td></tr></table>"
            "</body></html>\r\n"
        ).encode()
        text = parse_rfc822(html_only).text
        self.assertLess(text.index("Hello there"), text.index("the notes link https://example.com/a"))
        self.assertLess(text.index("the notes"), text.index("- One"))
        self.assertIn("- Two", text)
        self.assertIn("Left | Right", text)
        self.assertIn("\n", text)

        attached = (
            "Subject: Letter\r\n"
            "MIME-Version: 1.0\r\n"
            "Content-Type: multipart/mixed; boundary=mix\r\n"
            "\r\n"
            "--mix\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            "\r\n"
            "<p>The letter</p>\r\n"
            "--mix\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "Content-Disposition: attachment; filename=\"note.txt\"\r\n"
            "\r\n"
            "SECRET FILE\r\n"
            "--mix--\r\n"
        ).encode()
        letter = parse_rfc822(attached)
        self.assertIn("The letter", letter.text)
        self.assertNotIn("SECRET FILE", letter.text)
        self.assertEqual(letter.attachments[0].filename, "note.txt")

        glued = (
            "Subject: Glue\r\n"
            "MIME-Version: 1.0\r\n"
            "Content-Type: multipart/mixed; boundary=mix\r\n"
            "\r\n"
            "--mix\r\n"
            "Content-Type: text/plain\r\n"
            "\r\n"
            "body\r\n"
            "--mix\r\n"
            "Content-Type: application/pdf; charset=utf-8Content-Transfer-Encoding: base64\r\n"
            "Content-Disposition: attachment; filename=\"doc.pdf\"\r\n"
            "\r\n"
            "AAA=\r\n"
            "--mix--\r\n"
        ).encode()
        glued_mail = parse_rfc822(glued)
        self.assertEqual(glued_mail.attachments[0].filename, "doc.pdf")
        self.assertEqual(glued_mail.attachments[0].content_type, "application/pdf")
        self.assertGreater(glued_mail.attachments[0].size, 0)

        both_parts = parse_rfc822(both).mime_parts
        self.assertEqual([part.content_type for part in both_parts], ["text/plain", "text/html"])
        self.assertEqual(preferred_mime_index(both_parts), 0)
        empty_parts = parse_rfc822(empty_plain).mime_parts
        self.assertEqual(preferred_mime_index(empty_parts), 1)

    def test_empty_inbox_points_at_all_mail(self) -> None:
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            engine.store.set_messages_total(12)
            engine.store.apply_local_labels("m1", remove=["INBOX"])
            engine.store.apply_local_labels("m2", remove=["INBOX"])
            session = MailSession(engine)
            self.assertEqual(session.current_label().gmail_id, "INBOX")
            self.assertEqual(session.messages, [])
            self.assertIn("All mail", session.notice)
            self.assertIn("12 messages", session.notice)
        finally:
            engine.store.close()

    def _open_full(self, engine: Engine) -> MailSession:
        raw = (
            "Received: from relay.example\r\n"
            "X-Mailer: Test Mailer\r\n"
            "Subject: =?utf-8?q?Caf=C3=A9?=\r\n"
            "From: Ada <ada@example.com>\r\n"
            "To: me@gmail.com\r\n"
            "\r\n"
            "FULL BODY\r\n"
        ).encode()
        message = IncomingMessage(
            gmail_id="full1",
            thread_id="tfull",
            label_ids=["INBOX"],
            internal_date_ms=9_500,
            subject="Café",
            from_addr="Ada <ada@example.com>",
            to_addr="me@gmail.com",
        )
        self.store.upsert_metadata(message, "missing")
        self.store.write_body(message, raw, "FULL BODY")
        session = MailSession(engine)
        session.focus = "list"
        session.load_messages(fetch=False)
        for index, item in enumerate(session.messages):
            if item.gmail_id == "full1":
                session.message_cursor = index
                break
        session.load_reader(fetch=False)
        return session

    def test_headers_toggle_shows_every_field(self) -> None:
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = self._open_full(engine)
            short = "\n".join(session.reader)
            self.assertNotIn("X-Mailer", short)
            self.assertIn("Labels:", short)
            session.toggle_headers()
            full = "\n".join(session.reader)
            self.assertIn("Received: from relay.example", full)
            self.assertIn("X-Mailer: Test Mailer", full)
            self.assertIn("Subject: Café", full)
            self.assertIn("Labels:", full)
            self.assertIn("FULL BODY", full)
            self.assertEqual(session.notice, "all headers")
            # Sticky: another message opened later keeps the full set.
            session.load_reader(fetch=False)
            self.assertIn("X-Mailer: Test Mailer", "\n".join(session.reader))
            session.toggle_headers()
            self.assertNotIn("X-Mailer", "\n".join(session.reader))
        finally:
            engine.store.close()

    def test_raw_source_shows_the_message_as_saved(self) -> None:
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = self._open_full(engine)
            session.toggle_raw_source()
            self.assertTrue(session.raw_source)
            self.assertEqual(session.focus, "reader")
            raw = "\n".join(session.reader)
            self.assertIn("Subject: =?utf-8?q?Caf=C3=A9?=", raw)
            self.assertIn("X-Mailer: Test Mailer", raw)
            self.assertIn("FULL BODY", raw)
            self.assertNotIn("Labels:", raw)
            self.assertIn("raw source", status_bar(session, 120))
            session.toggle_raw_source()
            self.assertFalse(session.raw_source)
            self.assertIn("Subject: Café", "\n".join(session.reader))
            session.toggle_raw_source()
            self.assertTrue(session.leave_message())
            self.assertFalse(session.raw_source)
            self.assertEqual(session.focus, "list")
            self.assertNotIn("=?utf-8?q?", "\n".join(session.reader))
        finally:
            engine.store.close()

    def test_second_edge_scroll_leaves_the_reader(self) -> None:
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            message = IncomingMessage(
                gmail_id="edge1",
                thread_id="tedge",
                label_ids=["INBOX"],
                internal_date_ms=9_800,
                subject="Tall",
                from_addr="Ada <ada@example.com>",
                to_addr="me@gmail.com",
            )
            body = "\n".join(f"line {n}" for n in range(40))
            raw = (
                "Subject: Tall\r\nFrom: Ada <ada@example.com>\r\n"
                f"To: me@gmail.com\r\n\r\n{body}\r\n"
            ).encode()
            self.store.upsert_metadata(message, "ready")
            self.store.write_body(message, raw, body)
            session = MailSession(engine)
            session.focus = "list"
            session.load_messages(fetch=False)
            session.reader_window = 5
            session.activate()
            self.assertEqual(session.focus, "reader")
            session.reveal(geometry(30, 80))
            # Top: first k is a no-op bump, second leaves for the list.
            self.assertEqual(session.reader_scroll, 0)
            session.move(-1)
            self.assertEqual(session.focus, "reader")
            session.move(-1)
            self.assertEqual(session.focus, "list")

            session.activate()
            session.reveal(geometry(30, 80))
            # Bottom: scroll down, then a second j with nowhere to go leaves.
            while session.reader_scroll < max(0, len(session.reader_view) - session.reader_window):
                before = session.reader_scroll
                session.move(1)
                if session.reader_scroll == before:
                    break
            self.assertEqual(session.focus, "reader")
            session.move(1)
            self.assertEqual(session.focus, "reader")
            session.move(1)
            self.assertEqual(session.focus, "list")
        finally:
            engine.store.close()

    def test_hidden_urls_toggle_in_the_reader_and_find_follows(self) -> None:
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            message = IncomingMessage(
                gmail_id="url1",
                thread_id="turl",
                label_ids=["INBOX"],
                internal_date_ms=9_700,
                subject="Links",
                from_addr="Ada <ada@example.com>",
                to_addr="me@gmail.com",
            )
            body = "Notes at https://example.com/notes-page here"
            raw = (
                f"Subject: Links\r\nFrom: Ada <ada@example.com>\r\nTo: me@gmail.com\r\n\r\n{body}\r\n"
            ).encode()
            self.store.upsert_metadata(message, "missing")
            self.store.write_body(message, raw, body)
            session = MailSession(engine)
            session.focus = "list"
            session.load_messages(fetch=False)
            for index, item in enumerate(session.messages):
                if item.gmail_id == "url1":
                    session.message_cursor = index
                    break
            session.load_reader(fetch=False)
            geo = geometry(30, 100)
            session.reveal(geo)
            self.assertIn("Notes at link https://example.com/notes-page here", session.reader_view)

            session.toggle_urls()
            session.reveal(geo)
            self.assertIn("Notes at link here", session.reader_view)
            row = session.reader_view.index("Notes at link here")
            self.assertEqual(session.reader_marks[row].links, ((9, "https://example.com/notes-page"),))

            # Find only matches what is on screen, and highlights where it is drawn.
            session.focus = "reader"
            session.begin_search()
            session.prompt_text = "notes-page"
            session._apply_reader_find()
            self.assertIsNone(session.find_line)
            session.prompt_text = "here"
            session._apply_reader_find()
            match = reader_match(session, geo.width - 2)
            self.assertIsNotNone(match)
            assert match is not None
            self.assertEqual(match[1], len("Notes at link "))

            # Raw source never hides anything.
            session.prompt = None
            session.toggle_raw_source()
            session.reveal(geo)
            self.assertTrue(any("https://example.com/notes-page" in line for line in session.reader_view))
        finally:
            engine.store.close()

    def test_raw_source_needs_the_downloaded_body(self) -> None:
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            message = IncomingMessage(
                gmail_id="bare1",
                thread_id="tbare",
                label_ids=["INBOX"],
                internal_date_ms=9_600,
                subject="Bare",
                from_addr="Ada <ada@example.com>",
                to_addr="me@gmail.com",
            )
            self.store.upsert_metadata(message, "missing")
            session = MailSession(engine)
            session.focus = "list"
            session.load_messages(fetch=False)
            for index, item in enumerate(session.messages):
                if item.gmail_id == "bare1":
                    session.message_cursor = index
                    break
            session.load_reader(fetch=False)
            session.toggle_raw_source()
            self.assertFalse(session.raw_source)
            self.assertEqual(session.notice, "body not downloaded")
            self.assertIsNone(engine.store.message_source("bare1"))
        finally:
            engine.store.close()

    def test_mime_list_lets_you_switch_the_body(self) -> None:
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            both = (
                "Subject: Both\r\n"
                "From: Ada <ada@example.com>\r\n"
                "To: me@gmail.com\r\n"
                "MIME-Version: 1.0\r\n"
                "Content-Type: multipart/alternative; boundary=alt\r\n"
                "\r\n"
                "--alt\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                "\r\n"
                "PLAIN BODY\r\n"
                "--alt\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                "\r\n"
                "<p>HTML BODY</p>\r\n"
                "--alt--\r\n"
            ).encode()
            message = IncomingMessage(
                gmail_id="mime1",
                thread_id="tmime",
                label_ids=["INBOX"],
                internal_date_ms=9_000,
                subject="Both",
                from_addr="Ada <ada@example.com>",
                to_addr="me@gmail.com",
            )
            self.store.upsert_metadata(message, "missing")
            self.store.write_body(message, both, "PLAIN BODY")
            session = MailSession(engine)
            session.focus = "list"
            session.load_messages(fetch=False)
            for index, item in enumerate(session.messages):
                if item.gmail_id == "mime1":
                    session.message_cursor = index
                    break
            session.load_reader(fetch=False)
            listed = "\n".join(render_lines(session, 32, 110))
            self.assertIn("MIME:", listed)
            self.assertIn("text/plain", listed)
            self.assertIn("text/html", listed)
            self.assertIn("PLAIN BODY", listed)
            self.assertNotIn("HTML BODY", listed)
            before = [line for line in session.reader if "text/" in line]
            session.begin_mime()
            self.assertEqual(session.focus, "mime")
            types = [part.content_type for part in session.mime_parts]
            self.assertEqual(types, ["text/plain", "text/html"])
            after = [line for line in session.reader if "text/" in line]
            self.assertEqual(len(before), len(after))
            self.assertEqual(session.mime_parts[session.mime_cursor].content_type, "text/plain")
            self.assertIn("> 1  text/plain", "\n".join(session.reader))
            # Focusing MIME must not change the body for the preferred part.
            focused = "\n".join(session.reader)
            self.assertIn("PLAIN BODY", focused)
            self.assertNotIn("HTML BODY", focused)
            session.move(1)
            self.assertEqual(session.mime_parts[session.mime_cursor].content_type, "text/html")
            switched = "\n".join(session.reader)
            self.assertIn("HTML BODY", switched)
            self.assertNotIn("PLAIN BODY", switched)
            self.assertIn("> 2  text/html", switched)
            self.assertEqual(len([line for line in session.reader if "text/" in line]), len(after))
            status = status_bar(session, 80)
            self.assertIn("j/k switches MIME", status)
            session.close_mime()
            self.assertEqual(session.focus, "reader")
            self.assertFalse(any(line.startswith(">") for line in session.reader))
        finally:
            engine.store.close()

    def test_enter_opens_html_mime_with_xdg_open(self) -> None:
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            both = (
                "Subject: Both\r\n"
                "From: Ada <ada@example.com>\r\n"
                "To: me@gmail.com\r\n"
                "MIME-Version: 1.0\r\n"
                "Content-Type: multipart/alternative; boundary=alt\r\n"
                "\r\n"
                "--alt\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                "\r\n"
                "PLAIN BODY\r\n"
                "--alt\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                "\r\n"
                "<html><body><p>HTML BODY</p></body></html>\r\n"
                "--alt--\r\n"
            ).encode()
            message = IncomingMessage(
                gmail_id="mime-open",
                thread_id="tmime-open",
                label_ids=["INBOX"],
                internal_date_ms=9_050,
                subject="Both",
                from_addr="Ada <ada@example.com>",
                to_addr="me@gmail.com",
            )
            self.store.upsert_metadata(message, "missing")
            self.store.write_body(message, both, "PLAIN BODY")
            session = MailSession(engine)
            session.focus = "list"
            session.load_messages(fetch=False)
            for index, item in enumerate(session.messages):
                if item.gmail_id == "mime-open":
                    session.message_cursor = index
                    break
            session.load_reader(fetch=False)
            session.begin_mime()
            session.activate()
            self.assertIn("not HTML", session.notice)
            session.move(1)
            opened: list[Path] = []

            def fake_open(target: str) -> None:
                opened.append(Path(target))

            session._open_browser = fake_open  # type: ignore[method-assign]
            session.activate()
            self.assertEqual(len(opened), 1)
            self.assertTrue(opened[0].is_file())
            self.assertTrue(opened[0].name.endswith(".html"))
            self.assertIn("<p>HTML BODY</p>", opened[0].read_text(encoding="utf-8"))
            self.assertIn(str(opened[0]), session.notice)
            opened[0].unlink(missing_ok=True)
        finally:
            engine.store.close()

    def test_open_link_uses_the_configured_command(self) -> None:
        from gmail_tui.ui import ReaderMark

        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            session.reader_view = [
                "See link https://example.com/a",
                "and link https://example.com/b",
            ]
            session.reader_marks = [
                ReaderMark(links=((4, "https://example.com/a"),)),
                ReaderMark(links=((4, "https://example.com/b"),)),
            ]
            session.open_command = ("firefox", "-P", "settings")
            spawned: list[list[str]] = []

            def fake_open(target: str) -> None:
                spawned.append([*session.open_command, target])

            session._open_browser = fake_open  # type: ignore[method-assign]
            session.open_link()
            session.open_link()
            self.assertEqual(
                spawned,
                [
                    ["firefox", "-P", "settings", "https://example.com/a"],
                    ["firefox", "-P", "settings", "https://example.com/b"],
                ],
            )
            self.assertIn("https://example.com/b", session.notice)
            session.reader_marks = [ReaderMark()]
            session.reader_view = ["no address here"]
            session._link_key = None
            session.open_link()
            self.assertEqual(session.notice, "no link")
        finally:
            engine.store.close()

    def test_pick_url_opens_the_labeled_link(self) -> None:
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            session.open_command = ("firefox", "-P", "Work")
            opened: list[str] = []
            session._open_browser = opened.append  # type: ignore[method-assign]
            session.begin_url_pick(
                [
                    (4, 1, "link", "https://example.com/a"),
                    (5, 1, "link", "https://example.com/b"),
                ]
            )
            self.assertEqual(session.notice, "pick a link")
            self.assertEqual([hint.label for hint in session.url_hints or ()], ["s", "a"])
            session.type_url_hint("s")
            self.assertEqual(opened, ["https://example.com/a"])
            self.assertIsNone(session.url_hints)
            self.assertIn("opened", session.notice)
            session.begin_url_pick([(4, 1, "link", "https://example.com/a")])
            session.type_url_hint("x")
            self.assertIsNone(session.url_hints)
            self.assertEqual(opened, ["https://example.com/a"])
        finally:
            engine.store.close()

    def test_html_only_body_matches_mime_focus(self) -> None:
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            raw = (
                "Subject: Page\r\n"
                "From: Ada <ada@example.com>\r\n"
                "To: me@gmail.com\r\n"
                "MIME-Version: 1.0\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                "\r\n"
                "<html><body><p>Hello there</p>"
                "<p>See <a href=\"https://example.com/a\">the notes</a>.</p>"
                "</body></html>\r\n"
            ).encode()
            message = IncomingMessage(
                gmail_id="html1",
                thread_id="thtml",
                label_ids=["INBOX"],
                internal_date_ms=9_100,
                subject="Page",
                from_addr="Ada <ada@example.com>",
                to_addr="me@gmail.com",
            )
            # Stored text deliberately differs from a fresh HTML conversion so
            # the reader must prefer the MIME part for both views.
            self.store.upsert_metadata(message, "missing")
            self.store.write_body(message, raw, "STALE STORED TEXT")
            session = MailSession(engine)
            session.focus = "list"
            session.load_messages(fetch=False)
            for index, item in enumerate(session.messages):
                if item.gmail_id == "html1":
                    session.message_cursor = index
                    break
            session.load_reader(fetch=False)
            default = "\n".join(session.reader)
            self.assertIn("Hello there", default)
            self.assertIn("the notes", default)
            self.assertNotIn("STALE STORED TEXT", default)
            session.begin_mime()
            focused = "\n".join(session.reader)
            self.assertIn("Hello there", focused)
            self.assertNotIn("STALE STORED TEXT", focused)

            def without_mark(text: str) -> list[str]:
                rows = []
                for line in text.splitlines():
                    rows.append(line[1:] if line.startswith(("> ", "  ")) and line[2:3].isdigit() else line)
                return rows

            self.assertEqual(without_mark(default), without_mark(focused))
            self.assertIn("> 1  text/html", focused)
        finally:
            engine.store.close()

    def test_attachment_cursor_saves_the_selected_file(self) -> None:
        two = (
            "Subject: Pair\r\n"
            "MIME-Version: 1.0\r\n"
            "Content-Type: multipart/mixed; boundary=two\r\n"
            "\r\n"
            "--two\r\n"
            "Content-Type: text/plain\r\n"
            "\r\n"
            "files\r\n"
            "--two\r\n"
            "Content-Type: text/plain\r\n"
            "Content-Disposition: attachment; filename=\"a.txt\"\r\n"
            "\r\n"
            "aaa\r\n"
            "--two\r\n"
            "Content-Type: text/plain\r\n"
            "Content-Disposition: attachment; filename=\"b.txt\"\r\n"
            "\r\n"
            "bbb\r\n"
            "--two--\r\n"
        ).encode()
        pair = IncomingMessage(
            gmail_id="m6",
            thread_id="t6",
            label_ids=["INBOX"],
            internal_date_ms=9_000,
            subject="Pair",
            from_addr="Ada <ada@example.com>",
            to_addr="me@gmail.com",
            has_attachment=True,
        )
        self.store.upsert_metadata(pair, "missing")
        self.store.write_body(pair, two, "files")
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            self.assertEqual(session.messages[0].gmail_id, "m6")
            session.begin_attachments()
            self.assertTrue(any(line.startswith("> 1  a.txt") for line in session.reader))
            session.move(1)
            self.assertTrue(any(line.startswith("> 2  b.txt") for line in session.reader))
            self.assertFalse(any(line.startswith("> 1  a.txt") for line in session.reader))
            other = Path(self.tmp.name) / "b.txt"
            session.begin_save_attachment()
            session.prompt_text = str(other)
            session.prompt_move(-1)
            session.prompt_accept()
            self.assertEqual(other.read_text(encoding="utf-8"), "bbb")
            session.close_attachments()
            for index, item in enumerate(session.visible):
                if item.gmail_id == "Label_2":
                    session.label_cursor = index
                    break
            session.load_messages(fetch=False)
            self.assertEqual(session.messages[0].gmail_id, "m3")
            session.begin_attachments()
            self.assertEqual(session.focus, "attachments")
            self.assertTrue(any(line.startswith("> 1  note.pdf") for line in session.reader))
            kept = [item.gmail_id for item in session.messages]
            session.prompt_cancel()
            self.assertEqual([item.gmail_id for item in session.messages], kept)
            destination = Path(self.tmp.name) / "saved" / "note.pdf"
            session.begin_save_attachment()
            self.assertEqual(session.prompt, "save")
            session.prompt_text = str(destination)
            listed = "\n".join(render_lines(session, 32, 120))
            self.assertIn("Download", listed)
            self.assertIn("> open", listed)
            self.assertIn("download", listed)
            self.assertIn("esc closes", listed)
            self.assertIn(f"{destination}_", listed)
            self.assertIn("in [", listed)
            self.assertIsNotNone(session.popup)
            session.prompt_move(-1)
            session.prompt_accept()
            self.assertIsNone(session.prompt)
            self.assertTrue(destination.read_bytes().startswith(b"%PDF"))
            self.assertIn(str(destination), session.notice)
            session.begin_save_attachment()
            self.assertEqual(session.prompt, "save")
            _close_prompt(_Keys("[27u"), session)
            self.assertIsNone(session.prompt)
            self.assertEqual(session.focus, "attachments")
            session.begin_save_attachment()
            _close_prompt(_Keys(""), session)
            self.assertIsNone(session.prompt)
            closed = "\n".join(render_lines(session, 32, 120))
            self.assertNotIn("esc closes", closed)
            folder = Path(self.tmp.name) / "open"
            folder.mkdir()
            (folder / "note.pdf").write_bytes(b"taken")
            session.download_dir = folder
            opened: list[Path] = []
            session._xdg_open = lambda path: opened.append(path)
            session.begin_save_attachment()
            session.prompt_cancel()
            self.assertIsNone(session.prompt)
            self.assertEqual(opened, [])
            self.assertEqual((folder / "note.pdf").read_bytes(), b"taken")
            session.begin_save_attachment()
            chosen = "\n".join(render_lines(session, 32, 120))
            self.assertIn("> open", chosen)
            session.prompt_accept()
            self.assertEqual([path.name for path in opened], ["note-1.pdf"])
            self.assertTrue(opened[0].read_bytes().startswith(b"%PDF"))
            self.assertEqual((folder / "note.pdf").read_bytes(), b"taken")
            self.assertIsNone(session.prompt)
            self.assertIn("note-1.pdf", session.notice)
            again = reserve_download(folder, "note.pdf")
            self.assertEqual(again.name, "note-2.pdf")
            root = Path(self.tmp.name) / "complete"
            (root / "alpha").mkdir(parents=True)
            (root / "alpine.txt").write_text("x", encoding="utf-8")
            (root / "beta").mkdir()
            session.begin_save_attachment()
            session.prompt_text = str(root / "al")
            session.prompt_complete()
            self.assertEqual(session.prompt_text, str(root / "alp"))
            self.assertEqual(session.prompt_hint, "2 matches")
            _edit_prompt(session, "\t", defaults())
            self.assertEqual(session.prompt_text, str(root / "alpha") + os.sep)
            _edit_prompt(session, "\t", defaults())
            self.assertEqual(session.prompt_text, str(root / "alpine.txt"))
            _edit_prompt(session, curses.KEY_BTAB, defaults())
            self.assertEqual(session.prompt_text, str(root / "alpha") + os.sep)
            session._clear_save_complete()
            session.prompt_text = str(root / "alpin")
            session.prompt_complete()
            self.assertEqual(session.prompt_text, str(root / "alpine.txt"))
            session.prompt_text = str(root / "zz")
            session.prompt_complete()
            self.assertEqual(session.prompt_hint, "no match")
            listed = "\n".join(render_lines(session, 32, 140))
            self.assertIn("tab completes", listed)
            self.assertIn("up/down pick", listed)
            self.assertIn("? folders", listed)
            before = session.prompt_text
            cursor = session.prompt_cursor
            _edit_prompt(session, "j", defaults())
            _edit_prompt(session, "k", defaults())
            _edit_prompt(session, "d", defaults())
            _edit_prompt(session, "o", defaults())
            self.assertEqual(session.prompt_text, before + "jkdo")
            self.assertEqual(session.prompt_cursor, cursor)
            _edit_prompt(session, curses.KEY_UP, defaults())
            self.assertEqual(session.prompt_cursor, cursor - 1)
            session.prompt_text = str(root) + os.sep
            _edit_prompt(session, "?", defaults())
            self.assertEqual(session._save_listing, ["alpha/", "beta/"])
            folders = "\n".join(render_lines(session, 32, 140))
            self.assertIn("Folders:", folders)
            self.assertIn("alpha/", folders)
            self.assertIn("beta/", folders)
            self.assertNotIn("alpine.txt", "\n".join(session._save_listing))
            _edit_prompt(session, "?", defaults())
            self.assertEqual(session._save_listing, [])
            session.prompt_cancel()
            session.close_attachments()
            self.assertEqual(session.focus, "reader")
            self.assertFalse(any(line.startswith(">") for line in session.reader))
            session.begin_view_image()
            self.assertEqual(session.focus, "attachments")
            self.assertIsNone(session.prompt)
            self.assertEqual(session.notice, "not an image")
            session.close_attachments()
            self.assertEqual(session.focus, "reader")
            picture = (
                "Subject: Shot\r\n"
                "MIME-Version: 1.0\r\n"
                "Content-Type: multipart/mixed; boundary=pic\r\n"
                "\r\n"
                "--pic\r\n"
                "Content-Type: text/plain\r\n"
                "\r\n"
                "photo\r\n"
                "--pic\r\n"
                "Content-Type: image/png\r\n"
                "Content-Disposition: attachment; filename=\"cat.png\"\r\n"
                "\r\n"
                "\x89PNG\r\n"
                "--pic\r\n"
                "Content-Type: image/png\r\n"
                "Content-Disposition: attachment; filename=\"dog.png\"\r\n"
                "\r\n"
                "\x89PNG\r\n"
                "--pic--\r\n"
            ).encode()
            shot = IncomingMessage(
                gmail_id="m7",
                thread_id="t7",
                label_ids=["INBOX"],
                internal_date_ms=10_000,
                subject="Shot",
                from_addr="Ada <ada@example.com>",
                to_addr="me@gmail.com",
                has_attachment=True,
            )
            self.store.upsert_metadata(shot, "missing")
            self.store.write_body(shot, picture, "photo")
            for index, item in enumerate(session.visible):
                if item.gmail_id == "INBOX":
                    session.label_cursor = index
                    break
            session.load_messages(fetch=False)
            self.assertEqual(session.messages[0].gmail_id, "m7")
            session.sixel = True
            session.reader_window = 12
            session.load_reader(fetch=False)
            # Reading the mail must show the body; pictures wait for A / v.
            self.assertEqual(session.image_slots, [])
            self.assertIn("photo", "\n".join(session.reader))
            session.begin_view_image()
            self.assertEqual(session.focus, "attachments")
            self.assertIsNone(session.prompt)
            self.assertEqual(session.notice, "")
            self.assertEqual(session.image_slots[0][1], 1)
            self.assertGreater(session.image_slots[0][0], 5)
            self.assertIn("photo", "\n".join(session.reader))
            self.assertEqual(session.bitmap_indexes(), (1, 2))
            self.assertIn("From:", "\n".join(session.reader))
            listed = "\n".join(render_lines(session, 32, 120))
            self.assertIn("cat.png", listed)
            self.assertIn("MIME:", listed)
            self.assertNotIn("Download", listed)
            self.assertNotIn("xdg-open", listed)
            session.move(1)
            self.assertIsNone(session.prompt)
            self.assertEqual(session.attachments[session.attachment_cursor].filename, "dog.png")
            self.assertEqual(session.image_slots[0][1], 2)
            session.move(-1)
            self.assertEqual(session.image_slots[0][1], 1)
        finally:
            engine.store.close()

    def test_reader_wraps_long_lines_onto_the_next_row(self) -> None:
        view, starts = wrap_lines(["hello world", ""], 8)
        self.assertEqual(view, ["hello", "world", ""])
        self.assertEqual(starts, [0, 2])
        self.assertEqual(wrap_lines(["abcdefghij"], 4)[0], ["abcd", "efgh", "ij"])
        wide = "ab\u231bcd"
        pieces = wrap_lines([wide], 3)[0]
        self.assertTrue(all(_display_width(piece) <= 3 for piece in pieces))
        self.assertIn("\u231b", "".join(pieces))

        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            session._set_reader(["alpha beta gamma delta epsilon zeta eta theta"])
            session._rewrap_reader(12)
            self.assertEqual(session.reader_view[0], "alpha beta")
            self.assertNotIn("theta", session.reader_view[0])
            self.assertTrue(all(_display_width(row) <= 12 for row in session.reader_view))
            session.focus = "reader"
            session.reader_window = 2
            session._move_reader(1)
            self.assertEqual(session.reader_scroll, 1)
            words = " ".join(f"word{number:02d}" for number in range(40))
            session._set_reader([words])
            session.reader_scroll = 0
            screen = render_lines(session, 24, 80)
            width = geometry(24, 80).width - 2
            self.assertGreater(len(session.reader_view), 1)
            self.assertTrue(all(_display_width(row) <= width for row in session.reader_view))
            first = next(line for line in screen if "word00" in line)
            self.assertNotIn("word39", first)
            self.assertIn("word39", session.reader_view[-1])
        finally:
            engine.store.close()

    def test_holding_list_releases_an_accepted_change(self) -> None:
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            engine.store.apply_local_labels("m1", remove=["UNREAD"])
            groups = engine.store.queue_groups()
            engine.store.complete_push("m1", groups["m1"], ["INBOX"], "50")
            session = MailSession(engine)
            self.assertIn("holding 1", format_activity(session, 80))
            self.assertIn(" H", format_activity(session, 80))
            session.begin_holding()
            self.assertEqual(session.prompt, "holding")
            self.assertEqual(session.held[0].subject, "First")
            self.assertEqual(session.held[0].op, "remove")
            listed = "\n".join(render_lines(session, 24, 100))
            self.assertIn("First", listed)
            self.assertIn("-UNREAD", listed)
            self.assertIn("enter releases", listed)
            kept = [item.gmail_id for item in session.messages]
            session.prompt_cancel()
            self.assertIsNone(session.prompt)
            self.assertEqual([item.gmail_id for item in session.messages], kept)
            self.assertEqual(engine.store.queue_counts(), (1, 0))
            session.begin_holding()
            session.prompt_accept()
            self.assertIsNone(session.prompt)
            self.assertEqual(session.notice, "released")
            self.assertEqual(engine.store.queue_counts(), (0, 0))
            self.assertFalse(session.messages[0].unread)
            session.begin_holding()
            self.assertEqual(session.notice, "nothing holding")
        finally:
            engine.store.close()

    def test_order_key_flips_the_list_and_keeps_the_open_message(self) -> None:
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            session.toggle_order()
            self.assertEqual([item.subject for item in session.messages], ["Second", "First"])
            self.assertEqual(session.messages[session.message_cursor].subject, "First")
            listed = "\n".join(render_lines(session, 32, 120))
            self.assertIn("oldest", listed)
            when = format_when(session.messages[0].internal_date_ms)
            row = next(line for line in listed.splitlines() if "Second" in line)
            self.assertLess(row.index(when), row.index("bea@example.com"))
        finally:
            engine.store.close()

    def test_order_key_keeps_pages_already_loaded(self) -> None:
        import gmail_tui.ui as ui

        previous = ui.PAGE
        ui.PAGE = 3
        try:
            for number in range(9):
                self._message(
                    f"ord{number}",
                    f"to{number}",
                    50_000 + number,
                    f"Order {number}",
                    "Ada <ada@example.com>",
                    "me@gmail.com",
                    ["INBOX"],
                    "body",
                )
            engine = Engine.open(self.tmp.name, NullGmail())
            try:
                session = MailSession(engine)
                session.focus = "list"
                session.load_messages(fetch=False)
                session._fill_page(force=True)
                session._fill_page(force=True)
                self.assertGreater(len(session.messages), ui.PAGE)
                before_count = len(session.messages)
                depth = len(session._mail_threads)
                session.message_cursor = len(session.messages) - 1
                session.message_scroll = max(0, session.message_cursor - 2)
                held = session.messages[session.message_cursor].gmail_id
                session.toggle_order()
                self.assertFalse(session.newest_first)
                self.assertGreaterEqual(len(session._mail_threads), depth)
                self.assertEqual(len(session.messages), before_count)
                self.assertEqual(session.messages[session.message_cursor].gmail_id, held)
                # Without keeping depth, toggle_order used to drop back to one page.
                self.assertGreater(len(session.messages), ui.PAGE)
            finally:
                engine.store.close()
        finally:
            ui.PAGE = previous

    def test_list_does_not_wrap_and_page_down_reaches_the_end(self) -> None:
        for number in range(4):
            self._message(
                f"extra{number}",
                f"tx{number}",
                4_000 + number,
                f"Extra {number}",
                "Ada <ada@example.com>",
                "me@gmail.com",
                ["INBOX"],
                "body",
            )
        import gmail_tui.ui as ui

        previous = ui.PAGE
        ui.PAGE = 2
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            self.assertEqual(len(session.messages), 2)
            self.assertTrue(session._more)
            session.focus = "list"
            session.message_window = 2
            first = session.messages[0].gmail_id
            session.page(1)
            self.assertGreater(session.message_cursor, 0)
            self.assertNotEqual(session.messages[session.message_cursor].gmail_id, first)
            for _ in range(12):
                before = session.message_cursor
                session.page(1)
                if session.message_cursor == before:
                    break
            self.assertFalse(session._more)
            self.assertEqual(session.message_cursor, len(session.messages) - 1)
            last = session.messages[-1].gmail_id
            session.page(1)
            session.move(1)
            self.assertEqual(session.messages[session.message_cursor].gmail_id, last)
            session.move(-1)
            session.message_cursor = 0
            session.move(-1)
            session.page(-1)
            self.assertEqual(session.message_cursor, 0)
            anchor = session.messages[0].gmail_id
            session.message_cursor = len(session.messages) - 1
            deep = session.messages[session.message_cursor].gmail_id
            self.assertNotEqual(deep, anchor)
            session.refresh_view()
            self.assertEqual(session.messages[session.message_cursor].gmail_id, deep)
        finally:
            ui.PAGE = previous
            engine.store.close()

    def test_list_keeps_a_margin_while_more_mail_remains(self) -> None:
        for number in range(20):
            self._message(
                f"up{number}",
                f"tu{number}",
                20_000 + number,
                f"Up {number}",
                "Ada <ada@example.com>",
                "me@gmail.com",
                ["INBOX"],
                "body",
            )
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            session.focus = "list"
            window = 10
            geo = Geometry(40, 100, 24, 20, 8, window, 6, 1)
            session.message_window = window
            session.message_cursor = 0
            session.message_scroll = 0
            session.reveal(geo)
            # Walk down: the highlight must not sit on the last screen row
            # while more messages remain below.
            for _ in range(15):
                session.move(1)
                session.reveal(geo)
                row = session.message_cursor - session.message_scroll
                more_below = (
                    session.message_cursor < len(session.messages) - 1 or session._more
                )
                if more_below:
                    self.assertLess(row, window - 1)
                    self.assertGreaterEqual(window - 1 - row, 2)
            # At the real end the last row may use the bottom of the viewport.
            session.message_cursor = len(session.messages) - 1
            session.message_scroll = max(0, len(session.messages) - window)
            session.reveal(geo)
            self.assertEqual(
                session.message_cursor - session.message_scroll,
                min(window - 1, len(session.messages) - 1),
            )
            # Walking up keeps a top margin while rows remain above.
            session.message_cursor = 12
            session.message_scroll = 8
            session.reveal(geo)
            session.move(-1)
            session.reveal(geo)
            self.assertGreater(session.message_cursor - session.message_scroll, 0)
            self.assertGreater(session.message_scroll, 0)
        finally:
            engine.store.close()

    def test_refresh_view_shows_new_mail_and_keeps_the_open_thread(self) -> None:
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            session.focus = "list"
            self.assertEqual([item.subject for item in session.messages][:2], ["First", "Second"])
            first = session.messages[0]
            self.assertEqual(first.gmail_id, "m1")
            self._message(
                "nw1",
                "t-nw1",
                90_000,
                "Brand new",
                "Cy <cy@example.com>",
                "me@gmail.com",
                ["INBOX", "UNREAD"],
                "hello",
            )
            session.refresh_view()
            self.assertEqual(session.messages[0].subject, "Brand new")
            self.assertEqual(session.messages[session.message_cursor].gmail_id, "m1")

            # A reply changes the row identity (single → thread head). The old
            # list must not be restored; the new reply has to appear.
            session.message_cursor = next(
                index for index, item in enumerate(session.messages) if item.gmail_id == "m1"
            )
            before_key = session.messages[session.message_cursor].row_key
            self._message(
                "nw-reply",
                "t1",
                91_000,
                "Re: First",
                "Cy <cy@example.com>",
                "me@gmail.com",
                ["INBOX", "UNREAD"],
                "reply body",
            )
            session.refresh_view()
            head = next(item for item in session.messages if item.thread_id == first.thread_id)
            self.assertNotEqual(head.row_key, before_key)
            self.assertGreaterEqual(head.thread_count, 2)
            self.assertEqual(session.messages[session.message_cursor].thread_id, first.thread_id)
            self.assertIn("nw-reply", head.member_ids)
            subjects = [item.subject for item in session.messages]
            self.assertTrue(any("First" in subject for subject in subjects))
        finally:
            engine.store.close()

    def test_refresh_view_merges_new_mail_into_a_deep_page(self) -> None:
        for number in range(4):
            self._message(
                f"deep{number}",
                f"td{number}",
                4_000 + number,
                f"Deep {number}",
                "Ada <ada@example.com>",
                "me@gmail.com",
                ["INBOX"],
                "body",
            )
        import gmail_tui.ui as ui

        previous = ui.PAGE
        ui.PAGE = 2
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            session.focus = "list"
            while session._more:
                session._fill_page(force=True)
            session.message_cursor = len(session.messages) - 1
            deep = session.messages[session.message_cursor].gmail_id
            self._message(
                "top-new",
                "t-top-new",
                99_000,
                "Top new",
                "Cy <cy@example.com>",
                "me@gmail.com",
                ["INBOX", "UNREAD"],
                "hi",
            )
            session.refresh_view()
            self.assertEqual(session.messages[0].subject, "Top new")
            self.assertEqual(session.messages[session.message_cursor].gmail_id, deep)
        finally:
            ui.PAGE = previous
            engine.store.close()

    def test_observe_list_bump_bypasses_refresh_throttle(self) -> None:
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            session.focus = "list"
            job = BackgroundSync(engine)
            session.observe(job)
            held = session.messages[session.message_cursor].gmail_id
            self._message(
                "late",
                "t-late",
                95_000,
                "Late arrival",
                "Cy <cy@example.com>",
                "me@gmail.com",
                ["INBOX", "UNREAD"],
                "body",
            )
            job.dirty = True
            job.list_bump = True
            session._refreshed_at = time.monotonic()
            session.observe(job)
            self.assertEqual(session.messages[0].subject, "Late arrival")
            self.assertEqual(session.messages[session.message_cursor].gmail_id, held)
            self.assertEqual(session.message_scroll, 0)
            self.assertFalse(job.dirty)
            self.assertFalse(job.list_bump)
        finally:
            engine.store.close()

    def _arrive(self, gmail_id: str, date: int) -> None:
        self._message(
            gmail_id,
            f"t-{gmail_id}",
            date,
            f"Arrived {gmail_id}",
            "Cy <cy@example.com>",
            "me@gmail.com",
            ["INBOX", "UNREAD"],
            "hi",
        )

    def test_new_mail_never_moves_the_selection(self) -> None:
        for number in range(8):
            self._message(
                f"vis{number}",
                f"tv{number}",
                40_000 + number,
                f"Visible {number}",
                "Ada <ada@example.com>",
                "me@gmail.com",
                ["INBOX"],
                "body",
            )
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            session.focus = "list"
            session.message_window = 3

            # Newest first, scrolled down: the selected row keeps its screen row.
            session.refresh_view()
            session.message_cursor = 4
            session.message_scroll = 3
            held = session.messages[4].gmail_id
            self._arrive("new1", 99_000)
            session.refresh_view(pin_new_mail=True)
            self.assertEqual(session.messages[0].gmail_id, "new1")
            self.assertEqual(session.messages[session.message_cursor].gmail_id, held)
            self.assertEqual(session.message_cursor - session.message_scroll, 1)

            # Newest first, near the top: arrivals show up, selection unchanged.
            session.message_cursor = 1
            session.message_scroll = 0
            held = session.messages[1].gmail_id
            self._arrive("new2", 99_100)
            session.refresh_view(pin_new_mail=True)
            self.assertEqual(session.messages[0].gmail_id, "new2")
            self.assertEqual(session.messages[session.message_cursor].gmail_id, held)
            self.assertEqual(session.message_scroll, 0)

            # Oldest first, near the bottom: the arrival scrolls into view.
            session.toggle_order()
            self.assertFalse(session.newest_first)
            last = len(session.messages) - 1
            session.message_cursor = last - 1
            session.message_scroll = last - 2
            held = session.messages[session.message_cursor].gmail_id
            self._arrive("old1", 100_000)
            session.refresh_view(pin_new_mail=True)
            self.assertEqual(session.messages[-1].gmail_id, "old1")
            self.assertEqual(session.messages[session.message_cursor].gmail_id, held)
            self.assertEqual(session.message_scroll, len(session.messages) - 3)

            # Oldest first, at the top: nothing on screen moves.
            session.message_cursor = 0
            session.message_scroll = 0
            held = session.messages[0].gmail_id
            self._arrive("old2", 100_100)
            session.refresh_view(pin_new_mail=True)
            self.assertEqual(session.messages[-1].gmail_id, "old2")
            self.assertEqual(session.message_cursor, 0)
            self.assertEqual(session.messages[0].gmail_id, held)
            self.assertEqual(session.message_scroll, 0)
        finally:
            engine.store.close()

    def test_tab_counts_stay_put_when_switching_to_an_account(self) -> None:
        from types import SimpleNamespace

        from gmail_tui import tui

        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            inbox, unread = session.inbox_count, session.inbox_unread
            # Background sync stores more mail while the tab is not open.
            self._arrive("second", 20_000)
            self._arrive("third", 30_000)
            tab = Account("me@gmail.com", Path(self.tmp.name), 0, 0)
            slot = SimpleNamespace(engine=engine, session=session, job=SimpleNamespace(mail_alert=0))
            tui._remember(None, tab, slot)
            self.assertEqual((tab.inbox, tab.unread), (inbox + 2, unread + 2))
            # Once the tab is open its badge comes from the session.
            header = render_lines(session, 30, 100, [tab], 0)[0]
            self.assertIn(f"{unread + 2} [{inbox + 2}]", header)
        finally:
            engine.store.close()

    def test_refresh_keeps_top_visible_when_mail_arrives_above_selection(self) -> None:
        """A new top row must not shove the viewport down by one."""
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            session.focus = "list"
            session.message_window = 8
            self.assertEqual(session.message_cursor, 0)
            self.assertEqual(session.message_scroll, 0)
            held = session.messages[0].gmail_id
            self._message(
                "above",
                "t-above",
                90_000,
                "Above",
                "Cy <cy@example.com>",
                "me@gmail.com",
                ["INBOX", "UNREAD"],
                "hi",
            )
            session.refresh_view()
            self.assertEqual(session.messages[0].subject, "Above")
            self.assertEqual(session.messages[session.message_cursor].gmail_id, held)
            self.assertEqual(session.message_scroll, 0)
            self.assertEqual(session.messages[0].subject, "Above")

            # A second refresh after the list already contains the arrival still
            # pins the viewport to the top when asked (poll finish often looks
            # like this after an earlier unpinned refresh).
            session.message_scroll = 1
            session.refresh_view(pin_new_mail=True)
            self.assertEqual(session.message_scroll, 0)
            self.assertEqual(session.messages[0].subject, "Above")
        finally:
            engine.store.close()

    def test_search_results_keep_the_highlight_on_the_row_just_left(self) -> None:
        for number in range(12):
            self._message(
                f"find{number}",
                f"tf{number}",
                30_000 + number,
                f"Find {number}",
                "Ada <ada@example.com>",
                "me@gmail.com",
                ["INBOX"],
                "needle body",
            )
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            session.focus = "list"
            session.begin_search()
            session.prompt_type("needle")
            session.message_cursor = 3
            session.message_scroll = 8
            kept = session.messages[3].gmail_id
            session.prompt_accept()
            self.assertGreater(len(session.messages), 8)
            self.assertEqual(session.messages[session.message_cursor].gmail_id, kept)
            self.assertLessEqual(session.message_scroll, session.message_cursor)
            self.assertLess(session.message_cursor, session.message_scroll + session.message_window)
            window = 5
            session.message_window = window
            session.message_cursor = 4
            session.message_scroll = 0
            for step in range(6):
                session.move(1)
                offset = session.message_cursor - session.message_scroll
                self.assertGreaterEqual(offset, 0)
                self.assertLess(offset, window)
                self.assertEqual(
                    session.messages[session.message_scroll + offset].gmail_id,
                    session.messages[session.message_cursor].gmail_id,
                )
                more_below = (
                    session.message_cursor < len(session.messages) - 1 or session._more
                )
                if more_below:
                    self.assertLess(offset, window - 1)
            # Not yet on the last message: still keep at least one row below.
            self.assertLess(session.message_cursor, len(session.messages) - 1)
            self.assertLess(session.message_cursor - session.message_scroll, window - 1)
            for _ in range(6):
                session.move(-1)
                offset = session.message_cursor - session.message_scroll
                self.assertGreaterEqual(offset, 0)
                self.assertLess(offset, window)
                if session.message_scroll > 0:
                    self.assertGreater(offset, 0)
        finally:
            engine.store.close()

    def test_scrollbar_and_position_show_how_far_down_the_list_is(self) -> None:
        self.assertEqual(_scrollbar_column(4, 2, 0, False), [])
        top = _scrollbar_column(4, 20, 0, False)
        bottom = _scrollbar_column(4, 20, 19, False)
        self.assertEqual(top[0], "█")
        self.assertEqual(bottom[-1], "█")
        self.assertNotEqual(top, bottom)
        more = _scrollbar_column(4, 20, 19, True)
        self.assertNotEqual(more[-1], "█")
        for number in range(20):
            self._message(
                f"many{number}",
                f"tm{number}",
                10_000 + number,
                f"Many {number}",
                "Ada <ada@example.com>",
                "me@gmail.com",
                ["INBOX"],
                "body",
            )
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            screen = render_lines(session, 32, 100)
            self.assertIn("1/", "\n".join(screen))
            self.assertTrue(any("█" in line for line in screen))
            marked = [line for line in screen if line.endswith(("█", "░"))]
            self.assertGreaterEqual(len(marked), 2)
            for line in marked:
                self.assertEqual(_display_width(line), 100)
        finally:
            engine.store.close()

    def test_scrollbar_stays_on_rows_that_contain_wide_characters(self) -> None:
        self._message(
            "wide",
            "tw",
            80_000,
            "⌛",
            "Ada <ada@example.com>",
            "me@gmail.com",
            ["INBOX"],
            "body",
        )
        self._message(
            "zw",
            "tz",
            70_000,
            "A\u200bB",
            "Ada <ada@example.com>",
            "me@gmail.com",
            ["INBOX"],
            "body",
        )
        for number in range(20):
            self._message(
                f"edge{number}",
                f"te{number}",
                20_000 + number,
                f"Edge {number}",
                "Ada <ada@example.com>",
                "me@gmail.com",
                ["INBOX"],
                "body",
            )
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            screen = render_lines(session, 32, 100)
            wide = [line for line in screen if "⌛" in line and line.endswith(("█", "░"))]
            hidden = [line for line in screen if "A\u200bB" in line and line.endswith(("█", "░"))]
            self.assertEqual(len(wide), 1)
            self.assertEqual(len(hidden), 1)
            for line in wide + hidden:
                self.assertTrue(line.endswith(("█", "░")))
                self.assertEqual(_display_width(line), 100)
        finally:
            engine.store.close()

    def test_the_focused_pane_title_is_bracketed(self) -> None:
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            geo = geometry(32, 100)
            session.focus = "labels"
            lines = render_lines(session, 32, 100)
            self.assertIn("[Labels]", lines[geo.top])
            self.assertIn(" Message ", lines[geo.list_h])
            self.assertNotIn("[Inbox", lines[geo.top])
            session.focus = "list"
            lines = render_lines(session, 32, 100)
            self.assertIn("[Inbox", lines[geo.top])
            self.assertIn(" Labels ", lines[geo.top])
            self.assertIn(" Message ", lines[geo.list_h])
            session.focus = "reader"
            lines = render_lines(session, 32, 100)
            self.assertIn("[Message]", lines[geo.list_h])
            self.assertIn(" Labels ", lines[geo.top])
            self.assertNotIn("[Inbox", lines[geo.top])
            session.focus = "attachments"
            lines = render_lines(session, 32, 100)
            self.assertIn("[Message]", lines[geo.list_h])
        finally:
            engine.store.close()

    def test_the_reader_starts_at_the_left_of_the_folder_pane(self) -> None:
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            geo = geometry(32, 100)
            lines = render_lines(session, 32, 100)
            header = lines[geo.list_h]
            self.assertTrue(header.startswith("├"))
            self.assertTrue(header.endswith("┤"))
            self.assertEqual(header[geo.sidebar_w - 1], "┴")
            self.assertLess(header.index("Message"), geo.sidebar_w)
            body = lines[geo.list_h + 1]
            self.assertTrue(body.startswith("│From:"))
            self.assertNotEqual(body[geo.sidebar_w - 1], "│")
            self.assertNotIn("Inbox", body)
        finally:
            engine.store.close()

    def test_the_mail_list_header_has_a_rule_under_it(self) -> None:
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            screen = render_lines(session, 32, 110)
            header = next(
                index
                for index, line in enumerate(screen)
                if "From" in line and "Subject" in line
            )
            rule = screen[header + 1]
            top = screen[geometry(32, 110).top]
            pane = rule[geometry(32, 110).sidebar_w - 1 :]
            self.assertTrue(pane.startswith("├"))
            self.assertIn("─", pane)
            self.assertTrue(pane.endswith("┤"))
            self.assertNotIn("Subject", rule)
            geo = geometry(32, 110)
            separators = [
                index
                for index, char in enumerate(screen[header])
                if char == "│" and geo.sidebar_w <= index < geo.width - 1
            ]
            self.assertGreaterEqual(len(separators), 4)
            aligned = [column for column in separators if top[column] == "┬"]
            self.assertGreaterEqual(len(aligned), 1)
            for column in separators:
                self.assertEqual(rule[column], "┼", rule)
            bar = status_bar(session, 110)
            self.assertEqual(_display_width(bar), 109)
            for focus in ("labels", "list", "reader"):
                _row, column, span = _pane_highlight(focus, geo)
                self.assertGreater(column, 0)
                self.assertLess(column + span, geo.width)
            self.assertEqual(_open_folder_attr("labels"), curses.A_REVERSE | curses.A_BOLD)
            self.assertEqual(_open_folder_attr("list"), curses.A_BOLD)
            self.assertEqual(_open_folder_attr("reader"), curses.A_BOLD)
            self.assertEqual(_open_message_attr("list"), curses.A_REVERSE | curses.A_BOLD)
            self.assertEqual(_open_message_attr("reader"), curses.A_REVERSE)
            self.assertEqual(_open_message_attr("attachments"), curses.A_REVERSE)
            popup = (10, 20, 8, 40)
            self.assertEqual(_chgat_segments(12, 0, 100, popup), [(0, 20), (60, 40)])
            self.assertEqual(_chgat_segments(9, 0, 100, popup), [(0, 100)])
            self.assertTrue(_popup_covers(popup, 12, 25, 10))
            self.assertFalse(_popup_covers(popup, 12, 0, 20))
            session.focus = "reader"
            self.assertTrue(session.leave_message())
            self.assertEqual(session.focus, "list")
            self.assertFalse(session.leave_message())
            session.focus = "attachments"
            self.assertTrue(session.leave_message())
            self.assertEqual(session.focus, "list")
            session.focus = "labels"
            self.assertFalse(session.leave_message())
            self.assertEqual(session.focus, "labels")
            kept = [item.subject for item in session.messages]
            session.begin_quit()
            self.assertEqual(session.prompt, "quit")
            asked = "\n".join(render_lines(session, 24, 80))
            self.assertIn("Really quit?", asked)
            self.assertIn("enter quits", asked)
            self.assertIn("esc cancels", asked)
            self.assertIsNotNone(session.popup)
            session.prompt_type("q")
            self.assertEqual(session.prompt, "quit")
            session.prompt_cancel()
            self.assertIsNone(session.prompt)
            self.assertFalse(session.quitting)
            self.assertEqual([item.subject for item in session.messages], kept)
            self.assertNotIn("Really quit?", "\n".join(render_lines(session, 24, 80)))
            session.begin_quit()
            session.prompt_accept()
            self.assertTrue(session.quitting)
            self.assertIsNone(session.prompt)
        finally:
            engine.store.close()

    def test_selecting_a_message_opens_it_and_labels_filter_the_list(self) -> None:
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            self.assertEqual(session.focus, "list")
            self.assertEqual(session.current_label().gmail_id, "INBOX")
            self.assertEqual(session.messages[session.message_cursor].subject, "First")
            self.assertIn("first body", "\n".join(session.reader))
            self.assertFalse(session.needs_fetch)
            session.focus = "list"
            session.move(1)
            self.assertIn("second body", "\n".join(session.reader))
            self.assertIn("Bea <bea@example.com>", session.reader[0])
            session.focus = "labels"
            session.label_cursor = 0
            session.move(0)
            names = [item.name for item in session.visible]
            self.assertLess(names.index("Work"), names.index("Clients"))
            session.label_cursor = names.index("Clients") - 1
            session.move(1)
            self.assertEqual(session.current_label().name, "Clients")
            self.assertEqual([item.subject for item in session.messages], ["Files"])
            self.assertIn("see the file", "\n".join(session.reader))
            self.assertIn("1  note.pdf  application/pdf", "\n".join(session.reader))
            screen = "\n".join(render_lines(session, 32, 110))
            self.assertIn("┌", screen)
            self.assertIn("├", screen)
            self.assertIn("┴", screen)
            self.assertNotIn("+", screen.splitlines()[0])
            self.assertIn("Labels", screen)
            self.assertIn("From", screen)
            self.assertIn("Subject", screen)
            header = next(
                line for line in screen.splitlines() if "From" in line and "Subject" in line and "Labels" in line
            )
            self.assertIn(" A", header)
            self.assertNotIn("Att", header)
            self.assertIn("Files", screen)
            self.assertIn("Message", screen)
            self.assertIn("see the file", screen)
            self.assertIn("in [############]", screen)
            self.assertIn("out [############]", screen)
            self.assertIn("to Gmail: none", screen)
            self.store.apply_local_labels("m1", add=["STARRED"])
            queued = "\n".join(render_lines(session, 32, 140))
            self.assertIn("in [############]", queued)
            self.assertIn("out [------------]", queued)
            self.assertIn("to Gmail: 1", queued)
            self.assertIn("+STARRED", queued)
        finally:
            engine.store.close()

    def test_help_lists_the_commands_for_the_focused_pane(self) -> None:
        bindings = defaults().bindings
        folders = help_lines("labels", bindings)
        mail = help_lines("list", bindings)
        message = help_lines("reader", bindings)
        files = help_lines("attachments", bindings)
        folder_text = "\n".join(folders)
        self.assertIn("next folder", folder_text)
        self.assertIn("filter folders", folder_text)
        self.assertNotIn("reply", folder_text)
        self.assertIn("archive", "\n".join(mail))
        self.assertIn("search mail", "\n".join(mail))
        self.assertTrue(any(line.startswith("q Q") and "back to the mail list" in line for line in message))
        self.assertTrue(any("find in this message" in line for line in message))
        self.assertTrue(any(line.startswith("v ") and "show the selected picture" in line for line in message))
        self.assertNotIn("expand a folder", "\n".join(message))
        self.assertTrue(any(line.startswith("esc") and "back to the message" in line for line in files))
        self.assertTrue(any("save or open the file" in line for line in files))
        self.assertTrue(any(line.startswith("v ") and "show the selected picture" in line for line in files))
        custom = dict(bindings)
        custom["archive"] = ("z",)
        custom["quit"] = ("x",)
        rebound = help_lines("list", custom)
        self.assertTrue(any(line.startswith("z ") and line.endswith("archive") for line in rebound))
        self.assertTrue(any(line.startswith("x ") and line.endswith("quit") for line in rebound))
        self.assertFalse(any(line.startswith("a ") for line in rebound))

        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            session.focus = "reader"
            session.help_lines = message
            session.help_open = True
            screen = render_lines(session, 40, 110)
            self.assertTrue(any("Help" in line and "Message" in line for line in screen))
            self.assertTrue(any("find in this message" in line for line in screen))
            self.assertTrue(any("back to the mail list" in line for line in screen))
            session.focus = "labels"
            session.help_lines = folders
            listed = "\n".join(render_lines(session, 40, 110))
            self.assertIn("Help", listed)
            self.assertIn("Folders", listed)
            self.assertIn("next folder", listed)
            self.assertNotIn("find in this message", listed)
        finally:
            engine.store.close()

    def test_search_follows_the_focused_pane_and_label_prompt_filters(self) -> None:
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            session.focus = "labels"
            session.begin_search()
            session.prompt_type("clients")
            self.assertEqual(session.visible[0].gmail_id, "Label_2")
            self.assertIn("Work/Clients", session.visible[0].name)
            session.prompt_cancel()
            self.assertIsNone(session.prompt)
            self.assertEqual(session.current_label().gmail_id, "INBOX")

            session.begin_label()
            session.prompt_type("cli")
            self.assertEqual(session.completions[0].gmail_id, "Label_2")
            listed = "\n".join(render_lines(session, 32, 100))
            self.assertIn("label cli_", listed)
            self.assertIn("┌ Label ", listed)
            self.assertIn("Work/Clients", listed)
            dialog = next(line for line in listed.splitlines() if "┌ Label " in line)
            self.assertGreater(dialog.index("┌ Label "), geometry(32, 100).sidebar_w)
            self.assertGreater(session.popup[1], geometry(32, 100).sidebar_w)
            session.prompt_accept()
            session.flush_label_todo()
            pending = engine.store.pending_changes()
            self.assertEqual([(item.op, item.label) for item in pending], [("add", "Work/Clients")])
            self.assertIn("Label_2", engine.store.message_label_ids("m1"))

            session.begin_label()
            session.prompt_type("Clients")
            remove = next(item for item in session.completions if item.gmail_id == "Label_2")
            self.assertTrue(remove.remove)
            self.assertTrue(remove.title.startswith("- "))
            shown = "\n".join(render_lines(session, 32, 100))
            self.assertIn("- Work/Clients", shown)
            self.assertIn("enter applies", shown)
            session.prompt_cursor = session.completions.index(remove)
            session.prompt_accept()
            session.flush_label_todo()
            self.assertNotIn("Label_2", engine.store.message_label_ids("m1"))
            pending = [(item.op, item.label) for item in engine.store.pending_changes()]
            self.assertNotIn(("add", "Work/Clients"), pending)

            session.begin_label()
            session.prompt_type("Holiday")
            self.assertEqual(session.completions[0].create_name, "Holiday")
            created = "\n".join(render_lines(session, 32, 100))
            self.assertIn('create "Holiday"', created)
            self.assertIn("enter applies", created)
            self.assertGreater(session.popup[0], 1)
            self.assertGreater(session.popup[1], 8)
            session.prompt_accept()
            session.flush_label_todo()
            pending = engine.store.pending_changes()
            self.assertIn(("add", "Holiday"), [(item.op, item.label) for item in pending])
            self.assertEqual(
                engine.store.conn.execute("SELECT name FROM label_creates").fetchone()[0],
                "Holiday",
            )
            names = [row.name for row in engine.store.list_label_info()]
            self.assertIn("Holiday", names)
            closed = "\n".join(render_lines(session, 32, 100))
            self.assertNotIn('create "Holiday"', closed)
            self.assertIsNone(session.popup)

            session.focus = "list"
            session.begin_search()
            session.prompt_type("Files")
            self.assertEqual([item.subject for item in session.messages], ["Files"])
            session.prompt_accept()
            self.assertEqual(session.search_text, "Files")
            self.assertIsNone(session.prompt)
            self.assertFalse(session.leave_message())
            self.assertTrue(session.clear_search())
            self.assertEqual(session.search_text, "")
            self.assertEqual(session.current_label().gmail_id, "INBOX")
            self.assertEqual([item.subject for item in session.messages], ["First", "Second"])
            self.assertFalse(session.clear_search())
            filtered = help_lines("list", defaults().bindings, filtering=True)
            self.assertTrue(any("clear the search" in line for line in filtered))
            session.begin_search()
            session.prompt_backspace()
            self.assertIsNone(session.prompt)
            self.assertEqual([item.subject for item in session.messages], ["First", "Second"])
            session.begin_search()
            session.prompt_type("F")
            session.prompt_backspace()
            self.assertEqual(session.prompt, "search")
            self.assertEqual(session.prompt_text, "")
            session.prompt_backspace()
            self.assertIsNone(session.prompt)
            session.begin_search()
            session.prompt_type("Files")
            session.prompt_accept()
            session.focus = "reader"
            self.assertTrue(session.leave_message())
            self.assertEqual(session.focus, "list")
            self.assertEqual(session.search_text, "Files")

            session.focus = "reader"
            session.begin_search()
            session.prompt_type("file")
            self.assertTrue(session.prompt_hint.startswith("1/"))
            self.assertIsNotNone(session.find_line)
            self.assertIn("file", session.reader[session.find_line].lower())
            session.open_inbox()
            self.assertEqual(session.current_label().gmail_id, "INBOX")
            self.assertEqual(session.focus, "list")
            self.assertEqual(session.search_text, "")
            self.assertEqual([item.subject for item in session.messages], ["First", "Second"])
        finally:
            engine.store.close()

    def test_archive_drops_inbox_and_delete_moves_to_trash(self) -> None:
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            session.focus = "list"
            session.archive_selected()
            self.assertEqual([item.subject for item in session.messages], ["Second"])
            self.assertIn("second body", "\n".join(session.reader))
            self.assertTrue(session.label_todo)
            session.flush_label_todo()
            archived = {(item.op, item.label) for item in engine.store.pending_changes()}
            self.assertIn(("remove", "INBOX"), archived)
            self.assertNotIn(("add", "TRASH"), archived)
            session.delete_selected()
            self.assertEqual(session.messages, [])
            session.flush_label_todo()
            deleted = {(item.op, item.label) for item in engine.store.pending_changes()}
            self.assertIn(("add", "TRASH"), deleted)
            self.assertIn(("remove", "INBOX"), deleted)
            self.assertEqual(self.store.list_messages("TRASH")[0].subject, "Second")
            self.assertEqual(self.store.list_messages("INBOX"), [])
        finally:
            engine.store.close()

    def test_delete_fetches_the_next_body_like_down(self) -> None:
        """Delete used to open the next row with fetch=False, so the body waited."""
        self.store.mark_body_missing("m2")
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            session.focus = "list"
            self.assertEqual(session.messages[0].gmail_id, "m1")
            session.delete_selected()
            self.assertEqual(session.messages[0].gmail_id, "m2")
            self.assertTrue(session.needs_fetch)
        finally:
            engine.store.close()

    def test_undo_restores_an_archive_and_an_unread_flag(self) -> None:
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            session.focus = "list"
            session.undo()
            self.assertEqual(session.notice, "nothing to undo")
            session.archive_selected()
            self.assertEqual([item.subject for item in session.messages], ["Second"])
            session.undo()
            self.assertEqual(session.notice, "undone")
            self.assertEqual([item.subject for item in session.messages], ["First", "Second"])
            self.assertEqual(engine.store.pending_changes(), [])
            session.archive_selected()
            session.flush_label_todo()
            groups = engine.store.queue_groups()
            gmail_id = next(iter(groups))
            engine.store.complete_push(gmail_id, groups[gmail_id], ["UNREAD"], "9")
            session.undo()
            self.assertEqual([item.subject for item in session.messages], ["First", "Second"])
            pending = {(item.gmail_id, item.op, item.label) for item in engine.store.pending_changes()}
            self.assertIn((gmail_id, "add", "INBOX"), pending)
            self.assertTrue(session.messages[0].unread)
            session.toggle_unread()
            self.assertFalse(session.messages[0].unread)
            session.undo()
            self.assertTrue(session.messages[0].unread)
            self.assertEqual(session.messages[0].subject, "First")
        finally:
            engine.store.close()

    def test_x_marks_messages_and_archive_skips_the_unmarked_one(self) -> None:
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            session.focus = "list"
            session.toggle_selected()
            self.assertEqual(session.selected, {"m1"})
            self.assertEqual(session.messages[session.message_cursor].gmail_id, "m2")
            screen = render_lines(session, 32, 110)
            first = next(line for line in screen if "First" in line)
            second = next(line for line in screen if "Second" in line)
            self.assertLess(first.index("x "), first.index("First"))
            self.assertNotIn("x ", second)
            session.archive_selected()
            self.assertEqual([item.subject for item in session.messages], ["Second"])
            self.assertEqual(session.selected, set())
            session.flush_label_todo()
            queued = {(item.gmail_id, item.op, item.label) for item in engine.store.pending_changes()}
            self.assertIn(("m1", "remove", "INBOX"), queued)
            self.assertNotIn(("m2", "remove", "INBOX"), queued)
        finally:
            engine.store.close()

    def test_marking_at_the_bottom_loads_the_next_page(self) -> None:
        for number in range(3):
            self._message(
                f"more{number}",
                f"tm{number}",
                10_000 + number,
                f"More {number}",
                "Ada <ada@example.com>",
                "me@gmail.com",
                ["INBOX"],
                "body",
            )
        import gmail_tui.ui as ui

        previous = ui.PAGE
        ui.PAGE = 2
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            session.focus = "list"
            session.message_window = 2
            self.assertTrue(session._more)
            self.assertEqual(len(session.messages), 2)
            session.message_cursor = 1
            marked = session.messages[1].gmail_id
            session.toggle_selected()
            self.assertIn(marked, session.selected)
            self.assertGreater(len(session.messages), 2)
            self.assertEqual(session.message_cursor, 2)
            self.assertEqual(session.message_scroll, 1)
            self.assertEqual(session.messages[2].subject, "More 0")
        finally:
            ui.PAGE = previous
            engine.store.close()

    def test_marked_messages_take_a_label_and_a_delete_together(self) -> None:
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            session.focus = "list"
            session.toggle_selected()
            session.toggle_selected()
            self.assertEqual(session.selected, {"m1", "m2"})
            session.begin_label()
            session.prompt_type("Work")
            session.prompt_cursor = next(
                index for index, item in enumerate(session.completions) if item.gmail_id == "Label_1"
            )
            session.prompt_accept()
            session.flush_label_todo()
            added = {
                item.gmail_id
                for item in engine.store.pending_changes()
                if item.op == "add" and item.label == "Work"
            }
            self.assertEqual(added, {"m1", "m2"})
            self.assertEqual(session.selected, set())
            session.message_cursor = 0
            session.toggle_selected()
            session.toggle_selected()
            session.delete_selected()
            self.assertEqual(session.messages, [])
            session.flush_label_todo()
            trashed = {
                item.gmail_id
                for item in engine.store.pending_changes()
                if item.op == "add" and item.label == "TRASH"
            }
            self.assertEqual(trashed, {"m1", "m2"})
        finally:
            engine.store.close()

    def test_account_tab_shows_inbox_count_and_unread(self) -> None:
        self.store.set_email("ada@example.com")
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            other = Account("bea@example.com", Path(self.tmp.name) / "other", 4, 2)
            lines = render_lines(
                session,
                32,
                120,
                [Account("placeholder", Path(self.tmp.name), 0, 0), other],
                0,
            )
            self.assertTrue(lines[0].startswith("* ada@example.com  1 [2]"))
            self.assertIn("bea@example.com  2 [4]", lines[0])
            self.assertIn("┌", lines[1])
            self.assertEqual([span[2] for span in session.account_spans], [0, 1])
            session.focus = "list"
            session.toggle_selected()
            names = [item.name for item in session.visible]
            session.label_cursor = names.index("Clients")
            session.load_messages(fetch=False)
            self.assertEqual(session.selected, set())
            self.assertEqual([item.subject for item in session.messages], ["Files"])
        finally:
            engine.store.close()

    def test_account_tab_shows_last_check_age(self) -> None:
        from gmail_tui.ui import format_check_age, render_account_tabs

        self.assertEqual(format_check_age(None), "")
        self.assertEqual(format_check_age(100.0, now=112.0), "12s")
        self.assertEqual(format_check_age(100.0, now=100.0 + 3 * 60), "3m")
        self.assertEqual(format_check_age(100.0, now=100.0 + 2 * 3600), "2h")
        self.assertEqual(format_check_age(100.0, now=100.0 + 86400), "1d")
        self.store.set_email("ada@example.com")
        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            tab = Account("ada@example.com", Path(self.tmp.name), 2, 1, checked_at=time.time() - 45)
            header = render_lines(session, 32, 120, [tab], 0)[0]
            self.assertTrue(header.startswith("* ada@example.com  1 [2]  45s"))
            tab.checked_at = time.time() - 46
            alone = render_account_tabs(session, [tab], 0, 120)
            self.assertTrue(alone.startswith("* ada@example.com  1 [2]  46s"))
        finally:
            engine.store.close()

    def test_escape_then_l_is_add_label(self) -> None:
        keys = defaults()
        self.assertTrue(_is_escape("\x1b"))
        self.assertTrue(_is_escape(27))
        self.assertEqual(keys.action(ord("l")), "add-label")
        self.assertIsNone(keys.action(ord("l") | ALT))
        self.assertEqual(_csi_alt_letter(_Keys("108;3u")), "l")


class _Keys:
    def __init__(self, text: str) -> None:
        self._chars = list(text)
        self.delay = -1

    def timeout(self, delay: int) -> None:
        self.delay = delay

    def get_wch(self) -> str:
        if not self._chars:
            raise curses.error("end")
        return self._chars.pop(0)


class TtyTests(unittest.TestCase):
    def test_a_held_key_does_not_replay_after_it_is_released(self) -> None:
        screen = _Keys("xxxj")
        pending: list[str | int] = ["x", "x"]
        _drop_held_keys(screen, pending, "x")
        self.assertEqual(screen.delay, 0)
        self.assertEqual(pending, ["j"])
        self.assertEqual(screen._chars, [])
        later = _Keys("xjx")
        waiting: list[str | int] = []
        _drop_held_keys(later, waiting, "x")
        self.assertEqual(waiting, ["j"])
        self.assertEqual(later._chars, ["x"])

    def test_curses_ui_shows_three_panes_and_opens_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "mail"
            from gmail_tui.accounts import account_root

            mailbox = account_root(data, "me@gmail.com")
            store = Store.open(mailbox)
            store.set_email("me@gmail.com")
            store.replace_labels(
                [
                    Label("INBOX", "INBOX", "system"),
                    Label("Label_1", "Work", "user"),
                ]
            )
            first = IncomingMessage(
                gmail_id="m1",
                thread_id="t1",
                label_ids=["INBOX"],
                internal_date_ms=2_000,
                subject="Alpha subject",
                from_addr="Ada <ada@example.com>",
                to_addr="me@gmail.com",
                snippet="alpha body",
            )
            second = IncomingMessage(
                gmail_id="m2",
                thread_id="t2",
                label_ids=["INBOX"],
                internal_date_ms=1_000,
                subject="Beta subject",
                from_addr="Bea <bea@example.com>",
                to_addr="me@gmail.com",
                snippet="beta body",
            )
            for message, body in ((first, "alpha body"), (second, "beta body")):
                store.upsert_metadata(message, "missing")
                raw = (
                    f"Subject: {message.subject}\r\nFrom: {message.from_addr}\r\n"
                    f"To: {message.to_addr}\r\n\r\n{body}\r\n"
                ).encode()
                store.write_body(message, raw, body)
            store.close()

            master, slave = pty.openpty()
            _set_size(slave, 36, 120)
            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            env["LANG"] = "C.UTF-8"
            env["COLUMNS"] = "120"
            env["LINES"] = "36"
            env["XDG_CONFIG_HOME"] = str(Path(tmp) / "config")
            proc = subprocess.Popen(
                [sys.executable, "-m", "gmail_tui", "--data", str(data), "tui"],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
                start_new_session=True,
            )
            os.close(slave)
            try:
                screen = _read_until(master, "alpha body")
                self.assertIn("Labels", screen)
                self.assertIn("Alpha subject", screen)
                self.assertIn("Message", screen)
                self.assertIn("alpha body", screen)
                os.write(master, b"j")
                screen = _read_until(master, "beta body")
                self.assertIn("beta body", screen)
                os.write(master, b"q")
                screen = _read_until(master, "esc cancels")
                self.assertIn("Really quit?", screen)
                self.assertIn("esc cancels", screen)
                os.write(master, b"\n")
                proc.wait(timeout=3)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=3)
                os.close(master)


def _set_size(fd: int, rows: int, cols: int) -> None:
    import fcntl
    import struct
    import termios

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _read_until(fd: int, needle: str, timeout: float = 2.0) -> str:
    deadline = time.time() + timeout
    chunks: list[bytes] = []
    while time.time() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if not ready:
            if needle in b"".join(chunks).decode("utf-8", errors="replace"):
                break
            continue
        try:
            chunks.append(os.read(fd, 16384))
        except OSError:
            break
        if needle in b"".join(chunks).decode("utf-8", errors="replace"):
            break
    return b"".join(chunks).decode("utf-8", errors="replace")


if __name__ == "__main__":
    unittest.main()

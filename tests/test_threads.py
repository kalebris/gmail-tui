"""Collapsed conversations and the reply tree inside an open thread."""

from __future__ import annotations

import tempfile
import unittest

from gmail_tui.gmail import IncomingMessage, Label, NullGmail
from gmail_tui.store import MessageSummary, Store
from gmail_tui.sync import Engine
from gmail_tui.threads import arrange, bare_subject
from gmail_tui.ui import MailSession, message_row


def _summary(
    gmail_id: str,
    subject: str,
    date: int,
    *,
    message_id: int,
    header: str = "",
    reply: str = "",
    references: str = "",
) -> MessageSummary:
    return MessageSummary(
        message_id=message_id,
        gmail_id=gmail_id,
        from_addr="Ada <ada@example.com>",
        to_addr="me@gmail.com",
        subject=subject,
        snippet="",
        internal_date_ms=date,
        label_names=(),
        has_attachment=False,
        unread=False,
        message_id_header=header,
        in_reply_to=reply,
        references_header=references,
    )


class ThreadShapeTests(unittest.TestCase):
    def test_reply_prefixes_leave_the_conversation_subject(self) -> None:
        self.assertEqual(bare_subject("Re: Re: Fwd: Hello"), "Hello")
        self.assertEqual(bare_subject("RE[2]: Hello"), "Hello")
        self.assertEqual(bare_subject("Fw: Hello"), "Hello")
        self.assertEqual(bare_subject("Hello"), "Hello")

    def test_replies_nest_under_the_message_they_answer(self) -> None:
        root = _summary("a", "Re: Plan", 1, message_id=1, header="<a@example.com>")
        reply = _summary(
            "b",
            "Re: Plan",
            2,
            message_id=2,
            header="<b@example.com>",
            reply="<a@example.com>",
        )
        side = _summary(
            "c",
            "Fwd: Plan",
            3,
            message_id=3,
            header="<c@example.com>",
            references="<a@example.com> <b@example.com>",
        )
        placed = arrange([root, reply, side])
        self.assertEqual([item.gmail_id for item, _prefix in placed], ["a", "b", "c"])
        prefixes = [prefix for _item, prefix in placed]
        self.assertEqual(prefixes[0], "└ ")
        self.assertTrue(prefixes[1].startswith("  └ "))
        self.assertTrue(prefixes[2].startswith("    └ "))


class ThreadListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(self.tmp.name)
        self.store.replace_labels(
            [
                Label("INBOX", "INBOX", "system"),
                Label("UNREAD", "UNREAD", "system"),
                Label("Label_1", "Work", "user"),
                Label("TRASH", "TRASH", "system"),
            ]
        )

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _save(
        self,
        gmail_id: str,
        date: int,
        subject: str,
        labels: list[str],
        *,
        header: str,
        reply: str = "",
        raw_reply: str = "",
        thread: str = "thread-1",
    ) -> None:
        message = IncomingMessage(
            gmail_id=gmail_id,
            thread_id=thread,
            label_ids=labels,
            internal_date_ms=date,
            subject=subject,
            from_addr="Ada <ada@example.com>",
            to_addr="me@gmail.com",
            message_id_header=header,
            in_reply_to=reply,
        )
        self.store.upsert_metadata(message, "missing")
        lines = [
            f"Subject: {subject}",
            "From: Ada <ada@example.com>",
            "To: me@gmail.com",
            f"Message-ID: {header}",
        ]
        if raw_reply:
            lines.append(f"In-Reply-To: {raw_reply}")
        lines.extend(["", "body", ""])
        self.store.write_body(message, "\r\n".join(lines).encode(), "body")

    def test_a_conversation_collapses_and_opens_across_labels(self) -> None:
        self._save("root", 1_000, "Plan", ["INBOX"], header="<root@example.com>")
        self._save(
            "reply",
            2_000,
            "Re: Re: Plan",
            ["Label_1"],
            header="<reply@example.com>",
            raw_reply="<root@example.com>",
        )
        self._save("alone", 500, "Other", ["INBOX"], header="<other@example.com>", thread="thread-2")
        # The reply's parent link is only in the saved message, not the metadata row.
        self.store.conn.execute("UPDATE messages SET in_reply_to='' WHERE gmail_id='reply'")

        engine = Engine.open(self.tmp.name, NullGmail())
        try:
            session = MailSession(engine)
            self.assertEqual([item.gmail_id for item in session.messages], ["reply", "alone"])
            head = session.messages[0]
            self.assertEqual(head.thread_count, 2)
            self.assertEqual(head.subject, "Plan")
            self.assertIn("Work", head.label_names)
            self.assertIn("INBOX", head.label_names)
            row = message_row(head, 120)
            self.assertIn("> 2  Plan", row)
            self.assertNotIn("Re:", row)

            session.horizontal(True)
            self.assertEqual(
                [item.gmail_id for item in session.messages],
                ["reply", "root", "reply", "alone"],
            )
            child = session.messages[2]
            self.assertIn("└ ", child.subject)
            self.assertIn("Plan", child.subject)
            self.assertNotIn("Re:", child.subject)
            self.assertIn("Work", child.label_names)
            opened = message_row(session.messages[0], 120)
            self.assertIn("v 2  Plan", opened)

            session.horizontal(False)
            self.assertEqual([item.gmail_id for item in session.messages], ["reply", "alone"])
            self.assertEqual(session.focus, "list")
            session.fold(True)
            self.assertEqual(session.focus, "list")
            self.assertEqual(session.messages[0].expanded, True)
            session.fold(True)
            self.assertEqual(session.focus, "list")
            self.assertEqual(session.messages[0].expanded, True)
            session.fold(None)
            self.assertEqual([item.gmail_id for item in session.messages], ["reply", "alone"])
            session.archive_selected()
            self.assertEqual([item.gmail_id for item in session.messages], ["alone"])
            session.flush_label_todo()
            self.assertEqual(
                [item.gmail_id for item in engine.store.list_messages("Label_1")],
                ["reply"],
            )
        finally:
            engine.store.close()

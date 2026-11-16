"""Snooze presets, custom durations, and inbox wake-ups."""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from gmail_tui.fake import FakeGmail, FakeMessage
from gmail_tui.gmail import NullGmail
from gmail_tui.snooze import parse_duration, presets
from gmail_tui.sync import Engine
from gmail_tui.ui import MailSession, render_lines


class SnoozeParseTests(unittest.TestCase):
    def test_duration_units(self) -> None:
        now = datetime(2026, 11, 3, 15, 0, tzinfo=ZoneInfo("UTC"))
        self.assertEqual(parse_duration("30m", now=now), now + timedelta(minutes=30))
        self.assertEqual(parse_duration("2h", now=now), now + timedelta(hours=2))
        self.assertEqual(parse_duration("3d", now=now), now + timedelta(days=3))
        self.assertEqual(parse_duration("45", now=now), now + timedelta(minutes=45))
        self.assertIsNone(parse_duration("nope", now=now))

    def test_presets_are_in_the_future(self) -> None:
        now = datetime(2026, 11, 3, 15, 30, tzinfo=ZoneInfo("UTC"))
        choices = presets(now)
        self.assertGreaterEqual(len(choices), 5)
        for choice in choices:
            self.assertGreater(choice.until, now)


class SnoozeMailboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeGmail()
        self.fake.add(
            FakeMessage(
                id="in1",
                thread_id="t-in1",
                label_ids=["INBOX", "UNREAD"],
                subject="Snooze me",
                from_addr="Ada <ada@example.com>",
                body="later",
                internal_date_ms=2_000,
            )
        )
        self.engine = Engine.open(self.tmp.name, self.fake, sleeper=lambda _seconds: None)

    def tearDown(self) -> None:
        self.engine.store.close()
        self.tmp.cleanup()

    def test_snooze_leaves_inbox_and_wakes_later(self) -> None:
        self.engine.startup()
        session = MailSession(self.engine)
        session.focus = "list"
        self.assertEqual(session.messages[0].gmail_id, "in1")
        session.begin_snooze()
        self.assertEqual(session.prompt, "snooze")
        listed = "\n".join(render_lines(session, 32, 100))
        self.assertIn("Snooze", listed)
        self.assertIn("custom", listed)
        session.prompt_type("j")
        session.prompt_type("k")
        self.assertEqual(session.prompt_cursor, 0)
        session.prompt_type("2")
        session.prompt_type("h")
        self.assertEqual(session.prompt_text, "2h")
        before = datetime.now().astimezone()
        session.prompt_accept()
        self.assertIsNone(session.prompt)
        self.assertEqual(session.messages, [])
        self.assertNotIn("INBOX", self.engine.store.message_label_ids("in1"))
        until = self.engine.store.snooze_until_ms("in1")
        self.assertIsNotNone(until)
        self.assertGreater(until, int(before.timestamp() * 1000))
        woke = self.engine.wake_due_snoozes(now_ms=until + 1)
        self.assertEqual(woke, 1)
        self.assertIn("INBOX", self.engine.store.message_label_ids("in1"))
        self.assertIsNone(self.engine.store.snooze_until_ms("in1"))

    def test_preset_enter_without_custom_text(self) -> None:
        self.engine.startup()
        session = MailSession(self.engine)
        session.begin_snooze()
        session.prompt_move(1)
        session.prompt_accept()
        self.assertIsNone(session.prompt)
        self.assertIsNotNone(self.engine.store.snooze_until_ms("in1"))
        self.assertNotIn("INBOX", self.engine.store.message_label_ids("in1"))


if __name__ == "__main__":
    unittest.main()

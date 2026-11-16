"""Desktop new-mail notifications."""

from __future__ import annotations

import unittest
from unittest import mock

from gmail_tui import notify
from gmail_tui.fake import FakeGmail, FakeMessage, added
from gmail_tui.sync import BackgroundSync, Engine, _inbox_arrivals
from gmail_tui.gmail import HistoryMessage, HistoryRecord
import tempfile
import time


def _inbox(message_id: str, body: str, date: int, subject: str = "Hello") -> FakeMessage:
    return FakeMessage(
        id=message_id,
        thread_id=f"t-{message_id}",
        label_ids=["INBOX", "UNREAD"],
        subject=subject,
        from_addr="Ada <ada@example.com>",
        body=body,
        internal_date_ms=date,
    )


class NotifyTests(unittest.TestCase):
    def test_inbox_arrivals_counts_inbox_adds_only(self) -> None:
        records = [
            HistoryRecord(
                id="1",
                messages_added=[
                    HistoryMessage("a", "t-a", ["INBOX", "UNREAD"]),
                    HistoryMessage("b", "t-b", ["Label_1"]),
                ],
            ),
            added("c", "t-c", ["INBOX"]),
        ]
        self.assertEqual(_inbox_arrivals(records), 2)

    def test_new_mail_formats_accounts(self) -> None:
        with mock.patch("gmail_tui.notify.send") as send:
            send.return_value = True
            self.assertTrue(notify.new_mail({"alice@ex.com": 1, "bob@ex.com": 3}))
            send.assert_called_once_with(
                "New mail (4)",
                "alice@ex.com: 1 new message\nbob@ex.com: 3 new messages",
            )

    def test_new_mail_skips_empty(self) -> None:
        with mock.patch("gmail_tui.notify.send") as send:
            self.assertFalse(notify.new_mail({"alice@ex.com": 0}))
            send.assert_not_called()

    def test_send_uses_notify_send(self) -> None:
        with mock.patch("gmail_tui.notify.shutil.which", return_value="/bin/notify-send"):
            with mock.patch("gmail_tui.notify.subprocess.run") as run:
                self.assertTrue(notify.send("New mail", "alice: 1 new message"))
                run.assert_called_once()
                args = run.call_args.args[0]
                self.assertEqual(args[0], "/bin/notify-send")
                self.assertIn("New mail", args)
                self.assertIn("alice: 1 new message", args)

    def test_send_noop_without_binary(self) -> None:
        with mock.patch("gmail_tui.notify.shutil.which", return_value=None):
            with mock.patch("gmail_tui.notify.subprocess.run") as run:
                self.assertFalse(notify.send("New mail", "body"))
                run.assert_not_called()


class PollAlertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeGmail()
        self.fake.add(_inbox("in1", "alpha", 2_000, "Inbox note"))
        self.engine = Engine.open(self.tmp.name, self.fake, sleeper=lambda _seconds: None)

    def tearDown(self) -> None:
        self.engine.store.close()
        self.tmp.cleanup()

    def test_poll_sets_mail_alert_for_new_inbox(self) -> None:
        self.engine.startup()
        job = BackgroundSync(self.engine)
        for _ in range(500):
            if not job.step():
                break
        self.assertEqual(job.mode, "idle")
        self.assertEqual(job.mail_alert, 0)
        self.fake.add(_inbox("nw", "fresh", 8_000_000, "Brand new"))
        self.fake.add_history(added("nw", "t-nw", ["INBOX", "UNREAD"]))
        self.fake.add(
            FakeMessage(
                id="ar2",
                thread_id="t-ar2",
                label_ids=["Label_1"],
                subject="Archive only",
                from_addr="Bea <bea@example.com>",
                body="archive",
                internal_date_ms=9_000_000,
            )
        )
        self.fake.add_history(added("ar2", "t-ar2", ["Label_1"]))
        job._poll_at = time.monotonic() - 1
        for _ in range(80):
            job.step()
            if job.mail_alert:
                break
            if job.mode == "idle" and not job._polling:
                break
        self.assertEqual(job.mail_alert, 1)
        self.assertTrue(job.list_bump)
        self.assertTrue(job.dirty)


if __name__ == "__main__":
    unittest.main()

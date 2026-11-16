"""History, new-mail download, mutation push, backfill, and merged search."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from gmail_tui.fake import FakeGmail, FakeMessage, added, label_change
from gmail_tui.gmail import (
    HistoryMessage,
    HistoryRecord,
    IncomingMessage,
    Label,
    LabelChange,
    MessageFormat,
    Offline,
    UNITS_GET,
)
from gmail_tui.http_gmail import HttpStatus, is_rate_limited
from gmail_tui.quota import Quota, SyncStopped
from gmail_tui.sync import BackgroundSync, Engine, UrgentWork
from gmail_tui.tui import _job_due, _service
from gmail_tui.ui import MailSession


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


class SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = FakeGmail()
        self.fake.add(_inbox("in1", "alpha platypus", 2_000, "Inbox note"))
        self.fake.add(
            FakeMessage(
                id="ar1",
                thread_id="t-ar1",
                label_ids=["Label_1"],
                subject="Old archive",
                from_addr="Bea <bea@example.com>",
                body="archive note",
                internal_date_ms=1_000,
            )
        )
        self.engine = Engine.open(self.tmp.name, self.fake, sleeper=lambda _seconds: None)

    def tearDown(self) -> None:
        self.engine.store.close()
        self.tmp.cleanup()

    def test_startup_order_downloads_view_before_backfill(self) -> None:
        label, threads = self.engine.local_threads()
        self.assertEqual(label, "INBOX")
        self.assertEqual(threads, [])

        report = self.engine.startup()
        self.assertTrue(report.bootstrapped)
        self.assertEqual(report.bodies_downloaded, 1)
        self.assertEqual(report.backfill_downloaded, 1)
        calls = self.fake.calls
        raws = [call for call in calls if call.endswith(":raw")]
        self.assertEqual(raws, ["messages.get:in1:raw", "messages.get:ar1:raw"])
        inbox_lists = [call for call in calls if call == "messages.list:label=INBOX:q="]
        self.assertEqual(inbox_lists, ["messages.list:label=INBOX:q="])
        self.assertLess(calls.index("messages.list:label=INBOX:q="), calls.index("messages.batch:metadata:1"))
        self.assertLess(calls.index("messages.batch:metadata:1"), calls.index("messages.get:in1:raw"))
        self.assertLess(calls.index("messages.get:in1:raw"), calls.index("messages.get:ar1:metadata"))
        self.assertLess(calls.index("messages.get:ar1:metadata"), calls.index("messages.get:ar1:raw"))
        self.assertLess(calls.index("history.list:100"), calls.index("messages.get:ar1:raw"))
        self.assertEqual(
            [thread.subject for thread in self.engine.store.list_threads("INBOX")],
            ["Inbox note"],
        )
        self.assertTrue(self.engine.store.body_readable("in1"))
        self.assertTrue(self.engine.store.body_readable("ar1"))
        row = self.engine.store.conn.execute(
            "SELECT body_path FROM messages WHERE gmail_id='in1'"
        ).fetchone()
        self.assertEqual(row[0], "blobs/in1.eml")

    def test_startup_fills_the_minute_with_inbox_bodies(self) -> None:
        for index in range(9):
            self.fake.add(_inbox(f"m{index}", "body", 4_000 + index, f"Note {index}"))
        self.engine.startup()
        self.assertIn("messages.batch:raw:10", self.fake.calls)
        self.assertLess(
            self.fake.calls.index("messages.batch:raw:10"),
            self.fake.calls.index("messages.get:ar1:raw"),
        )
        self.assertEqual(self.fake.calls.count("messages.batch:raw:10"), 1)

    def test_inbox_bodies_yield_between_chunks(self) -> None:
        for index in range(14):
            self.fake.add(_inbox(f"m{index}", "body", 4_000 + index, f"Note {index}"))
        self.engine.startup()
        batches = [call for call in self.fake.calls if call.startswith("messages.batch:raw:")]
        self.assertEqual(batches[:2], ["messages.batch:raw:10", "messages.batch:raw:5"])
        self.assertLess(
            self.fake.calls.index("messages.batch:raw:5"),
            self.fake.calls.index("messages.get:ar1:raw"),
        )

    def test_inbox_bodies_spread_a_fast_connection(self) -> None:
        clock = {"t": 1_000.0}

        def now() -> float:
            return clock["t"]

        def sleep(seconds: float) -> None:
            clock["t"] += seconds

        for index in range(3):
            self.fake.add(_inbox(f"m{index}", "body", 4_000 + index, f"Note {index}"))
        root = Path(self.tmp.name) / "inbox-paced"
        engine = Engine.open(root, self.fake, sleeper=sleep, now=now)
        try:
            engine.startup()
            shares = [wait for wait in engine.quota.waits if wait < 1]
            self.assertGreaterEqual(len(shares), 3)
            self.assertAlmostEqual(shares[0], 0.6)
            self.assertGreaterEqual(clock["t"] - 1_000.0, 1.8)
            self.assertTrue(engine.store.body_readable("m2"))
        finally:
            engine.store.close()

    def test_listing_progress_counts_the_whole_mailbox(self) -> None:
        for index in range(3):
            self.fake.add(_inbox(f"more{index}", "body", 3_000 + index, f"More {index}"))
        self.fake.page_size = 2
        job = BackgroundSync(self.engine)
        phases: list[str] = []
        for _ in range(500):
            if not job.step():
                break
            if job.phase.startswith("listing "):
                phases.append(job.phase)
        inbox = [phase for phase in phases if phase.startswith("listing Inbox")]
        self.assertIn("listing Inbox 2/4", inbox)
        self.assertIn("listing Inbox 4/4", inbox)
        self.assertNotIn("listing Inbox 2/2", inbox)
        mail = [phase for phase in phases if phase.startswith("listing all mail")]
        self.assertIn("listing all mail 2/5", mail)
        self.assertIn("listing all mail 5/5", mail)
        self.assertGreater(job.done, 0)
        batches = [call for call in self.fake.calls if call.startswith("messages.batch:metadata:")]
        self.assertTrue(batches)
        self.assertLess(len(batches), 4)

    def test_inbox_count_stays_put_when_a_page_claims_to_be_the_whole_inbox(self) -> None:
        for index in range(3):
            self.fake.add(_inbox(f"more{index}", "body", 3_000 + index, f"More {index}"))
        self.fake.page_size = 2
        self.fake.estimate_pages = True
        job = BackgroundSync(self.engine)
        phases: list[str] = []
        for _ in range(500):
            if not job.step():
                break
            if job.phase.startswith("listing Inbox"):
                phases.append(job.phase)
        self.assertIn("listing Inbox 2/4", phases)
        self.assertIn("listing Inbox 4/4", phases)
        self.assertNotIn("listing Inbox 2/2", phases)
        self.assertFalse(any(phase.startswith("listing Inbox 1") and phase.endswith("/101") for phase in phases))

    def test_all_mail_progress_is_the_whole_account(self) -> None:
        self.fake.reported_messages_total = 40
        job = BackgroundSync(self.engine)
        phases: list[str] = []
        for _ in range(200):
            if not job.step():
                break
            if job.phase.startswith("listing all mail"):
                phases.append(job.phase)
        self.assertIn("listing all mail 1/40", phases)
        self.assertIn("listing all mail 2/40", phases)
        self.assertNotIn("listing all mail 2/2", phases)

    def test_opening_applies_history_before_older_mail(self) -> None:
        self.engine.startup()
        self.fake.messages["in1"].label_ids = ["Label_1"]
        self.fake.add(_inbox("new1", "just arrived", 5_000, "Just arrived"))
        self.fake.add_history(label_change("in1", "t-in1", ["INBOX"], added_labels=False))
        self.fake.add_history(added("new1", "t-new1", ["INBOX", "UNREAD"]))
        self.fake.calls.clear()
        self.engine.startup()
        self.assertNotIn("INBOX", self.engine.store.message_label_ids("in1"))
        self.assertIn("INBOX", self.engine.store.message_label_ids("new1"))
        self.assertTrue(self.engine.store.body_readable("new1"))
        calls = self.fake.calls
        history = next(call for call in calls if call.startswith("history.list:"))
        self.assertLess(calls.index(history), calls.index("messages.get:new1:raw"))
        self.assertNotIn("messages.list:label=INBOX:q=", calls)

    def test_inbox_label_downloads_the_body_before_other_mail(self) -> None:
        self.engine.startup()
        self.engine.store.mark_body_missing("ar1")
        self.fake.messages["ar1"].label_ids = ["INBOX", "Label_1"]
        self.fake.add(
            FakeMessage(
                id="zz",
                thread_id="t-zz",
                label_ids=["Label_1"],
                subject="Later archive",
                from_addr="Cy <cy@example.com>",
                body="later",
                internal_date_ms=9_000,
            )
        )
        self.engine.store.upsert_metadata(
            IncomingMessage(
                gmail_id="zz",
                thread_id="t-zz",
                label_ids=["Label_1"],
                internal_date_ms=9_000,
                subject="Later archive",
            ),
            "missing",
        )
        self.fake.add_history(
            HistoryRecord(
                id="0",
                labels_added=[
                    LabelChange(HistoryMessage("ar1", "t-ar1", ["INBOX", "Label_1"]), ["INBOX"])
                ],
            )
        )
        self.fake.calls.clear()
        self.engine.startup()
        calls = self.fake.calls
        self.assertLess(calls.index("messages.get:ar1:raw"), calls.index("messages.get:zz:raw"))
        self.assertTrue(self.engine.store.body_readable("ar1"))

    def test_pending_intents_survive_remote_label_changes(self) -> None:
        self.engine.startup()
        self.engine.store.apply_local_labels("in1", add=["STARRED"], remove=["UNREAD"])
        self.fake.add_history(
            HistoryRecord(
                id="0",
                labels_removed=[
                    LabelChange(HistoryMessage("in1", "t-in1", []), ["INBOX"]),
                    LabelChange(HistoryMessage("in1", "t-in1", []), ["STARRED"]),
                ],
                labels_added=[
                    LabelChange(HistoryMessage("in1", "t-in1", []), ["UNREAD"]),
                    LabelChange(HistoryMessage("in1", "t-in1", []), ["IMPORTANT"]),
                ],
            )
        )
        self.engine.sync_metadata()
        labels = self.engine.store.message_label_ids("in1")
        self.assertIn("STARRED", labels)
        self.assertNotIn("UNREAD", labels)
        self.assertNotIn("INBOX", labels)
        self.assertIn("IMPORTANT", labels)
        self.assertIn(("in1", "STARRED", "add"), self.engine.store.pending_mutations())
        self.assertIn(("in1", "UNREAD", "remove"), self.engine.store.pending_mutations())
        self.assertEqual(self.engine.store.history_id(), "101")

        self.fake.messages["in1"].label_ids = ["UNREAD", "IMPORTANT"]
        self.fake.calls.clear()
        pushed, rebased = self.engine.flush_queue()
        self.assertEqual((pushed, rebased), (1, 0))
        modify = [call for call in self.fake.calls if call.startswith("messages.modify")]
        self.assertEqual(modify, ["messages.modify:in1:add=STARRED:remove=UNREAD"])
        self.assertEqual(self.engine.store.pending_mutations(), [])
        self.assertEqual(self.engine.store.message_label_ids("in1"), {"IMPORTANT", "STARRED"})

    def test_conflict_rebases_then_pushes_the_same_intent(self) -> None:
        self.engine.startup()
        self.engine.store.apply_local_labels("in1", remove=["UNREAD"])
        self.fake.messages["in1"].label_ids = ["INBOX", "UNREAD", "EXTRA"]
        self.fake.conflict_once.add("in1")
        self.fake.calls.clear()
        pushed, rebased = self.engine.flush_queue()
        self.assertEqual((pushed, rebased), (1, 1))
        self.assertEqual(
            [call for call in self.fake.calls if "modify" in call],
            [
                "messages.modify:in1:conflict",
                "messages.modify:in1:add=:remove=UNREAD",
            ],
        )
        self.assertEqual(self.engine.store.message_label_ids("in1"), {"INBOX", "EXTRA"})
        self.assertEqual(self.engine.store.pending_mutations(), [])

    def test_history_add_downloads_body_before_backfill(self) -> None:
        self.engine.startup()
        self.engine.store.mark_body_missing("ar1")
        self.fake.add(
            FakeMessage(
                id="nw",
                thread_id="t-nw",
                label_ids=["Label_1"],
                subject="Seen",
                from_addr="Cy <cy@example.com>",
                body="seen body",
                internal_date_ms=8_000,
            )
        )
        self.fake.add_history(added("nw", "t-nw", ["Label_1"]))
        self.fake.calls.clear()
        self.engine.startup()
        calls = self.fake.calls
        self.assertLess(calls.index("messages.get:nw:raw"), calls.index("messages.get:ar1:raw"))
        self.assertTrue(self.engine.store.body_readable("nw"))

    def test_caching_shows_how_many_bodies_remain(self) -> None:
        self.engine.startup()
        for index, date in enumerate((500, 400)):
            gmail_id = f"old{index}"
            self.fake.add(
                FakeMessage(
                    id=gmail_id,
                    thread_id=f"t-{gmail_id}",
                    label_ids=["Label_1"],
                    subject=f"Old {index}",
                    from_addr="Cy <cy@example.com>",
                    body="old",
                    internal_date_ms=date,
                )
            )
            self.engine.store.upsert_metadata(
                IncomingMessage(
                    gmail_id=gmail_id,
                    thread_id=f"t-{gmail_id}",
                    label_ids=["Label_1"],
                    internal_date_ms=date,
                    subject=f"Old {index}",
                ),
                "missing",
            )
        job = BackgroundSync(self.engine)
        phases: list[str] = []
        for _ in range(200):
            if not job.step():
                break
            if job.phase.startswith("caching mail"):
                phases.append(job.phase)
        self.assertIn("caching mail 2/4", phases)
        self.assertIn("caching mail 3/4", phases)
        self.assertIn("caching mail 4/4", phases)
        self.assertNotIn("caching mail 0/2", phases)

    def test_caching_resumes_from_bodies_already_saved(self) -> None:
        self.engine.startup()
        self.engine.store.mark_body_missing("ar1")
        job = BackgroundSync(self.engine)
        started: list[str] = []
        for _ in range(200):
            if not job.step():
                break
            if job.phase.startswith("caching mail"):
                started.append(job.phase)
                break
        self.assertEqual(started, ["caching mail 1/2"])

    def test_caching_does_not_reload_the_open_list(self) -> None:
        self.engine.startup()
        self.engine.store.mark_body_missing("ar1")
        job = BackgroundSync(self.engine)
        entered = False
        saved = False
        for _ in range(200):
            if not job.step():
                break
            if job.mode != "backfill":
                continue
            if not entered:
                job.dirty = False
                entered = True
                continue
            self.assertFalse(job.dirty)
            if self.engine.store.body_readable("ar1"):
                saved = True
                break
        self.assertTrue(saved)

    def test_raw_download_costs_more_than_a_metadata_read(self) -> None:
        self.engine.startup()
        self.engine.store.mark_body_missing("in1")
        before = self.engine.quota.units
        self.engine._download_body("in1")
        self.assertEqual(self.engine.quota.units - before, 60)
        before = self.engine.quota.units
        self.engine._call(
            UNITS_GET,
            lambda: self.engine.gmail.get_message("in1", MessageFormat.METADATA),
        )
        self.assertEqual(self.engine.quota.units - before, 20)

    def test_inbox_bodies_download_before_newer_mail(self) -> None:
        self.engine.startup()
        self.engine.store.mark_body_missing("in1")
        self.fake.add(
            FakeMessage(
                id="newer",
                thread_id="t-newer",
                label_ids=["Label_1"],
                subject="Newer archive",
                from_addr="Cy <cy@example.com>",
                body="later",
                internal_date_ms=9_000_000,
            )
        )
        self.engine.store.upsert_metadata(
            IncomingMessage(
                gmail_id="newer",
                thread_id="t-newer",
                label_ids=["Label_1"],
                internal_date_ms=9_000_000,
                subject="Newer archive",
            ),
            "missing",
        )
        self.fake.calls.clear()
        job = BackgroundSync(self.engine)
        raws: list[str] = []
        for _ in range(400):
            if not job.step():
                break
            raws.extend(call for call in self.fake.calls if call.endswith(":raw"))
            self.fake.calls.clear()
            if "messages.get:newer:raw" in raws and "messages.get:in1:raw" in raws:
                break
        self.assertIn("messages.get:in1:raw", raws)
        self.assertLess(raws.index("messages.get:in1:raw"), raws.index("messages.get:newer:raw"))
        self.assertTrue(self.engine.store.body_readable("in1"))

    def test_a_due_check_fetches_new_mail_before_more_caching(self) -> None:
        self.engine.startup()
        self.engine.store.mark_body_missing("ar1")
        job = BackgroundSync(self.engine)
        for _ in range(500):
            job.step()
            if job.mode == "backfill":
                break
        self.assertEqual(job.mode, "backfill")
        self.fake.add(_inbox("nw", "fresh mail", 8_000_000, "Brand new"))
        self.fake.add_history(added("nw", "t-nw", ["INBOX", "UNREAD"]))
        self.fake.calls.clear()
        job._poll_at = time.monotonic() - 1
        raws: list[str] = []
        for _ in range(80):
            if not job.step():
                break
            raws.extend(call for call in self.fake.calls if call.endswith(":raw"))
            self.fake.calls.clear()
            if "messages.get:nw:raw" in raws and "messages.get:ar1:raw" in raws:
                break
        self.assertLess(raws.index("messages.get:nw:raw"), raws.index("messages.get:ar1:raw"))
        self.assertTrue(self.engine.store.body_readable("nw"))

    def test_paused_cache_leaves_bodies_until_a_message_is_opened(self) -> None:
        self.engine.startup()
        self.engine.store.mark_body_missing("in1")
        self.engine.store.mark_body_missing("ar1")
        self.engine.background_bodies = False
        self.fake.calls.clear()
        job = BackgroundSync(self.engine)
        for _ in range(500):
            if not job.step():
                break
        self.assertEqual(job.mode, "idle")
        self.assertEqual(job.phase, "cache paused")
        self.assertFalse(self.engine.store.body_readable("in1"))
        self.assertFalse(self.engine.store.body_readable("ar1"))
        self.assertFalse(any(call.endswith(":raw") for call in self.fake.calls))
        session = MailSession(self.engine)
        session.load_reader(fetch=True)
        self.assertTrue(session.needs_fetch)
        for index, item in enumerate(session.visible):
            if item.gmail_id == "Label_1":
                session.label_cursor = index
                break
        session.load_messages(fetch=True)
        self.assertEqual(session.messages[session.message_cursor].gmail_id, "ar1")
        self.assertTrue(session.needs_fetch)
        self.fake.calls.clear()
        found = self.engine.search("archive", 10)
        self.assertTrue(any(hit.gmail_id == "ar1" for hit in found.hits))
        self.assertTrue(any("q=archive" in call for call in self.fake.calls))
        self.assertFalse(any(call.endswith(":raw") for call in self.fake.calls))
        self.engine.open_message("in1")
        self.assertTrue(self.engine.store.body_readable("in1"))
        self.assertFalse(self.engine.store.body_readable("ar1"))
        self.engine.background_bodies = True
        self.assertTrue(job.cache_due())
        for _ in range(500):
            if self.engine.store.body_readable("ar1"):
                break
            job.step()
        self.assertTrue(self.engine.store.body_readable("ar1"))

    def test_a_new_mail_check_does_not_resume_a_paused_cache(self) -> None:
        self.engine.startup()
        self.engine.store.mark_body_missing("ar1")
        job = BackgroundSync(self.engine)
        for _ in range(500):
            job.step()
            if job.mode == "backfill":
                break
        self.assertEqual(job.mode, "backfill")
        job.set_background_bodies(False)
        for _ in range(20):
            if job.mode == "idle":
                break
            job.step()
        self.assertEqual(job.phase, "cache paused")
        self.fake.add(_inbox("nw", "fresh mail", 8_000_000, "Brand new"))
        self.fake.add_history(added("nw", "t-nw", ["INBOX", "UNREAD"]))
        self.fake.calls.clear()
        job._poll_at = time.monotonic() - 1
        for _ in range(80):
            if not job.step():
                break
        self.assertFalse(self.engine.background_bodies)
        self.assertEqual(job.mode, "idle")
        self.assertEqual(job.phase, "cache paused")
        self.assertTrue(self.engine.store.has_metadata("nw"))
        self.assertFalse(self.engine.store.body_readable("nw"))
        self.assertFalse(self.engine.store.body_readable("ar1"))
        self.assertFalse(any(call.endswith(":raw") for call in self.fake.calls))

    def test_backfill_spreads_a_fast_connection_across_the_minute(self) -> None:
        clock = {"t": 1_000.0}

        def now() -> float:
            return clock["t"]

        def sleep(seconds: float) -> None:
            clock["t"] += seconds

        for index in range(3):
            self.fake.add(
                FakeMessage(
                    id=f"old{index}",
                    thread_id=f"t-old{index}",
                    label_ids=["Label_1"],
                    subject="Older",
                    from_addr="Bea <bea@example.com>",
                    body="older note",
                    internal_date_ms=900 - index,
                )
            )
        root = Path(self.tmp.name) / "paced"
        engine = Engine.open(root, self.fake, sleeper=sleep, now=now)
        try:
            engine.startup()
            shares = [wait for wait in engine.quota.waits if wait < 1]
            self.assertGreaterEqual(len(shares), 3)
            self.assertAlmostEqual(shares[0], 0.6)
            self.assertGreaterEqual(clock["t"] - 1_000.0, 1.8)
            self.assertTrue(engine.store.body_readable("old2"))
        finally:
            engine.store.close()

    def test_backfill_waits_until_the_queue_can_flush(self) -> None:
        self.engine.startup()
        self.engine.store.mark_body_missing("ar1")
        self.engine.store.apply_local_labels("in1", add=["STARRED"])
        self.fake.modify_offline = True
        self.fake.calls.clear()
        with self.assertRaises(Offline):
            self.engine.startup()
        self.assertTrue(any(call.endswith(":offline") for call in self.fake.calls))
        self.assertFalse(any(call.endswith(":raw") for call in self.fake.calls))
        self.assertFalse(self.engine.store.body_readable("ar1"))

    def test_expired_history_rebuilds_metadata_inbox_first(self) -> None:
        self.engine.startup()
        self.fake.expired_ids.add(self.engine.store.history_id() or "")
        self.fake.profile_history_id = "500"
        del self.fake.messages["ar1"]
        self.fake.add(_inbox("nw", "fresh mail", 9_000, "Brand new"))
        self.fake.calls.clear()
        report = self.engine.startup()
        self.assertTrue(report.bootstrapped)
        self.assertIsNone(self.engine.store.opened_message("ar1"))
        opened = self.engine.store.opened_message("nw")
        self.assertIsNotNone(opened)
        self.assertEqual(opened["subject"], "Brand new")
        self.assertTrue(self.engine.store.body_readable("nw"))
        self.assertLess(
            self.fake.calls.index("messages.batch:metadata:1"),
            self.fake.calls.index("messages.get:nw:raw"),
        )
        self.assertIn("history.list:100", self.fake.calls)
        self.assertIn("history.list:500", self.fake.calls)

    def test_wrong_account_token_does_not_wipe_mail(self) -> None:
        self.engine.startup()
        self.assertIsNotNone(self.engine.store.opened_message("ar1"))
        self.fake.email = "other@gmail.com"
        self.fake.expired_ids.add(self.engine.store.history_id() or "")
        with self.assertRaises(Offline) as raised:
            self.engine.startup()
        self.assertIn("other@gmail.com", str(raised.exception))
        self.assertIsNotNone(self.engine.store.opened_message("ar1"))
        self.assertFalse(self.engine.store.resyncing())

    def test_search_merges_fts_and_gmail_without_downloading_bodies(self) -> None:
        self.engine.startup()
        self.fake.add(_inbox("b1", "beta platypus", 3_000, "Later"))
        self.fake.calls.clear()
        response = self.engine.search("platypus")
        self.assertEqual([hit.gmail_id for hit in response.hits], ["b1", "in1"])
        self.assertIsNone(response.remote_error)
        self.assertFalse(any(call.endswith(":raw") for call in self.fake.calls))
        self.assertIn("messages.get:b1:metadata", self.fake.calls)
        self.assertNotIn("messages.get:in1:metadata", self.fake.calls)
        self.assertFalse(next(hit.body_cached for hit in response.hits if hit.gmail_id == "b1"))
        self.assertTrue(next(hit.body_cached for hit in response.hits if hit.gmail_id == "in1"))

        opened = self.engine.open_message("b1")
        self.assertIn("beta platypus", opened.body_text)
        self.assertTrue(any(call == "messages.get:b1:raw" for call in self.fake.calls))

        self.fake.calls.clear()
        cached = self.engine.open_message("in1")
        self.assertIn("alpha platypus", cached.body_text)
        self.assertEqual(self.fake.calls, [])

    def test_quota_is_shared_by_every_call(self) -> None:
        clock = {"t": 0.0}
        engine = Engine.open(
            Path(self.tmp.name) / "quota",
            self.fake,
            per_minute=100,
            sleeper=lambda seconds: clock.__setitem__("t", clock["t"] + seconds),
            now=lambda: clock["t"],
        )
        try:
            engine.startup()
            self.assertGreaterEqual(engine.quota.units, 60)
            self.assertGreaterEqual(clock["t"], 60)
        finally:
            engine.store.close()

    def test_flush_creates_a_label_before_applying_it(self) -> None:
        self.engine.startup()
        local = self.engine.store.create_user_label("Holiday")
        self.engine.store.apply_local_labels("in1", add=[local])
        self.engine.store.replace_labels(list(self.fake.labels))
        self.assertEqual(self.engine.store.pending_label_name(local), "Holiday")
        pushed, rebased = self.engine.flush_queue()
        self.assertEqual((pushed, rebased), (1, 0))
        created = next(label for label in self.fake.labels if label.name == "Holiday")
        self.assertIn(created.id, self.fake.messages["in1"].label_ids)
        self.assertIsNone(self.engine.store.pending_label_name(local))
        self.assertIn("labels.create:Holiday", self.fake.calls)
        row = self.engine.store.conn.execute(
            "SELECT gmail_id FROM labels WHERE name=?",
            ("Holiday",),
        ).fetchone()
        self.assertEqual(row[0], created.id)

    def test_flush_reuses_a_label_that_already_exists_on_gmail(self) -> None:
        self.engine.startup()
        self.fake.labels.append(Label("Label_9", "Holiday", "user"))
        local = self.engine.store.create_user_label("Holiday")
        self.engine.store.apply_local_labels("in1", add=[local])
        self.engine.flush_queue()
        self.assertIn("Label_9", self.fake.messages["in1"].label_ids)
        self.assertIsNone(self.engine.store.pending_label_name(local))

    def test_queued_changes_survive_restart(self) -> None:
        self.engine.startup()
        self.engine.store.apply_local_labels("in1", remove=["INBOX"])
        root = self.engine.store.root
        reopened = Engine.open(root, self.fake, sleeper=lambda _seconds: None)
        try:
            self.assertEqual(reopened.store.pending_mutations(), [("in1", "INBOX", "remove")])
            self.assertNotIn("INBOX", reopened.store.message_label_ids("in1"))
            reopened.store.upsert_metadata(
                IncomingMessage(
                    gmail_id="in1",
                    thread_id="t-in1",
                    label_ids=["INBOX", "UNREAD"],
                    internal_date_ms=2_000,
                ),
                "missing",
            )
            self.assertNotIn("INBOX", reopened.store.message_label_ids("in1"))
            reopened.startup()
            self.assertNotIn("INBOX", self.fake.messages["in1"].label_ids)
            self.assertNotIn("INBOX", reopened.store.message_label_ids("in1"))
            self.assertEqual(reopened.store.pending_mutations(), [])
        finally:
            reopened.store.close()

    def test_sent_change_stays_local_until_history_catches_up(self) -> None:
        self.engine.startup()
        self.engine.store.apply_local_labels("in1", remove=["INBOX"])
        pushed, _rebased = self.engine.flush_queue()
        self.assertEqual(pushed, 1)
        self.assertEqual(self.engine.store.pending_mutations(), [])
        self.engine.store.upsert_metadata(
            IncomingMessage(
                gmail_id="in1",
                thread_id="t-in1",
                label_ids=["INBOX", "UNREAD"],
                internal_date_ms=2_000,
            ),
            "missing",
        )
        self.assertNotIn("INBOX", self.engine.store.message_label_ids("in1"))
        root = self.engine.store.root
        reopened = Engine.open(root, self.fake, sleeper=lambda _seconds: None)
        try:
            self.assertNotIn("INBOX", reopened.store.message_label_ids("in1"))
            self.assertEqual(reopened.store.pending_mutations(), [])
        finally:
            reopened.store.close()

    def test_stale_history_does_not_restore_a_label_just_pushed(self) -> None:
        self.engine.startup()
        self.fake.add_history(
            HistoryRecord(
                id="0",
                labels_added=[
                    LabelChange(HistoryMessage("in1", "t-in1", ["INBOX", "UNREAD"]), ["INBOX"])
                ],
            )
        )
        job = BackgroundSync(self.engine)
        for _ in range(40):
            if job._hist_page is not None:
                break
            self.assertTrue(job.step())
        self.assertIsNotNone(job._hist_page)
        self.engine.store.apply_local_labels("in1", remove=["INBOX"])
        self.assertTrue(UrgentWork(self.engine).step(SimpleNamespace(needs_fetch=False)))
        self.assertNotIn("INBOX", self.fake.messages["in1"].label_ids)
        while job.step():
            pass
        self.assertIsNone(job.exc)
        self.assertNotIn("INBOX", self.engine.store.message_label_ids("in1"))
        self.assertNotIn("INBOX", self.fake.messages["in1"].label_ids)

    def test_urgent_work_sends_changes_before_more_mail_is_fetched(self) -> None:
        self.engine.startup()
        self.engine.store.mark_body_missing("ar1")
        self.engine.store.mark_body_missing("in1")
        self.engine.store.apply_local_labels("in1", add=["STARRED"])
        self.fake.calls.clear()
        session = MailSession(self.engine)
        self.assertEqual(session.current_label().gmail_id, "INBOX")
        self.assertFalse(session.needs_fetch)
        session.load_reader(fetch=True)
        session.refresh_view()
        self.assertFalse(session.needs_fetch)
        self.assertFalse(any(call.endswith(":raw") for call in self.fake.calls))
        for index, item in enumerate(session.visible):
            if item.gmail_id == "Label_1":
                session.label_cursor = index
                break
        session.load_messages(fetch=True)
        self.assertTrue(session.needs_fetch)
        self.fake.calls.clear()
        urgent = UrgentWork(self.engine)
        self.assertTrue(urgent.step(session))
        self.assertTrue(self.engine.store.body_readable("ar1"))
        self.assertFalse(self.engine.store.body_readable("in1"))
        self.assertFalse(any(call.startswith("messages.modify") for call in self.fake.calls))
        self.assertTrue(any("ar1:raw" in call for call in self.fake.calls))
        self.fake.calls.clear()
        self.assertTrue(urgent.step(session))
        self.assertEqual(self.engine.store.pending_mutations(), [])
        self.assertIn("STARRED", self.fake.messages["in1"].label_ids)
        self.assertFalse(any(call.endswith(":raw") for call in self.fake.calls))

    def test_the_screen_does_not_push_a_delete_itself(self) -> None:
        self.engine.startup()
        session = MailSession(self.engine)
        session.focus = "list"
        self.fake.calls.clear()
        session.delete_selected()
        self.assertFalse(any(call.startswith("messages.modify") for call in self.fake.calls))
        self.assertTrue(self.engine.store.has_pending_mutations())
        started = time.monotonic()
        _service(session, BackgroundSync(self.engine), UrgentWork(self.engine))
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertFalse(any(call.startswith("messages.modify") for call in self.fake.calls))
        self.assertTrue(self.engine.store.has_pending_mutations())

    def test_sync_restart_shows_signing_in_before_gmail(self) -> None:
        self.engine.startup()
        job = BackgroundSync(self.engine)
        while job.step():
            pass
        refreshed = {"n": 0}

        def reauthorize(*, force: bool = False) -> bool:
            refreshed["n"] += 1
            return True

        self.engine.gmail.reauthorize = reauthorize  # type: ignore[attr-defined]
        job.restart(refresh_auth=True)
        self.assertEqual(job.phase, "signing in")
        self.assertEqual(job.total, 0)
        self.assertTrue(job.step())
        self.assertEqual(refreshed["n"], 1)
        self.assertFalse(job._auth_pending)
        self.assertEqual(job.phase, "refreshing labels")

    def test_invalid_auth_refreshes_the_token_once(self) -> None:
        self.engine.startup()
        hits = {"history": 0, "refresh": 0}
        inner = self.fake

        class Gate:
            def reauthorize(self, *, force: bool = False) -> bool:
                hits["refresh"] += 1
                self.token = "fresh"
                return True

            def list_history(self, start_history_id: str, page_token: str | None = None):
                hits["history"] += 1
                if hits["history"] == 1:
                    raise HttpStatus(
                        401,
                        "Request had invalid authentication credentials.",
                    )
                return inner.list_history(start_history_id, page_token)

            def __getattr__(self, name: str):
                return getattr(inner, name)

        self.engine.gmail = Gate()
        job = BackgroundSync(self.engine)
        job._poll_at = time.monotonic() - 1
        for _ in range(40):
            if not job.step():
                break
        self.assertIsNone(job.exc)
        self.assertEqual(job.mode, "idle")
        self.assertEqual(hits["history"], 2)
        self.assertEqual(hits["refresh"], 1)

    def test_offline_sync_retries_after_a_wait(self) -> None:
        self.engine.startup()
        job = BackgroundSync(self.engine)
        while job.step():
            pass
        self.assertEqual(job.mode, "idle")
        self.fake.history_offline = True
        job._poll_at = time.monotonic() - 1
        job.step()
        self.assertEqual(job.mode, "offline")
        self.assertIn("name resolution", job.phase)
        wait = job.seconds_until_poll()
        self.assertIsNotNone(wait)
        self.assertGreater(wait, 0)
        self.assertLessEqual(wait, 30)
        self.assertFalse(job.retry_due())
        self.assertFalse(_job_due(job))
        self.fake.history_offline = False
        self.fake.add(_inbox("later", "wake", 9_000, "After sleep"))
        self.fake.add_history(added("later", "t-later", ["INBOX", "UNREAD"]))
        job._retry_at = time.monotonic() - 1
        self.assertTrue(_job_due(job))
        self.assertEqual(job.phase, "signing in")
        while job.step():
            pass
        self.assertEqual(job.mode, "idle")
        self.assertIsNone(job.exc)
        self.assertTrue(self.engine.store.body_readable("later"))

    def test_warm_start_checks_history_without_relisting_inbox(self) -> None:
        self.engine.startup()
        self.fake.calls.clear()
        job = BackgroundSync(self.engine)
        self.assertFalse(job._inbox_catchup)
        phases: list[str] = []
        for _ in range(80):
            if not job.step():
                break
            phases.append(job.phase)
        self.assertEqual(job.mode, "idle")
        self.assertFalse(any(phase.startswith("listing Inbox") for phase in phases))
        self.assertTrue(any(call.startswith("history.list:") for call in self.fake.calls))
        self.assertNotIn("messages.list:label=INBOX:q=", self.fake.calls)

    def test_check_mail_starts_a_poll_on_the_idle_mailbox(self) -> None:
        self.engine.startup()
        job = BackgroundSync(self.engine)
        while job.step():
            pass
        self.assertEqual(job.mode, "idle")
        self.fake.add(_inbox("fresh", "wake", 9_500, "Just now"))
        self.fake.add_history(added("fresh", "t-fresh", ["INBOX", "UNREAD"]))
        job.check_mail()
        self.assertEqual(job.mode, "history")
        self.assertEqual(job.phase, "checking for new mail")
        while job.step():
            pass
        self.assertEqual(job.mode, "idle")
        self.assertTrue(self.engine.store.body_readable("fresh"))

    def test_check_mail_wakes_an_offline_mailbox(self) -> None:
        self.engine.startup()
        job = BackgroundSync(self.engine)
        while job.step():
            pass
        self.fake.history_offline = True
        job._poll_at = time.monotonic() - 1
        job.step()
        self.assertEqual(job.mode, "offline")
        self.fake.history_offline = False
        job.check_mail(refresh_auth=True)
        self.assertTrue(job.retry_due())
        self.assertTrue(_job_due(job))
        while job.step():
            pass
        self.assertEqual(job.mode, "idle")

    def test_unexpected_step_error_is_retried(self) -> None:
        self.engine.startup()
        job = BackgroundSync(self.engine)
        while job.step():
            pass

        def broken() -> None:
            raise RuntimeError("boom")

        job.mode = "labels"
        job._polling = True
        job._dispatch = broken  # type: ignore[method-assign]
        job.step()
        self.assertEqual(job.mode, "error")
        self.assertEqual(job.phase, "boom")
        self.assertGreater(job._retry_at, time.monotonic())
        self.assertFalse(job._polling)

    def test_abort_ends_a_sync_step_without_an_error(self) -> None:
        job = BackgroundSync(self.engine)
        self.engine.quota.abort()
        self.assertFalse(job.step())
        self.assertIsNone(job.exc)
        self.assertEqual(job.mode, "labels")

    def test_rate_limit_is_retried(self) -> None:
        self.fake.profile_rate_limits = 1
        self.engine.startup()
        self.assertGreaterEqual(self.fake.calls.count("profile"), 2)

    def test_per_minute_quota_waits_instead_of_stopping(self) -> None:
        self.fake.raw_quota_once = 1
        self.engine.startup()
        self.assertTrue(self.engine.store.body_readable("in1"))
        self.assertGreaterEqual(self.fake.calls.count("messages.get:in1:raw"), 2)
        self.assertTrue(any(wait >= 60 for wait in self.engine.quota.waits))

    def test_minute_window_stays_under_the_user_cap(self) -> None:
        clock = {"t": 0.0}

        def now() -> float:
            return clock["t"]

        def sleep(seconds: float) -> None:
            clock["t"] += seconds

        quota = Quota(100, sleeper=sleep, now=now)
        quota.acquire(60)
        quota.acquire(60)
        self.assertGreaterEqual(clock["t"], 60)
        self.assertEqual(quota.units, 120)

    def test_a_fresh_minute_pays_for_a_full_inbox_batch(self) -> None:
        clock = {"t": 0.0}

        def now() -> float:
            return clock["t"]

        def sleep(seconds: float) -> None:
            clock["t"] += seconds

        quota = Quota(6_000, sleeper=sleep, now=now)
        self.assertEqual(quota.available(60, 100), 100)
        self.assertEqual(clock["t"], 0.0)
        for _ in range(100):
            quota.acquire(60)
        self.assertEqual(clock["t"], 0.0)
        self.assertEqual(quota.available(60, 100), 100)
        self.assertGreaterEqual(clock["t"], 60)

    def test_raw_fetches_stay_under_a_gmail_minute(self) -> None:
        clock = {"t": 0.0}

        def now() -> float:
            return clock["t"]

        def sleep(seconds: float) -> None:
            clock["t"] += seconds

        quota = Quota(6_000, sleeper=sleep, now=now)
        stamps: list[float] = []
        for _ in range(200):
            quota.acquire(60)
            stamps.append(clock["t"])
        self.assertEqual(quota.units, 12_000)
        for stamp in stamps:
            in_minute = sum(60 for earlier in stamps if stamp - 60 < earlier <= stamp)
            self.assertLessEqual(in_minute, 6_000)

    def test_abort_wakes_a_quota_sleep(self) -> None:
        quota = Quota(100)
        quota.acquire(100)
        done = threading.Event()
        error: list[BaseException] = []

        def waiter() -> None:
            try:
                quota.acquire(1)
            except SyncStopped:
                done.set()
            except BaseException as exc:
                error.append(exc)
                done.set()

        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        started = time.monotonic()
        quota.abort()
        self.assertTrue(done.wait(1))
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(error, [])
        thread.join(1)
        self.assertFalse(thread.is_alive())

    def test_minute_quota_http_status_is_a_retry(self) -> None:
        detail = (
            "Quota exceeded for quota metric 'Total Query Cost' "
            "and limit 'Units per minute per user'"
        )
        self.assertTrue(is_rate_limited(403, detail))
        self.assertTrue(is_rate_limited(429, "slow down"))
        self.assertFalse(is_rate_limited(403, "Insufficient Permission"))


if __name__ == "__main__":
    unittest.main()

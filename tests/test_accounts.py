"""Several Gmail accounts share one data root, each with its own index."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from gmail_tui.accounts import Account, account_root, arrange, discover, resolve_account_dir
from gmail_tui.fake import FakeGmail, FakeMessage
from gmail_tui.gmail import IncomingMessage, Label, NullGmail
from gmail_tui.store import Store
from gmail_tui.sync import Engine
from gmail_tui.tui import (
    _Account,
    _Desk,
    _account_loop,
    _halt,
    _input_timeout,
    _service,
    _switch,
)


class AccountTests(unittest.TestCase):
    def test_inbox_counts_skip_trash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(tmp)
            try:
                store.replace_labels(
                    [
                        Label("INBOX", "INBOX", "system"),
                        Label("UNREAD", "UNREAD", "system"),
                        Label("TRASH", "TRASH", "system"),
                    ]
                )
                store.set_email("ada@example.com")
                self._message(store, "m1", ["INBOX", "UNREAD"])
                self._message(store, "m2", ["INBOX"])
                self._message(store, "m3", ["INBOX", "UNREAD", "TRASH"])
                self.assertEqual(store.inbox_counts(), (2, 1))
                self.assertEqual(store.account_email(), "ada@example.com")
            finally:
                store.close()

    def test_discover_lists_accounts_under_accounts_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "mail"
            first = account_root(data, "ada@example.com")
            root = Store.open(first)
            root.set_email("ada@example.com")
            root.close()
            extra = account_root(data, "bea@example.com")
            other = Store.open(extra)
            other.set_profile("bea@example.com", "9", 3)
            other.close()
            accounts = discover(data)
            self.assertEqual([item.name for item in accounts], ["ada@example.com", "bea@example.com"])
            self.assertEqual(accounts[0].root, first)
            self.assertEqual(accounts[1].root, extra)
            self.assertFalse((data / "index.sqlite").exists())

    def test_resolve_account_dir_follows_the_signed_in_email(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "mail"
            first = account_root(data, "ada@example.com")
            root = Store.open(first)
            root.set_email("ada@example.com")
            root.close()
            extra = account_root(data, "bea@example.com")
            other = Store.open(extra)
            other.set_profile("bea@example.com", "9", 3)
            other.close()
            self.assertEqual(resolve_account_dir(data, "Ada@example.com"), first)
            self.assertEqual(resolve_account_dir(data, "bea@example.com"), extra)
            fresh = resolve_account_dir(data, "cy@example.com")
            self.assertEqual(fresh, data / "accounts" / "cy@example.com")
            empty = Path(tmp) / "empty"
            self.assertEqual(
                resolve_account_dir(empty, "ada@example.com"),
                empty / "accounts" / "ada@example.com",
            )

    def test_legacy_data_root_moves_into_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "mail"
            root = Store.open(data)
            root.set_email("ada@example.com")
            root.replace_labels([Label("INBOX", "INBOX", "system")])
            root.upsert_metadata(
                IncomingMessage(
                    gmail_id="m1",
                    thread_id="t1",
                    label_ids=["INBOX"],
                    internal_date_ms=1_000,
                    subject="kept",
                    from_addr="ada@example.com",
                    to_addr="me@gmail.com",
                ),
                "missing",
            )
            root.close()
            accounts = discover(data)
            dest = data / "accounts" / "ada@example.com"
            self.assertEqual([item.name for item in accounts], ["ada@example.com"])
            self.assertEqual(accounts[0].root, dest)
            self.assertGreater(accounts[0].local_messages, 0)
            self.assertFalse((data / "index.sqlite").exists())
            self.assertTrue((dest / "index.sqlite").exists())

    def test_discover_dedupes_the_same_address(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "mail"
            thin = account_root(data, "ada@example.com")
            root = Store.open(thin)
            root.set_email("ada@example.com")
            root.close()
            # Simulate a leftover duplicate folder name with more mail by using a
            # second discover pass after writing more into the same path is enough;
            # instead plant a sibling that shares the email via store identity.
            other = Store.open(thin)
            other.replace_labels([Label("INBOX", "INBOX", "system")])
            other.upsert_metadata(
                IncomingMessage(
                    gmail_id="m1",
                    thread_id="t1",
                    label_ids=["INBOX"],
                    internal_date_ms=1_000,
                    subject="kept",
                    from_addr="ada@example.com",
                    to_addr="me@gmail.com",
                ),
                "missing",
            )
            other.close()
            # Legacy root duplicate with fewer messages is discarded on migrate.
            legacy = Store.open(data)
            legacy.set_email("ada@example.com")
            legacy.close()
            accounts = discover(data)
            self.assertEqual([item.name for item in accounts], ["ada@example.com"])
            self.assertEqual(accounts[0].root, thin)
            self.assertGreater(accounts[0].local_messages, 0)
            self.assertFalse((data / "index.sqlite").exists())

    def test_set_email_refuses_a_different_address(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(tmp)
            try:
                store.set_email("ada@example.com")
                with self.assertRaises(ValueError):
                    store.set_email("bea@example.com")
                self.assertEqual(store.account_email(), "ada@example.com")
            finally:
                store.close()

    def test_arrange_follows_the_config_then_the_rest(self) -> None:
        accounts = [
            Account("bela@example.com", Path("bela")),
            Account("ada@example.com", Path("ada")),
            Account("cy@example.com", Path("cy")),
        ]
        ordered = arrange(accounts, ("Cy@example.com", "ada@example.com"))
        self.assertEqual([item.name for item in ordered], ["cy@example.com", "ada@example.com", "bela@example.com"])
        self.assertEqual(arrange(accounts, ()), accounts)
        skipped = arrange(accounts, ("nobody@example.com", "ada@example.com"))
        self.assertEqual([item.name for item in skipped], ["ada@example.com", "bela@example.com", "cy@example.com"])

    def test_desk_does_not_build_calendar_until_toggled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = Engine.open(tmp, NullGmail(), sleeper=lambda _seconds: None)
            desk = _Desk([_Account(engine)], [Account("ada", Path(tmp))])
            try:
                self.assertIsNone(desk._calendar)
                desk.toggle_calendar()
                self.assertIsNotNone(desk._calendar)
                self.assertEqual(desk.mode, "calendar")
                desk.toggle_calendar()
                self.assertEqual(desk.mode, "mail")
            finally:
                engine.store.close()

    def test_switch_opens_the_other_mailbox(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "mail"
            first = account_root(data, "ada@example.com")
            root = Store.open(first)
            root.set_email("ada@example.com")
            root.close()
            extra = account_root(data, "bea@example.com")
            other = Store.open(extra)
            other.set_profile("bea@example.com", "9", 3)
            other.close()
            accounts = discover(data)
            desk = _Desk(
                [
                    _Account(Engine.open(accounts[0].root, NullGmail(), sleeper=lambda _seconds: None)),
                    _Account(Engine.open(accounts[1].root, NullGmail(), sleeper=lambda _seconds: None)),
                ],
                accounts,
            )
            try:
                first_job = desk.jobs[0].job
                desk.jobs[1].session.focus = "reader"
                _switch(desk, accounts, 1)
                self.assertEqual(desk.index, 1)
                self.assertEqual(desk.engine.store.root, extra)
                self.assertEqual(desk.engine.store.account_email(), "bea@example.com")
                self.assertEqual(accounts[0].name, "ada@example.com")
                self.assertIs(desk.jobs[0].job, first_job)
                self.assertEqual(desk.session.focus, "reader")
                self.assertEqual(desk.engine.store.open_label(), "INBOX")
            finally:
                for slot in desk.jobs:
                    slot.engine.store.close()

    def test_switch_restores_the_folder_and_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "mail"
            first = account_root(data, "ada@example.com")
            root = Store.open(first)
            root.set_email("ada@example.com")
            root.replace_labels(
                [
                    Label("INBOX", "INBOX", "system"),
                    Label("Label_1", "Work", "user"),
                ]
            )
            message = IncomingMessage(
                gmail_id="a1",
                thread_id="t1",
                label_ids=["Label_1"],
                internal_date_ms=2_000,
                subject="Plans",
                from_addr="Ada <ada@example.com>",
                to_addr="me@gmail.com",
            )
            root.upsert_metadata(message, "missing")
            root.write_body(
                message,
                b"Subject: Plans\r\nFrom: Ada <ada@example.com>\r\n\r\nwork notes\r\n",
                "work notes",
            )
            root.close()
            extra = account_root(data, "bea@example.com")
            other = Store.open(extra)
            other.set_profile("bea@example.com", "9", 1)
            other.close()
            accounts = discover(data)
            desk = _Desk(
                [
                    _Account(Engine.open(accounts[0].root, NullGmail())),
                    _Account(Engine.open(accounts[1].root, NullGmail())),
                ],
                accounts,
            )
            try:
                session = desk.session
                names = [item.name for item in session.visible]
                session.label_cursor = names.index("Work")
                session.load_messages(fetch=False)
                self.assertEqual([item.subject for item in session.messages], ["Plans"])
                session.focus = "list"
                session.begin_search()
                session.prompt_type("Plans")
                session.prompt_accept()
                self.assertEqual(session.search_text, "Plans")
                self.assertEqual(session.focus, "list")
                kept = session.message_cursor
                _switch(desk, accounts, 1)
                self.assertEqual(desk.session.search_text, "")
                _switch(desk, accounts, 0)
                restored = desk.session
                self.assertEqual(restored.search_text, "Plans")
                self.assertEqual(restored.focus, "list")
                self.assertEqual(restored.message_cursor, kept)
                self.assertEqual([item.subject for item in restored.messages], ["Plans"])
                restored.clear_search()
                self.assertEqual(restored.current_label().name, "Work")
                self.assertEqual([item.subject for item in restored.messages], ["Plans"])
            finally:
                for slot in desk.jobs:
                    slot.engine.store.close()

    def test_switch_keeps_the_inbox_listing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            shown = FakeGmail()
            shown.email = "ada@example.com"
            hidden_gmail = FakeGmail()
            hidden_gmail.email = "bea@example.com"
            hidden_gmail.page_size = 2
            for index, gmail in enumerate((shown, hidden_gmail)):
                for number in range(3):
                    gmail.add(
                        FakeMessage(
                            id=f"m{index}-{number}",
                            thread_id=f"t{index}-{number}",
                            label_ids=["INBOX"],
                            subject=f"Note {number}",
                            from_addr=gmail.email,
                            body="body",
                            internal_date_ms=1_000 + number,
                        )
                    )
            tabs = [
                Account("ada@example.com", data / "ada"),
                Account("bea@example.com", data / "bea"),
            ]
            desk = _Desk(
                [
                    _Account(Engine.open(data / "ada", shown, sleeper=lambda _seconds: None)),
                    _Account(Engine.open(data / "bea", hidden_gmail, sleeper=lambda _seconds: None)),
                ],
                tabs,
            )
            hidden = desk.jobs[1]
            try:
                for _ in range(20):
                    hidden.job.step()
                    if hidden.job._inbox_token:
                        break
                self.assertTrue(hidden.job._inbox_token)
                seen = set(hidden.job._inbox_seen)
                calls = list(hidden_gmail.calls)
                _switch(desk, tabs, 1)
                _switch(desk, tabs, 0)
                self.assertIs(desk.jobs[1].job, hidden.job)
                self.assertEqual(hidden.job._inbox_seen, seen)
                self.assertEqual(hidden_gmail.calls, calls)
                self.assertTrue(hidden.job._inbox_catchup)
            finally:
                for slot in desk.jobs:
                    slot.engine.store.close()

    def test_hidden_account_syncs_while_another_is_on_screen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            gmail = FakeGmail()
            gmail.add(
                FakeMessage(
                    id="m1",
                    thread_id="t1",
                    label_ids=["INBOX"],
                    subject="Hello",
                    from_addr="bea@example.com",
                    body="body",
                    internal_date_ms=1_000,
                )
            )
            tabs = [Account("ada", data / "ada"), Account("bea", data / "bea")]
            desk = _Desk(
                [
                    _Account(Engine.open(data / "ada", NullGmail(), sleeper=lambda _seconds: None)),
                    _Account(Engine.open(data / "bea", gmail, sleeper=lambda _seconds: None)),
                ],
                tabs,
            )
            desk.stop.clear()
            threads = [
                threading.Thread(target=_account_loop, args=(desk, slot, tab), daemon=True)
                for slot, tab in zip(desk.jobs, tabs)
            ]
            for thread in threads:
                thread.start()
            try:
                deadline = time.monotonic() + 2
                while (
                    desk.jobs[1].job.tick == 0 or desk.jobs[0].job.tick == 0
                ) and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertGreater(desk.jobs[1].job.tick, 0)
                self.assertGreater(desk.jobs[0].job.tick, 0)
            finally:
                desk.stop.set()
                desk.poke.set()
                for thread in threads:
                    thread.join(2)
                for slot in desk.jobs:
                    slot.engine.store.close()

    def test_switch_does_not_wait_for_the_other_accounts_download(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            tabs = [Account("ada", data / "ada"), Account("bea", data / "bea")]
            desk = _Desk(
                [
                    _Account(Engine.open(data / "ada", NullGmail(), sleeper=lambda _seconds: None)),
                    _Account(Engine.open(data / "bea", NullGmail(), sleeper=lambda _seconds: None)),
                ],
                tabs,
            )
            entered = threading.Event()
            release = threading.Event()

            def blocked() -> bool:
                entered.set()
                release.wait(3)
                return False

            desk.jobs[1].job.step = blocked
            desk.stop.clear()
            thread = threading.Thread(
                target=_account_loop, args=(desk, desk.jobs[1], tabs[1]), daemon=True
            )
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                started = time.monotonic()
                _switch(desk, tabs, 1)
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(desk.index, 1)
                self.assertEqual(desk.session.focus, "list")
            finally:
                release.set()
                desk.stop.set()
                desk.poke.set()
                thread.join(2)
                for slot in desk.jobs:
                    slot.engine.store.close()

    def test_quit_does_not_wait_out_a_sync_sleep(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = Engine.open(tmp, NullGmail())
            engine.quota.acquire(engine.quota.per_minute)
            desk = _Desk([_Account(engine)], [Account("ada", Path("ada"))])
            slot = desk.jobs[0]
            entered = threading.Event()

            def blocked() -> bool:
                entered.set()
                engine.quota.acquire(1)
                return False

            slot.job.step = blocked  # type: ignore[method-assign]
            tab = Account("ada", Path(tmp))
            thread = threading.Thread(target=_account_loop, args=(desk, slot, tab), daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                started = time.monotonic()
                _halt(desk)
                thread.join(1)
                self.assertLess(time.monotonic() - started, 1)
                self.assertFalse(thread.is_alive())
            finally:
                _halt(desk)
                thread.join(1)
                engine.store.close()

    def test_an_idle_account_sleeps_instead_of_polling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            desk = _Desk(
                [_Account(Engine.open(data / "ada", NullGmail()))],
                [Account("ada", data / "ada")],
            )
            slot = desk.jobs[0]
            slot.engine.store.set_history_id("50")
            slot.job.mode = "idle"
            slot.job.phase = "idle"
            slot.job._poll_at = time.monotonic() + 30
            tab = Account("ada", data / "ada")
            thread = threading.Thread(target=_account_loop, args=(desk, slot, tab), daemon=True)
            thread.start()
            try:
                time.sleep(0.25)
                self.assertEqual(slot.job.tick, 0)
                self.assertTrue(thread.is_alive())
                self.assertGreaterEqual(_input_timeout(desk, slot.session, slot.urgent), 20_000)
                slot.job.mode = "backfill"
                self.assertEqual(_input_timeout(desk, slot.session, slot.urgent), 1000)
                # A shown check age arms the header watchdog to one second.
                slot.job.mode = "idle"
                desk.accounts[0].checked_at = time.time()
                header_at = time.monotonic() + 1.0
                self.assertLessEqual(
                    _input_timeout(desk, slot.session, slot.urgent, header_at=header_at),
                    1000,
                )
            finally:
                desk.stop.set()
                desk.poke.set()
                thread.join(2)
                self.assertFalse(thread.is_alive())
                slot.engine.store.close()

    def test_a_quiet_sync_does_not_ask_for_another_paint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            desk = _Desk(
                [_Account(Engine.open(tmp, NullGmail()))],
                [Account("me", Path(tmp))],
            )
            try:
                session = desk.session
                job = desk.job
                job.mode = "idle"
                job.phase = session.sync_phase
                job.done = session.sync_done
                job.total = session.sync_total
                job.tick = session.sync_tick
                job.dirty = False
                self.assertFalse(_service(session, job, desk.urgent))
                job.phase = "caching mail 3/10"
                job.done = 3
                job.total = 10
                self.assertFalse(_service(session, job, desk.urgent))
                self.assertEqual(session.sync_phase, "caching mail 3/10")
                job.dirty = True
                self.assertTrue(_service(session, job, desk.urgent))
                job.dirty = True
                session._refreshed_at = time.monotonic()
                # A throttled counter update must not wipe the panes (or sixel).
                self.assertFalse(_service(session, job, desk.urgent))
                self.assertTrue(job.dirty)
            finally:
                desk.engine.store.close()

    def _message(self, store: Store, gmail_id: str, labels: list[str]) -> None:
        store.upsert_metadata(
            IncomingMessage(
                gmail_id=gmail_id,
                thread_id=gmail_id,
                label_ids=labels,
                internal_date_ms=1_000,
                subject=gmail_id,
                from_addr="ada@example.com",
                to_addr="me@gmail.com",
                snippet="",
            ),
            "missing",
        )


if __name__ == "__main__":
    unittest.main()

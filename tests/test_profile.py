"""The profile log records memory, per-message time, and functions."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from gmail_tui import profile


def _burn() -> int:
    total = 0
    for value in range(5000):
        total += value
    return total


def _other_account() -> int:
    total = 0
    for value in range(5000):
        total += value
    return total


class ProfileTests(unittest.TestCase):
    def tearDown(self) -> None:
        profile.stop()

    def test_nothing_is_recorded_unless_the_flag_started_it(self) -> None:
        self.assertFalse(profile.recording())
        profile.begin_message()
        profile.add("parse", 1.0)
        profile.finish_message("abc", 10)
        profile.checkpoint()

    def test_log_has_memory_message_time_and_functions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.log"
            profile.start(path, interval=None)
            try:
                self.assertTrue(profile.recording())
                profile.attach()
                _burn()
                profile.begin_message()
                profile.add("read", 0.8, wire=1200)
                profile.add("json", 0.002)
                profile.add("parse", 0.004)
                profile.add("save", 0.003)
                profile.finish_message("abc123", 900)
                profile.sample()
            finally:
                profile.stop()
            text = path.read_text(encoding="utf-8")
        self.assertFalse(profile.recording())
        self.assertIn("profile start", text)
        self.assertIn("profile stop", text)
        self.assertIn("mem rss=", text)
        self.assertIn(
            "msg abc123 raw=900 wire=1200 read=800ms json=2ms parse=4ms save=3ms",
            text,
        )
        self.assertIn("n=1", text)
        self.assertIn("_burn", text)

    def test_each_account_thread_is_profiled(self) -> None:
        errors: list[BaseException] = []

        def second() -> None:
            try:
                profile.attach()
                _other_account()
                profile.detach()
            except BaseException as exc:
                errors.append(exc)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.log"
            profile.start(path, interval=None)
            try:
                profile.attach()
                _burn()
                worker = threading.Thread(target=second, name="gmail-tui-other@gmail")
                worker.start()
                worker.join()
            finally:
                profile.stop()
            text = path.read_text(encoding="utf-8")
        self.assertEqual(errors, [])
        self.assertIn("_burn", text)
        self.assertIn("_other_account", text)
        self.assertIn("gmail-tui-other@gmail", text)


if __name__ == "__main__":
    unittest.main()

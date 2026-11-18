"""Key config: defaults, overrides, and the file created on first load."""

from __future__ import annotations

import curses
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from gmail_tui.keys import KeysError, config_path, defaults, load, parse


class KeyTests(unittest.TestCase):
    def test_defaults_keep_the_original_motions(self) -> None:
        keys = defaults()
        self.assertEqual(keys.action(ord("q")), "quit")
        self.assertEqual(keys.action(ord("j")), "down")
        self.assertEqual(keys.action(curses.KEY_DOWN), "down")
        self.assertEqual(keys.action(ord("k")), "up")
        self.assertEqual(keys.action(ord("J")), "page-down")
        self.assertEqual(keys.action(ord("K")), "page-up")
        self.assertEqual(keys.action(ord(" ")), "page-down")
        self.assertEqual(keys.action(curses.KEY_NPAGE), "page-down")
        self.assertEqual(keys.action(curses.KEY_PPAGE), "page-up")
        self.assertEqual(keys.action(ord("s")), "sync")
        self.assertEqual(keys.action(ord("S")), "sync-all")
        self.assertEqual(keys.action(ord("b")), "snooze")
        self.assertEqual(keys.action(ord("B")), "cache")
        self.assertEqual(keys.action(ord("\t")), "next-pane")
        self.assertEqual(keys.action(10), "activate")
        self.assertEqual(keys.action(ord("/")), "search")
        self.assertEqual(keys.action(ord("l")), "add-label")
        self.assertEqual(keys.action(curses.KEY_RIGHT), "right")
        self.assertEqual(keys.action(ord("g")), "top")
        self.assertTrue(keys.has_prefix(ord("g")))
        self.assertEqual(keys.sequence(ord("g"), ord("i")), "inbox")
        self.assertTrue(keys.has_prefix(ord("z")))
        self.assertEqual(keys.sequence(ord("z"), ord("o")), "open")
        self.assertEqual(keys.sequence(ord("z"), ord("c")), "close")
        self.assertEqual(keys.sequence(ord("z"), ord("a")), "toggle-fold")
        self.assertEqual(keys.action(ord("a")), "archive")
        self.assertEqual(keys.action(ord("e")), "archive")
        self.assertEqual(keys.action(ord("d")), "delete")
        self.assertEqual(keys.action(ord("x")), "select")
        self.assertEqual(keys.action(ord("o")), "order")
        self.assertEqual(keys.action(ord("u")), "toggle-unread")
        self.assertEqual(keys.action(ord("n")), "next-unread")
        self.assertEqual(keys.action(ord("c")), "compose")
        self.assertEqual(keys.action(ord("C")), "calendar")
        self.assertEqual(keys.action(ord("r")), "reply-all")
        self.assertEqual(keys.action(ord("R")), "reply")
        self.assertEqual(keys.action(ord("H")), "holding")
        self.assertEqual(keys.action(ord("A")), "attachments")
        self.assertEqual(keys.action(ord("m")), "mime-parts")
        self.assertEqual(keys.action(ord("v")), "view-image")
        self.assertEqual(keys.action(ord("V")), "raw-source")
        self.assertEqual(keys.action(ord("F")), "headers")
        self.assertEqual(keys.action(ord("L")), "urls")
        self.assertEqual(keys.action(ord("U")), "undo")
        self.assertEqual(keys.action(ord("]")), "next-account")
        self.assertEqual(keys.action(ord("[")), "prev-account")
        self.assertEqual(keys.action(ord("?")), "help")

    def test_file_overrides_one_action_and_keeps_the_rest(self) -> None:
        keys = parse("down n\nquit x\n")
        self.assertEqual(keys.action(ord("n")), "down")
        self.assertIsNone(keys.action(ord("j")))
        self.assertEqual(keys.action(ord("x")), "quit")
        self.assertEqual(keys.bindings["select"], ())
        self.assertEqual(keys.action(ord("k")), "up")

    def test_fold_keys_remain_when_right_is_only_the_arrow(self) -> None:
        keys = parse("right right\nleft h left\n")
        self.assertEqual(keys.sequence(ord("z"), ord("o")), "open")
        self.assertEqual(keys.sequence(ord("z"), ord("c")), "close")
        self.assertEqual(keys.sequence(ord("z"), ord("a")), "toggle-fold")
        self.assertEqual(keys.action(ord("l")), "add-label")
        self.assertEqual(keys.action(curses.KEY_RIGHT), "right")

    def test_duplicate_key_is_rejected(self) -> None:
        with self.assertRaises(KeysError):
            parse("down j\nup j\n")

    def test_missing_file_is_created_with_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gmail-tui" / "keys"
            keys = load(path)
            text = path.read_text(encoding="utf-8")
            self.assertIn("quit q Q", text)
            self.assertIn("cache B", text)
            self.assertIn("snooze b", text)
            self.assertIn("search /", text)
            self.assertIn("add-label l", text)
            self.assertIn("inbox g-i", text)
            self.assertIn("open z-o", text)
            self.assertIn("close z-c", text)
            self.assertIn("toggle-fold z-a", text)
            self.assertIn("select x", text)
            self.assertIn("order o", text)
            self.assertIn("next-account ]", text)
            self.assertIn("help ?", text)
            self.assertIn(str(path), text)
            self.assertEqual(keys.action(ord("G")), "bottom")
            self.assertEqual(config_path().name, "keys")

    def test_env_keys_path_is_not_auto_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nix-store-keys"
            with unittest.mock.patch.dict("os.environ", {"GMAIL_TUI_KEYS": str(missing)}):
                with self.assertRaises(KeysError):
                    load()
            self.assertFalse(missing.exists())

    def test_env_keys_path_is_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "keys"
            path.write_text("quit x\n", encoding="utf-8")
            with unittest.mock.patch.dict("os.environ", {"GMAIL_TUI_KEYS": str(path)}):
                keys = load()
                self.assertEqual(config_path(), path)
            self.assertEqual(keys.action(ord("x")), "quit")
            self.assertIsNone(keys.action(ord("q")))


if __name__ == "__main__":
    unittest.main()

"""Settings file for download and data directories."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from gmail_tui.settings import (
    SettingsError,
    config_dir,
    config_path,
    default_data,
    default_text,
    load,
    normalize_profile,
    parse,
    resolve_config_path,
    resolve_data_dir,
    xdg_config_home,
    xdg_data_home,
)


class SettingsTests(unittest.TestCase):
    def test_parse_reads_download_and_data(self) -> None:
        settings = parse("download ~/Mail/Saved\ndata /var/gmail-tui\n")
        self.assertEqual(settings.download, Path("~/Mail/Saved").expanduser())
        self.assertEqual(settings.data, Path("/var/gmail-tui"))
        self.assertEqual(settings.accounts, ())

    def test_account_lines_keep_file_order(self) -> None:
        settings = parse(
            "account Bea@example.com\n"
            "# account skipped@example.com\n"
            "account ada@example.com\n"
            "account Bea@example.com\n"
        )
        self.assertEqual(settings.accounts, ("Bea@example.com", "ada@example.com", "Bea@example.com"))

    def test_account_line_takes_one_address(self) -> None:
        with self.assertRaises(SettingsError):
            parse("account ada@example.com bea@example.com\n")

    def test_unknown_setting_is_an_error(self) -> None:
        with self.assertRaises(SettingsError):
            parse("theme dark\n")

    def test_load_creates_a_default_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config"
            settings = load(path)
            self.assertTrue(path.exists())
            text = path.read_text(encoding="utf-8")
            self.assertIn("download", text)
            self.assertIn("data", text)
            self.assertIsNotNone(settings.download)
            self.assertIsNotNone(settings.data)
            again = load(path)
            self.assertEqual(again.download, settings.download)
            self.assertEqual(again.data, settings.data)

    def test_default_text_documents_the_keys(self) -> None:
        text = default_text(Path("/tmp/gmail-tui-config"))
        self.assertIn("download", text)
        self.assertIn("data", text)
        self.assertIn("--profile", text)
        self.assertIn("account", text)
        self.assertIn("date", text)
        self.assertIn("time", text)
        self.assertIn("datetime", text)
        self.assertIn("weekday", text)
        self.assertIn("oauth-tpm", text)

    def test_profile_nests_under_the_data_directory(self) -> None:
        base = Path("/var/gmail-tui")
        self.assertEqual(resolve_data_dir(base), base)
        self.assertEqual(resolve_data_dir(base, "work"), base / "work")
        self.assertEqual(normalize_profile("  home  "), "home")
        self.assertIsNone(normalize_profile(""))
        self.assertIsNone(normalize_profile(None))
        with self.assertRaises(SettingsError):
            normalize_profile("../escape")
        with self.assertRaises(SettingsError):
            normalize_profile("a/b")

    def test_date_formats_apply_to_helpers(self) -> None:
        from datetime import datetime
        from gmail_tui import formats
        from gmail_tui.settings import apply

        settings = parse(
            "date %d/%m/%Y\n"
            "time %H.%M\n"
            "weekday %a %d/%m\n"
        )
        apply(settings)
        self.assertEqual(settings.datetime, "%d/%m/%Y %H.%M")
        stamp = datetime(2026, 11, 12, 9, 30)
        self.assertEqual(formats.format_date(stamp), "12/11/2026")
        self.assertEqual(formats.format_time(stamp), "09.30")
        self.assertEqual(formats.format_datetime(stamp), "12/11/2026 09.30")
        # Restore defaults for other tests.
        apply(parse(""))

    def test_xdg_paths_are_absolute_and_under_home_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = os.environ.copy()
            try:
                os.environ["XDG_CONFIG_HOME"] = str(Path(tmp) / "cfg")
                os.environ["XDG_DATA_HOME"] = str(Path(tmp) / "share")
                os.environ.pop("GMAIL_TUI_CONFIG", None)
                self.assertEqual(xdg_config_home(), Path(tmp) / "cfg")
                self.assertEqual(xdg_data_home(), Path(tmp) / "share")
                self.assertEqual(config_dir(), Path(tmp) / "cfg" / "gmail-tui")
                self.assertEqual(default_data(), Path(tmp) / "share" / "gmail-tui")
                self.assertTrue(default_data().is_absolute())
                self.assertEqual(resolve_config_path(), config_path())
            finally:
                for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "GMAIL_TUI_CONFIG"):
                    if key in env:
                        os.environ[key] = env[key]
                    else:
                        os.environ.pop(key, None)

    def test_resolve_config_path_accepts_file_dir_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_path = root / "custom.conf"
            file_path.write_text("download /tmp\n", encoding="utf-8")
            folder = root / "portable"
            folder.mkdir()
            self.assertEqual(resolve_config_path(file_path), file_path)
            self.assertEqual(resolve_config_path(folder), folder / "config")
            env = os.environ.copy()
            try:
                os.environ["GMAIL_TUI_CONFIG"] = str(folder)
                self.assertEqual(resolve_config_path(), folder / "config")
                os.environ["GMAIL_TUI_CONFIG"] = str(file_path)
                self.assertEqual(resolve_config_path(), file_path)
            finally:
                if "GMAIL_TUI_CONFIG" in env:
                    os.environ["GMAIL_TUI_CONFIG"] = env["GMAIL_TUI_CONFIG"]
                else:
                    os.environ.pop("GMAIL_TUI_CONFIG", None)

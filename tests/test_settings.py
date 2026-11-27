"""Settings file for download and data directories."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from gmail_tui.settings import (
    DEFAULT_ON_DEMAND,
    SettingsError,
    apply,
    config_dir,
    config_path,
    default_data,
    default_text,
    load,
    normalize_profile,
    parse,
    parse_profile,
    parse_profile_open,
    resolve_config_path,
    resolve_data_dir,
    resolve_open_command,
    resolve_signature,
    xdg_config_home,
    xdg_data_home,
)
from gmail_tui import store


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
        self.assertIn("on-demand", text)
        self.assertIn("open", text)
        self.assertIn("signature", text)
        self.assertIn("profiles/<name>", text)

    def test_on_demand_folders_default_and_override(self) -> None:
        self.assertEqual(parse("").on_demand, DEFAULT_ON_DEMAND)
        self.assertEqual(
            parse("on-demand trash\non-demand Label_9\n").on_demand,
            ("TRASH", "Label_9"),
        )
        self.assertEqual(parse("on-demand none\n").on_demand, ())
        self.assertEqual(
            parse("on-demand none\non-demand SPAM\n").on_demand,
            ("SPAM",),
        )
        with self.assertRaises(SettingsError):
            parse("on-demand TRASH SPAM\n")
        with self.assertRaises(SettingsError):
            parse("on-demand weird/label\n")
        apply(parse("on-demand SPAM\n"))
        try:
            self.assertEqual(store.on_demand_labels(), frozenset({"SPAM"}))
            self.assertTrue(store.defers_body(["SPAM"], "INBOX"))
            self.assertFalse(store.defers_body(["TRASH"], "INBOX"))
        finally:
            apply(parse(""))
            self.assertEqual(store.on_demand_labels(), frozenset(DEFAULT_ON_DEMAND))

    def test_signature_is_a_path(self) -> None:
        self.assertIsNone(parse("").signature)
        self.assertEqual(parse("signature ~/mail/signature\n").signature, Path.home() / "mail" / "signature")

    def test_signature_may_name_one_account(self) -> None:
        settings = parse(
            "signature ~/mail/default\n"
            "signature Ada@Example.com ~/mail/ada\n"
            "signature bea@example.com ~/mail/bea\n"
            "signature ada@example.com ~/mail/ada2\n"
        )
        self.assertEqual(settings.signature, Path.home() / "mail" / "default")
        self.assertEqual(
            settings.signatures,
            (
                ("Ada@Example.com", Path.home() / "mail" / "ada"),
                ("bea@example.com", Path.home() / "mail" / "bea"),
                ("ada@example.com", Path.home() / "mail" / "ada2"),
            ),
        )
        with self.assertRaises(SettingsError):
            parse("signature\n")

    def test_resolve_signature_is_per_profile_per_account(self) -> None:
        shared = parse(
            "signature /shared/default\n"
            "signature ada@example.com /shared/ada\n"
            "signature bea@example.com /shared/bea\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config"
            config.write_text("signature /shared/default\n", encoding="utf-8")
            self.assertEqual(
                resolve_signature(shared, config, None, "ada@example.com"),
                Path("/shared/ada"),
            )
            self.assertEqual(
                resolve_signature(shared, config, None, "other@example.com"),
                Path("/shared/default"),
            )
            profile = root / "profiles" / "work"
            profile.parent.mkdir()
            profile.write_text(
                "signature /work/default\n"
                "signature ADA@example.com /work/ada\n",
                encoding="utf-8",
            )
            self.assertEqual(
                resolve_signature(shared, config, "work", "ada@example.com"),
                Path("/work/ada"),
            )
            self.assertEqual(
                resolve_signature(shared, config, "work", "bea@example.com"),
                Path("/work/default"),
            )
            self.assertEqual(
                resolve_signature(shared, config, "home", "bea@example.com"),
                Path("/shared/bea"),
            )
            overlay = parse_profile(
                "open firefox\nsignature /p/default\nsignature x@y.z /p/x\n"
            )
            self.assertEqual(overlay.open_command, ("firefox",))
            self.assertEqual(overlay.signature, Path("/p/default"))
            self.assertEqual(overlay.signatures, (("x@y.z", Path("/p/x")),))

    def test_open_command_defaults_to_xdg_open_and_parses_argv(self) -> None:
        self.assertEqual(parse("").open_command, ("xdg-open",))
        self.assertEqual(
            parse("open firefox -P settings\n").open_command,
            ("firefox", "-P", "settings"),
        )
        self.assertEqual(
            parse('open firefox -P "my settings"\n').open_command,
            ("firefox", "-P", "my settings"),
        )
        with self.assertRaises(SettingsError):
            parse("open\n")
        with self.assertRaises(SettingsError):
            parse('open firefox -P "unterminated\n')

    def test_profile_file_overrides_open_for_that_profile(self) -> None:
        shared = parse("open xdg-open\n")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config"
            config.write_text("open xdg-open\n", encoding="utf-8")
            self.assertEqual(resolve_open_command(shared, config, None), ("xdg-open",))
            self.assertEqual(resolve_open_command(shared, config, "work"), ("xdg-open",))
            profile = root / "profiles" / "work"
            profile.parent.mkdir()
            profile.write_text(
                "# work browser\nopen firefox -P settings\n",
                encoding="utf-8",
            )
            self.assertEqual(
                resolve_open_command(shared, config, "work"),
                ("firefox", "-P", "settings"),
            )
            self.assertEqual(resolve_open_command(shared, config, "home"), ("xdg-open",))
            self.assertIsNone(parse_profile_open("# nothing yet\n"))
            with self.assertRaises(SettingsError):
                parse_profile_open("download /tmp\n")

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

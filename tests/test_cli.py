"""Command line reads the local index before it talks to Gmail."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def test_init_and_list_need_no_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "mail"
            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(Path(tmp) / "config")
            init = subprocess.run(
                [sys.executable, "-m", "gmail_tui", "--data", str(data), "init"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(init.returncode, 0, init.stderr)
            self.assertTrue((data / "accounts" / "primary" / "index.sqlite").exists())
            listing = subprocess.run(
                [sys.executable, "-m", "gmail_tui", "--data", str(data), "list"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(listing.returncode, 0, listing.stderr)
            self.assertIn("(no messages)", listing.stdout)
            self.assertFalse((data / "cur").exists())
            env.pop("GMAIL_TUI_CLIENT_ID", None)
            env.pop("GMAIL_TUI_CLIENT_SECRET", None)
            env.pop("GMAIL_TUI_TOKEN", None)
            before = {path.name for path in (data / "accounts").iterdir()} if (data / "accounts").is_dir() else set()
            added = subprocess.run(
                [sys.executable, "-m", "gmail_tui", "--data", str(data), "add-account", "--no-browser"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(added.returncode, 1, added.stderr)
            self.assertIn("client_secret.json", added.stderr)
            after = {path.name for path in (data / "accounts").iterdir()} if (data / "accounts").is_dir() else set()
            self.assertEqual(after, before)

    def test_perf_flag_writes_a_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "mail"
            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(Path(tmp) / "config")
            result = subprocess.run(
                [sys.executable, "-m", "gmail_tui", "--data", str(data), "--perf", "init"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            log = (data / "profile.log").read_text(encoding="utf-8")
            self.assertIn("profile start", log)
            self.assertIn("mem rss=", log)
            self.assertIn("profile stop", log)

    def test_profile_uses_a_subdirectory_of_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "mail-store"
            config_home = root / "config"
            config_home.mkdir()
            (config_home / "gmail-tui").mkdir()
            (config_home / "gmail-tui" / "config").write_text(
                f"data {data}\ndownload {root / 'saved'}\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(config_home)
            env.pop("GMAIL_TUI_PROFILE", None)
            result = subprocess.run(
                [sys.executable, "-m", "gmail_tui", "--profile", "work", "init"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((data / "work" / "accounts" / "primary" / "index.sqlite").exists())
            self.assertFalse((data / "accounts").exists())

    def test_config_data_is_used_without_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "mail-store"
            config_home = root / "config"
            config_home.mkdir()
            (config_home / "gmail-tui").mkdir()
            (config_home / "gmail-tui" / "config").write_text(
                f"data {data}\ndownload {root / 'saved'}\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(config_home)
            init = subprocess.run(
                [sys.executable, "-m", "gmail_tui", "init"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(init.returncode, 0, init.stderr)
            self.assertTrue((data / "accounts" / "primary" / "index.sqlite").exists())

    def test_config_flag_overrides_xdg_location(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "portable-mail"
            cfg_dir = root / "portable-cfg"
            cfg_dir.mkdir()
            (cfg_dir / "config").write_text(
                f"data {data}\ndownload {root / 'saved'}\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(root / "unused-xdg")
            env.pop("GMAIL_TUI_CONFIG", None)
            init = subprocess.run(
                [sys.executable, "-m", "gmail_tui", "--config", str(cfg_dir), "init"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(init.returncode, 0, init.stderr)
            self.assertTrue((data / "accounts" / "primary" / "index.sqlite").exists())
            self.assertFalse((root / "unused-xdg").exists())

    def test_xdg_data_home_is_used_without_config_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            share = root / "share"
            config_home = root / "config"
            config_home.mkdir()
            (config_home / "gmail-tui").mkdir()
            (config_home / "gmail-tui" / "config").write_text(
                f"download {root / 'saved'}\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(config_home)
            env["XDG_DATA_HOME"] = str(share)
            # Keep the package importable when cwd is not the source tree.
            project = Path(__file__).resolve().parents[1]
            env["PYTHONPATH"] = os.pathsep.join(
                [str(project), env["PYTHONPATH"]] if env.get("PYTHONPATH") else [str(project)]
            )
            # Run from an empty cwd so a relative gmail-tui-data default would fail.
            cwd = root / "elsewhere"
            cwd.mkdir()
            init = subprocess.run(
                [sys.executable, "-m", "gmail_tui", "init"],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                cwd=cwd,
            )
            self.assertEqual(init.returncode, 0, init.stderr)
            self.assertTrue((share / "gmail-tui" / "accounts" / "primary" / "index.sqlite").exists())
            self.assertFalse((cwd / "gmail-tui-data").exists())


if __name__ == "__main__":
    unittest.main()

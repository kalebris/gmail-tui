"""Open the message editor in a separate terminal window.

The TUI stays up. A small wrapper runs the editor, then writes the editor's
exit status to a sentinel file the curses loop can poll.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

# Prefix plus the editor argv. Terminals that take the command as trailing
# arguments come first. ``-e`` / ``--`` forms follow.
_CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("kitty", ("kitty",)),
    ("foot", ("foot",)),
    ("ghostty", ("ghostty", "-e")),
    ("alacritty", ("alacritty", "-e")),
    ("wezterm", ("wezterm", "start", "--")),
    ("gnome-terminal", ("gnome-terminal", "--wait", "--")),
    ("konsole", ("konsole", "--nofork", "-e")),
    ("xfce4-terminal", ("xfce4-terminal", "--disable-server", "-e")),
    ("xterm", ("xterm", "-e")),
)

_WRAPPER = (
    "import pathlib, subprocess, sys\n"
    "sentinel = pathlib.Path(sys.argv[1])\n"
    "status = subprocess.call(sys.argv[2:])\n"
    "sentinel.write_text(str(status), encoding='ascii')\n"
    "raise SystemExit(status)\n"
)


def terminal_prefix() -> list[str] | None:
    """Command that starts a new terminal and runs the rest of the argv."""
    custom = os.environ.get("GMAIL_TUI_TERMINAL", "").strip()
    if custom:
        parts = shlex.split(custom)
        return parts or None
    for name, argv in _CANDIDATES:
        if shutil.which(name):
            return list(argv)
    chosen = os.environ.get("TERMINAL", "").strip()
    if not chosen:
        return None
    parts = shlex.split(chosen)
    if not parts:
        return None
    return [*parts, "-e"]


def editor_argv(path: Path, sentinel: Path, editor: list[str]) -> list[str]:
    prefix = terminal_prefix()
    if not prefix:
        raise OSError("could not open a terminal")
    inner = [sys.executable, "-c", _WRAPPER, str(sentinel), *editor, str(path)]
    return [*prefix, *inner]


def launch_editor(path: Path, sentinel: Path, editor: list[str]) -> subprocess.Popen:
    """Start the editor. Returns once the window process has been spawned."""
    argv = editor_argv(path, sentinel, editor)
    return subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

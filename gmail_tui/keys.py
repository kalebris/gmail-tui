"""Keyboard commands loaded from the user's config file.

The file is created on first use at ``$XDG_CONFIG_HOME/gmail-tui/keys`` (or
``~/.config/gmail-tui/keys``), next to the other gmail-tui settings. Actions that the
file does not mention keep their defaults. Keys are case-sensitive.
"""

from __future__ import annotations

import curses
import os
from pathlib import Path

DEFAULT_BINDINGS: dict[str, tuple[str, ...]] = {
    "quit": ("q", "Q"),
    "sync": ("s",),
    "sync-all": ("S",),
    "cache": ("B",),
    "snooze": ("b",),
    "down": ("j", "down"),
    "up": ("k", "up"),
    "page-down": ("J", "space", "pagedown"),
    "page-up": ("K", "pageup"),
    "top": ("g", "home"),
    "bottom": ("G", "end"),
    "inbox": ("g-i",),
    "left": ("h", "left"),
    "right": ("right",),
    "open": ("z-o",),
    "close": ("z-c",),
    "toggle-fold": ("z-a",),
    "activate": ("enter",),
    "next-pane": ("tab",),
    "prev-pane": ("backtab",),
    "search": ("/",),
    "add-label": ("l",),
    "archive": ("a", "e"),
    "delete": ("d",),
    "select": ("x",),
    "order": ("o",),
    "toggle-unread": ("u",),
    "next-unread": ("n",),
    "compose": ("c",),
    "calendar": ("C",),
    "reply-all": ("r",),
    "reply": ("R",),
    "holding": ("H",),
    "attachments": ("A",),
    "mime-parts": ("m",),
    "view-image": ("v",),
    "raw-source": ("V",),
    "headers": ("F",),
    "urls": ("L",),
    "undo": ("U",),
    "next-account": ("]",),
    "prev-account": ("[",),
    "help": ("?",),
}

# Combined with a key code so Alt+letter survives as one value.
ALT = 0x100000

_SPECIAL = {
    "tab": (ord("\t"),),
    "enter": (10, 13, curses.KEY_ENTER),
    "space": (ord(" "),),
    "backtab": (curses.KEY_BTAB,),
    "up": (curses.KEY_UP,),
    "down": (curses.KEY_DOWN,),
    "left": (curses.KEY_LEFT,),
    "right": (curses.KEY_RIGHT,),
    "home": (curses.KEY_HOME,),
    "end": (curses.KEY_END,),
    "pageup": (curses.KEY_PPAGE,),
    "pagedown": (curses.KEY_NPAGE,),
}


class KeysError(Exception):
    pass


class KeyMap:
    def __init__(self, bindings: dict[str, tuple[str, ...]]) -> None:
        codes: dict[int, str] = {}
        owners: dict[int, str] = {}
        sequences: dict[tuple[int, int], str] = {}
        for action, names in bindings.items():
            for name in names:
                for chord in _chords(name):
                    if len(chord) == 1:
                        code = chord[0]
                        previous = owners.get(code)
                        if previous is not None and previous != action:
                            raise KeysError(f"{name} is bound to both {previous} and {action}")
                        owners[code] = action
                        codes[code] = action
                        continue
                    previous = sequences.get(chord)
                    if previous is not None and previous != action:
                        raise KeysError(f"{name} is bound to both {previous} and {action}")
                    sequences[chord] = action
        self.bindings = bindings
        self._codes = codes
        self._sequences = sequences

    def action(self, code: int) -> str | None:
        return self._codes.get(code)

    def has_prefix(self, code: int) -> bool:
        return any(first == code for first, _second in self._sequences)

    def sequence(self, first: int, second: int) -> str | None:
        return self._sequences.get((first, second))


def config_path() -> Path:
    """Keys file path: ``GMAIL_TUI_KEYS`` if set, else ``$XDG_CONFIG_HOME/gmail-tui/keys``."""
    env = os.environ.get("GMAIL_TUI_KEYS")
    if env:
        return Path(env).expanduser()
    from gmail_tui.settings import config_dir

    return config_dir() / "keys"


def default_text(path: Path | None = None) -> str:
    where = str(path) if path is not None else "~/.config/gmail-tui/keys"
    lines = [
        f"# {where}",
        "# gmail-tui reads this file when the terminal UI starts.",
        "# One action per line: action, then the keys that run it.",
        "# Actions omitted here keep the defaults. Keys are case-sensitive.",
        "# Special keys: tab enter space backtab up down left right home end pageup pagedown",
        "# Alt keys: alt-l, alt-L, and the same form for any other letter.",
        "# Two-key sequences use a hyphen: g-i means press g, then i.",
        "# Example: down j down",
        "",
    ]
    for action, names in DEFAULT_BINDINGS.items():
        lines.append(f"{action} {' '.join(names)}")
    lines.append("")
    return "\n".join(lines)


def load(path: Path | None = None) -> KeyMap:
    chosen = path or config_path()
    if not chosen.exists():
        # Nix-managed paths are read-only / must not be invented on first run.
        if _managed_keys_path(chosen):
            raise KeysError(f"keys file not found: {chosen}")
        chosen.parent.mkdir(parents=True, exist_ok=True)
        chosen.write_text(default_text(chosen), encoding="utf-8")
    return parse(chosen.read_text(encoding="utf-8"))


def _managed_keys_path(path: Path) -> bool:
    if str(path).startswith("/nix/store/"):
        return True
    env = os.environ.get("GMAIL_TUI_KEYS")
    if not env:
        return False
    return path.expanduser() == Path(env).expanduser()


def parse(text: str) -> KeyMap:
    overrides: dict[str, tuple[str, ...]] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        action = parts[0]
        names = tuple(parts[1:])
        if action not in DEFAULT_BINDINGS:
            raise KeysError(f"line {lineno}: unknown action {action}")
        if not names:
            raise KeysError(f"line {lineno}: {action} has no keys")
        for name in names:
            _chords(name, lineno)
        overrides[action] = names
    bindings = dict(DEFAULT_BINDINGS)
    bindings.update(overrides)
    taken = {name for names in overrides.values() for name in names}
    for action, names in bindings.items():
        if action in overrides:
            continue
        bindings[action] = tuple(name for name in names if name not in taken)
    return KeyMap(bindings)


def defaults() -> KeyMap:
    return KeyMap(dict(DEFAULT_BINDINGS))


def _chords(name: str, lineno: int | None = None) -> tuple[tuple[int, ...], ...]:
    if "-" in name and not name.startswith("alt-"):
        parts = name.split("-")
        if len(parts) != 2 or not all(parts):
            raise KeysError(_unknown_key(name, lineno))
        first = _codes(parts[0], lineno)
        second = _codes(parts[1], lineno)
        return tuple((left, right) for left in first for right in second)
    return tuple((code,) for code in _codes(name, lineno))


def _codes(name: str, lineno: int | None = None) -> tuple[int, ...]:
    if name.startswith("alt-") and len(name) > 4:
        base = name[4:]
        if base.startswith("alt-"):
            raise KeysError(_unknown_key(name, lineno))
        return tuple(code | ALT for code in _codes(base, lineno))
    special = _SPECIAL.get(name)
    if special is not None:
        return special
    if len(name) == 1:
        return (ord(name),)
    raise KeysError(_unknown_key(name, lineno))


def _unknown_key(name: str, lineno: int | None) -> str:
    where = f"line {lineno}: " if lineno is not None else ""
    known = " ".join(sorted(_SPECIAL))
    return f"{where}unknown key {name} (special keys: {known}; alt-letter for Alt)"

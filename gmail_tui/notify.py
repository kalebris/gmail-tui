"""Desktop notifications for new mail (libnotify via ``notify-send``)."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping


def new_mail(counts: Mapping[str, int]) -> bool:
    """Notify which accounts gained inbox messages. Returns True if sent."""
    lines = [
        f"{name}: {_plural(n)}"
        for name, n in sorted(counts.items())
        if n > 0
    ]
    if not lines:
        return False
    total = sum(n for n in counts.values() if n > 0)
    title = "New mail" if total == 1 else f"New mail ({total})"
    return send(title, "\n".join(lines))


def send(title: str, body: str) -> bool:
    """Fire a libnotify notification. No-op when ``notify-send`` is missing."""
    binary = shutil.which("notify-send")
    if not binary:
        return False
    try:
        subprocess.run(
            [binary, "--app-name=gmail-tui", "--category=email.arrived", title, body],
            check=False,
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _plural(n: int) -> str:
    return "1 new message" if n == 1 else f"{n} new messages"

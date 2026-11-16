"""Snooze presets and custom duration parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

_DURATION = re.compile(r"^\s*(\d+)\s*([mMhHdD])?\s*$")


@dataclass(frozen=True)
class SnoozeChoice:
    key: str
    label: str
    until: datetime


def parse_duration(text: str, *, now: datetime | None = None) -> datetime | None:
    """Parse ``30m``, ``2h``, ``3d``, or a bare number of minutes."""
    match = _DURATION.match(text or "")
    if not match:
        return None
    amount = int(match.group(1))
    unit = (match.group(2) or "m").lower()
    base = now or datetime.now().astimezone()
    if unit == "m":
        return base + timedelta(minutes=amount)
    if unit == "h":
        return base + timedelta(hours=amount)
    return base + timedelta(days=amount)


def presets(now: datetime | None = None) -> list[SnoozeChoice]:
    """Common wake times. j/k walks this list; a typed duration overrides it."""
    base = now or datetime.now().astimezone()
    later = base + timedelta(hours=3)
    tonight = base.replace(hour=18, minute=0, second=0, microsecond=0)
    if tonight <= base:
        tonight += timedelta(days=1)
    tomorrow_morning = (base + timedelta(days=1)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    tomorrow_afternoon = (base + timedelta(days=1)).replace(
        hour=13, minute=0, second=0, microsecond=0
    )
    days_until_monday = (7 - base.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7
    monday = (base + timedelta(days=days_until_monday)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    next_week = base + timedelta(days=7)
    return [
        SnoozeChoice("later", f"Later today ({_stamp(later)})", later),
        SnoozeChoice("tonight", f"Tonight ({_stamp(tonight)})", tonight),
        SnoozeChoice(
            "tomorrow_morning",
            f"Tomorrow morning ({_stamp(tomorrow_morning)})",
            tomorrow_morning,
        ),
        SnoozeChoice(
            "tomorrow_afternoon",
            f"Tomorrow afternoon ({_stamp(tomorrow_afternoon)})",
            tomorrow_afternoon,
        ),
        SnoozeChoice("monday", f"Next Monday ({_stamp(monday)})", monday),
        SnoozeChoice("next_week", f"Next week ({_stamp(next_week)})", next_week),
    ]


def _stamp(when: datetime) -> str:
    hour = when.strftime("%I").lstrip("0") or "12"
    return f"{when.strftime('%a')} {hour}:{when.strftime('%M%p')}".lower()

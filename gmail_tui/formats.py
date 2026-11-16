"""Display date/time formats from the user config.

Call ``configure`` once after settings load. Helpers below are used by mail,
calendar, and the CLI so stamps stay consistent.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

_DATE = "%Y-%m-%d"
_TIME = "%H:%M"
_DATETIME = "%Y-%m-%d %H:%M"
_WEEKDAY = "%a %d"


def configure(
    *,
    date_fmt: str | None = None,
    time_fmt: str | None = None,
    datetime_fmt: str | None = None,
    weekday_fmt: str | None = None,
) -> None:
    global _DATE, _TIME, _DATETIME, _WEEKDAY
    if date_fmt:
        _DATE = date_fmt
    if time_fmt:
        _TIME = time_fmt
    if weekday_fmt:
        _WEEKDAY = weekday_fmt
    if datetime_fmt:
        _DATETIME = datetime_fmt
    elif date_fmt or time_fmt:
        _DATETIME = f"{_DATE} {_TIME}"


def date_format() -> str:
    return _DATE


def time_format() -> str:
    return _TIME


def datetime_format() -> str:
    return _DATETIME


def weekday_format() -> str:
    return _WEEKDAY


def format_date(value: date | datetime) -> str:
    if isinstance(value, datetime):
        value = _local(value).date()
    return value.strftime(_DATE)


def format_time(value: datetime) -> str:
    return _local(value).strftime(_TIME)


def format_datetime(value: datetime) -> str:
    return _local(value).strftime(_DATETIME)


def format_weekday(value: date | datetime) -> str:
    if isinstance(value, datetime):
        value = _local(value).date()
    return value.strftime(_WEEKDAY)


def format_month_year(value: date | datetime) -> str:
    if isinstance(value, datetime):
        value = _local(value).date()
    # Month names stay locale-aware; day/year layout follows the date format
    # when it includes those fields, otherwise fall back to "Month YYYY".
    if "%B" in _DATE or "%b" in _DATE:
        return value.strftime(_DATE)
    return value.strftime("%B %Y")


def format_millis(millis: int) -> str:
    if millis <= 0:
        return "-"
    moment = datetime.fromtimestamp(millis / 1000, timezone.utc)
    return format_datetime(moment)


def parse_datetime(text: str) -> datetime:
    """Parse a stamp written with the active formats (ISO still accepted)."""
    text = text.strip()
    tz = datetime.now().astimezone().tzinfo or timezone.utc
    for fmt in (_DATETIME, _DATE, "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=tz)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"unrecognized date/time: {text}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed


def parse_date(text: str) -> date:
    text = text.strip()
    for fmt in (_DATE, "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return date.fromisoformat(text[:10])


def _local(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone()

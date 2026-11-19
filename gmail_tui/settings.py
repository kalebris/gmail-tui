"""User settings under the XDG config directory.

Config files live in ``$XDG_CONFIG_HOME/gmail-tui/`` (``~/.config/gmail-tui/``),
or whatever ``--config`` / ``GMAIL_TUI_CONFIG`` points at. The mail index defaults
to ``$XDG_DATA_HOME/gmail-tui/`` (``~/.local/share/gmail-tui/``) so an installed
``gmail-tui`` on ``PATH`` does not depend on the current working directory.
``download`` is where saved attachments and xdg-open copies go.
``account`` lines, one address each, set the left-to-right order of the top bar.
``date`` / ``time`` / ``datetime`` / ``weekday`` are strftime patterns for stamps.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from gmail_tui import formats


class SettingsError(ValueError):
    pass


# Bodies for these labels wait until the folder is open (or a message is).
DEFAULT_ON_DEMAND = ("TRASH", "SPAM")


@dataclass(frozen=True)
class Settings:
    data: Path | None = None
    download: Path | None = None
    accounts: tuple[str, ...] = ()
    date: str = "%Y-%m-%d"
    time: str = "%H:%M"
    datetime: str = "%Y-%m-%d %H:%M"
    weekday: str = "%a %d"
    # Seal oauth.json with age-plugin-tpm; startup asks for the TPM PIN.
    oauth_tpm: bool = False
    oauth_tpm_identity: Path | None = None
    # Gmail label ids whose bodies are not background-cached.
    on_demand: tuple[str, ...] = DEFAULT_ON_DEMAND


def xdg_config_home() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return Path(base) if base else Path.home() / ".config"


def xdg_data_home() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    return Path(base) if base else Path.home() / ".local" / "share"


def config_dir() -> Path:
    return xdg_config_home() / "gmail-tui"


def config_path() -> Path:
    return config_dir() / "config"


def resolve_config_path(explicit: Path | str | None = None) -> Path:
    """Config file from ``explicit``, ``GMAIL_TUI_CONFIG``, or the XDG default.

    A directory path means ``<dir>/config`` so one flag can point at a portable
    config folder. Relative paths are resolved from the current working directory.
    """
    raw = explicit if explicit is not None else os.environ.get("GMAIL_TUI_CONFIG")
    if raw is None or raw == "":
        return config_path()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if path.is_dir():
        return path / "config"
    return path


def default_data() -> Path:
    """Absolute mail store used when neither ``--data`` nor config set it."""
    return xdg_data_home() / "gmail-tui"


def normalize_profile(name: str | None) -> str | None:
    """Validate a profile directory name. Empty means the base data directory."""
    if name is None:
        return None
    chosen = name.strip()
    if not chosen:
        return None
    if chosen in {".", ".."} or "/" in chosen or "\\" in chosen:
        raise SettingsError(
            f"profile {name!r} must be a single directory name (no slashes)"
        )
    return chosen


def resolve_data_dir(
    base: Path,
    profile: str | None = None,
) -> Path:
    """Mail store root. With a profile, use ``<base>/<profile>/``."""
    name = normalize_profile(profile)
    if name is None:
        return base
    return base / name


def default_download() -> Path:
    folder = Path.home() / "Downloads"
    if folder.is_dir():
        return folder
    return Path.home()


def default_text(path: Path | None = None) -> str:
    where = str(path) if path is not None else str(Path.home() / ".config" / "gmail-tui" / "config")
    download = default_download()
    data = default_data()
    return (
        f"# {where}\n"
        "# gmail-tui reads this file when it starts, from any working directory.\n"
        "# download   directory for saved attachments (tab-complete, xdg-open)\n"
        "# data       mail index and body cache when --data is omitted\n"
        "#            gmail-tui --profile NAME uses <data>/NAME/ for that run;\n"
        "#            keys and this config file stay shared across profiles.\n"
        "# account    one address per line, left to right on the top bar.\n"
        "#            Any mailbox omitted here still appears, after these.\n"
        "# date       strftime for calendar days / all-day stamps (default %Y-%m-%d)\n"
        "# time       strftime for hour labels (default %H:%M)\n"
        "# datetime   strftime for mail and event stamps (default date + time)\n"
        "# weekday    strftime for week columns (default %a %d)\n"
        "# oauth-tpm  yes → encrypt each account's oauth.json with a TPM age key\n"
        "#            (needs age + age-plugin-tpm on PATH; asks for the TPM PIN)\n"
        "# oauth-tpm-identity  optional path to that age-plugin-tpm identity file\n"
        "# on-demand  label whose bodies wait until that folder is open\n"
        "#            (default: TRASH and SPAM). One label per line.\n"
        "#            on-demand none  → background-fetch every folder\n"
        "#\n"
        f"download {download}\n"
        f"data {data}\n"
        "date %Y-%m-%d\n"
        "time %H:%M\n"
        "datetime %Y-%m-%d %H:%M\n"
        "weekday %a %d\n"
        "# oauth-tpm no\n"
        "# on-demand TRASH\n"
        "# on-demand SPAM\n"
    )


def load(path: Path | None = None) -> Settings:
    chosen = path or config_path()
    if not chosen.exists():
        chosen.parent.mkdir(parents=True, exist_ok=True)
        chosen.write_text(default_text(chosen), encoding="utf-8")
    settings = parse(chosen.read_text(encoding="utf-8"))
    apply(settings)
    return settings


def apply(settings: Settings) -> None:
    """Push date/time patterns and on-demand folders into shared helpers."""
    formats.configure(
        date_fmt=settings.date,
        time_fmt=settings.time,
        datetime_fmt=settings.datetime,
        weekday_fmt=settings.weekday,
    )
    from gmail_tui import store

    store.configure_on_demand(settings.on_demand)


def parse(text: str) -> Settings:
    data: Path | None = None
    download: Path | None = None
    accounts: list[str] = []
    date_fmt = "%Y-%m-%d"
    time_fmt = "%H:%M"
    datetime_fmt: str | None = None
    weekday_fmt = "%a %d"
    seen_datetime = False
    oauth_tpm = False
    oauth_tpm_identity: Path | None = None
    on_demand: list[str] | None = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            raise SettingsError(f"line {lineno}: expected 'name value'")
        name, value = parts[0], parts[1].strip()
        if name == "download":
            download = _path(value)
        elif name == "data":
            data = _path(value)
        elif name == "account":
            if any(ch.isspace() for ch in value):
                raise SettingsError(f"line {lineno}: account takes one address")
            accounts.append(value)
        elif name == "date":
            date_fmt = _strftime(value, lineno)
        elif name == "time":
            time_fmt = _strftime(value, lineno)
        elif name == "datetime":
            datetime_fmt = _strftime(value, lineno)
            seen_datetime = True
        elif name == "weekday":
            weekday_fmt = _strftime(value, lineno)
        elif name == "oauth-tpm":
            oauth_tpm = _bool(value, lineno)
        elif name == "oauth-tpm-identity":
            oauth_tpm_identity = _path(value)
        elif name == "on-demand":
            if on_demand is None:
                on_demand = []
            key = value.casefold()
            if key in {"none", "off", "-"}:
                on_demand = []
                continue
            if any(ch.isspace() for ch in value):
                raise SettingsError(f"line {lineno}: on-demand takes one label")
            on_demand.append(_on_demand_label(value, lineno))
        else:
            raise SettingsError(f"line {lineno}: unknown setting {name}")
    if not seen_datetime or datetime_fmt is None:
        datetime_fmt = f"{date_fmt} {time_fmt}"
    return Settings(
        data=data,
        download=download,
        accounts=tuple(accounts),
        date=date_fmt,
        time=time_fmt,
        datetime=datetime_fmt,
        weekday=weekday_fmt,
        oauth_tpm=oauth_tpm,
        oauth_tpm_identity=oauth_tpm_identity,
        on_demand=tuple(on_demand) if on_demand is not None else DEFAULT_ON_DEMAND,
    )


def _bool(value: str, lineno: int) -> bool:
    key = value.strip().casefold()
    if key in {"yes", "true", "on", "1"}:
        return True
    if key in {"no", "false", "off", "0"}:
        return False
    raise SettingsError(f"line {lineno}: expected yes/no for boolean, got {value!r}")


def _strftime(value: str, lineno: int) -> str:
    if not value:
        raise SettingsError(f"line {lineno}: empty format")
    from datetime import date as date_cls
    from datetime import datetime as datetime_cls

    try:
        date_cls.today().strftime(value)
        datetime_cls.now().strftime(value)
    except ValueError as exc:
        raise SettingsError(f"line {lineno}: invalid strftime pattern: {value}") from exc
    return value


def _path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


_ON_DEMAND_ALIASES = {
    "trash": "TRASH",
    "spam": "SPAM",
    "bin": "TRASH",
    "junk": "SPAM",
    "deleted": "TRASH",
}


def _on_demand_label(value: str, lineno: int) -> str:
    token = value.strip()
    mapped = _ON_DEMAND_ALIASES.get(token.casefold(), token)
    if not mapped or any(ch.isspace() for ch in mapped):
        raise SettingsError(f"line {lineno}: on-demand needs a Gmail label id")
    # System ids are TRASH/SPAM; user labels look like Label_12.
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
    if any(ch not in allowed for ch in mapped):
        raise SettingsError(f"line {lineno}: invalid on-demand label {value!r}")
    return mapped

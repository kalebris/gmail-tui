"""Calendar mode: account→calendar tree on the left, day/week/month on the right."""

from __future__ import annotations

import calendar as pycal
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from gmail_tui.accounts import Account
from gmail_tui import formats
from gmail_tui.gmail import Offline
from gmail_tui.http_calendar import (
    CalEvent,
    CalendarError,
    CalendarInfo,
    EventPatch,
    HttpCalendar,
)
from gmail_tui.oauth import OAuthError, current_access_token
from gmail_tui.terminal import launch_editor
from gmail_tui.ui import _ExternalEdit, _clip, _display_width, _pad, editor_command


Layout = str  # "day" | "week" | "month"

# Keep about a year on each side of the cursor so week/month shifts stay local.
_CACHE_YEARS = 1
# When the view gets this close to a cache edge, recenter in the background.
_CACHE_EDGE = timedelta(days=60)


@dataclass
class CalNode:
    key: str
    label: str
    depth: int
    account: str = ""
    calendar_id: str = ""
    expandable: bool = False
    expanded: bool = False
    enabled: bool = False
    prefix: str = ""


@dataclass
class CalendarDesk:
    """Shared calendar view across every signed-in account."""

    accounts: list[Account]
    layout: Layout = "week"
    focus: str = "cals"  # cals | view
    cursor: date = field(default_factory=lambda: date.today())
    cal_cursor: int = 0
    cal_scroll: int = 0
    event_cursor: int = 0
    event_scroll: int = 0
    hour_scroll: int = -1  # first hour in day/week; -1 = around "now"
    notice: str = ""
    prompt: str | None = None
    help_open: bool = False
    external: _ExternalEdit | None = None
    _edit_event: CalEvent | None = None
    _create_account: str = ""
    _create_calendar: str = ""
    expanded: set[str] = field(default_factory=set)
    enabled: set[str] = field(default_factory=set)
    calendars: dict[str, list[CalendarInfo]] = field(default_factory=dict)
    events: list[CalEvent] = field(default_factory=list)
    visible: list[CalNode] = field(default_factory=list)
    clients: dict[str, HttpCalendar] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    _cache: list[CalEvent] = field(default_factory=list, repr=False)
    _cache_start: datetime | None = field(default=None, repr=False)
    _cache_stop: datetime | None = field(default=None, repr=False)
    _cache_keys: frozenset[str] = field(default_factory=frozenset, repr=False)
    _cache_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _cache_dirty: bool = field(default=False, repr=False)
    _cache_thread: threading.Thread | None = field(default=None, repr=False)
    _cache_pending: bool = field(default=False, repr=False)
    _lists_pending: bool = field(default=False, repr=False)
    _open_thread: threading.Thread | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for account in self.accounts:
            self.expanded.add(f"acct:{account.name}")
        # Do not hit the Calendar API here. Mail startup used to block on a
        # full calendar list+events fetch for every account before first paint.
        self._rebuild_tree()

    def present(self) -> None:
        """Show the calendar at once. Network fills the week, then the year cache."""
        view_start, view_stop = self.range_bounds()
        keys = frozenset(self.enabled)
        with self._cache_lock:
            covered = bool(self.calendars) and self._cache_covers_locked(view_start, view_stop, keys)
            if covered:
                self._apply_cache_locked(view_start, view_stop)
                near = self._near_cache_edge_locked(view_start, view_stop)
            else:
                near = None
        if covered:
            if self.notice in {"loading calendars", "loading events"}:
                self.notice = ""
            if near and not self.cache_busy():
                self._spawn_cache_refresh(recenter=True)
            return
        if self._open_alive() or (
            self._cache_thread is not None and self._cache_thread.is_alive()
        ):
            if not self.notice:
                self.notice = "loading events" if self.calendars else "loading calendars"
            return
        self.notice = "loading calendars" if not self.calendars else "loading events"
        self._start_open()

    def _open_alive(self) -> bool:
        return self._open_thread is not None and self._open_thread.is_alive()

    def _start_open(self) -> None:
        if self._open_alive():
            return

        def work() -> None:
            try:
                self._open_work()
            except Exception:
                with self._cache_lock:
                    self._cache_dirty = True
            finally:
                with self._cache_lock:
                    self._open_thread = None

        self._open_thread = threading.Thread(
            target=work, name="gmail-tui-cal-open", daemon=True
        )
        self._open_thread.start()

    def _open_work(self) -> None:
        try:
            if not self.calendars:
                self._pull_lists()
                with self._cache_lock:
                    self._lists_pending = True
            view_start, view_stop = self.range_bounds()
            collected = self._fetch_events(view_start, view_stop)
            keys = frozenset(self.enabled)
            with self._cache_lock:
                # A wider cache that already covers this week stays; the year
                # refresh below replaces a week-sized one.
                if not self._cache_covers_locked(view_start, view_stop, keys):
                    self._cache = collected
                    self._cache_start = view_start
                    self._cache_stop = view_stop
                    self._cache_keys = keys
                self._cache_dirty = True
        finally:
            self._spawn_cache_refresh(recenter=True)

    def _pull_lists(self) -> None:
        accounts = list(self.accounts)

        def one(account: Account) -> tuple[str, list[CalendarInfo], str]:
            try:
                found = self._client(account).list_calendars()
            except (CalendarError, Offline, OAuthError, ValueError, OSError) as exc:
                return account.name, [], str(exc) or "calendar unavailable"
            return account.name, found, ""

        if len(accounts) <= 1:
            results = [one(account) for account in accounts]
        else:
            workers = min(8, len(accounts))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(one, accounts))
        calendars: dict[str, list[CalendarInfo]] = {}
        errors: dict[str, str] = {}
        enable: set[str] = set()
        for name, found, err in results:
            calendars[name] = found
            if err:
                errors[name] = err
            for cal in found:
                if cal.selected or cal.primary:
                    enable.add(_cal_key(name, cal.id))
        with self._cache_lock:
            self.calendars = calendars
            self.errors = errors
            self.enabled.update(enable)

    def reload_events(self, *, force: bool = False) -> None:
        """Fill the current view from cache when possible; otherwise fetch ±1 year."""
        view_start, view_stop = self.range_bounds()
        keys = frozenset(self.enabled)
        with self._cache_lock:
            cached = (
                not force
                and self._cache_covers_locked(view_start, view_stop, keys)
            )
            if cached:
                self._apply_cache_locked(view_start, view_stop)
                near_edge = self._near_cache_edge_locked(view_start, view_stop)
            else:
                near_edge = False
        if cached:
            if near_edge:
                self._spawn_cache_refresh(recenter=True)
            return
        # The open/year fetch is already in flight. Keep the screen moving.
        if not force and self.cache_busy():
            with self._cache_lock:
                self._apply_cache_locked(view_start, view_stop)
            self._cache_pending = True
            return
        win_start, win_stop = self._cache_window()
        collected = self._fetch_events(win_start, win_stop)
        with self._cache_lock:
            self._cache = collected
            self._cache_start = win_start
            self._cache_stop = win_stop
            self._cache_keys = keys
            self._apply_cache_locked(*self.range_bounds())

    def poll_cache_update(self) -> bool:
        """True when a background load changed the tree or the visible events."""
        with self._cache_lock:
            lists = self._lists_pending
            self._lists_pending = False
            dirty = self._cache_dirty
            self._cache_dirty = False
            if dirty:
                self._apply_cache_locked(*self.range_bounds())
        if not lists and not dirty:
            return False
        if lists:
            self._rebuild_tree()
        if dirty or not self.cache_busy():
            if self.notice in {"loading calendars", "loading events"}:
                self.notice = ""
        elif lists:
            self.notice = "loading events"
        return True

    def cache_busy(self) -> bool:
        return (
            self._cache_pending
            or self._open_alive()
            or (self._cache_thread is not None and self._cache_thread.is_alive())
        )

    def _fetch_events(self, start: datetime, stop: datetime) -> list[CalEvent]:
        grouped: list[tuple[Account, list[CalendarInfo]]] = []
        for account in self.accounts:
            chosen = [
                cal
                for cal in self.calendars.get(account.name, [])
                if _cal_key(account.name, cal.id) in self.enabled
            ]
            if chosen:
                grouped.append((account, chosen))

        def fetch_account(item: tuple[Account, list[CalendarInfo]]) -> list[CalEvent]:
            account, cals = item
            found: list[CalEvent] = []
            for cal in cals:
                try:
                    rows = self._client(account).list_events(cal.id, start, stop)
                except (CalendarError, Offline, OAuthError, ValueError, OSError) as exc:
                    with self._cache_lock:
                        self.errors[account.name] = str(exc) or "events unavailable"
                    continue
                for row in rows:
                    found.append(
                        replace(
                            row,
                            account=account.name,
                            calendar_name=cal.summary,
                        )
                    )
            return found

        if len(grouped) <= 1:
            parts = [fetch_account(item) for item in grouped]
        else:
            workers = min(8, len(grouped))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                parts = list(pool.map(fetch_account, grouped))
        collected = [event for part in parts for event in part]
        collected.sort(key=lambda item: (item.start, item.summary.casefold()))
        return collected

    def _cache_window(self) -> tuple[datetime, datetime]:
        tz = datetime.now().astimezone().tzinfo or timezone.utc
        day = self.cursor
        start_day = _shift_years(day, -_CACHE_YEARS)
        stop_day = _shift_years(day, _CACHE_YEARS)
        start = datetime(start_day.year, start_day.month, start_day.day, tzinfo=tz)
        stop = datetime(stop_day.year, stop_day.month, stop_day.day, tzinfo=tz)
        return start, stop

    def _cache_covers_locked(
        self,
        view_start: datetime,
        view_stop: datetime,
        keys: frozenset[str],
    ) -> bool:
        if self._cache_start is None or self._cache_stop is None:
            return False
        if keys != self._cache_keys:
            return False
        return self._cache_start <= view_start and view_stop <= self._cache_stop

    def _near_cache_edge_locked(self, view_start: datetime, view_stop: datetime) -> bool:
        if self._cache_start is None or self._cache_stop is None:
            return True
        return (
            view_start - self._cache_start <= _CACHE_EDGE
            or self._cache_stop - view_stop <= _CACHE_EDGE
        )

    def _apply_cache_locked(self, view_start: datetime, view_stop: datetime) -> None:
        self.events = [
            item
            for item in self._cache
            if item.start < view_stop and item.end > view_start
        ]
        if self.events:
            self.event_cursor = min(self.event_cursor, len(self.events) - 1)
        else:
            self.event_cursor = 0
        self.event_scroll = min(self.event_scroll, self.event_cursor)
        self._reveal_selected_hour()

    def _spawn_cache_refresh(self, *, recenter: bool) -> None:
        if self._cache_thread is not None and self._cache_thread.is_alive():
            self._cache_pending = True
            return
        keys = frozenset(self.enabled)
        if recenter:
            win_start, win_stop = self._cache_window()
        else:
            with self._cache_lock:
                if self._cache_start is None or self._cache_stop is None:
                    win_start, win_stop = self._cache_window()
                else:
                    win_start, win_stop = self._cache_start, self._cache_stop

        def work() -> None:
            try:
                collected = self._fetch_events(win_start, win_stop)
            except Exception:
                collected = None
            with self._cache_lock:
                if collected is not None:
                    self._cache = collected
                    self._cache_start = win_start
                    self._cache_stop = win_stop
                    self._cache_keys = keys
                    # The UI thread applies this on the next poll, so a paint
                    # never reads a half-updated event list.
                    self._cache_dirty = True
                again = self._cache_pending
                self._cache_pending = False
                self._cache_thread = None
            if again:
                self._spawn_cache_refresh(recenter=True)

        self._cache_pending = False
        self._cache_thread = threading.Thread(
            target=work, name="gmail-tui-cal-cache", daemon=True
        )
        self._cache_thread.start()

    def _client(self, account: Account) -> HttpCalendar:
        existing = self.clients.get(account.name)
        if existing is not None:
            return existing
        token = current_access_token(account.root)
        if not token:
            raise OAuthError(f"{account.name}: not signed in (run gmail-tui login)")

        def refresh(*, force: bool = False) -> str | None:
            return current_access_token(account.root, force=force)

        client = HttpCalendar(token, refresh=refresh)
        self.clients[account.name] = client
        return client

    def _rebuild_tree(self) -> None:
        nodes: list[CalNode] = []
        for index, account in enumerate(self.accounts):
            is_last = index == len(self.accounts) - 1
            acct_key = f"acct:{account.name}"
            open_acct = acct_key in self.expanded
            kids = self.calendars.get(account.name, [])
            prefix = ("v " if open_acct else "> ") if kids or account.name in self.errors else "  "
            nodes.append(
                CalNode(
                    key=acct_key,
                    label=account.name,
                    depth=0,
                    account=account.name,
                    expandable=True,
                    expanded=open_acct,
                    prefix=prefix,
                )
            )
            if not open_acct:
                continue
            err = self.errors.get(account.name)
            if err and not kids:
                nodes.append(
                    CalNode(
                        key=f"err:{account.name}",
                        label=err,
                        depth=1,
                        account=account.name,
                        prefix=("  └ " if is_last else "│ └ "),
                    )
                )
                continue
            for child_i, cal in enumerate(kids):
                child_last = child_i == len(kids) - 1
                branch = "  " if is_last else "│ "
                mark = "└ " if child_last else "├ "
                enabled = _cal_key(account.name, cal.id) in self.enabled
                box = "[x]" if enabled else "[ ]"
                nodes.append(
                    CalNode(
                        key=_cal_key(account.name, cal.id),
                        label=f"{box} {cal.summary}",
                        depth=1,
                        account=account.name,
                        calendar_id=cal.id,
                        enabled=enabled,
                        prefix=branch + mark,
                    )
                )
        self.visible = nodes
        if self.visible:
            self.cal_cursor = min(self.cal_cursor, len(self.visible) - 1)

    def range_bounds(self) -> tuple[datetime, datetime]:
        tz = datetime.now().astimezone().tzinfo or timezone.utc
        day = self.cursor
        if self.layout == "day":
            start = datetime(day.year, day.month, day.day, tzinfo=tz)
            return start, start + timedelta(days=1)
        if self.layout == "month":
            first = date(day.year, day.month, 1)
            if day.month == 12:
                nxt = date(day.year + 1, 1, 1)
            else:
                nxt = date(day.year, day.month + 1, 1)
            start = datetime(first.year, first.month, first.day, tzinfo=tz)
            stop = datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
            return start, stop
        # week: Monday-start
        monday = day - timedelta(days=day.weekday())
        start = datetime(monday.year, monday.month, monday.day, tzinfo=tz)
        return start, start + timedelta(days=7)

    def visible_events(self) -> list[CalEvent]:
        start, stop = self.range_bounds()
        return [item for item in self.events if item.start < stop and item.end > start]

    def current_cal(self) -> CalNode | None:
        if not self.visible:
            return None
        return self.visible[max(0, min(self.cal_cursor, len(self.visible) - 1))]

    def current_event(self) -> CalEvent | None:
        rows = self.visible_events()
        if not rows:
            return None
        return rows[max(0, min(self.event_cursor, len(rows) - 1))]

    def move(self, delta: int) -> None:
        if self.focus == "cals":
            if not self.visible:
                return
            self.cal_cursor = max(0, min(len(self.visible) - 1, self.cal_cursor + delta))
            return
        rows = self.visible_events()
        if not rows:
            # Empty timetable: scroll hours with j/k.
            if self.hour_scroll < 0:
                self.hour_scroll = _default_hour_scroll()
            self.hour_scroll = max(0, min(23, self.hour_scroll + delta))
            return
        self.event_cursor = max(0, min(len(rows) - 1, self.event_cursor + delta))
        self._reveal_selected_hour()

    def _reveal_selected_hour(self, window: int = 12) -> None:
        """Keep the selected timed event's start hour inside the timetable window."""
        if self.hour_scroll < 0:
            self.hour_scroll = _default_hour_scroll()
        event = self.current_event()
        if event is None or event.all_day or _is_day_band_event(event, _timetable_days(self)):
            return
        hour = _local_dt(event.start).hour
        if hour < self.hour_scroll:
            self.hour_scroll = hour
        elif hour >= self.hour_scroll + max(1, window):
            self.hour_scroll = max(0, hour - max(1, window) + 1)

    def horizontal(self, right: bool) -> None:
        if self.focus == "cals":
            node = self.current_cal()
            if node and node.expandable and node.expanded != right:
                self.toggle_fold()
                return
            if right:
                self.focus = "view"
            return
        if not right:
            self.focus = "cals"

    def toggle_fold(self) -> None:
        node = self.current_cal()
        if node is None or not node.expandable:
            return
        if node.key in self.expanded:
            self.expanded.remove(node.key)
        else:
            self.expanded.add(node.key)
        self._rebuild_tree()

    def activate(self) -> None:
        if self.focus == "cals":
            node = self.current_cal()
            if node is None:
                return
            if node.calendar_id:
                key = node.key
                if key in self.enabled:
                    self.enabled.remove(key)
                else:
                    self.enabled.add(key)
                self._rebuild_tree()
                self.reload_events(force=True)
                return
            if node.expandable:
                self.toggle_fold()
            return
        event = self.current_event()
        if event is not None:
            self.begin_edit(event)

    def set_layout(self, layout: Layout) -> None:
        if layout not in {"day", "week", "month"}:
            return
        self.layout = layout
        self.event_cursor = 0
        self.hour_scroll = -1
        self.reload_events()

    def cycle_layout(self) -> None:
        order = ("day", "week", "month")
        self.set_layout(order[(order.index(self.layout) + 1) % 3])

    def shift_range(self, steps: int) -> None:
        day = self.cursor
        if self.layout == "day":
            self.cursor = day + timedelta(days=steps)
        elif self.layout == "week":
            self.cursor = day + timedelta(weeks=steps)
        else:
            month = day.month - 1 + steps
            year = day.year + month // 12
            month = month % 12 + 1
            last = pycal.monthrange(year, month)[1]
            self.cursor = date(year, month, min(day.day, last))
        self.event_cursor = 0
        self.reload_events()

    def jump_today(self) -> None:
        self.cursor = date.today()
        self.hour_scroll = -1
        self.event_cursor = 0
        self.reload_events()

    def default_target(self) -> tuple[str, str] | None:
        node = self.current_cal()
        if node and node.calendar_id:
            return node.account, node.calendar_id
        for account in self.accounts:
            for cal in self.calendars.get(account.name, []):
                key = _cal_key(account.name, cal.id)
                if key in self.enabled and cal.access_role in {"owner", "writer"}:
                    return account.name, cal.id
            for cal in self.calendars.get(account.name, []):
                if cal.primary:
                    return account.name, cal.id
        return None

    def begin_create(self) -> bool:
        target = self.default_target()
        if target is None:
            self.notice = "no writable calendar"
            return False
        account_name, calendar_id = target
        start = datetime(
            self.cursor.year,
            self.cursor.month,
            self.cursor.day,
            10,
            0,
            tzinfo=datetime.now().astimezone().tzinfo,
        )
        end = start + timedelta(hours=1)
        text = event_draft_text(
            EventPatch(summary="", start=start, end=end, all_day=False),
            account=account_name,
            calendar_id=calendar_id,
        )
        self._edit_event = None
        self._create_account = account_name
        self._create_calendar = calendar_id
        return self._open_editor(text)

    def begin_edit(self, event: CalEvent | None = None) -> bool:
        event = event or self.current_event()
        if event is None:
            self.notice = "no event"
            return False
        patch = EventPatch(
            summary=event.summary,
            start=event.start,
            end=event.end,
            all_day=event.all_day,
            location=event.location,
            description=event.description,
        )
        text = event_draft_text(patch, account=event.account, calendar_id=event.calendar_id)
        self._edit_event = event
        self._create_account = event.account
        self._create_calendar = event.calendar_id
        return self._open_editor(text)

    def begin_delete(self) -> None:
        event = self.current_event()
        if event is None:
            self.notice = "no event"
            return
        self.prompt = "delete-event"
        self.notice = ""

    def confirm_delete(self) -> None:
        if self.prompt != "delete-event":
            return
        self.prompt = None
        event = self.current_event()
        if event is None:
            return
        account = next((item for item in self.accounts if item.name == event.account), None)
        if account is None:
            self.notice = "account missing"
            return
        try:
            self._client(account).delete_event(event.calendar_id, event.id)
        except (CalendarError, Offline, OAuthError) as exc:
            self.notice = str(exc) or "delete failed"
            return
        self.notice = "deleted"
        self.reload_events(force=True)

    def _open_editor(self, text: str) -> bool:
        if self.external is not None:
            self.notice = "already editing an event"
            return False
        handle, name = tempfile.mkstemp(prefix="gmail-tui-event-", suffix=".txt")
        os.close(handle)
        path = Path(name)
        sentinel = Path(str(path) + ".done")
        try:
            path.write_text(text, encoding="utf-8")
            process = launch_editor(path, sentinel, editor_command())
        except OSError as exc:
            path.unlink(missing_ok=True)
            sentinel.unlink(missing_ok=True)
            self.notice = str(exc) or "could not open a terminal"
            return False
        self.external = _ExternalEdit(path, sentinel, process, "", "")
        self.notice = "editing in another window"
        return True

    def poll_external_edit(self) -> bool:
        pending = self.external
        if pending is None:
            return False
        process = pending.process
        if process is not None and hasattr(process, "poll"):
            try:
                process.poll()
            except OSError:
                pass
        if not pending.sentinel.is_file():
            return False
        try:
            status = int(pending.sentinel.read_text(encoding="ascii").strip() or "0")
        except (OSError, ValueError):
            status = 1
        try:
            edited: str | None = pending.path.read_text(encoding="utf-8")
        except OSError:
            edited = None
        self.external = None
        pending.path.unlink(missing_ok=True)
        pending.sentinel.unlink(missing_ok=True)
        if edited is None:
            self.notice = "editor failed"
            return True
        if status != 0:
            self.notice = "not saved"
            return True
        try:
            account_name, calendar_id, patch = parse_event_draft(edited)
        except CalendarError as exc:
            self.notice = str(exc) or "bad event"
            return True
        account_name = account_name or self._create_account
        calendar_id = calendar_id or self._create_calendar
        account = next((item for item in self.accounts if item.name == account_name), None)
        if account is None:
            self.notice = "unknown account"
            return True
        try:
            client = self._client(account)
            if self._edit_event is None:
                client.create_event(calendar_id, patch)
                self.notice = "created"
            else:
                client.update_event(calendar_id, self._edit_event.id, patch)
                self.notice = "updated"
        except (CalendarError, Offline, OAuthError) as exc:
            self.notice = str(exc) or "save failed"
            return True
        self._edit_event = None
        self.reload_events(force=True)
        return True

    def forget_external_edit(self) -> None:
        pending = self.external
        if pending is None:
            return
        self.external = None
        pending.path.unlink(missing_ok=True)
        pending.sentinel.unlink(missing_ok=True)
        self.notice = "editor closed"


def _cal_key(account: str, calendar_id: str) -> str:
    return f"cal:{account}:{calendar_id}"


def _shift_years(day: date, years: int) -> date:
    try:
        return date(day.year + years, day.month, day.day)
    except ValueError:
        # Feb 29 → Feb 28 in non-leap years.
        return date(day.year + years, day.month, 28)


def event_draft_text(patch: EventPatch, *, account: str, calendar_id: str) -> str:
    start = patch.start or datetime.now().astimezone()
    end = patch.end or (start + timedelta(hours=1))
    if patch.all_day:
        when = formats.format_date(start)
        until = formats.format_date(end)
        kind = "all-day"
    else:
        when = formats.format_datetime(start)
        until = formats.format_datetime(end)
        kind = "timed"
    lines = [
        f"Account: {account}",
        f"Calendar: {calendar_id}",
        f"Title: {patch.summary}",
        f"Kind: {kind}",
        f"When: {when}",
        f"Until: {until}",
        f"Location: {patch.location}",
        "Description:",
        patch.description.rstrip("\n"),
        "",
    ]
    return "\n".join(lines)


def parse_event_draft(text: str) -> tuple[str, str, EventPatch]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if normalized.endswith("\n"):
        normalized = normalized[:-1]
    header, sep, body = normalized.partition("\nDescription:\n")
    if not sep:
        header, sep, body = normalized.partition("\nDescription:")
        body = body.lstrip("\n") if sep else ""
    account = calendar_id = title = location = kind = when = until = ""
    for line in header.split("\n"):
        name, colon, value = line.partition(":")
        if not colon:
            continue
        key = name.strip().casefold()
        value = value.strip()
        if key == "account":
            account = value
        elif key == "calendar":
            calendar_id = value
        elif key == "title":
            title = value
        elif key == "location":
            location = value
        elif key == "kind":
            kind = value.casefold()
        elif key == "when":
            when = value
        elif key == "until":
            until = value
    if not when or not until:
        raise CalendarError("When and Until are required")
    all_day = kind.startswith("all")
    start = _parse_when(when, all_day=all_day)
    end = _parse_when(until, all_day=all_day)
    if end <= start and not all_day:
        raise CalendarError("Until must be after When")
    if all_day and end < start:
        raise CalendarError("Until must be on or after When")
    if all_day and end == start:
        end = start + timedelta(days=1)
    return (
        account,
        calendar_id,
        EventPatch(
            summary=title or "(no title)",
            start=start,
            end=end,
            all_day=all_day,
            location=location,
            description=body,
        ),
    )


def _parse_when(text: str, *, all_day: bool) -> datetime:
    text = text.strip()
    tz = datetime.now().astimezone().tzinfo or timezone.utc
    if all_day:
        day = formats.parse_date(text)
        return datetime(day.year, day.month, day.day, tzinfo=tz)
    try:
        return formats.parse_datetime(text)
    except ValueError:
        pass
    # Fall through for older drafts / partial stamps.
    if len(text) <= 10:
        day = formats.parse_date(text)
        return datetime(day.year, day.month, day.day, tzinfo=tz)
    for fmt in (formats.datetime_format(), "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=tz)
    raise CalendarError(f"bad time: {text}")


def render_calendar(desk: CalendarDesk, height: int, width: int, accounts: list[Account], active: int) -> list[str]:
    from gmail_tui.ui import geometry, _Canvas

    geo = geometry(height, width)
    canvas = _Canvas(height, width)
    _write_account_tabs(canvas, accounts, active, geo.width)
    _frame(canvas, geo)
    _write_cal_tree(canvas, desk, geo)
    _write_cal_view(canvas, desk, geo)
    if desk.prompt == "delete-event":
        _write_delete_prompt(canvas, desk, geo)
    if desk.help_open:
        _write_cal_help(canvas, desk, geo)
    return canvas.lines()


def _write_account_tabs(canvas, accounts: list[Account], active: int, width: int) -> None:
    column = 0
    for index, account in enumerate(accounts):
        mark = "* " if index == active else "  "
        text = f"{mark}{account.name}"
        if index:
            gap = " │ "
            if column + len(gap) >= width:
                break
            canvas.write(0, column, gap)
            column += len(gap)
        if column >= width:
            break
        shown = text[: width - column]
        canvas.write(0, column, shown)
        column += len(shown)


def status_calendar(desk: CalendarDesk, width: int) -> str:
    if desk.prompt == "delete-event":
        return _clip("delete this event?  y / n", max(0, width - 1))
    hint = f"{desk.layout}  {formats.format_date(desk.cursor)}  {len(desk.visible_events())} events"
    if desk.notice:
        hint = f"{desk.notice}  |  {hint}"
    return _clip(hint, max(0, width - 1))


def _frame(canvas, geo) -> None:
    from gmail_tui.ui import _hline

    top = geo.top
    bottom = geo.height - 2
    right = geo.width - 1
    split = geo.sidebar_w - 1
    _hline(canvas, top, 0, right, "┌", "─", "┐")
    for row in range(top + 1, bottom):
        canvas.write(row, 0, "│")
        canvas.write(row, split, "│")
        canvas.write(row, right, "│")
    _hline(canvas, bottom, 0, right, "└", "─", "┘")
    canvas.write(top, split, "┬")
    canvas.write(bottom, split, "┴")


def _write_cal_tree(canvas, desk: CalendarDesk, geo) -> None:
    top = geo.top + 1
    width = geo.sidebar_w - 2
    canvas.write(top, 1, _clip("Calendars", width))
    rows = desk.visible[desk.cal_scroll : desk.cal_scroll + max(0, geo.label_rows)]
    # Keep cursor in view.
    if desk.cal_cursor < desk.cal_scroll:
        desk.cal_scroll = desk.cal_cursor
    elif desk.cal_cursor >= desk.cal_scroll + max(1, geo.label_rows):
        desk.cal_scroll = desk.cal_cursor - max(1, geo.label_rows) + 1
    rows = desk.visible[desk.cal_scroll : desk.cal_scroll + max(0, geo.label_rows)]
    for offset, item in enumerate(rows):
        canvas.write(top + 1 + offset, 1, _clip(f"{item.prefix}{item.label}", width))


def _write_cal_view(canvas, desk: CalendarDesk, geo) -> None:
    left = geo.sidebar_w
    width = geo.width - geo.sidebar_w - 1
    header = _header_line(desk, width)
    canvas.write(geo.top + 1, left + 1, header)
    from gmail_tui.ui import _hline

    _hline(canvas, geo.top + 2, geo.sidebar_w - 1, geo.width - 1, "├", "─", "┤")
    body_top = geo.top + 3
    body_rows = max(0, geo.height - 2 - body_top)
    events = desk.visible_events()
    if desk.event_cursor < desk.event_scroll:
        desk.event_scroll = desk.event_cursor
    elif desk.event_cursor >= desk.event_scroll + max(1, body_rows):
        desk.event_scroll = desk.event_cursor - max(1, body_rows) + 1
    if desk.layout == "month":
        _write_month(canvas, desk, body_top, left + 1, body_rows, width - 1)
        return
    _write_timetable(canvas, desk, body_top, left + 1, body_rows, width - 1, events)


def _header_line(desk: CalendarDesk, width: int) -> str:
    start, stop = desk.range_bounds()
    if desk.layout == "day":
        span = f"{desk.cursor.strftime('%a')} {formats.format_date(desk.cursor)}"
    elif desk.layout == "week":
        last = (stop - timedelta(seconds=1)).date()
        span = f"{formats.format_date(start)} – {formats.format_date(last)}"
    else:
        span = formats.format_month_year(desk.cursor)
    modes = []
    for name in ("day", "week", "month"):
        modes.append(name.upper() if name == desk.layout else name)
    return _clip(f"{' '.join(modes)}   < {span} >", max(0, width - 1))


def _event_line(event: CalEvent, layout: str) -> str:
    if event.all_day:
        when = formats.format_date(event.start) + " all-day"
    elif layout == "day":
        when = f"{formats.format_time(event.start)}–{formats.format_time(event.end)}"
    else:
        when = f"{formats.format_weekday(event.start)} {formats.format_time(event.start)}"
    cal = event.calendar_name or event.calendar_id
    return f"{when}  {event.summary}  ({event.account}/{cal})"


def _local_dt(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone()


def _default_hour_scroll() -> int:
    """Start a couple of hours before local now, clamped to the morning."""
    hour = datetime.now().astimezone().hour
    return max(0, min(16, hour - 2))


def _write_timetable(
    canvas,
    desk: CalendarDesk,
    top: int,
    left: int,
    rows: int,
    width: int,
    events: list[CalEvent],
) -> None:
    if rows <= 0 or width <= 0:
        return
    selected = desk.current_event()
    used = 0
    # Reserve one footer row for the selected event's full line.
    footer = 1 if rows >= 3 else 0
    body_budget = rows - footer
    days = _timetable_days(desk)
    # True all-day plus long blocks (≥75% of a day) sit under the day headers.
    band = [item for item in events if _is_day_band_event(item, days)]
    timed = [item for item in events if not _is_day_band_event(item, days)]
    gutter = 6  # "HH:00 "
    today = date.today()
    now_hour = datetime.now().astimezone().hour
    if len(days) > 1:
        col_w = max(4, (width - gutter) // len(days))
    else:
        col_w = max(8, width - gutter)

    if len(days) > 1:
        parts = []
        for day in days:
            label = formats.format_weekday(day)
            if day == today:
                label = "*" + label
            parts.append(_pad(label, col_w - 1))
        canvas.write(
            top + used,
            left,
            _pad(" " * gutter + "".join(f"│{part}" for part in parts), width),
        )
        used += 1
        if used >= body_budget:
            _write_timetable_footer(canvas, desk, top + rows - footer, left, width, selected, footer)
            return
    # Day band under the day headers, above the hour grid.
    used += _write_all_day_band(
        canvas,
        desk,
        top + used,
        left,
        max(0, body_budget - used - 1),
        width,
        gutter,
        days,
        col_w,
        band,
        selected,
    )
    if used >= body_budget:
        _write_timetable_footer(canvas, desk, top + rows - footer, left, width, selected, footer)
        return

    hour_rows = body_budget - used
    if hour_rows <= 0:
        _write_timetable_footer(canvas, desk, top + rows - footer, left, width, selected, footer)
        return
    desk._reveal_selected_hour(hour_rows)
    if hour_rows >= 24:
        desk.hour_scroll = 0
    else:
        desk.hour_scroll = max(0, min(24 - hour_rows, desk.hour_scroll))

    # Side-by-side lanes so overlapping timed events stay visible.
    lanes_by_day = {day: _assign_lanes(_events_on_day(timed, day)) for day in days}

    for offset in range(hour_rows):
        hour = desk.hour_scroll + offset
        if hour >= 24:
            break
        current = any(day == today for day in days) and hour == now_hour
        sample = datetime(2000, 1, 1, hour, 0)
        stamp = formats.format_time(sample)
        if current:
            stamp = f"*{stamp}"
        label = _pad(stamp, gutter)
        if len(days) == 1:
            cell = _day_hour_cell(
                timed,
                days[0],
                hour,
                selected,
                width - gutter,
                lanes_by_day[days[0]],
                current_day=days[0] == today,
            )
            line = f"{label}│{cell}"
        else:
            cells = [
                _day_hour_cell(
                    timed,
                    day,
                    hour,
                    selected,
                    col_w - 1,
                    lanes_by_day[day],
                    current_day=day == today,
                )
                for day in days
            ]
            line = label + "".join(f"│{cell}" for cell in cells)
        canvas.write(top + used + offset, left, _pad(line, width))
    _write_timetable_footer(canvas, desk, top + rows - footer, left, width, selected, footer)


def _write_all_day_band(
    canvas,
    desk: CalendarDesk,
    top: int,
    left: int,
    rows: int,
    width: int,
    gutter: int,
    days: list[date],
    col_w: int,
    events: list[CalEvent],
    selected: CalEvent | None,
) -> int:
    """Draw all-day events above the hour grid. Returns rows consumed."""
    if rows <= 0 or not events:
        return 0
    canvas.write(top, left, _pad("all-day", width))
    used = 1
    # Leave room for day headers + at least a few hour rows when possible.
    limit = min(len(events), max(1, rows - 1))
    for event in events[:limit]:
        if used >= rows:
            break
        mark = ">" if selected is not None and event is selected else "■"
        title = event.summary or "(no title)"
        if len(days) == 1:
            canvas.write(top + used, left, _pad(f" {mark} {title}", width))
        else:
            parts = []
            first = True
            for day in days:
                if _band_covers(event, day):
                    if first:
                        parts.append(_pad(f"{mark}{title}", col_w - 1))
                        first = False
                    else:
                        parts.append(_pad("━" * max(0, col_w - 1), col_w - 1))
                else:
                    parts.append(_pad("", col_w - 1))
            canvas.write(
                top + used,
                left,
                _pad(" " * gutter + "".join(f"│{part}" for part in parts), width),
            )
        used += 1
    hidden = len(events) - limit
    if hidden > 0 and used < rows:
        canvas.write(top + used, left, _pad(f"  (+{hidden} more all-day)", width))
        used += 1
    return used


def _write_timetable_footer(canvas, desk, top: int, left: int, width: int, selected, footer: int) -> None:
    if footer <= 0:
        return
    if selected is None:
        canvas.write(top, left, _pad("(no event selected)", width))
        return
    canvas.write(top, left, _pad(_event_line(selected, desk.layout), width))


def _timetable_days(desk: CalendarDesk) -> list[date]:
    if desk.layout == "day":
        return [desk.cursor]
    monday = desk.cursor - timedelta(days=desk.cursor.weekday())
    return [monday + timedelta(days=index) for index in range(7)]


_DAY_SECONDS = 24 * 60 * 60
# Timed blocks that fill most of a calendar day belong in the day band, not the hour grid.
_DAY_BAND_FRACTION = 0.75


def _day_coverage_seconds(event: CalEvent, day: date) -> float:
    start = _local_dt(event.start)
    end = _local_dt(event.end)
    day_start = datetime(day.year, day.month, day.day, tzinfo=start.tzinfo)
    day_end = day_start + timedelta(days=1)
    overlap_start = max(start, day_start)
    overlap_end = min(end, day_end)
    if overlap_end <= overlap_start:
        return 0.0
    return (overlap_end - overlap_start).total_seconds()


def _is_day_band_event(event: CalEvent, days: list[date]) -> bool:
    """True all-day, or timed coverage of ≥75% of any visible day / overall."""
    if event.all_day:
        return True
    start = _local_dt(event.start)
    end = _local_dt(event.end)
    if (end - start).total_seconds() >= _DAY_SECONDS * _DAY_BAND_FRACTION:
        return True
    threshold = _DAY_SECONDS * _DAY_BAND_FRACTION
    return any(_day_coverage_seconds(event, day) >= threshold for day in days)


def _band_covers(event: CalEvent, day: date) -> bool:
    if event.all_day:
        return _all_day_covers(event, day)
    return _day_coverage_seconds(event, day) > 0


def _all_day_covers(event: CalEvent, day: date) -> bool:
    start = _local_dt(event.start).date()
    # All-day end is exclusive in Google Calendar.
    end = _local_dt(event.end).date()
    if event.all_day and end > start:
        return start <= day < end
    return start <= day <= end


def _events_on_day(events: list[CalEvent], day: date) -> list[CalEvent]:
    hits: list[CalEvent] = []
    for event in events:
        if _day_coverage_seconds(event, day) > 0:
            hits.append(event)
    return hits


def _assign_lanes(events: list[CalEvent]) -> tuple[dict[str, int], int]:
    """Map event id → lane so overlaps sit side by side. Returns (lanes, count)."""
    ordered = sorted(
        events,
        key=lambda item: (_local_dt(item.start), _local_dt(item.end), item.summary.casefold()),
    )
    lane_free_at: list[datetime] = []
    lanes: dict[str, int] = {}
    for event in ordered:
        start = _local_dt(event.start)
        end = _local_dt(event.end)
        placed = False
        for index, free_at in enumerate(lane_free_at):
            if free_at <= start:
                lane_free_at[index] = end
                lanes[event.id] = index
                placed = True
                break
        if not placed:
            lanes[event.id] = len(lane_free_at)
            lane_free_at.append(end)
    return lanes, max(1, len(lane_free_at))


def _day_hour_cell(
    events: list[CalEvent],
    day: date,
    hour: int,
    selected: CalEvent | None,
    width: int,
    lane_info: tuple[dict[str, int], int],
    *,
    current_day: bool = False,
) -> str:
    if width <= 0:
        return ""
    lanes, lane_count = lane_info
    hits = [
        event
        for event in events
        if _covers_hour(event, day, hour)
    ]
    if not hits:
        if current_day and hour == datetime.now().astimezone().hour:
            return _pad("─" * max(0, width), width)
        return _pad("", width)
    # Split the day column evenly so every overlapping event stays visible.
    lane_count = max(lane_count, max(lanes.get(event.id, 0) for event in hits) + 1)
    lane_w = max(1, width // lane_count)
    pieces: list[str] = []
    by_lane = {lanes.get(event.id, 0): event for event in hits}
    # If two events somehow share a lane id in this hour, keep the selected one.
    for event in hits:
        index = lanes.get(event.id, 0)
        current = by_lane.get(index)
        if current is None or (selected is not None and event is selected):
            by_lane[index] = event
    for index in range(lane_count):
        event = by_lane.get(index)
        if event is None:
            pieces.append(_pad("", lane_w))
            continue
        selected_here = selected is not None and event is selected
        # ┃ separates overlapping lanes; every covered hour repeats from–to + title.
        rail = ">" if selected_here else "┃"
        text = rail + _event_hour_label(event)
        pieces.append(_pad(text, lane_w))
    joined = "".join(pieces)
    return _pad(joined, width)


def _event_hour_label(event: CalEvent) -> str:
    start = _local_dt(event.start)
    end = _local_dt(event.end)
    when = f"{formats.format_time(start)}–{formats.format_time(end)}"
    title = event.summary or "(no title)"
    return f"{when} {title}"


def _covers_hour(event: CalEvent, day: date, hour: int) -> bool:
    start = _local_dt(event.start)
    end = _local_dt(event.end)
    slot_start = datetime(day.year, day.month, day.day, hour, tzinfo=start.tzinfo)
    slot_end = slot_start + timedelta(hours=1)
    return start < slot_end and end > slot_start


def _write_month(canvas, desk: CalendarDesk, top: int, left: int, rows: int, width: int) -> None:
    if rows <= 0:
        return
    weeks = pycal.Calendar(firstweekday=0).monthdayscalendar(desk.cursor.year, desk.cursor.month)
    head = " ".join(f"{name:>3}" for name in ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"))
    canvas.write(top, left, _pad(head, width))
    counts: dict[int, int] = {}
    for event in desk.visible_events():
        local = _local_dt(event.start)
        if local.month == desk.cursor.month and local.year == desk.cursor.year:
            counts[local.day] = counts.get(local.day, 0) + 1
    today = date.today()
    grid_rows = min(len(weeks), max(0, rows - 1))
    for offset, week in enumerate(weeks[:grid_rows]):
        cells = []
        for day in week:
            if day == 0:
                cells.append("   ")
            elif day == desk.cursor.day:
                cells.append(f"{day:2d}*")
            elif day == today.day and desk.cursor.month == today.month and desk.cursor.year == today.year:
                cells.append(f"{day:2d}·")
            elif counts.get(day):
                cells.append(f"{day:2d}.")
            else:
                cells.append(f"{day:2d} ")
        canvas.write(top + 1 + offset, left, _pad(" ".join(cells), width))
    used = 1 + grid_rows
    rest = rows - used
    if rest <= 0:
        return
    events = desk.visible_events()
    if not events:
        canvas.write(top + used, left, _pad("(no events this month)", width))
        return
    agenda = _month_agenda(events, desk.current_event())
    # Keep the selected event (and the now marker near it) in the scroll window.
    selected_index = next((i for i, (kind, _text, event) in enumerate(agenda) if event is desk.current_event()), 0)
    if selected_index < desk.event_scroll:
        desk.event_scroll = selected_index
    elif selected_index >= desk.event_scroll + rest:
        desk.event_scroll = max(0, selected_index - rest + 1)
    shown = agenda[desk.event_scroll : desk.event_scroll + rest]
    for offset, (_kind, text, _event) in enumerate(shown):
        canvas.write(top + used + offset, left, _pad(text, width))


def _month_agenda(
    events: list[CalEvent],
    selected: CalEvent | None,
) -> list[tuple[str, str, CalEvent | None]]:
    """Agenda lines with a 'now' marker between past and upcoming events."""
    now = datetime.now().astimezone()
    lines: list[tuple[str, str, CalEvent | None]] = []
    inserted_now = False
    for event in events:
        start = _local_dt(event.start)
        if not inserted_now and start >= now:
            lines.append(("now", "── now ──", None))
            inserted_now = True
        past = _local_dt(event.end) <= now
        if selected is not None and event is selected:
            mark = ">"
        elif past:
            mark = "·"
        else:
            mark = " "
        lines.append(("event", f"{mark} {_event_line(event, 'month')}", event))
    if not inserted_now:
        lines.append(("now", "── now ──", None))
    return lines


def _write_delete_prompt(canvas, desk: CalendarDesk, geo) -> None:
    event = desk.current_event()
    title = event.summary if event else "event"
    msg = f"Delete “{title}”?  y / n"
    width = min(geo.width - 4, max(28, _display_width(msg) + 4))
    left = max(0, (geo.width - width) // 2)
    top = max(geo.top + 2, (geo.height - 5) // 2)
    for row in range(3):
        canvas.write(top + row, left, " " * width)
    canvas.write(top, left, "┌" + "─" * (width - 2) + "┐")
    canvas.write(top + 1, left, "│" + _clip(msg, width - 2).center(width - 2)[: width - 2] + "│")
    canvas.write(top + 2, left, "└" + "─" * (width - 2) + "┘")


def _write_cal_help(canvas, desk: CalendarDesk, geo) -> None:
    lines = [
        "Calendar",
        "C           back to mail",
        "Tab         calendars / timetable",
        "Enter       toggle calendar, or edit event",
        "c           new event",
        "e / Enter   edit event",
        "d           delete event",
        "t           today",
        "D / W / M   day / week / month",
        "n / p       next / previous range",
        "j / k       next/prev event (or scroll hours)",
        "?           close help",
    ]
    width = min(geo.width - 4, max(len(line) for line in lines) + 4)
    height = len(lines) + 2
    left = max(0, (geo.width - width) // 2)
    top = max(geo.top + 1, (geo.height - height) // 2)
    for row in range(height):
        canvas.write(top + row, left, " " * width)
    canvas.write(top, left, "┌" + "─" * (width - 2) + "┐")
    for offset, line in enumerate(lines):
        canvas.write(top + 1 + offset, left, "│ " + _clip(line, width - 3).ljust(width - 3)[: width - 3] + "│")
    canvas.write(top + height - 1, left, "└" + "─" * (width - 2) + "┘")

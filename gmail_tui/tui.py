"""Curses front end for the three-pane mailbox."""

from __future__ import annotations

import base64
import curses
import locale
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from gmail_tui import profile
from gmail_tui.accounts import Account, arrange, discover
from gmail_tui.gmail import NullGmail
from gmail_tui.http_gmail import HttpGmail
from gmail_tui.keys import ALT, KeyMap, KeysError, config_path, defaults, load
from gmail_tui.settings import SettingsError, default_download, load as load_settings
from gmail_tui.links import osc8
from gmail_tui.oauth import OAuthError, current_access_token
from gmail_tui.quota import SyncStopped
from gmail_tui.sixel import encode_image, probe_terminal
from gmail_tui.notify import new_mail as notify_new_mail
from gmail_tui.sync import BackgroundSync, Engine, UrgentWork
from gmail_tui.cal_ui import CalendarDesk, render_calendar, status_calendar
from gmail_tui.ui import (
    MailSession,
    _clip,
    _display_width,
    format_check_age,
    geometry,
    help_lines,
    link_at,
    mark_spans,
    reader_hyperlinks,
    reader_match,
    render_account_tabs,
    render_lines,
    status_bar,
)


_images: _SixelPump | None = None


class _SixelPump:
    """Encode attached pictures away from the key handler."""

    def __init__(self, desk: _Desk) -> None:
        self.desk = desk
        self.wake = threading.Event()
        self.shown: tuple | None = None
        self.queue: list[tuple[str, int, tuple[int, int]]] = []
        self.generation = 0
        # One entry per picture. Switching attachments then hits the cache.
        self.cache: dict[tuple[str, int, tuple[int, int]], bytes] = {}
        self._lock = threading.Lock()

    def ask(
        self,
        session: MailSession,
        gmail_id: str,
        indexes: tuple[int, ...],
        pixels: tuple[int, int],
        preload: tuple[int, ...] = (),
    ) -> None:
        shown = (gmail_id, indexes, pixels)
        frames = self._frames_for(gmail_id, indexes, pixels)
        session.sixel_frames = frames
        with self._lock:
            needed = [
                index
                for index in indexes
                if (gmail_id, index, pixels) not in self.cache
            ]
            warm = [
                index
                for index in preload
                if index not in indexes and (gmail_id, index, pixels) not in self.cache
            ]
            self.shown = shown
            # Cache hits are drawn by this paint. Arming ready here would erase
            # and re-emit the picture on every poll, which reads as flicker.
            if not needed and not warm:
                session.sixel_pending = False
                return
            if needed:
                self.generation += 1
                self.queue = [(gmail_id, index, pixels) for index in [*needed, *warm]]
            else:
                queued = {(item[0], item[1], item[2]) for item in self.queue}
                for index in warm:
                    item = (gmail_id, index, pixels)
                    if item not in queued:
                        self.queue.append(item)
        session.sixel_pending = bool(needed)
        self.wake.set()

    def _frames_for(
        self,
        gmail_id: str,
        indexes: tuple[int, ...],
        pixels: tuple[int, int],
    ) -> tuple[tuple[str, int, bytes], ...]:
        found: list[tuple[str, int, bytes]] = []
        with self._lock:
            for index in indexes:
                payload = self.cache.get((gmail_id, index, pixels))
                if payload is not None:
                    found.append((gmail_id, index, payload))
        return tuple(found)

    def run(self) -> None:
        while not self.desk.stop.is_set():
            if not self.wake.wait(0.5):
                continue
            self.wake.clear()
            if self.desk.stop.is_set():
                return
            self._encode()

    def _encode(self) -> None:
        while not self.desk.stop.is_set():
            with self._lock:
                if not self.queue:
                    return
                generation = self.generation
                gmail_id, index, pixels = self.queue.pop(0)
                shown = self.shown
            session = self.desk.session
            store = session.engine.store
            width, height = pixels
            data = store.attachment_bytes(gmail_id, index)
            encoded = encode_image(data or b"", width, height)
            with self._lock:
                if self.generation != generation:
                    continue
                if encoded:
                    self.cache[(gmail_id, index, pixels)] = encoded
                    if len(self.cache) > 48:
                        self.cache.pop(next(iter(self.cache)))
            if not encoded or shown is None:
                continue
            shown_id, shown_indexes, shown_pixels = shown
            if gmail_id != shown_id or pixels != shown_pixels:
                continue
            if self.desk.session is not session:
                continue
            session.sixel_frames = self._frames_for(shown_id, shown_indexes, shown_pixels)
            if index in shown_indexes:
                session.sixel_pending = any(
                    (shown_id, item, shown_pixels) not in self.cache
                    for item in shown_indexes
                )
                session.sixel_ready = True


class _Account:
    """One mailbox and the sync job that keeps running while another account is shown."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.session = MailSession(engine)
        self.job = BackgroundSync(engine)
        self.urgent = UrgentWork(engine)
        self.lock = threading.Lock()


class _Desk:
    def __init__(self, jobs: list[_Account], accounts: list[Account]) -> None:
        self.jobs = jobs
        self.accounts = accounts
        self.index = 0
        self.mode = "mail"  # mail | calendar
        self._calendar: CalendarDesk | None = None
        self.stop = threading.Event()
        self.poke = threading.Event()
        self._alert_lock = threading.Lock()
        self._alerts: dict[str, int] = {}

    @property
    def calendar(self) -> CalendarDesk:
        if self._calendar is None:
            self._calendar = CalendarDesk(self.accounts)
        return self._calendar

    @property
    def current(self) -> _Account:
        return self.jobs[self.index]

    @property
    def engine(self) -> Engine:
        return self.current.engine

    @property
    def session(self) -> MailSession:
        return self.current.session

    @property
    def job(self) -> BackgroundSync:
        return self.current.job

    @property
    def urgent(self) -> UrgentWork:
        return self.current.urgent

    def note_mail(self, account: str, count: int) -> None:
        if count <= 0:
            return
        with self._alert_lock:
            self._alerts[account] = self._alerts.get(account, 0) + count

    def take_alerts(self) -> dict[str, int]:
        with self._alert_lock:
            out = dict(self._alerts)
            self._alerts.clear()
            return out

    def toggle_calendar(self) -> None:
        if self.mode == "calendar":
            self.mode = "mail"
            return
        self.mode = "calendar"
        cal = self.calendar
        cal.notice = ""
        cal.present()


def run_tui(
    data: Path,
    token: str | None = None,
    keys_path=None,
    *,
    download_dir: Path | None = None,
    settings_path=None,
    open_command: tuple[str, ...] = ("xdg-open",),
) -> None:
    locale.setlocale(locale.LC_ALL, "")
    notice = ""
    try:
        keys = load(keys_path or config_path())
    except KeysError as exc:
        keys = defaults()
        notice = str(exc)
    try:
        user = load_settings(settings_path)
    except SettingsError as exc:
        user = None
        notice = str(exc) if not notice else f"{notice}; {exc}"
    if download_dir is not None:
        downloads = Path(download_dir).expanduser()
    elif user is not None and user.download is not None:
        downloads = user.download
    else:
        downloads = default_download()
    data = Path(data)
    accounts = discover(data)
    if user is not None and user.accounts:
        accounts = arrange(accounts, user.accounts)
    if not accounts:
        raise SystemExit("no accounts — run gmail-tui login")
    jobs: list[_Account] = []
    desk: _Desk | None = None
    threads: list[threading.Thread] = []
    token_root = accounts[0].root if token else None
    try:
        # Open mailboxes in parallel: each does its own SQLite label scan and
        # may refresh OAuth. Serial startup felt multi-second with several large accounts.
        jobs = _open_accounts(accounts, token_root, token, notice)
        # Tab badges come from the open sessions, not a second discover scan.
        for tab, slot in zip(accounts, jobs):
            tab.inbox = slot.session.inbox_count
            tab.unread = slot.session.inbox_unread
            email = slot.engine.store.account_email()
            if email:
                tab.name = email
        desk = _Desk(jobs, accounts)
        try:
            supported, cell = probe_terminal()
        except Exception:
            supported, cell = False, (8, 16)
        for slot in jobs:
            slot.session.sixel = supported
            slot.session.sixel_cell = cell
            slot.session.download_dir = downloads
            slot.session.open_command = open_command
        images = _SixelPump(desk)
        global _images
        _images = images
        threads = _start_workers(desk, accounts)
        threads.append(
            threading.Thread(target=images.run, name="gmail-tui-sixel", daemon=True)
        )
        threads[-1].start()
        try:
            curses.wrapper(lambda screen: _loop(screen, desk, accounts, keys))
        finally:
            _mouse_drag_tracking(False)
    finally:
        if desk is not None:
            _halt(desk)
        if _images is not None:
            _images.wake.set()
        for thread in threads:
            thread.join(timeout=_QUIT_JOIN_SECONDS)
        for index, slot in enumerate(jobs):
            worker = threads[index] if index < len(threads) else None
            if worker is not None and worker.is_alive():
                continue
            slot.engine.store.close()


def _gmail_for(root: Path, token_root: Path | None, token: str | None):
    if token and token_root is not None and root == token_root:
        return HttpGmail(token)
    # Startup must not block on a token refresh for every account; sync will
    # refresh on the first 401. Prefer a cached access token even if near expiry.
    access = current_access_token(root, allow_expired=True)
    if not access:
        return NullGmail()

    def refresh(*, force: bool = False) -> str | None:
        return current_access_token(root, force=force)

    return HttpGmail(access, refresh=refresh)


def _open_account(
    account: Account,
    token_root: Path | None,
    token: str | None,
    notice: str,
) -> _Account:
    try:
        gmail = _gmail_for(account.root, token_root, token)
    except OAuthError as exc:
        gmail = NullGmail()
        notice = str(exc)
    slot = _Account(Engine.open(account.root, gmail))
    slot.session.notice = notice
    return slot


def _open_accounts(
    accounts: list[Account],
    token_root: Path | None,
    token: str | None,
    notice: str,
) -> list[_Account]:
    if len(accounts) == 1:
        return [_open_account(accounts[0], token_root, token, notice)]
    jobs: list[_Account | None] = [None] * len(accounts)
    errors: list[BaseException] = []

    def work(index: int, account: Account) -> None:
        try:
            jobs[index] = _open_account(
                account,
                token_root,
                token,
                notice if index == 0 else "",
            )
        except BaseException as exc:  # noqa: BLE001 - surface any open failure
            errors.append(exc)

    threads = [
        threading.Thread(target=work, args=(index, account), daemon=True)
        for index, account in enumerate(accounts)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]
    return [slot for slot in jobs if slot is not None]


def _start_workers(desk: _Desk, accounts: list[Account]) -> list[threading.Thread]:
    threads: list[threading.Thread] = []
    for slot, tab in zip(desk.jobs, accounts):
        thread = threading.Thread(
            target=_account_loop,
            args=(desk, slot, tab),
            name=f"gmail-tui-{tab.name}",
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    return threads


def _account_loop(desk: _Desk, slot: _Account, tab: Account) -> None:
    profile.attach()
    try:
        _account_work(desk, slot, tab)
    finally:
        profile.detach()


def _halt(desk: _Desk) -> None:
    """Stop every account thread without waiting for the Gmail call it is in."""
    desk.stop.set()
    for slot in desk.jobs:
        slot.engine.quota.abort()
        abort = getattr(slot.engine.gmail, "abort", None)
        if callable(abort):
            try:
                abort()
            except Exception:
                pass
    desk.poke.set()


def _account_work(desk: _Desk, slot: _Account, tab: Account) -> None:
    while not desk.stop.is_set():
        profile.checkpoint()
        # Hibernate freezes the NIC; do not hammer Gmail before DHCP returns.
        slot.job.note_clock()
        if _visible(desk, slot):
            try:
                progressed = _sync_visible(desk, slot, tab)
            except SyncStopped:
                return
            except Exception as exc:
                _fail_job(slot, exc)
                progressed = False
            if progressed:
                continue
            if _idle(desk, slot):
                return
            continue
        progressed = False
        try:
            with slot.lock:
                if _visible(desk, slot) or desk.stop.is_set():
                    continue
                launch = slot.urgent.take(slot.session)
            if launch:
                slot.urgent.run()
                progressed = True
                with slot.lock:
                    if not _visible(desk, slot):
                        slot.urgent.apply(slot.session)
                        if slot.urgent.dirty:
                            _remember(desk, tab, slot)
                            slot.urgent.dirty = False
        except SyncStopped:
            return
        except Exception as exc:
            _fail_job(slot, exc)
            progressed = True
        if progressed:
            continue
        if _visible(desk, slot) or desk.stop.is_set():
            continue
        try:
            if _job_due(slot.job):
                slot.job.step()
                checked = slot.job.checked_at
                if checked is not None:
                    tab.checked_at = checked
                if slot.job.dirty or slot.job.mail_alert:
                    _remember(desk, tab, slot)
                progressed = True
        except SyncStopped:
            return
        except Exception as exc:
            _fail_job(slot, exc)
        if not progressed and _idle(desk, slot):
            return


def _fail_job(slot: _Account, exc: Exception) -> None:
    """Surface the error and schedule a retry for this mailbox."""
    slot.session.notice = str(exc)
    if slot.job.mode not in {"offline", "error"}:
        slot.job._stop(exc)


def _sync_visible(desk: _Desk, slot: _Account, tab: Account) -> bool:
    """Gmail calls stay on this thread so a key never waits on them."""
    if slot.urgent.take(slot.session):
        slot.urgent.run()
        return True
    if _job_due(slot.job):
        slot.job.step()
        _queue_mail_alert(desk, tab, slot)
        return True
    return False


def _job_due(job: BackgroundSync) -> bool:
    if job.recover_if_due():
        return True
    return job.running or job.poll_due() or job.cache_due()


def _visible(desk: _Desk, slot: _Account) -> bool:
    return desk.jobs[desk.index] is slot


def _queue_mail_alert(desk: _Desk, tab: Account, slot: _Account) -> None:
    count = slot.job.mail_alert
    if not count:
        return
    slot.job.mail_alert = 0
    name = tab.name or slot.engine.store.account_email() or "account"
    desk.note_mail(name, count)


def _remember(desk: _Desk, tab: Account, slot: _Account) -> None:
    tab.inbox, tab.unread = slot.engine.store.inbox_counts()
    # The open tab's badge is drawn from the session. Switching in clears the
    # job's dirty flag, so without this the badge fell back to older counts.
    slot.session.inbox_count, slot.session.inbox_unread = tab.inbox, tab.unread
    checked = getattr(slot.job, "checked_at", None)
    if checked is not None:
        tab.checked_at = checked
    email = slot.engine.store.account_email()
    if email:
        tab.name = email
    _queue_mail_alert(desk, tab, slot)


def _pull_check_times(accounts: list[Account], desk: _Desk) -> None:
    """Copy each job's last successful check onto its tab for the header."""
    for tab, slot in zip(accounts, desk.jobs):
        checked = getattr(slot.job, "checked_at", None)
        if checked is not None:
            tab.checked_at = checked


def _tab_ages(accounts: list[Account], *, now: float | None = None) -> tuple[str, ...]:
    return tuple(format_check_age(account.checked_at, now=now) for account in accounts)


def _flush_mail_alerts(desk: _Desk, accounts: list[Account]) -> bool:
    """Send desktop notices and reload lists that gained mail. True if painted state changed."""
    counts = desk.take_alerts()
    if not counts:
        return False
    changed = False
    for tab, slot in zip(accounts, desk.jobs):
        if not counts.get(tab.name):
            continue
        slot.job.list_bump = False
        slot.job.dirty = False
        slot.session.refresh_view(pin_new_mail=True)
        changed = True
    notify_new_mail(counts)
    return changed


def _switch(desk: _Desk, accounts: list[Account], index: int) -> None:
    if index == desk.index or not 0 <= index < len(desk.jobs):
        return
    arriving = desk.jobs[index]
    with arriving.lock:
        _remember(desk, accounts[desk.index], desk.jobs[desk.index])
        desk.index = index
        session = arriving.session
        session.sync_phase = arriving.job.phase
        session.sync_done = arriving.job.done
        session.sync_total = arriving.job.total
        session.sync_tick = arriving.job.tick
        arriving.job.dirty = False
        arriving.urgent.dirty = False
        # Each account keeps its own folder, search, and cursors.
        _remember(desk, accounts[index], arriving)


def _loop(
    screen: curses.window,
    desk: _Desk,
    accounts: list[Account],
    keys: KeyMap,
) -> None:
    curses.curs_set(0)
    screen.keypad(True)
    # A short escape delay lets Escape through while the screen is polling.
    # The default one-second wait is longer than that poll, so the key is lost.
    try:
        curses.set_escdelay(25)
    except curses.error:
        pass
    try:
        curses.meta(True)
    except curses.error:
        pass
    _colors()
    try:
        curses.mousemask(_mouse_mask())
        curses.mouseinterval(0)
    except curses.error:
        pass
    # ncurses enables click tracking; ask for motion while the button is held
    # so the reverse-video mark can grow during the drag, not only on release.
    _mouse_drag_tracking(True)
    desk.session.observe(desk.job)
    pending: list[str | int] = []
    # The key just handled. Repeats already waiting are from a hold that the
    # terminal kept sending while a page load or a redraw was busy.
    repeat: str | int | None = None
    dirty = True
    acted = False
    shown_status: tuple | None = None
    shown_ages: tuple[str, ...] | None = None
    header_at = 0.0
    while True:
        session = desk.session
        if session.poll_external_edit():
            dirty = True
        # Only touch a calendar desk that already exists. Creating one here
        # used to refresh every account's calendars before the first mail paint.
        cal = desk._calendar
        if cal is not None and cal.poll_external_edit():
            dirty = True
        if cal is not None and cal.poll_cache_update():
            dirty = True
        job = desk.job
        urgent = desk.urgent
        if session.sixel_ready:
            session.sixel_ready = False
            dirty = True
        if _flush_mail_alerts(desk, accounts):
            dirty = True
        if dirty:
            _pull_check_times(accounts, desk)
            if desk.mode == "calendar":
                _paint_calendar(screen, desk, accounts)
                shown_status = ("calendar", desk.calendar.notice, desk.calendar.layout, desk.calendar.cursor)
            else:
                _paint(screen, session, accounts, desk.index)
                shown_status = _status_stamp(session)
            shown_ages = _tab_ages(accounts)
            header_at = _arm_header_watch(accounts)
            dirty = False
        # Paint the key's frame before waking the account thread. A deferred
        # archive/delete used to start its SQLite write during that paint.
        if acted:
            desk.poke.set()
            acted = False
        if repeat is not None:
            _drop_held_keys(screen, pending, repeat)
            repeat = None
        if pending:
            key = pending.pop(0)
        else:
            screen.timeout(_input_timeout(desk, session, urgent, header_at=header_at))
            try:
                key = screen.get_wch()
            except curses.error:
                profile.checkpoint()
                if desk.mode == "calendar":
                    if cal is not None and cal.poll_cache_update():
                        dirty = True
                    continue
                _pull_check_times(accounts, desk)
                ages = _tab_ages(accounts)
                due = header_at > 0 and time.monotonic() >= header_at
                if desk.mode != "calendar" and (ages != shown_ages or due):
                    if ages != shown_ages:
                        _paint_tabs(screen, session, accounts, desk.index)
                        shown_ages = ages
                    header_at = _arm_header_watch(accounts)
                if _service(session, job, urgent):
                    dirty = True
                elif not dirty:
                    status = _status_stamp(session)
                    if status != shown_status:
                        _paint_status(screen, session)
                        shown_status = status
                continue
        repeat = key if _repeatable_key(session, key) else None
        acted = True
        dirty = True
        if desk.mode == "calendar":
            if _handle_calendar_key(desk, keys, key, accounts):
                break
            continue
        if _is_escape(key):
            if session.help_open:
                session.help_open = False
                continue
            if session.prompt:
                _close_prompt(screen, session)
                session.observe(job)
                continue
            followed = _alt(screen, keys, session)
            if followed:
                session.observe(job)
            elif session.focus == "attachments":
                session.close_attachments()
            elif session.focus == "mime":
                session.close_mime()
            elif session.raw_source:
                session.close_raw_source()
            elif session.external is not None:
                session.forget_external_edit()
            continue
        if key == curses.KEY_MOUSE:
            before = (session.mark_start, session.mark_end, session.marking)
            chosen = _mouse(session, screen)
            if chosen is not None and session.prompt != "send":
                _switch(desk, accounts, chosen)
                continue
            after = (session.mark_start, session.mark_end, session.marking)
            # Drag motion only moves the mark. Rebuild panes once; after that
            # rewrite the reverse-video run so the highlight keeps up.
            if after[2] and before[2]:
                dirty = False
                acted = False
                if before == after:
                    continue
                chosen = _drain_motion(screen, session, pending)
                if chosen is not None and session.prompt != "send":
                    _switch(desk, accounts, chosen)
                    dirty = True
                    continue
                if session.marking:
                    _paint_mark(screen, session, desk.index)
                else:
                    dirty = True
            continue
        if session.mark_start is not None:
            session.clear_mark()
        if session.prompt:
            _edit_prompt(session, key, keys)
            if session.quitting:
                _halt(desk)
                return
            if not session.prompt:
                session.observe(job)
            continue
        action = _action(keys, key)
        code = _code(key)
        if code is not None and keys.has_prefix(code) and not session.prompt and not session.help_open:
            screen.timeout(400)
            try:
                nxt = screen.get_wch()
            except curses.error:
                nxt = None
            if nxt is not None:
                nxt_code = _code(nxt)
                followed = keys.sequence(code, nxt_code) if nxt_code is not None else None
                if followed:
                    action = followed
                else:
                    pending.append(nxt)
        if session.help_open:
            if action in {"help", "quit"}:
                session.help_open = False
            elif action == "down":
                session.help_scroll += 1
            elif action == "up":
                session.help_scroll -= 1
            elif action == "page-down":
                session.help_scroll += 8
            elif action == "page-up":
                session.help_scroll -= 8
            continue
        if action == "calendar":
            desk.toggle_calendar()
            continue
        if action == "help":
            session.help_lines = help_lines(
                session.focus, keys.bindings, filtering=bool(session.search_text)
            )
            session.help_scroll = 0
            session.help_open = True
            continue
        if action == "quit":
            if session.leave_message():
                continue
            if session.clear_search():
                continue
            session.begin_quit()
            continue
        if action == "sync":
            urgent.paused = False
            urgent.phase = ""
            session.urgent_phase = ""
            job.engine.background_bodies = True
            job.check_mail(refresh_auth=True)
            desk.poke.set()
            # Bypass the refresh throttle so a manual check always reloads the list.
            job.dirty = True
            session.observe(job, force=True)
            continue
        if action == "sync-all":
            urgent.paused = False
            urgent.phase = ""
            session.urgent_phase = ""
            for slot in desk.jobs:
                slot.engine.background_bodies = True
                slot.job.check_mail(refresh_auth=True)
            desk.poke.set()
            job.dirty = True
            session.observe(job, force=True)
            continue
        if action == "cache":
            enabled = not job.engine.background_bodies
            for slot in desk.jobs:
                slot.job.set_background_bodies(enabled)
            desk.poke.set()
            session.observe(job)
            continue
        if action == "inbox":
            session.open_inbox()
            continue
        if action == "search":
            session.begin_search()
            continue
        if action == "add-label":
            session.begin_label()
            continue
        if action == "archive":
            session.archive_selected()
            continue
        if action == "delete":
            session.delete_selected()
            continue
        if action == "select":
            session.toggle_selected()
            continue
        if action == "order":
            session.toggle_order()
            continue
        if action == "toggle-unread":
            session.toggle_unread()
            continue
        if action == "undo":
            session.undo()
            continue
        if action == "next-unread":
            session.next_unread()
            continue
        if action == "holding":
            session.begin_holding()
            continue
        if action == "snooze":
            session.begin_snooze()
            continue
        if action == "attachments":
            session.begin_attachments()
            continue
        if action == "mime-parts":
            session.begin_mime()
            continue
        if action == "view-image":
            session.begin_view_image()
            continue
        if action == "raw-source":
            session.toggle_raw_source()
            continue
        if action == "headers":
            session.toggle_headers()
            continue
        if action == "urls":
            session.toggle_urls()
            continue
        if action == "open-url":
            session.open_link()
            continue
        if action == "compose":
            if session.external is not None:
                session.notice = "already writing a message"
            elif session.begin_compose():
                session.begin_external_edit()
            continue
        if action == "reply-all" or action == "reply":
            if session.external is not None:
                session.notice = "already writing a message"
            elif session.begin_reply(everyone=action == "reply-all"):
                session.begin_external_edit()
            continue
        if action == "next-account":
            _switch(desk, accounts, (desk.index + 1) % len(accounts))
            continue
        if action == "prev-account":
            _switch(desk, accounts, (desk.index - 1) % len(accounts))
            continue
        if action == "next-pane":
            session.cycle(False)
        elif action == "prev-pane":
            session.cycle(True)
        elif action == "down":
            session.move(1)
        elif action == "up":
            session.move(-1)
        elif action == "page-down":
            session.page(1)
        elif action == "page-up":
            session.page(-1)
        elif action == "top":
            session.jump(False)
        elif action == "bottom":
            session.jump(True)
        elif action == "left":
            session.horizontal(False)
        elif action == "right":
            session.horizontal(True)
        elif action == "open":
            session.fold(True)
        elif action == "close":
            session.fold(False)
        elif action == "toggle-fold":
            session.fold(None)
        elif action == "activate":
            session.activate()
        elif key == curses.KEY_RESIZE:
            continue


# A background download changes a counter, not the message on screen.
# Waking more often than this only redraws that counter.
_PROGRESS_WAIT_MS = 1000
# While a single Gmail call is open the counter does not move. Pulse the
# bar a few times a second so the status line still looks alive.
_BUSY_WAIT_MS = 250
# A finished open or label push should show up quickly. The key itself
# already moved the screen.
_URGENT_WAIT_MS = 200
# Account-tab check ages tick once a second; a full paint resets this.
_HEADER_WAIT_MS = 1000
# After quit, give a stuck download this long to notice the closed socket.
_QUIT_JOIN_SECONDS = 2.0


def _input_timeout(
    desk: _Desk,
    session: MailSession,
    urgent: UrgentWork,
    *,
    header_at: float = 0.0,
) -> int:
    if session.external is not None or session.marking:
        return _BUSY_WAIT_MS
    if session.sixel_pending or session.sixel_ready:
        return _URGENT_WAIT_MS
    if urgent.has_work(session):
        return _URGENT_WAIT_MS
    # A throttled observe() leaves dirty set; do not sleep until the next poll.
    if desk.job.dirty or desk.job.list_bump:
        return _BUSY_WAIT_MS
    if desk._calendar is not None and desk._calendar.cache_busy():
        return _BUSY_WAIT_MS
    if any(slot.job.running for slot in desk.jobs):
        if session.sync_total <= 0:
            return _BUSY_WAIT_MS
        return _header_cap(_PROGRESS_WAIT_MS, header_at)
    return _header_cap(_poll_timeout(desk), header_at)


def _header_cap(wait: int, header_at: float) -> int:
    """Wake by the header watchdog so check ages keep ticking."""
    if header_at <= 0:
        return wait
    due = max(1, int((header_at - time.monotonic()) * 1000))
    if wait < 0:
        return due
    return min(wait, due)


def _arm_header_watch(accounts: list[Account]) -> float:
    """Next time the tab row should refresh. Zero when no check age is shown."""
    if any(account.checked_at is not None for account in accounts):
        return time.monotonic() + _HEADER_WAIT_MS / 1000.0
    return 0.0


def _needs_attention(slot: _Account) -> bool:
    return _job_due(slot.job) or slot.urgent.has_work(slot.session)


def _idle_delay(slot: _Account) -> float:
    wait = slot.job.seconds_until_poll()
    if wait is None:
        return 60.0
    return max(0.0, wait)


def _idle(desk: _Desk, slot: _Account) -> bool:
    """Sleep until this account has work. True when the program is stopping."""
    if desk.stop.is_set():
        return True
    deadline = time.monotonic() + _idle_delay(slot)
    while not desk.stop.is_set():
        if _needs_attention(slot):
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if not desk.poke.wait(remaining):
            return desk.stop.is_set()
        desk.poke.clear()
    return True


def _poll_timeout(desk: _Desk) -> int:
    waits = [
        wait
        for slot in desk.jobs
        if (wait := slot.job.seconds_until_poll()) is not None
    ]
    if not waits:
        return -1
    return max(1, int(min(waits) * 1000))


def _status_stamp(session: MailSession) -> tuple:
    """The cache counter. It can change without the open message changing."""
    pulse = 0
    if session.sync_total <= 0 and session.sync_phase not in {"", "idle"}:
        pulse = int(time.monotonic() * 4)
    return (
        session.sync_phase,
        session.sync_done,
        session.sync_total,
        session.notice,
        session.urgent_phase,
        pulse,
    )


def _service(session: MailSession, job: BackgroundSync, urgent: UrgentWork) -> bool:
    """Record finished Gmail work. The call itself runs on the account thread.

    The result is true when the screen should be drawn again.
    """
    changed = False
    if session.labels_stale and not session.prompt:
        session.labels_stale = False
        session.reload_labels()
        changed = True
    applied = urgent.apply(session)
    session.urgent_phase = urgent.phase if urgent.paused or urgent.phase else ""
    if applied and urgent.dirty:
        urgent.dirty = False
        session.refresh_view()
        return True
    before = (session.notice, session.urgent_phase)
    was_dirty = job.dirty
    reader_ready = session.observe(job)
    after = (session.notice, session.urgent_phase)
    # observe() may leave dirty set while it throttles list refreshes. That
    # counter belongs on the status line; wiping the panes clears sixel.
    # A body that just arrived paints the reader without that list rebuild.
    refreshed = was_dirty and not job.dirty
    return changed or bool(applied) or refreshed or reader_ready or before != after


def _paint_status(screen: curses.window, session: MailSession) -> None:
    """Rewrite the bottom line. The messages above it stay as they are."""
    _write_status(screen, session)
    try:
        screen.refresh()
    except curses.error:
        return


def _paint_tabs(
    screen: curses.window,
    session: MailSession,
    accounts: list[Account],
    active: int,
) -> None:
    """Rewrite the account tab row. Panes underneath stay as they are."""
    height, width = screen.getmaxyx()
    if height < 2 or width < 2:
        return
    line = render_account_tabs(session, accounts, active, width)
    if session.painted:
        painted = list(session.painted)
        painted[0] = line
        session.painted = painted
    try:
        screen.move(0, 0)
        screen.clrtoeol()
    except curses.error:
        return
    _add(screen, 0, 0, line, 0)
    _highlight_tab(screen, session, active)
    try:
        screen.refresh()
    except curses.error:
        return


def _write_status(screen: curses.window, session: MailSession) -> None:
    height, width = screen.getmaxyx()
    if height < 1 or width < 2:
        return
    _add(screen, height - 1, 0, status_bar(session, width), curses.A_REVERSE)


def _paint_calendar(screen: curses.window, desk: _Desk, accounts: list[Account]) -> None:
    height, width = screen.getmaxyx()
    if height < 9 or width < 40:
        screen.erase()
        screen.addnstr(0, 0, "Terminal is too small for the calendar.", max(0, width - 1))
        screen.refresh()
        return
    lines = render_calendar(desk.calendar, height, width, accounts, desk.index)
    screen.erase()
    for y, line in enumerate(lines):
        if y >= height or y == height - 1:
            continue
        _add(screen, y, 0, line, 0)
    _add(screen, height - 1, 0, status_calendar(desk.calendar, width), curses.A_REVERSE)
    _refresh(screen)


def _handle_calendar_key(
    desk: _Desk,
    keys: KeyMap,
    key: str | int,
    accounts: list[Account],
) -> bool:
    """Handle one key in calendar mode. True means quit the TUI."""
    cal = desk.calendar
    if _is_escape(key):
        if cal.help_open:
            cal.help_open = False
            return False
        if cal.prompt:
            cal.prompt = None
            return False
        if cal.external is not None:
            cal.forget_external_edit()
            return False
        desk.mode = "mail"
        return False
    if cal.prompt == "delete-event":
        typed = _printable(key)
        action = _action(keys, key)
        if (typed and typed.lower() == "y") or action == "activate":
            cal.confirm_delete()
        elif (typed and typed.lower() == "n") or action == "quit":
            cal.prompt = None
            cal.notice = "not deleted"
        return False
    action = _action(keys, key)
    if action == "calendar":
        desk.toggle_calendar()
        return False
    if action == "quit":
        if cal.help_open:
            cal.help_open = False
            return False
        desk.mode = "mail"
        return False
    if action == "help" or key == "?":
        cal.help_open = not cal.help_open
        return False
    if cal.help_open:
        return False
    if action == "next-account":
        _switch(desk, accounts, (desk.index + 1) % len(accounts))
        return False
    if action == "prev-account":
        _switch(desk, accounts, (desk.index - 1) % len(accounts))
        return False
    if action == "next-pane":
        cal.focus = "view"
        return False
    if action == "prev-pane":
        cal.focus = "cals"
        return False
    if action == "down":
        cal.move(1)
        return False
    if action == "up":
        cal.move(-1)
        return False
    if action == "left":
        cal.horizontal(False)
        return False
    if action == "right":
        cal.horizontal(True)
        return False
    if action == "activate":
        cal.activate()
        return False
    if action == "open":
        cal.toggle_fold()
        return False
    if action == "close":
        node = cal.current_cal()
        if node and node.expandable and node.expanded:
            cal.toggle_fold()
        return False
    if action == "toggle-fold":
        cal.toggle_fold()
        return False
    if action == "compose":
        cal.begin_create()
        return False
    if action == "reply" or action == "reply-all":
        cal.begin_edit()
        return False
    if action == "delete":
        cal.begin_delete()
        return False
    if action == "inbox" or key in ("t", "T"):
        cal.jump_today()
        return False
    if key in ("D",):
        cal.set_layout("day")
        return False
    if key in ("W",):
        cal.set_layout("week")
        return False
    if key in ("M",):
        cal.set_layout("month")
        return False
    if key in ("n",):
        cal.shift_range(1)
        return False
    if key in ("p", "P"):
        cal.shift_range(-1)
        return False
    if key in ("e", "E"):
        cal.begin_edit()
        return False
    if action == "page-down":
        cal.shift_range(1)
        return False
    if action == "page-up":
        cal.shift_range(-1)
        return False
    if key == curses.KEY_RESIZE:
        return False
    return False


def _paint(screen: curses.window, session: MailSession, accounts: list[Account], active: int) -> None:
    height, width = screen.getmaxyx()
    if height < 9 or width < 40:
        screen.erase()
        session.painted = []
        screen.addnstr(0, 0, "Terminal is too small for the three panes.", max(0, width - 1))
        screen.refresh()
        return
    lines = render_lines(session, height, width, accounts, active)
    session.painted = lines
    geo = geometry(height, width)
    screen.erase()
    for y, line in enumerate(lines):
        if y >= height or y == height - 1:
            continue
        _add(screen, y, 0, line, _line_attr(session, geo, y))
    # The canvas drops the spaces that carry the highlight to the right edge.
    _write_status(screen, session)
    _highlight_tab(screen, session, active)
    _highlight_pane(screen, session, geo)
    _highlight_selection(screen, session, geo)
    _highlight_mark(screen, session)
    session.mark_rows = _mark_rows(session)
    _highlight_find(screen, session, geo)
    # Sixel and OSC-8 work fight the drag highlight and are expensive mid-drag.
    if session.marking:
        _refresh(screen, soft=True)
        return
    _refresh(screen)
    _emit_hyperlinks(session, geo)
    _request_sixel(session, geo)
    _emit_sixel(session, geo)


def _paint_mark(screen: curses.window, session: MailSession, active: int) -> None:
    """Update only the drag highlight. The panes underneath stay as they are."""
    height, width = screen.getmaxyx()
    if height < 2 or width < 2 or not session.painted:
        return
    geo = geometry(height, width)
    rows = _mark_rows(session)
    covered = [span for span in (rows, session.mark_rows) if span is not None]
    if covered:
        # Only rows under the old or new mark change. Measuring every row of
        # the screen on each motion event let a drag fall behind the mouse.
        first = max(0, min(span[0] for span in covered))
        last = min(len(session.painted), height - 1, max(span[1] for span in covered) + 1)
        for y in range(first, last):
            _add(screen, y, 0, session.painted[y], _line_attr(session, geo, y))
    _highlight_tab(screen, session, active)
    _highlight_pane(screen, session, geo)
    _highlight_selection(screen, session, geo)
    _highlight_mark(screen, session)
    _highlight_find(screen, session, geo)
    session.mark_rows = rows
    _refresh(screen, soft=True)


def _mark_rows(session: MailSession) -> tuple[int, int] | None:
    """First and last screen row of the drag mark, inclusive."""
    if session.mark_start is None or session.mark_end is None:
        return None
    top = min(session.mark_start[0], session.mark_end[0])
    return top, max(session.mark_start[0], session.mark_end[0])


def _drain_motion(screen: curses.window, session: MailSession, pending: list[str | int]) -> int | None:
    """Apply mouse events already queued behind this one so a drag paints once.

    Returns an account tab index when a queued release turned into a tab click.
    """
    screen.timeout(0)
    while session.marking and not pending:
        try:
            key = screen.get_wch()
        except curses.error:
            return None
        if key != curses.KEY_MOUSE:
            pending.append(key)
            return None
        chosen = _mouse(session, screen)
        if chosen is not None:
            return chosen
    return None


def _refresh(screen: curses.window, *, soft: bool = False) -> None:
    """Redraw every cell, and don't let a wide glyph wrap onto the next row.

    A glyph the terminal draws wider than the cell count spills the rest of
    the line, including the selection bar, onto the rows below. Those rows
    are not always written again, so the spill stays behind as a highlight.
    Soft refresh skips redrawwin so a drag mark can keep up with the mouse.
    """
    if not soft:
        screen.redrawwin()
    curses.putp(b"\x1b[?7l")
    try:
        screen.refresh()
    finally:
        curses.putp(b"\x1b[?7h")


def _emit_hyperlinks(session: MailSession, geo) -> None:
    """Open the word link in front of a URL. The address itself stays plain text.

    A custom ``open`` command launches that program on click instead, so the
    terminal's own opener does not also run.
    """
    if not session.terminal_opens_links():
        return
    if session.popup:
        return
    regions = reader_hyperlinks(
        session.reader_view,
        session.reader_marks,
        session.reader_scroll,
        geo.list_h + 1,
        1,
        geo.reader_rows,
        geo.width,
    )
    if not regions:
        return
    cup = curses.tigetstr("cup")
    if not cup:
        return
    plain = curses.tigetstr("sgr0") or b"\x1b[0m"
    out = bytearray(b"\x1b[?7l")
    out += plain
    for y, x, text, url in regions:
        out += curses.tparm(cup, y, x)
        out += osc8(text, url).encode()
    out += plain
    out += curses.tparm(cup, geo.height - 1, 0)
    out += b"\x1b[?7h"
    try:
        os.write(sys.stdout.fileno(), bytes(out))
    except OSError:
        return
    curses.setsyx(geo.height - 1, 0)


def _request_sixel(session: MailSession, geo) -> None:
    if _images is None or not session.sixel or not session.messages or not session.image_slots:
        return
    gmail_id = session.messages[session.message_cursor].gmail_id
    indexes = tuple(index for _line, index in session.image_slots)
    cell_w, cell_h = session.sixel_cell
    rows = max(1, session.image_rows)
    # Leave one cell of slack so a slightly tall raster cannot cover the status line.
    pixels = (
        max(8, (geo.width - 2) * cell_w - cell_w),
        max(6, rows * cell_h - cell_h),
    )
    session.sixel_pixels = pixels
    preload = ()
    if session.focus == "attachments":
        preload = session.bitmap_indexes()
    _images.ask(session, gmail_id, indexes, pixels, preload=preload)


def _emit_sixel(session: MailSession, geo) -> None:
    """Draw the picture inside the reader. Sixel scrolling keeps it off the folders."""
    # The save dialog stays on top of the text. The picture under the cursor still shows.
    if session.popup and session.prompt != "save":
        return
    if not session.sixel or not session.messages or not session.image_slots:
        return
    gmail_id = session.messages[session.message_cursor].gmail_id
    frames = {
        index: payload
        for gid, index, payload in session.sixel_frames
        if gid == gmail_id
    }
    if not frames:
        return
    chosen = None
    if session.prompt == "save" and session.attachments:
        chosen = session.attachments[session.attachment_cursor].index
    cup = curses.tigetstr("cup")
    if not cup:
        return
    # Private mode 80 set draws the picture at the top-left and covers the folders.
    out = bytearray(b"\x1b[?80l")
    drawn = False
    bottom = geo.height - 1
    for line, index in session.image_slots:
        if chosen is not None and index != chosen:
            continue
        payload = frames.get(index)
        if payload is None or line >= len(session._reader_starts):
            continue
        view = session._reader_starts[line] - session.reader_scroll
        row = geo.list_h + 1 + view
        # The whole band must sit in the reader. A partial draw ran into the panes.
        if row <= geo.list_h or row + session.image_rows > bottom:
            continue
        out += curses.tparm(cup, row, 1)
        out += payload
        drawn = True
    if not drawn:
        return
    out += curses.tparm(cup, geo.height - 1, 0)
    try:
        os.write(sys.stdout.fileno(), bytes(out))
    except OSError:
        return
    curses.setsyx(geo.height - 1, 0)


def _highlight_tab(screen: curses.window, session: MailSession, active: int) -> None:
    for start, end, index in session.account_spans:
        if index != active:
            continue
        _chgat(screen, 0, start, end - start, curses.A_REVERSE, session.popup)


def _highlight_pane(screen: curses.window, session: MailSession, geo) -> None:
    """Invert the header of the pane that has the keyboard."""
    place = _pane_highlight(session.focus, geo)
    if place is None:
        return
    row, column, span = place
    _chgat(screen, row, column, span, curses.A_REVERSE | curses.A_BOLD, session.popup)


def _pane_highlight(focus: str, geo) -> tuple[int, int, int] | None:
    """Active title span. The box corners on either end stay as they are."""
    split = geo.sidebar_w - 1
    width = geo.width
    if focus == "labels":
        return geo.top, 1, max(0, geo.sidebar_w - 2)
    if focus == "list":
        return geo.top, split + 1, max(0, width - split - 2)
    if focus in {"reader", "attachments", "mime"}:
        return geo.list_h, 1, max(0, width - 2)
    return None


def _highlight_selection(screen: curses.window, session: MailSession, geo) -> None:
    if session.visible:
        row = geo.top + 1 + session.label_cursor - session.label_scroll
        if geo.top + 1 <= row < geo.list_h:
            _chgat(
                screen,
                row,
                1,
                geo.sidebar_w - 2,
                _open_folder_attr(session.focus),
                session.popup,
            )
    if session.messages:
        row = geo.top + 3 + session.message_cursor - session.message_scroll
        selected_attr = _open_message_attr(session.focus)
        if geo.top + 3 <= row < geo.list_h:
            left = geo.sidebar_w
            width = geo.width - geo.sidebar_w - 1
            _chgat(screen, row, left, width, selected_attr, session.popup)


def _open_folder_attr(focus: str) -> int:
    """How the open folder looks in the label tree.

    Bold keeps the current folder visible while reading mail. Reverse marks it
    when the label pane itself has focus.
    """
    if focus == "labels":
        return curses.A_REVERSE | curses.A_BOLD
    return curses.A_BOLD


def _open_message_attr(focus: str) -> int:
    """How the open row looks in the mail list.

    The list pane uses a strong bar. The message pane keeps a bar too, so the
    open mail stays visible while it is being read.
    """
    if focus == "list":
        return curses.A_REVERSE | curses.A_BOLD
    if focus in {"reader", "attachments", "mime"}:
        return curses.A_REVERSE
    return curses.A_BOLD


def _highlight_find(screen: curses.window, session: MailSession, geo) -> None:
    width = geo.width - 2
    place = reader_match(session, width)
    if place is None:
        return
    view_row, column, text = place
    row = geo.list_h + 1 + view_row - session.reader_scroll
    if row <= geo.list_h or row >= geo.height - 1:
        return
    if _popup_covers(session.popup, row, 1 + column, len(text)):
        return
    _add(screen, row, 1 + column, text, curses.A_REVERSE)


def _repeatable_key(session: MailSession, key: str | int) -> bool:
    """Held keys may repeat. Typed text in a prompt has to keep every character."""
    if key == curses.KEY_MOUSE:
        return False
    if session.prompt and isinstance(key, str) and key.isprintable():
        return False
    return True


def _drop_held_keys(screen: curses.window, pending: list[str | int], key: str | int) -> None:
    """Forget copies of ``key`` that arrived before this call.

    A held key fills the terminal queue while the last press is still working.
    Playing that queue back after release runs the command again, and ``x``
    then flips the same message off and on. A different key stops the drop.
    """
    while pending and pending[0] == key:
        pending.pop(0)
    screen.timeout(0)
    try:
        while True:
            nxt = screen.get_wch()
            if nxt != key:
                pending.append(nxt)
                return
    except curses.error:
        return


def _is_escape(key: str | int) -> bool:
    return key in (27, "\x1b")


def _close_prompt(screen: curses.window, session: MailSession) -> None:
    """Cancel the open dialog. A sequence that follows Escape is discarded."""
    screen.timeout(25)
    try:
        nxt = screen.get_wch()
    except curses.error:
        nxt = None
    if nxt == "[":
        _csi_alt_letter(screen)
    session.prompt_cancel()


def _alt(screen: curses.window, keys: KeyMap, session: MailSession) -> bool:
    screen.timeout(150)
    try:
        nxt = screen.get_wch()
    except curses.error:
        nxt = None
    if nxt is None:
        if session.prompt:
            session.prompt_cancel()
            return True
        return False
    if nxt == "[":
        letter = _csi_alt_letter(screen)
        action = keys.action(ord(letter) | ALT) if letter else None
    else:
        action = _alt_action(keys, _code(nxt))
    if session.prompt or not action:
        return False
    if action == "add-label":
        session.begin_label()
    elif action == "search":
        session.begin_search()
    return False


def _alt_action(keys: KeyMap, code: int | None) -> str | None:
    if code is None:
        return None
    named = keys.action(code | ALT)
    if named:
        return named
    if 128 <= code <= 255 and chr(code & 0x7F).isalpha():
        return keys.action((code & 0x7F) | ALT)
    return None


def _csi_alt_letter(screen: curses.window) -> str | None:
    """Kitty and xterm encode Alt+letter as an escape sequence after ``[``."""
    screen.timeout(20)
    chars: list[str] = []
    for _ in range(24):
        try:
            part = screen.get_wch()
        except curses.error:
            return None
        if not isinstance(part, str) or len(part) != 1:
            return None
        chars.append(part)
        if "@" <= part <= "~":
            break
    else:
        return None
    body = "".join(chars)
    if body.endswith("u") and ";" in body:
        code_text, modifier = body[:-1].split(";", 1)
        if modifier.split(";")[0] in {"3", "4", "7", "8"}:
            return _codepoint_char(code_text)
    if body.endswith("~") and body.startswith("27;"):
        bits = body[:-1].split(";")
        if len(bits) >= 3 and bits[1] in {"3", "4", "7", "8"}:
            return _codepoint_char(bits[2])
    return None


def _codepoint_char(text: str) -> str | None:
    try:
        code = int(text)
    except ValueError:
        return None
    if 32 <= code < 127:
        return chr(code)
    return None


def _edit_prompt(session: MailSession, key: str | int, keys: KeyMap) -> None:
    if key in (curses.KEY_BACKSPACE, 127, 8, "\b", "\x7f"):
        session.prompt_backspace()
        return
    if key == curses.KEY_UP:
        session.prompt_move(-1)
        return
    if key == curses.KEY_DOWN:
        session.prompt_move(1)
        return
    action = _action(keys, key)
    if session.prompt == "save" and (
        key in ("\t",) or key == curses.KEY_BTAB or action in {"next-pane", "prev-pane"}
    ):
        session.prompt_complete(reverse=key == curses.KEY_BTAB or action == "prev-pane")
        return
    if session.prompt == "save" and (key == "?" or action == "help"):
        session.prompt_list_folders()
        return
    if action == "activate":
        session.prompt_accept()
        return
    if isinstance(key, str) and key.isprintable():
        session.prompt_type(key)


def _action(keys: KeyMap, key: str | int) -> str | None:
    code = _code(key)
    if code is None:
        return None
    meta = _alt_action(keys, code) if 128 <= code <= 255 else None
    if meta:
        return meta
    return keys.action(code)


def _code(key: str | int) -> int | None:
    if isinstance(key, str):
        return ord(key) if len(key) == 1 else None
    return key


def _line_attr(session: MailSession, geo, y: int) -> int:
    if y == geo.height - 1:
        return curses.A_REVERSE
    if y == geo.top or y == geo.list_h:
        return curses.A_BOLD
    return curses.A_NORMAL


def _popup_covers(
    popup: tuple[int, int, int, int] | None, y: int, x: int, span: int
) -> bool:
    """True when the whole span sits under a dialog."""
    if popup is None or span <= 0:
        return False
    top, left, height, width = popup
    return top <= y < top + height and x >= left and x + span <= left + width


def _chgat_segments(
    y: int,
    x: int,
    span: int,
    popup: tuple[int, int, int, int] | None,
) -> list[tuple[int, int]]:
    """Attribute runs that stay outside a popup on this row."""
    if span <= 0:
        return []
    if popup is None:
        return [(x, span)]
    top, left, pop_h, pop_w = popup
    if not (top <= y < top + pop_h):
        return [(x, span)]
    right = left + pop_w
    end = x + span
    if end <= left or x >= right:
        return [(x, span)]
    parts: list[tuple[int, int]] = []
    if x < left:
        parts.append((x, left - x))
    if end > right:
        parts.append((right, end - right))
    return parts


def _chgat(
    screen: curses.window,
    y: int,
    x: int,
    span: int,
    attr: int,
    popup: tuple[int, int, int, int] | None = None,
) -> None:
    """Paint attributes across columns without reading the text back.

    Reading the row and writing it again stops early on multibyte characters,
    so the tail of the line, including the attachment mark, stays unhighlighted.
    A popup dialog stays in front: highlight bars stop at its edges.
    """
    height, width = screen.getmaxyx()
    if span <= 0 or y < 0 or y >= height or x >= width:
        return
    span = min(span, width - x)
    if y == height - 1 and x + span >= width:
        span = width - x - 1
    if span <= 0:
        return
    for start, length in _chgat_segments(y, x, span, popup):
        if length <= 0:
            continue
        try:
            screen.chgat(y, start, length, attr)
        except curses.error:
            pass


def _add(screen: curses.window, y: int, x: int, text: str, attr: int) -> None:
    height, width = screen.getmaxyx()
    if y < 0 or y >= height or x >= width or not text:
        return
    limit = width - x
    if y == height - 1:
        limit -= 1
    if limit <= 0:
        return
    shown = text if _display_width(text) <= limit else _clip(text, limit)
    # n counts characters. A column limit drops the tail of a row that also
    # holds zero-width characters, which is where the scrollbar sits.
    try:
        screen.addnstr(y, x, shown, len(shown.encode()), attr)
    except curses.error:
        pass


def _mouse_mask() -> int:
    mask = getattr(curses, "ALL_MOUSE_EVENTS", curses.BUTTON1_CLICKED)
    mask |= getattr(curses, "REPORT_MOUSE_POSITION", 0)
    return mask


def _mouse_drag_tracking(enabled: bool) -> None:
    """Ask xterm-style terminals for motion while button 1 is held (mode 1002)."""
    if enabled:
        sys.stdout.write("\033[?1000h\033[?1002h")
    else:
        sys.stdout.write("\033[?1002l\033[?1000l")
    try:
        sys.stdout.flush()
    except OSError:
        pass


def _point_in_popup(popup: tuple[int, int, int, int] | None, y: int, x: int) -> bool:
    if popup is None:
        return False
    top, left, height, width = popup
    return top <= y < top + height and left <= x < left + width


def _highlight_mark(screen: curses.window, session: MailSession) -> None:
    if session.mark_start is None or session.mark_end is None:
        return
    if session.mark_start == session.mark_end:
        return
    for y, x, span in mark_spans(session.mark_start, session.mark_end):
        _chgat(screen, y, x, span, curses.A_REVERSE, session.popup)


def _copy_text(text: str) -> None:
    """Hand the selection to the terminal and, when one is installed, the clipboard."""
    payload = base64.b64encode(text.encode()).decode("ascii")
    sys.stdout.write(f"\033]52;c;{payload}\a")
    try:
        sys.stdout.flush()
    except OSError:
        pass
    for argv in (
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
    ):
        if shutil.which(argv[0]) is None:
            continue
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            continue
        if proc.stdin is None:
            continue
        try:
            proc.stdin.write(text.encode())
            proc.stdin.close()
        except OSError:
            pass
        return


def _mouse(session: MailSession, screen: curses.window) -> int | None:
    try:
        _, x, y, _, state = curses.getmouse()
    except curses.error:
        return None
    released = getattr(curses, "BUTTON1_RELEASED", 0)
    pressed = getattr(curses, "BUTTON1_PRESSED", 0)
    clicked = curses.BUTTON1_CLICKED
    modal = session.prompt == "send"
    inside = _point_in_popup(session.popup, y, x)
    # Prefer RELEASED for the send dialog. Many terminals also emit CLICKED for
    # the same press; handling both used to confirm Send twice before the
    # one-shot lock existed, and still races with a slow send on some setups.
    popup_click = bool(released and (state & released)) or (
        not released and bool(state & clicked)
    )
    if state & (released | clicked):
        dragged = session.mark_start is not None and session.mark_start != (y, x)
        if dragged:
            if session.marking:
                text = session.finish_mark(y, x)
                if text.strip():
                    _copy_text(text)
                    session.notice = "copied"
            return None
        session.clear_mark()
        if modal:
            if popup_click and inside:
                session.click_popup_choice(y)
            return None
        return _mouse_click(session, screen, x, y)
    # Motion while held (1002) or a pressed event with a new cell: grow the mark.
    if session.marking and not inside:
        session.mark_drag(y, x)
        return None
    if state & pressed and not inside:
        session.mark_press(y, x)
    return None


def _mouse_click(session: MailSession, screen: curses.window, x: int, y: int) -> int | None:
    height, width = screen.getmaxyx()
    geo = geometry(height, width)
    if y < geo.top:
        for start, end, index in session.account_spans:
            if start <= x < end:
                return index
        return None
    if y <= geo.top or y == geo.list_h or y >= geo.height - 2:
        return None
    if y > geo.list_h:
        if (
            not session.terminal_opens_links()
            and session.prompt is None
            and not session.help_open
            and session.popup is None
        ):
            url = link_at(
                session.reader_view,
                session.reader_marks,
                session.reader_scroll,
                y,
                x,
                top=geo.list_h + 1,
                pane_left=1,
                rows=geo.reader_rows,
            )
            if url:
                session.focus = "reader"
                # Press and release are separate events for one click.
                now = time.monotonic()
                stamp = (y, x, url)
                if session._mouse_link != stamp or now - session._mouse_link_at > 0.4:
                    session._mouse_link = stamp
                    session._mouse_link_at = now
                    session.open_url(url)
                return None
        session.focus = "reader"
        return None
    if x < geo.sidebar_w:
        session.focus = "labels"
        row = y - (geo.top + 1) + session.label_scroll
        if 0 <= row < len(session.visible):
            session.label_cursor = row
            session.load_messages(fetch=True)
        return None
    session.focus = "list"
    row = y - (geo.top + 3) + session.message_scroll
    if 0 <= row < len(session.messages):
        session.message_cursor = row
        session.load_reader(fetch=True)
    return None


def _colors() -> None:
    try:
        curses.start_color()
        curses.use_default_colors()
    except curses.error:
        return

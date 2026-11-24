"""Three-pane mailbox state and a plain-text rendering of that screen.

The left pane is the label tree. The top-right pane lists conversations.
A thread with more than one saved message is one row, marked with a count.
``zo`` opens its tree, ``zc`` closes it, and ``za`` toggles it. Messages saved
under other labels stay in that tree. The bottom-right pane is the open message. Moving the
list selection opens the message. A tab row above the frame names each
account, with its unread count, the inbox count in brackets, and how long
ago the last successful Gmail check finished. ``x``
marks messages so archive, delete, and label changes apply to every marked row.
"""

from __future__ import annotations

import curses
import os
import shlex
import sqlite3
import subprocess
import tempfile
import time
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path

from gmail_tui.accounts import Account
from gmail_tui.gmail import GmailError, Offline
from gmail_tui.labels import FlatLabel, TreeNode, build_label_tree, expandable_keys, flatten
from gmail_tui.links import LABEL, jump_labels, link_spans, present_links, strip_link_urls
from gmail_tui.search import parse_query
from gmail_tui.snooze import SnoozeChoice, parse_duration, presets as snooze_presets
from gmail_tui.store import HeldChange, MessagePreview, MessageSummary, ThreadCursor
from gmail_tui.sixel import IMAGE_LIMIT, band_rows, image_band, is_bitmap
from gmail_tui.textutil import (
    Attachment,
    MimePart,
    header_fields,
    preferred_mime_index,
    reserve_download,
    safe_filename,
)
from gmail_tui.threads import arrange, bare_subject
from gmail_tui.sync import Engine, message_token
from gmail_tui.terminal import launch_editor

PAGE = 100
_COMPOSE_CHOICES = ("Send", "Save as draft")
# One row. A larger jump moves the highlight to a different line than the mail
# the key just selected.
SCROLL_JUMP = 1
# Keep this many rows past the highlight while more mail exists beyond the
# viewport, so the list does not pin to the first or last screen line.
SCROLL_MARGIN = 5
_SAVE_CHOICES = ("download", "open")


@dataclass(frozen=True)
class LabelChoice:
    title: str
    gmail_id: str | None
    create_name: str | None = None
    remove: bool = False


@dataclass
class Geometry:
    height: int
    width: int
    sidebar_w: int
    list_h: int
    label_rows: int
    message_rows: int
    reader_rows: int
    top: int = 1


@dataclass(frozen=True)
class ReaderMark:
    """``link`` words on one reader row, as ``(column, url)``."""

    links: tuple[tuple[int, str], ...] = ()


@dataclass(frozen=True)
class UrlHint:
    """One visible link during pick mode: screen cell, destination, jump label."""

    y: int
    x: int
    url: str
    label: str


class _ExternalEdit:
    """A compose buffer open in another terminal."""

    def __init__(
        self,
        path: Path,
        sentinel: Path,
        process: object,
        thread: str,
        reply_to: str,
    ) -> None:
        self.path = path
        self.sentinel = sentinel
        self.process = process
        self.thread = thread
        self.reply_to = reply_to


class MailSession:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.focus = "labels"
        self.label_cursor = 0
        self.message_cursor = 0
        self.label_scroll = 0
        self.message_scroll = 0
        self.reader_scroll = 0
        # After a no-op scroll at the reader edge, a second press the same way
        # leaves for the message list (j/down at the bottom, k/up at the top).
        self._reader_edge: int | None = None
        self.sync_phase = "idle"
        self.sync_done = 1
        self.sync_total = 1
        self.sync_tick = 0
        self.notice = ""
        self.quitting = False
        self.download_dir = Path.home() / "Downloads"
        # Links and HTML parts. Attachments stay on xdg-open.
        self.open_command: tuple[str, ...] = ("xdg-open",)
        self._link_key: tuple[str, int] | None = None
        self._link_pick: int | None = None
        # Jump labels over the visible link words. None until pick-url starts.
        self.url_hints: tuple[UrlHint, ...] | None = None
        self.url_typed = ""
        # Last link a click opened, so the matching release does not launch twice.
        self._mouse_link: tuple[int, int, str] | None = None
        self._mouse_link_at = 0.0
        self.prompt: str | None = None
        self.prompt_text = ""
        self.prompt_cursor = 0
        self.prompt_hint = ""
        self.compose_to = ""
        self.compose_cc = ""
        self.compose_bcc = ""
        self.compose_subject = ""
        self.compose_lines: list[str] = []
        self.compose_thread = ""
        self.compose_reply_to = ""
        self._compose_contacts: tuple[str, ...] = ()
        self._compose_matches: list[str] = []
        self._compose_match_i = -1
        self._sending = False
        # Bodies already handed to Gmail (or ambiguously interrupted mid-send).
        self._sent_fingerprints = self.engine.store.sent_fingerprints()
        self.external: _ExternalEdit | None = None
        self.mark_start: tuple[int, int] | None = None
        self.mark_end: tuple[int, int] | None = None
        self.marking = False
        # Rows the drag mark covered at the last paint, so the next one can erase it.
        self.mark_rows: tuple[int, int] | None = None
        self.painted: list[str] = []
        self.popup_choices: tuple[int, ...] = ()
        self.held: list[HeldChange] = []
        self.attachments: tuple[Attachment, ...] = ()
        self.attachment_cursor = 0
        self.mime_parts: tuple[MimePart, ...] = ()
        self.mime_cursor = 0
        self._mime_gmail_id = ""
        self.full_headers = False
        self.raw_source = False
        self._raw_gmail_id = ""
        self._source: tuple[str, bytes, list[tuple[str, str]] | None] | None = None
        self._header_rows = 5
        self.sixel = False
        self.sixel_cell = (8, 16)
        self.sixel_pixels = (0, 0)
        self.image_slots: list[tuple[int, int]] = []
        self.image_rows = 16
        self.sixel_frames: tuple[tuple[str, int, bytes], ...] = ()
        self.sixel_pending = False
        self.sixel_ready = False
        self._undo: list[list[tuple[str, tuple[str, ...], tuple[str, ...]]]] = []
        self.completions: list[LabelChoice] = []
        self.popup: tuple[int, int, int, int] | None = None
        self.search_text = ""
        self.find_line: int | None = None
        self.find_query = ""
        self._save_matches: list[str] = []
        self._save_match_i = -1
        self._save_cycle_dir = ""
        self._save_cycle_tilde = False
        self._save_listing: list[str] = []
        self._snooze_choices: list[SnoozeChoice] = []
        self._prompt_focus = "labels"
        self._saved_visible: list[FlatLabel] = []
        self._saved_label_cursor = 0
        self._saved_messages: list[MessageSummary] = []
        self._saved_message_cursor = 0
        self._saved_reader_scroll = 0
        self._find_lines: list[int] = []
        self.messages: list[MessageSummary] = []
        self._mail_threads: list[MessageSummary] = []
        self.expanded_threads: set[int] = set()
        self.reader: list[str] = ["(no message)"]
        self.reader_view: list[str] = ["(no message)"]
        self.reader_marks: list[ReaderMark] = [ReaderMark()]
        self._reader_starts: list[int] = [0]
        self._reader_width = 0
        self._reader_term = 0
        self._reader_left = 0
        self._reader_hidden = False
        self.hide_urls = False
        self.visible: list[FlatLabel] = []
        self.expanded: set[str] = set()
        self.needs_fetch = False
        self._fetch_gmail_id: str | None = None
        self.urgent_phase = ""
        self.labels_stale = False
        # Archive/delete: update the list now; store + Gmail push run on the
        # account thread so d feels as light as x.
        self.label_todo: list[tuple[list[str], list[str], list[str]]] = []
        self._hidden: set[str] = set()
        self.inbox_count = 0
        self.inbox_unread = 0
        self._more = False
        self._seen_labels = False
        self.selected: set[str] = set()
        self.account_spans: list[tuple[int, int, int]] = []
        self._selection_scope: str | None = None
        self.newest_first = True
        self.message_window = 10
        self.label_window = 8
        self.reader_window = 8
        self.help_open = False
        self.help_scroll = 0
        self.help_lines: list[str] = []
        self._refreshed_at = 0.0
        # Gmail id whose saved body is currently on screen. Empty until that
        # file exists, so a later download can fill the reader alone.
        self._shown_body = ""
        self._label_tree_cache = None
        self.reload_labels()
        self._select_initial()

    def reload_labels(self) -> None:
        self._label_tree_cache = None
        tree = self._label_tree()
        keys = expandable_keys(tree)
        if not self._seen_labels:
            self.expanded = set(keys)
            self._seen_labels = True
        else:
            self.expanded &= keys
        self.visible = flatten(tree, self.expanded)
        if self.visible:
            self.label_cursor = min(self.label_cursor, len(self.visible) - 1)
        self.inbox_count, self.inbox_unread = self.engine.store.inbox_counts()

    def sync(self) -> None:
        try:
            self.engine.startup()
        except (Offline, GmailError) as exc:
            self.sync_phase = str(exc)
            self.sync_done = 0
            self.sync_total = 1
            return
        self.sync_phase = "idle"
        self.sync_done = 1
        self.sync_total = 1
        self.reload_labels()
        self.load_messages(fetch=False)

    def observe(self, job, *, force: bool = False) -> bool:
        """Apply background progress. True when the open message should be drawn.

        A missing body used to rebuild the folder list on every tick. That ran
        on the same thread as j/k, so stepping through a burst felt late.
        """
        self.sync_phase = job.phase
        self.sync_done = job.done
        self.sync_total = job.total
        self.sync_tick = job.tick
        reader_ready = self._take_downloaded_body()
        if self.prompt or not job.dirty:
            return reader_ready
        now = time.monotonic()
        # New rows still skip the half-second throttle. list_bump stays set
        # until that refresh runs, and the key loop wakes while it is set, so
        # the list cannot sit unchanged through a long idle.
        bump = bool(getattr(job, "list_bump", False))
        if not force and not bump and now - self._refreshed_at < 0.5:
            return reader_ready
        job.dirty = False
        if bump:
            job.list_bump = False
        self._refreshed_at = now
        self.refresh_view(pin_new_mail=bump)
        return True

    def _take_downloaded_body(self) -> bool:
        """Fill the open reader once its file exists. The folder list stays put."""
        if self.prompt or not self.messages:
            return False
        gmail_id = self.messages[self.message_cursor].gmail_id
        if self._shown_body == gmail_id:
            return False
        if not self.engine.store.body_readable(gmail_id):
            return False
        self.note_body()
        return self._shown_body == gmail_id

    def refresh_view(self, *, pin_new_mail: bool = False) -> None:
        if self.prompt:
            return
        fetch_id = self._fetch_gmail_id
        if self.search_text:
            self._show_saved_search()
        else:
            label_id = None
            current = self.current_label()
            if current is not None:
                label_id = current.gmail_id
            selected = self.messages[self.message_cursor] if self.messages else None
            message_id = _row_identity(selected) if selected is not None else None
            thread_id = selected.thread_id if selected is not None else 0
            selected_gmail = selected.gmail_id if selected is not None else ""
            reader_scroll = self.reader_scroll
            kept_messages = self.messages
            kept_more = self._more
            kept_threads = self._mail_threads
            kept_cursor = self.message_cursor
            kept_row = kept_cursor - self.message_scroll
            self.reload_labels()
            if label_id is not None:
                for index, item in enumerate(self.visible):
                    if item.gmail_id == label_id:
                        self.label_cursor = index
                        break
            self.load_messages(fetch=False, open_reader=False)
            # Prefer the fresh first page so new mail appears. If the open row
            # was only present after paging further, merge that tail back on
            # instead of throwing away the update (or the deep window).
            if kept_messages and _list_index(
                self.messages, message_id, selected_gmail, thread_id
            ) is None:
                self.messages, self._mail_threads, self._more = _merge_mail_page(
                    self.messages,
                    self._mail_threads,
                    kept_messages,
                    kept_threads,
                    kept_more,
                )
            self._restore_list_cursor(
                message_id,
                selected_gmail,
                thread_id,
                kept_cursor,
                kept_row,
            )
            if pin_new_mail:
                self._pin_new_mail(kept_messages)
            self.load_reader(fetch=False)
            self.reader_scroll = reader_scroll
        if not fetch_id or not self.messages:
            return
        current_id = self.messages[self.message_cursor].gmail_id
        preview = self.engine.store.preview_message(current_id)
        if current_id == fetch_id and self._wants_body(preview):
            self.needs_fetch = True
            self._fetch_gmail_id = fetch_id

    def _restore_list_cursor(
        self,
        row_id: str | None,
        gmail_id: str,
        thread_id: int,
        kept_cursor: int,
        kept_row: int,
    ) -> None:
        if not self.messages:
            return
        index = _list_cursor_for(self.messages, row_id, gmail_id, thread_id, kept_cursor)
        self.message_cursor = index
        shifted = max(0, index - kept_cursor)
        desired = index - max(0, kept_row)
        # At the top of the list, let rows that arrived above the selection
        # show up there. Scrolled further down, the selected row keeps its
        # place on screen so a busy inbox does not slide under the reader.
        if self.newest_first and shifted and kept_cursor - kept_row <= 0:
            desired -= shifted
        self.message_scroll = max(0, desired)

    def _pin_new_mail(self, previous: list[MessageSummary]) -> None:
        """Bring new mail into view when that keeps the selected row on screen.

        The selection itself never moves: arrivals must not change the open
        message while someone is reading or about to act on it.
        """
        if not self.messages:
            return
        window = max(1, self.message_window)
        if self.newest_first:
            if self.message_cursor < window:
                self.message_scroll = 0
            return
        new_indexes = _new_mail_indexes(previous, self.messages)
        bottom = max(new_indexes) if new_indexes else len(self.messages) - 1
        wanted = max(0, bottom - window + 1)
        if wanted <= self.message_cursor:
            self.message_scroll = max(self.message_scroll, wanted)

    def move(self, delta: int) -> None:
        if delta == 0:
            return
        if self.focus == "labels":
            self.label_cursor = _clamp(self.label_cursor + delta, len(self.visible))
            self.load_messages(fetch=True)
        elif self.focus == "list":
            self._move_list(delta)
        elif self.focus == "attachments":
            self._move_attachment(delta)
        elif self.focus == "mime":
            self._move_mime(delta)
        else:
            self._move_reader(delta)

    def page(self, delta: int) -> None:
        if delta == 0:
            return
        if self.focus == "attachments":
            self._move_attachment(1 if delta > 0 else -1)
            return
        if self.focus == "mime":
            self._move_mime(1 if delta > 0 else -1)
            return
        if self.focus == "reader":
            step = max(1, self.reader_window)
            self._move_reader(step if delta > 0 else -step)
            return
        if self.focus == "labels":
            step = max(1, self.label_window)
            self.move(step if delta > 0 else -step)
            return
        step = max(1, self.message_window)
        self._move_list(step if delta > 0 else -step)

    def _move_list(self, delta: int) -> None:
        if not self.messages:
            return
        window = max(1, self.message_window)
        if delta == 1 and self._list_should_scroll_down(window):
            self._jump_page(SCROLL_JUMP)
            return
        if delta == -1 and self._list_should_scroll_up(window):
            self._jump_page(-SCROLL_JUMP)
            return
        target = self.message_cursor + delta
        if delta > 0:
            while target >= len(self.messages) and self._more:
                before = len(self.messages)
                self._fill_page(force=True)
                if len(self.messages) == before:
                    break
            self.message_cursor = min(target, len(self.messages) - 1)
        else:
            self.message_cursor = max(0, target)
        self.load_reader(fetch=True)

    def _list_should_scroll_down(self, window: int) -> bool:
        """True when j should scroll instead of walking onto the last screen rows."""
        bottom = self.message_scroll + window - 1
        at_end = self.message_cursor >= len(self.messages) - 1 and not self._more
        if at_end:
            return self.message_cursor > bottom
        margin = _scroll_margin(window)
        return self.message_cursor >= bottom - margin

    def _list_should_scroll_up(self, window: int) -> bool:
        """True when k should scroll instead of walking onto the first screen rows."""
        if self.message_cursor <= 0:
            return False
        if self.message_scroll <= 0:
            return self.message_cursor <= self.message_scroll
        margin = _scroll_margin(window)
        return self.message_cursor <= self.message_scroll + margin

    def _jump_page(self, jump: int) -> None:
        """Move one mail and scroll by the same amount, so the highlight stays put."""
        window = max(1, self.message_window)
        if jump > 0:
            need = self.message_scroll + jump + window
            while len(self.messages) < need and self._more:
                before = len(self.messages)
                self._fill_page(force=True)
                if len(self.messages) == before:
                    break
            if self.message_cursor >= len(self.messages) - 1:
                return
            self.message_cursor += 1
            limit = max(0, len(self.messages) - window)
            self.message_scroll = min(self.message_scroll + jump, limit)
        else:
            if self.message_cursor <= 0:
                return
            self.message_cursor -= 1
            self.message_scroll = max(0, self.message_scroll + jump)
        self.message_scroll = _reveal_list(
            self.message_cursor,
            self.message_scroll,
            window,
            len(self.messages),
            more=self._more,
        )
        self.load_reader(fetch=True)

    def _move_reader(self, delta: int) -> None:
        window = max(1, self.reader_window)
        total = len(self.reader_view)
        limit = max(0, total - window)
        before = self.reader_scroll
        after = min(limit, max(0, before + delta))
        if after != before:
            self.reader_scroll = after
            self._reader_edge = None
            return
        # Already at the edge this move was trying to cross.
        edge = 1 if delta > 0 else -1
        if self._reader_edge == edge:
            self._reader_edge = None
            self.leave_message()
            return
        self._reader_edge = edge

    def activate(self) -> None:
        if self.focus == "labels":
            current = self.current_label()
            if current and current.gmail_id is None:
                self.toggle_fold()
            else:
                self.focus = "list"
        elif self.focus == "list":
            self.focus = "reader"
            self.load_reader(fetch=True)
        elif self.focus == "attachments":
            self.begin_save_attachment()
        elif self.focus == "mime":
            self.open_mime_html()

    def toggle_fold(self) -> None:
        current = self.current_label()
        if current is None or not current.expandable:
            return
        if current.key in self.expanded:
            self.expanded.remove(current.key)
        else:
            self.expanded.add(current.key)
        self.visible = flatten(self._label_tree(), self.expanded)
        for index, item in enumerate(self.visible):
            if item.key == current.key:
                self.label_cursor = index
                break

    def fold(self, opening: bool | None) -> None:
        """Open, close, or toggle the thread or label folder under the cursor.

        ``True`` opens, ``False`` closes, and ``None`` toggles. The pane stays put.
        """
        if self.focus == "labels":
            current = self.current_label()
            if current is None or not current.expandable:
                return
            want = (not current.expanded) if opening is None else opening
            if current.expanded != want:
                self.toggle_fold()
            return
        if self.focus != "list":
            return
        self._fold_thread(opening)

    def _fold_thread(self, opening: bool | None) -> None:
        if not self.messages:
            return
        current = self.messages[_clamp(self.message_cursor, len(self.messages))]
        if current.thread_count <= 1 or current.thread_role not in {"head", "child"}:
            return
        opened = current.thread_id in self.expanded_threads
        want = (not opened) if opening is None else opening
        if want == opened:
            return
        restore = f"t:{current.thread_id}" if not want or current.thread_role == "child" else current.row_key
        if want:
            self.expanded_threads.add(current.thread_id)
        else:
            self.expanded_threads.discard(current.thread_id)
        self._show_threads()
        self._restore_row(restore)
        self.load_reader(fetch=False)

    def horizontal(self, right: bool) -> None:
        if self.focus == "labels":
            current = self.current_label()
            if current and current.expandable and current.expanded != right:
                self.toggle_fold()
                return
            if right:
                self.focus = "list"
            return
        if self.focus == "list":
            if self._nudge_thread(right):
                return
            self.focus = "reader" if right else "labels"
            return
        if self.focus in {"attachments", "mime"} and not right:
            if self.focus == "attachments":
                self.close_attachments()
            else:
                self.close_mime()
            self.focus = "list"
            return
        if not right:
            self.close_raw_source()
            self.focus = "list"

    def cycle(self, backward: bool = False) -> None:
        order = ("labels", "list", "reader")
        current = self.focus if self.focus in order else "reader"
        index = order.index(current)
        self.focus = order[(index - 1) % 3] if backward else order[(index + 1) % 3]

    def archive_selected(self) -> None:
        self._change_selected(remove=["INBOX"])

    def delete_selected(self) -> None:
        self._change_selected(add=["TRASH"], remove=["INBOX"])

    def toggle_order(self) -> None:
        self.newest_first = not self.newest_first
        anchor = None
        kept_cursor = self.message_cursor
        kept_row = kept_cursor - self.message_scroll
        depth = len(self._mail_threads)
        if self.messages:
            index = _clamp(self.message_cursor, len(self.messages))
            anchor = _row_identity(self.messages[index])
        if self.search_text:
            self._show_saved_search()
            return
        self.load_messages(fetch=False, open_reader=False)
        # load_messages only pulls the first page. Keep the same depth so a
        # flip mid-scroll (or mid-download) does not wipe already-loaded mail.
        self._ensure_mail_depth(depth, anchor)
        if self.messages:
            found = _list_index(self.messages, anchor, "", 0) if anchor else None
            self.message_cursor = (
                found if found is not None else min(kept_cursor, len(self.messages) - 1)
            )
            window = max(1, self.message_window)
            preferred = max(0, self.message_cursor - max(0, kept_row))
            # A full reverse on a short folder should not scroll the earlier
            # rows away just to pin the selection on the same screen line.
            if len(self.messages) <= window:
                preferred = 0
            self.message_scroll = _reveal_list(
                self.message_cursor,
                preferred,
                window,
                len(self.messages),
                more=self._more,
            )
            self.load_reader(fetch=False)

    def _ensure_mail_depth(self, depth: int, anchor: str | None) -> None:
        """Load further pages until ``depth`` thread heads (or the anchor) are back."""
        while self._more and len(self._mail_threads) < max(0, depth):
            before = len(self._mail_threads)
            self._fill_page(force=True)
            if len(self._mail_threads) == before:
                break
        if not anchor:
            return
        while self._more and all(_row_identity(item) != anchor for item in self.messages):
            before = len(self._mail_threads)
            self._fill_page(force=True)
            if len(self._mail_threads) == before:
                break

    def toggle_selected(self) -> None:
        if not self.messages:
            return
        index = _clamp(self.message_cursor, len(self.messages))
        ids = self._row_ids(self.messages[index])
        if all(gmail_id in self.selected for gmail_id in ids):
            for gmail_id in ids:
                self.selected.discard(gmail_id)
        else:
            self.selected.update(ids)
        # Same step as j: at the bottom of the list this scrolls and loads the next page.
        if self.focus == "list":
            self._move_list(1)
        elif index + 1 < len(self.messages):
            self.message_cursor = index + 1
            self.load_reader(fetch=True)

    def _targets(self) -> list[str]:
        if not self.messages:
            return []
        if not self.selected:
            index = _clamp(self.message_cursor, len(self.messages))
            return list(self._row_ids(self.messages[index]))
        marked = [item for item in self.messages if _row_marked(item, self.selected)]
        if not marked:
            index = _clamp(self.message_cursor, len(self.messages))
            marked = [self.messages[index]]
        seen: set[str] = set()
        chosen: list[str] = []
        for item in marked:
            for gmail_id in self._row_ids(item):
                if gmail_id not in seen:
                    seen.add(gmail_id)
                    chosen.append(gmail_id)
        return chosen

    def _change_selected(self, add: list[str] | None = None, remove: list[str] | None = None) -> None:
        targets = self._targets()
        if not targets:
            return
        index = _clamp(self.message_cursor, len(self.messages))
        anchor = targets[0]
        add_labels = list(add or ())
        remove_labels = list(remove or ())
        for gmail_id in targets:
            self.selected.discard(gmail_id)
        self._push_undo(
            [(gmail_id, tuple(add_labels), tuple(remove_labels)) for gmail_id in targets]
        )
        if self.search_text or not self._leaves_current_view(add_labels, remove_labels):
            self.labels_stale = True
            self.engine.store.apply_local_labels_batch(
                [(gmail_id, add_labels, remove_labels) for gmail_id in targets]
            )
            self._reload_open_list(anchor, index)
            return
        # Leave the folder immediately; persist + push in the background.
        # Skip labels_stale here — a full folder recount on the next tick made
        # d feel heavier than x. Counts refresh once the store write lands.
        self._note_left_inbox(targets, remove_labels)
        self._hidden.update(targets)
        self.label_todo.append((list(targets), add_labels, remove_labels))
        self._drop_acted(set(targets), index)

    def _note_left_inbox(self, targets: list[str], remove_labels: list[str]) -> None:
        """Adjust the tab badge without scanning every folder."""
        if "INBOX" not in remove_labels or not targets:
            return
        gone = set(targets)
        unread = 0
        for item in self.messages:
            ids = self._row_ids(item)
            if not any(gmail_id in gone for gmail_id in ids):
                continue
            if item.unread:
                unread += 1
        self.inbox_count = max(0, self.inbox_count - len(targets))
        self.inbox_unread = max(0, self.inbox_unread - unread)

    def flush_label_todo(self) -> bool:
        """Apply deferred archive/delete intents. True when anything was written."""
        todo = self.label_todo
        if not todo:
            return False
        self.label_todo = []
        changes: list[tuple[str, list[str], list[str]]] = []
        done: set[str] = set()
        for targets, add_labels, remove_labels in todo:
            for gmail_id in targets:
                changes.append((gmail_id, add_labels, remove_labels))
                done.add(gmail_id)
        self.engine.store.apply_local_labels_batch(changes)
        self._hidden -= done
        self.labels_stale = True
        return True

    def _strip_hidden(self) -> None:
        if not self._hidden:
            return
        self.messages = [item for item in self.messages if not self._row_hidden(item)]
        self._mail_threads = [item for item in self._mail_threads if not self._row_hidden(item)]
        if self.messages:
            self.message_cursor = min(self.message_cursor, len(self.messages) - 1)
        else:
            self.message_cursor = 0

    def _row_hidden(self, item: MessageSummary) -> bool:
        if item.gmail_id in self._hidden:
            return True
        members = item.member_ids or ()
        return bool(members) and all(gmail_id in self._hidden for gmail_id in members)

    def _leaves_current_view(self, add: list[str] | None, remove: list[str] | None) -> bool:
        current = self.current_label()
        label = current.gmail_id if current is not None else None
        if label is None:
            return False
        if label and label in (remove or []):
            return True
        return "TRASH" in (add or []) and label != "TRASH"

    def _drop_acted(self, ids: set[str], index: int) -> None:
        """Remove acted rows and open the next one without reloading the mailbox."""
        drop_threads = {
            item.thread_id
            for item in self.messages
            if item.thread_role == "head" and item.member_ids and set(item.member_ids) <= ids
        }

        def gone(item: MessageSummary) -> bool:
            if item.thread_id and item.thread_id in drop_threads:
                return True
            if item.thread_role == "head" and item.member_ids:
                return False
            return item.gmail_id in ids

        nxt = next((_row_identity(item) for item in self.messages[index:] if not gone(item)), None)
        if nxt is None:
            nxt = next(
                (_row_identity(item) for item in reversed(self.messages[:index]) if not gone(item)),
                None,
            )
        self.messages = [item for item in self.messages if not gone(item)]
        self._mail_threads = [item for item in self._mail_threads if not gone(item)]
        self.message_cursor = 0
        if nxt:
            for cursor, item in enumerate(self.messages):
                if _row_identity(item) == nxt:
                    self.message_cursor = cursor
                    break
        elif self.messages:
            self.message_cursor = min(index, len(self.messages) - 1)
        self.message_scroll = _reveal_list(
            self.message_cursor,
            self.message_scroll,
            max(1, self.message_window),
            len(self.messages),
            more=self._more,
        )
        # Same as j/select: ask UrgentWork for the next body immediately.
        self.load_reader(fetch=True)

    def toggle_unread(self) -> None:
        """Flip UNREAD on the selection, or the cursor message. Marking the cursor read moves on."""
        if not self.messages:
            return
        index = _clamp(self.message_cursor, len(self.messages))
        cursor_id = self.messages[index].gmail_id
        chosen = [item for item in self.messages if _row_marked(item, self.selected)]
        if not chosen:
            chosen = [self.messages[index]]
        covered: set[str] = set()
        for item in chosen:
            if item.thread_role == "head":
                covered.update(item.member_ids)
        chosen = [
            item for item in chosen if item.thread_role == "head" or item.gmail_id not in covered
        ]
        cursor_was_unread = any(item.gmail_id == cursor_id and item.unread for item in chosen)
        steps: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
        updates: dict[str, bool] = {}
        for item in chosen:
            for gmail_id in self._row_ids(item):
                if item.unread:
                    self.engine.store.apply_local_labels(gmail_id, remove=["UNREAD"])
                    steps.append((gmail_id, (), ("UNREAD",)))
                else:
                    self.engine.store.apply_local_labels(gmail_id, add=["UNREAD"])
                    steps.append((gmail_id, ("UNREAD",), ()))
                updates[gmail_id] = not item.unread
        self._push_undo(steps)
        self.messages = [
            replace(item, unread=updates[item.gmail_id]) if item.gmail_id in updates else item
            for item in self.messages
        ]
        if any(item.thread_role == "head" for item in self.messages):
            self._refresh_thread_unread()
        self.labels_stale = True
        current = self.current_label()
        if current is not None and current.gmail_id == "UNREAD":
            removed = {gmail_id for gmail_id, unread in updates.items() if not unread}
            following = next(
                (item.gmail_id for item in self.messages[index + 1 :] if item.gmail_id not in removed),
                None,
            )
            self.messages = [item for item in self.messages if item.gmail_id not in removed]
            if following is not None:
                for cursor, item in enumerate(self.messages):
                    if item.gmail_id == following:
                        self.message_cursor = cursor
                        break
            else:
                self.message_cursor = min(index, max(0, len(self.messages) - 1))
            self.load_reader(fetch=True)
            return
        if cursor_was_unread:
            before = self.message_cursor
            self.next_unread()
            if self.message_cursor == before:
                self.load_reader(fetch=False)
            return
        self.load_reader(fetch=False)

    def undo(self) -> None:
        """Reverse the last archive, delete, label, or unread change."""
        if not self._undo:
            self.notice = "nothing to undo"
            return
        # Deferred deletes must hit the store before undo can reverse them.
        self.flush_label_todo()
        steps = self._undo.pop()
        for gmail_id, added, removed in steps:
            self.engine.store.undo_labels(gmail_id, list(added), list(removed))
        self.notice = "undone"
        anchor = steps[0][0] if steps else None
        index = _clamp(self.message_cursor, len(self.messages)) if self.messages else 0
        self._reload_open_list(anchor, index)

    def _push_undo(self, steps: list[tuple[str, tuple[str, ...], tuple[str, ...]]]) -> None:
        if not steps:
            return
        self._undo.append(steps)
        if len(self._undo) > 100:
            del self._undo[:-100]

    def _reload_open_list(self, anchor: str | None, index: int) -> None:
        current = self.current_label()
        label_id = current.gmail_id if current is not None else None
        searching = self.search_text
        self.reload_labels()
        if label_id is not None:
            for cursor, item in enumerate(self.visible):
                if item.gmail_id == label_id:
                    self.label_cursor = cursor
                    break
        if searching:
            self.search_text = searching
            self._show_saved_search()
        else:
            self.load_messages(fetch=False, open_reader=False)
        if anchor:
            for cursor, item in enumerate(self.messages):
                if item.gmail_id == anchor:
                    self.message_cursor = cursor
                    break
            else:
                self.message_cursor = min(index, max(0, len(self.messages) - 1))
        if self.messages:
            self.load_reader(fetch=True)

    def next_unread(self) -> None:
        """Move to the next unread message, loading further pages and then wrapping."""
        if not self.messages:
            return
        found = self._unread_from(self.message_cursor + 1, len(self.messages))
        while found is None and self._more:
            before = len(self.messages)
            self._fill_page(force=True)
            if len(self.messages) == before:
                break
            found = self._unread_from(before, len(self.messages))
        if found is None:
            found = self._unread_from(0, self.message_cursor + 1)
        if found is None or found == self.message_cursor:
            return
        self.message_cursor = found
        self.load_reader(fetch=True)

    def _unread_from(self, start: int, stop: int) -> int | None:
        stop = min(stop, len(self.messages))
        for index in range(max(0, start), stop):
            if self.messages[index].unread:
                return index
        return None

    def open_inbox(self) -> None:
        self.prompt = None
        self.prompt_text = ""
        self.prompt_hint = ""
        self.completions = []
        self.find_line = None
        self.find_query = ""
        self._expand_to("id:INBOX")
        for index, item in enumerate(self.visible):
            if item.gmail_id == "INBOX":
                self.label_cursor = index
                self.focus = "list"
                self.load_messages(fetch=True)
                return
        self.search_text = ""
        self.engine.store.set_open_label("INBOX")
        self._mail_threads = self.engine.store.list_mail_threads(
            "INBOX", None, PAGE, newest_first=self.newest_first
        )
        self._more = len(self._mail_threads) == PAGE
        self._show_threads()
        self.message_cursor = 0
        self.message_scroll = 0
        self.focus = "list"
        self.load_reader(fetch=True)

    def jump(self, end: bool) -> None:
        if self.focus == "labels":
            self.label_cursor = (len(self.visible) - 1) if end else 0
            self.load_messages(fetch=False)
        elif self.focus == "list":
            self.message_cursor = (len(self.messages) - 1) if end else 0
            self.load_reader(fetch=True)
        elif self.focus == "attachments":
            if self.attachments:
                self.attachment_cursor = (len(self.attachments) - 1) if end else 0
                self._show_attachment_cursor()
        elif self.focus == "mime":
            if self.mime_parts:
                self.mime_cursor = (len(self.mime_parts) - 1) if end else 0
                self._show_mime_cursor()
        else:
            self.reader_scroll = len(self.reader_view) if end else 0

    def current_label(self) -> FlatLabel | None:
        if not self.visible:
            return None
        self.label_cursor = _clamp(self.label_cursor, len(self.visible))
        return self.visible[self.label_cursor]

    def _wants_body(self, preview) -> bool:
        """The open message should be fetched now.

        Background caching still fills the rest of the mailbox. The message
        on screen jumps the queue through UrgentWork so j/k through new mail
        does not wait on the paced download backlog.
        """
        if preview is None or preview.body_cached:
            return False
        return True

    def load_messages(self, *, fetch: bool, open_reader: bool = True) -> None:
        current = self.current_label()
        if current is None or current.gmail_id is None:
            self.messages = []
            self._mail_threads = []
            self._more = False
            scope = ""
        else:
            label = None if current.gmail_id == "" else current.gmail_id
            scope = label or ""
            if label:
                self.engine.store.set_open_label(label)
            self._mail_threads = self.engine.store.list_mail_threads(
                label, None, PAGE, newest_first=self.newest_first
            )
            self._more = len(self._mail_threads) == PAGE
            self._show_threads()
            self._strip_hidden()
        if scope != self._selection_scope:
            self.selected.clear()
            self._selection_scope = scope
        self.search_text = ""
        self.message_cursor = 0 if self.messages else 0
        self.message_scroll = 0
        self._note_empty_folder()
        if open_reader:
            self.load_reader(fetch=fetch)

    def _note_empty_folder(self) -> None:
        """Empty Inbox is easy to mistake for a failed download."""
        if self.messages or self.search_text or self.prompt:
            return
        total = self.engine.store.messages_total()
        if total <= 0:
            return
        current = self.current_label()
        name = current.name if current is not None else "This folder"
        self.notice = f"{name} is empty — open All mail ({total} messages)"

    def load_reader(self, *, fetch: bool) -> None:
        self.reader_scroll = 0
        self._reader_edge = None
        self.find_line = None
        self.find_query = ""
        if not self.messages:
            self._set_reader(["(no message)"])
            self._shown_body = ""
            self.needs_fetch = False
            self._fetch_gmail_id = None
            self._set_open_waiting()
            return
        self.message_cursor = _clamp(self.message_cursor, len(self.messages))
        message = self.messages[self.message_cursor]
        preview = self.engine.store.preview_message(message.gmail_id)
        self._show_reader(preview, message.gmail_id)
        missing = self._wants_body(preview)
        if fetch:
            self.needs_fetch = missing
        elif message.gmail_id != self._fetch_gmail_id or not missing:
            self.needs_fetch = False
        self._fetch_gmail_id = message.gmail_id if self.needs_fetch else None
        self._set_open_waiting()

    def note_body(self) -> None:
        if not self.messages:
            self.needs_fetch = False
            self._fetch_gmail_id = None
            self._set_open_waiting()
            return
        message = self.messages[self.message_cursor]
        preview = self.engine.store.preview_message(message.gmail_id)
        scroll = self.reader_scroll
        self._show_reader(preview, message.gmail_id)
        self.reader_scroll = scroll
        self._refresh_attachment_flag(message.gmail_id)
        if preview is None or preview.body_cached:
            self.needs_fetch = False
            self._fetch_gmail_id = None
        self._set_open_waiting()

    def fetch_open(self) -> None:
        self.needs_fetch = False
        self._set_open_waiting()
        if not self.messages:
            return
        message = self.messages[self.message_cursor]
        try:
            self.engine.open_message(message.gmail_id)
        except (Offline, GmailError) as exc:
            self.sync_phase = str(exc)
            self.sync_done = 0
            self.sync_total = 1
        preview = self.engine.store.preview_message(message.gmail_id)
        self._show_reader(preview, message.gmail_id)
        self._refresh_attachment_flag(message.gmail_id)

    def _set_open_waiting(self) -> None:
        self.engine.open_waiting = bool(self.needs_fetch)

    def reveal(self, geo: Geometry) -> None:
        self.label_window = geo.label_rows
        self.message_window = geo.message_rows
        self.reader_window = geo.reader_rows
        self.label_scroll = _reveal(self.label_cursor, self.label_scroll, geo.label_rows)
        self.message_scroll = _reveal_list(
            self.message_cursor,
            self.message_scroll,
            geo.message_rows,
            len(self.messages),
            more=self._more,
        )
        if self.sixel and self.messages and self.image_slots:
            wanted = self._image_band_rows()
            if wanted != self.image_rows:
                kept = self.reader_scroll
                preview = self.engine.store.preview_message(
                    self.messages[self.message_cursor].gmail_id
                )
                self._show_reader(preview, self.messages[self.message_cursor].gmail_id)
                self.reader_scroll = kept
        self._rewrap_reader(
            max(1, geo.width - 2),
            geo.width,
            1,
        )
        max_reader = max(0, len(self.reader_view) - geo.reader_rows)
        self.reader_scroll = min(self.reader_scroll, max_reader)

    def _show_reader(self, preview: MessagePreview | None, gmail_id: str) -> None:
        self._set_reader(self._reader_for(preview))
        self._shown_body = gmail_id if preview is not None and preview.body_cached else ""

    def _set_reader(self, lines: list[str]) -> None:
        self.reader = lines
        self.reader_view = lines
        self.reader_marks = [ReaderMark() for _ in lines]
        self._reader_starts = list(range(len(lines)))
        self._reader_width = 0
        self._reader_term = 0
        self._reader_left = 0

    def _rewrap_reader(self, width: int, term_width: int | None = None, left: int = 0) -> None:
        width = max(1, width)
        term_width = max(width, term_width or width)
        left = max(0, min(left, term_width - 1))
        hide = self._hiding_urls()
        if (
            width == self._reader_width
            and term_width == self._reader_term
            and left == self._reader_left
            and hide == self._reader_hidden
        ):
            return
        self.reader_view, self._reader_starts, self.reader_marks = layout_reader(
            self.reader, width, term_width, left, hide_urls=hide
        )
        self._reader_width = width
        self._reader_term = term_width
        self._reader_left = left
        self._reader_hidden = hide

    def toggle_urls(self) -> None:
        """Hide link addresses. The word link stays and still opens the URL."""
        self.hide_urls = not self.hide_urls
        self.notice = "link addresses hidden" if self.hide_urls else "link addresses shown"

    def _hiding_urls(self) -> bool:
        # The raw source view promises every byte of the message.
        return self.hide_urls and not self.raw_source

    def _find_text(self, line: str) -> str:
        """The part of a reader line that is on screen, for find."""
        if self._hiding_urls() and "link " in line:
            return strip_link_urls(_plain(line))[0]
        return line

    def begin_search(self) -> None:
        self._remember()
        self.prompt = "search"
        self.prompt_text = ""
        self.prompt_cursor = 0
        self.prompt_hint = ""
        self.completions = []
        self._prompt_focus = self.focus
        self._refresh_prompt()

    def begin_label(self) -> None:
        if not self._targets():
            self.sync_phase = "no message selected"
            return
        self._remember()
        self.prompt = "label"
        self.prompt_text = ""
        self.prompt_cursor = 0
        self.completions = []
        self._prompt_focus = self.focus
        self._refresh_prompt()

    def begin_compose(self) -> bool:
        """Ask for To, Cc, and Bcc before the editor opens."""
        self._reset_compose()
        self.compose_lines = [""]
        self.prompt = "compose"
        self.prompt_cursor = 0
        self.prompt_text = ""
        self.prompt_hint = ""
        self._compose_contacts = tuple(self.engine.store.contact_addresses())
        self.notice = ""
        return True

    def begin_reply(self, *, everyone: bool) -> bool:
        if not self.messages:
            return False
        current = self.messages[self.message_cursor]
        preview = self.engine.store.preview_message(current.gmail_id)
        if preview is None:
            return False
        opened = self.engine.store.opened_message(current.gmail_id) or {}
        body = preview.body_text if preview.body_cached else preview.snippet
        to, cc, subject, lines = reply_draft(
            from_addr=preview.from_addr,
            to_addr=preview.to_addr,
            cc_addr=preview.cc_addr,
            subject=preview.subject,
            body=body,
            me=self.engine.store.account_email(),
            everyone=everyone,
        )
        self._reset_compose()
        self.compose_to = to
        self.compose_cc = cc
        self.compose_subject = subject
        self.compose_lines = lines
        self.compose_thread = str(opened.get("thread_gmail_id") or "")
        reply_to = str(opened.get("message_id_header") or "").strip()
        if not reply_to:
            # Sent-from-here copies used to lack Message-ID until re-fetched.
            reply_to = self.engine.store.message_id_for(current.gmail_id)
        self.compose_reply_to = reply_to
        self.notice = ""
        return True

    def _reset_compose(self) -> None:
        self.compose_to = ""
        self.compose_cc = ""
        self.compose_bcc = ""
        self.compose_subject = ""
        self.compose_lines = []
        self.compose_thread = ""
        self.compose_reply_to = ""
        self._compose_matches = []
        self._compose_match_i = -1
        self._sending = False

    def begin_holding(self) -> None:
        self.held = self.engine.store.held_changes()
        if not self.held:
            self.notice = "nothing holding"
            return
        self.prompt = "holding"
        self.prompt_cursor = 0
        self.notice = ""

    def release_holding(self) -> None:
        if self.prompt != "holding" or not self.held:
            return
        chosen = self.held[_clamp(self.prompt_cursor, len(self.held))]
        self.engine.store.release_held(chosen.queue_id)
        self.held = self.engine.store.held_changes()
        if not self.held:
            self.prompt = None
            self.notice = "released"
            return
        self.prompt_cursor = _clamp(self.prompt_cursor, len(self.held))

    def begin_attachments(self) -> None:
        if not self.messages:
            return
        current = self.messages[self.message_cursor]
        preview = self.engine.store.preview_message(current.gmail_id)
        if preview is None or not preview.body_cached:
            self.notice = "attachment not downloaded"
            return
        if not preview.attachments:
            self.notice = "no attachment"
            return
        self.close_mime()
        self.attachment_cursor = 0
        self.focus = "attachments"
        self.notice = ""
        self._set_reader(self._reader_for(preview))
        self._reveal_attachment()

    def close_attachments(self) -> None:
        if self.focus != "attachments":
            return
        self.focus = "reader"
        if not self.messages:
            return
        preview = self.engine.store.preview_message(self.messages[self.message_cursor].gmail_id)
        scroll = self.reader_scroll
        self._set_reader(self._reader_for(preview))
        self.reader_scroll = scroll

    def begin_mime(self) -> None:
        """Focus the MIME list so j/k switches the body part on screen."""
        if not self.messages:
            return
        current = self.messages[self.message_cursor]
        # Parse the raw message so alternative parts show up, not only the saved text.
        preview = self.engine.store.preview_message(current.gmail_id, mime=True)
        if preview is None or not preview.body_cached:
            self.notice = "body not downloaded"
            return
        if not preview.mime_parts:
            self.notice = "no mime parts"
            return
        self.close_attachments()
        self._mime_gmail_id = ""
        self.focus = "mime"
        self.notice = ""
        self._set_reader(self._reader_for(preview))
        self._reveal_mime()

    def close_mime(self) -> None:
        if self.focus != "mime":
            return
        self.focus = "reader"
        if not self.messages:
            return
        preview = self.engine.store.preview_message(self.messages[self.message_cursor].gmail_id)
        scroll = self.reader_scroll
        self._set_reader(self._reader_for(preview))
        self.reader_scroll = scroll

    def leave_message(self) -> bool:
        """Leave the open message for the list. False when q should quit."""
        if self.focus not in {"reader", "attachments", "mime"}:
            return False
        self.close_attachments()
        self.close_mime()
        self.close_raw_source()
        self._reader_edge = None
        self.focus = "list"
        return True

    def toggle_headers(self) -> None:
        """Show every header field of the open message, or only the short set."""
        self.full_headers = not self.full_headers
        if not self.full_headers:
            self.notice = "short headers"
        elif self.messages and self._message_source(self.messages[self.message_cursor].gmail_id) is None:
            self.notice = "all headers once the body is downloaded"
        else:
            self.notice = "all headers"
        self._refresh_open_reader(scroll=0)

    def toggle_raw_source(self) -> None:
        """Show the whole saved message: every header, MIME boundary, and encoded part."""
        if self.raw_source:
            self.close_raw_source()
            return
        if not self.messages:
            return
        gmail_id = self.messages[self.message_cursor].gmail_id
        if self._message_source(gmail_id) is None:
            self.notice = "body not downloaded"
            return
        self.close_attachments()
        self.close_mime()
        self.raw_source = True
        self._raw_gmail_id = gmail_id
        self.focus = "reader"
        self.notice = ""
        self._refresh_open_reader(scroll=0)

    def close_raw_source(self) -> None:
        if not self.raw_source:
            return
        self.raw_source = False
        self._refresh_open_reader(scroll=0)

    def _refresh_open_reader(self, scroll: int | None = None) -> None:
        if not self.messages:
            return
        preview = self.engine.store.preview_message(self.messages[self.message_cursor].gmail_id)
        kept = self.reader_scroll if scroll is None else scroll
        self._set_reader(self._reader_for(preview))
        self.reader_scroll = kept

    def _message_source(self, gmail_id: str) -> bytes | None:
        # Gmail never rewrites a message, so one copy per id stays valid.
        if self._source is not None and self._source[0] == gmail_id:
            return self._source[1]
        raw = self.engine.store.message_source(gmail_id)
        if raw is not None:
            self._source = (gmail_id, raw, None)
        return raw

    def _header_fields(self, gmail_id: str) -> list[tuple[str, str]] | None:
        raw = self._message_source(gmail_id)
        if raw is None or self._source is None:
            return None
        if self._source[2] is None:
            self._source = (gmail_id, raw, header_fields(raw))
        return self._source[2]

    def clear_search(self) -> bool:
        """Show the folder again. False when the list is not a search."""
        if not self.search_text:
            return False
        self.load_messages(fetch=False)
        return True

    def begin_quit(self) -> None:
        self.prompt = "quit"
        self.prompt_text = ""
        self.notice = ""

    def begin_view_image(self) -> None:
        """Show the picture under the cursor. The save dialog stays closed."""
        if self.focus != "attachments":
            self.begin_attachments()
            if self.focus != "attachments":
                return
        if not self.messages or not self.attachments:
            return
        item = self.attachments[self.attachment_cursor]
        if not is_bitmap(item.content_type):
            self.notice = "not an image"
            return
        preview = self.engine.store.preview_message(self.messages[self.message_cursor].gmail_id)
        self._set_reader(self._reader_for(preview))
        self._reveal_selected_image()
        self.notice = ""

    def begin_save_attachment(self) -> None:
        if self.focus != "attachments" or not self.attachments:
            return
        item = self.attachments[self.attachment_cursor]
        folder = self.download_dir.expanduser()
        if not folder.is_dir():
            folder = Path.home()
        self.prompt = "save"
        self.prompt_text = str(folder / safe_filename(item.filename))
        self.prompt_cursor = _SAVE_CHOICES.index("open")
        self.prompt_hint = ""
        self._clear_save_complete()
        self.notice = ""
        self._reveal_selected_image()

    def begin_snooze(self) -> None:
        if not self._targets():
            self.notice = "no message selected"
            return
        self.prompt = "snooze"
        self.prompt_text = ""
        self.prompt_cursor = 0
        self.prompt_hint = ""
        self._snooze_choices = snooze_presets()
        self.notice = ""

    def _move_attachment(self, delta: int) -> None:
        if not self.attachments:
            self.focus = "reader"
            return
        self.attachment_cursor = _clamp(self.attachment_cursor + delta, len(self.attachments))
        self._show_attachment_cursor()

    def _move_mime(self, delta: int) -> None:
        if not self.mime_parts:
            self.focus = "reader"
            return
        self.mime_cursor = _clamp(self.mime_cursor + delta, len(self.mime_parts))
        self._show_mime_cursor()

    def _show_mime_cursor(self) -> None:
        if not self.messages:
            return
        preview = self.engine.store.preview_message(self.messages[self.message_cursor].gmail_id)
        self._set_reader(self._reader_for(preview))
        self._reveal_mime()
        self.notice = ""

    def _reveal_mime(self) -> None:
        if not self.mime_parts:
            return
        needle = f"{self.mime_parts[self.mime_cursor].index}  "
        for index, line in enumerate(self.reader):
            if line.lstrip().startswith(needle) and self.mime_parts[self.mime_cursor].content_type in line:
                self.reader_scroll = _reveal(index, self.reader_scroll, max(1, self.reader_window))
                return

    def _show_attachment_cursor(self) -> None:
        if not self.messages:
            return
        preview = self.engine.store.preview_message(self.messages[self.message_cursor].gmail_id)
        self._set_reader(self._reader_for(preview))
        # j/k should show the picture under the cursor. Scrolling only to the
        # filename left the band off-screen, so Enter looked required.
        if self.sixel and self.attachments and is_bitmap(
            self.attachments[self.attachment_cursor].content_type
        ):
            self._reveal_selected_image()
            return
        self._reveal_attachment()

    def _reveal_attachment(self) -> None:
        if not self.attachments:
            return
        index = self.attachments[self.attachment_cursor].index
        prefix = f"> {index} "
        window = max(1, self.reader_window)
        for row, line in enumerate(self.reader):
            if not line.startswith(prefix):
                continue
            if row < self.reader_scroll or row >= self.reader_scroll + window:
                self.reader_scroll = row
            return

    def _reveal_selected_image(self) -> None:
        """Keep the header and file list visible. The picture sits under them."""
        if not self.attachments:
            return
        item = self.attachments[self.attachment_cursor]
        if not is_bitmap(item.content_type):
            self._reveal_attachment()
            return
        window = max(1, self.reader_window)
        height = max(1, self.image_rows)
        for line, index in self.image_slots:
            if index != item.index:
                continue
            if line + height <= window:
                self.reader_scroll = 0
            else:
                self.reader_scroll = max(0, line + height - window)
            return
        self._reveal_attachment()

    def _preferred_image(self) -> int | None:
        if self.focus == "attachments" and self.attachments:
            return self.attachments[self.attachment_cursor].index
        if self.prompt == "save" and self.attachments:
            return self.attachments[self.attachment_cursor].index
        return None

    def _lines_above_image(self) -> int:
        """Header and attachment rows that stay above the picture."""
        above = self._header_rows
        if self.mime_parts:
            above += 1 + len(self.mime_parts)
        else:
            above += 1
        if self.attachments:
            above += 1 + len(self.attachments)
        elif self.messages:
            above += 1
        return above

    def _image_band_rows(self) -> int:
        preferred = self._preferred_image()
        bitmaps = sum(1 for item in self.attachments if is_bitmap(item.content_type))
        if preferred is not None:
            room = max(4, self.reader_window - self._lines_above_image())
            return band_rows(room, 1)
        count = min(IMAGE_LIMIT, max(1, bitmaps))
        return band_rows(self.reader_window, count)

    def bitmap_indexes(self) -> tuple[int, ...]:
        return tuple(
            item.index for item in self.attachments if is_bitmap(item.content_type)
        )

    def _reader_for(self, preview: MessagePreview | None) -> list[str]:
        self.attachments = preview.attachments if preview is not None else ()
        self.mime_parts = preview.mime_parts if preview is not None else ()
        selected = None
        if self.focus == "attachments":
            if not self.attachments:
                self.focus = "reader"
            else:
                self.attachment_cursor = _clamp(self.attachment_cursor, len(self.attachments))
                selected = self.attachments[self.attachment_cursor].index
        if self.focus == "mime" and not self.mime_parts:
            self.focus = "reader"
        gmail_id = preview.gmail_id if preview is not None else ""
        if gmail_id != self._mime_gmail_id:
            self._mime_gmail_id = gmail_id
            self.mime_cursor = preferred_mime_index(self.mime_parts)
        elif self.mime_parts:
            self.mime_cursor = _clamp(self.mime_cursor, len(self.mime_parts))
        else:
            self.mime_cursor = 0
        mime_index = self.mime_parts[self.mime_cursor].index if self.mime_parts else None
        if self.raw_source and gmail_id and gmail_id == self._raw_gmail_id:
            raw = self._message_source(gmail_id)
            if raw is not None:
                self.image_slots = []
                return format_source(raw)
        self.raw_source = False
        headers = None
        if self.full_headers and preview is not None and preview.body_cached:
            headers = self._header_fields(gmail_id)
        # Labels sits under the header block either way.
        self._header_rows = len(headers) + 1 if headers else 5
        lines = format_reader(
            preview,
            selected,
            mime_index,
            mime_focus=self.focus == "mime",
            headers=headers,
        )
        preferred = self._preferred_image()
        self.image_rows = self._image_band_rows()
        # Only reserve sixel rows while viewing an attachment. Otherwise a
        # Word spacer or logo paints over the message body.
        lines, self.image_slots = image_band(
            lines,
            self.attachments,
            self.sixel and preferred is not None,
            preferred=preferred,
            rows=self.image_rows,
        )
        return lines

    def _accept_save(self) -> None:
        if not self.messages or not self.attachments:
            self.prompt = None
            self.prompt_text = ""
            return
        text = self.prompt_text.strip()
        if not text:
            self.notice = "no path"
            return
        item = self.attachments[self.attachment_cursor]
        destination = Path(text).expanduser()
        if text.endswith(("/", "\\")) or destination.is_dir():
            destination = destination / safe_filename(item.filename)
        try:
            saved = self.engine.store.save_attachment(
                self.messages[self.message_cursor].gmail_id,
                item.index,
                destination,
            )
        except OSError as exc:
            self.notice = str(exc) or "save failed"
            return
        self.prompt = None
        self.prompt_text = ""
        self.notice = f"saved {saved}"

    def _open_attachment(self) -> None:
        if not self.messages or not self.attachments:
            self.prompt = None
            self.prompt_text = ""
            return
        item = self.attachments[self.attachment_cursor]
        destination = None
        try:
            destination = reserve_download(self.download_dir, item.filename)
            saved = self.engine.store.save_attachment(
                self.messages[self.message_cursor].gmail_id,
                item.index,
                destination,
            )
        except OSError as exc:
            if destination is not None:
                destination.unlink(missing_ok=True)
            self.notice = str(exc) or "open failed"
            return
        try:
            self._xdg_open(saved)
        except OSError as exc:
            self.notice = str(exc) or "open failed"
            return
        self.prompt = None
        self.prompt_text = ""
        self.notice = f"opened {saved}"

    def open_mime_html(self) -> None:
        """Write the selected HTML part to a temp file and open it with xdg-open."""
        if self.focus != "mime" or not self.mime_parts:
            return
        part = self.mime_parts[self.mime_cursor]
        if part.content_type != "text/html":
            self.notice = "not HTML — select a text/html part"
            return
        html = part.source or part.text
        if not html.strip():
            self.notice = "empty HTML part"
            return
        handle = None
        path: Path | None = None
        try:
            handle, name = tempfile.mkstemp(prefix="gmail-tui-", suffix=".html")
            path = Path(name)
            os.write(handle, html.encode("utf-8"))
        except OSError as exc:
            self.notice = str(exc) or "open failed"
            return
        finally:
            if handle is not None:
                try:
                    os.close(handle)
                except OSError:
                    pass
        if path is None:
            self.notice = "open failed"
            return
        try:
            self._open_browser(os.fspath(path))
        except OSError as exc:
            path.unlink(missing_ok=True)
            self.notice = str(exc) or "open failed"
            return
        self.notice = f"opened {path}"

    def terminal_opens_links(self) -> bool:
        """The default opener matches the terminal, which opens OSC 8 clicks."""
        return self.open_command == ("xdg-open",)

    def begin_url_pick(self, regions: list[tuple[int, int, str, str]]) -> None:
        """Show a jump label on each visible link. A matching key opens it."""
        if not regions:
            self.url_hints = None
            self.url_typed = ""
            self.notice = "no link"
            return
        labels = jump_labels(len(regions))
        self.url_hints = tuple(
            UrlHint(y, x, url, label)
            for (y, x, _text, url), label in zip(regions, labels, strict=True)
        )
        self.url_typed = ""
        self.notice = "pick a link"

    def cancel_url_pick(self) -> None:
        self.url_hints = None
        self.url_typed = ""

    def type_url_hint(self, key: str | int) -> None:
        """Eat one key while jump labels are showing."""
        if not self.url_hints:
            return
        if key in (curses.KEY_BACKSPACE, 127, 8, "\x7f", "\b"):
            self.url_typed = self.url_typed[:-1]
            self.notice = "pick a link" if not self.url_typed else f"link {self.url_typed}"
            return
        if not isinstance(key, str) or len(key) != 1 or not key.isprintable() or key == " ":
            self.cancel_url_pick()
            return
        typed = self.url_typed + key
        matches = [hint for hint in self.url_hints if hint.label.startswith(typed)]
        if not matches:
            self.cancel_url_pick()
            return
        self.url_typed = typed
        if len(matches) == 1 and matches[0].label == typed:
            url = matches[0].url
            self.cancel_url_pick()
            self.open_url(url)
            return
        self.notice = f"link {self.url_typed}"

    def open_hint_at(self, y: int, x: int) -> bool:
        """Open the hinted link under the pointer. A miss cancels the picker."""
        if not self.url_hints:
            return False
        for hint in self.url_hints:
            if not hint.label.startswith(self.url_typed):
                continue
            if y == hint.y and hint.x <= x < hint.x + len(LABEL):
                url = hint.url
                self.cancel_url_pick()
                self.open_url(url)
                return True
        self.cancel_url_pick()
        return True

    def open_link(self) -> None:
        """Open the next link at or below the reader scroll, then the one after."""
        targets: list[tuple[int, str]] = []
        for row, mark in enumerate(self.reader_marks):
            for _column, url in mark.links:
                targets.append((row, url))
        if not targets:
            self.notice = "no link"
            return
        anchor = 0
        for index, (row, _url) in enumerate(targets):
            if row >= self.reader_scroll:
                anchor = index
                break
        shown = ""
        if self.messages:
            shown = self.messages[self.message_cursor].gmail_id
        key = (shown, self.reader_scroll)
        pick = anchor
        if self._link_key == key and self._link_pick is not None:
            pick = (self._link_pick + 1) % len(targets)
        self._link_key = key
        self._link_pick = pick
        self.open_url(targets[pick][1])

    def open_url(self, url: str) -> None:
        try:
            self._open_browser(url)
        except OSError as exc:
            self.notice = str(exc) or "open failed"
            return
        self.notice = f"opened {url}"

    def _open_browser(self, target: str) -> None:
        self._spawn([*self.open_command, target])

    def _xdg_open(self, path: Path) -> None:
        self._spawn(["xdg-open", os.fspath(path)])

    def _spawn(self, argv: list[str]) -> None:
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _compose_field(self) -> str:
        if self.prompt_cursor <= 0:
            return self.compose_to
        if self.prompt_cursor == 1:
            return self.compose_cc
        return self.compose_bcc

    def _set_compose_field(self, value: str) -> None:
        if self.prompt_cursor <= 0:
            self.compose_to = value
        elif self.prompt_cursor == 1:
            self.compose_cc = value
        else:
            self.compose_bcc = value

    def complete_compose_address(self, *, reverse: bool = False) -> None:
        """Fill the address being typed from From and To headers already stored."""
        if self.prompt != "compose":
            return
        current = self._compose_field()
        if self._compose_matches:
            step = -1 if reverse else 1
            self._compose_match_i = (self._compose_match_i + step) % len(self._compose_matches)
            chosen = self._compose_matches[self._compose_match_i]
            self._set_compose_field(_join_address(current, chosen))
            self.prompt_hint = f"{self._compose_match_i + 1}/{len(self._compose_matches)}"
            return
        prefix = _address_fragment(current)
        if not prefix:
            self.prompt_hint = "type to complete"
            return
        folded = prefix.casefold()
        matches = [addr for addr in self._compose_contacts if addr.casefold().startswith(folded)]
        if not matches:
            self.prompt_hint = "no match"
            return
        self._compose_matches = matches
        self._compose_match_i = len(matches) - 1 if reverse else 0
        self._set_compose_field(_join_address(current, matches[self._compose_match_i]))
        self.prompt_hint = "" if len(matches) == 1 else f"{self._compose_match_i + 1}/{len(matches)}"

    def focus_compose_field(self, y: int) -> None:
        if self.prompt != "compose" or self.popup is None:
            return
        index = y - (self.popup[0] + 1)
        if 0 <= index <= 2:
            self.prompt_cursor = index
            self._compose_matches = []
            self.prompt_hint = ""

    def draft_text(self) -> str:
        lines = [f"To: {self.compose_to}"]
        if self.compose_cc or not self.compose_reply_to:
            # Blank compose: leave a Cc line to fill in the editor.
            lines.append(f"Cc: {self.compose_cc}")
        if self.compose_bcc:
            lines.append(f"Bcc: {self.compose_bcc}")
        lines.append(f"Subject: {self.compose_subject}")
        lines.append("")
        lines.extend(self.compose_lines)
        text = "\n".join(lines)
        if not text.endswith("\n"):
            text += "\n"
        return text

    def finish_editor(self, _original: str, edited: str | None, status: int) -> None:
        if edited is None:
            self.notice = "editor failed"
            return
        if status != 0:
            self.notice = "not sent"
            return
        to, cc, bcc, subject, body = parse_draft(edited)
        self.compose_to = to
        self.compose_cc = cc
        self.compose_bcc = bcc
        self.compose_subject = subject
        self.compose_lines = body.split("\n")
        self._sending = False
        self._set_reader(edited.splitlines() or [""])
        self.reader_scroll = 0
        self.prompt = "send"
        self.prompt_cursor = 0
        self.notice = ""

    def begin_external_edit(self, launch=None) -> bool:
        """Write the draft and open it in another terminal. This window stays up."""
        if self.external is not None:
            self.notice = "already writing a message"
            return False
        text = self.draft_text()
        handle, name = tempfile.mkstemp(prefix="gmail-tui-reply-", suffix=".eml")
        os.close(handle)
        path = Path(name)
        sentinel = Path(str(path) + ".done")
        try:
            path.write_text(text, encoding="utf-8")
            opener = launch or launch_editor
            process = opener(path, sentinel, editor_command())
        except OSError as exc:
            path.unlink(missing_ok=True)
            sentinel.unlink(missing_ok=True)
            self.notice = str(exc) or "could not open a terminal"
            return False
        self.external = _ExternalEdit(
            path,
            sentinel,
            process,
            self.compose_thread,
            self.compose_reply_to,
        )
        self.notice = "writing in another window"
        return True

    def poll_external_edit(self) -> bool:
        """Open the send dialog once the other terminal's editor has exited."""
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
        self.compose_thread = pending.thread
        self.compose_reply_to = pending.reply_to
        self._drop_external(pending)
        self.finish_editor("", edited, status)
        return True

    def forget_external_edit(self) -> None:
        pending = self.external
        if pending is None:
            return
        self._drop_external(pending)
        self.notice = "editor closed"

    def _drop_external(self, pending: _ExternalEdit) -> None:
        self.external = None
        process = pending.process
        if process is not None and hasattr(process, "poll"):
            try:
                process.poll()
            except OSError:
                pass
        pending.path.unlink(missing_ok=True)
        pending.sentinel.unlink(missing_ok=True)

    def mark_press(self, y: int, x: int) -> None:
        self.marking = True
        self.mark_start = (y, x)
        self.mark_end = (y, x)

    def mark_drag(self, y: int, x: int) -> None:
        if self.marking:
            self.mark_end = (y, x)

    def finish_mark(self, y: int, x: int) -> str:
        self.marking = False
        if self.mark_start is None:
            self.mark_end = None
            return ""
        self.mark_end = (y, x)
        return selected_text(self.painted, self.mark_start[0], self.mark_start[1], y, x)

    def clear_mark(self) -> None:
        self.marking = False
        self.mark_start = None
        self.mark_end = None

    def click_popup_choice(self, y: int) -> bool:
        if self.prompt != "send":
            return False
        try:
            index = self.popup_choices.index(y)
        except ValueError:
            return False
        self.prompt_cursor = index
        self.prompt_accept()
        return True

    def confirm_send(self) -> None:
        if self.prompt != "send":
            return
        self.send_compose()

    def send_compose(self) -> None:
        if self._sending:
            return
        if not self.compose_to.strip():
            self.notice = "needs an address"
            return
        raw = compose_message(
            from_addr=self.engine.store.account_email(),
            to_addr=self.compose_to,
            cc_addr=self.compose_cc,
            bcc_addr=self.compose_bcc,
            subject=self.compose_subject,
            body="\n".join(self.compose_lines),
            in_reply_to=self.compose_reply_to,
        )
        token = message_token(raw)
        if token in self._sent_fingerprints:
            self.prompt = None
            self._sending = True
            self.notice = "already sent"
            self._clear_compose_fields()
            self.load_reader(fetch=False)
            return
        # One send at a time. The dialog stays up when Gmail refuses or Sent
        # does not yet show this token, so the same draft can be confirmed again.
        self._sending = True
        try:
            self.engine.send_reply(raw, self.compose_thread)
        except GmailError as exc:
            text = str(exc) or "send failed"
            if "already sent" in text:
                self._sent_fingerprints.add(token)
                self.prompt = None
                self.notice = "already sent"
                self._clear_compose_fields()
                self.load_reader(fetch=False)
                return
            self._sending = False
            self.prompt = "send"
            self.notice = text
            return
        self._sent_fingerprints.add(token)
        self.prompt = None
        self.notice = "sent"
        self._clear_compose_fields()
        self.load_reader(fetch=False)

    def _clear_compose_fields(self) -> None:
        self.compose_to = ""
        self.compose_cc = ""
        self.compose_bcc = ""
        self.compose_subject = ""
        self.compose_lines = []
        self.compose_thread = ""
        self.compose_reply_to = ""
        self._compose_matches = []

    def save_draft(self) -> None:
        if self.prompt != "send" or self._sending:
            return
        self.prompt = None
        raw = compose_message(
            from_addr=self.engine.store.account_email(),
            to_addr=self.compose_to,
            cc_addr=self.compose_cc,
            bcc_addr=self.compose_bcc,
            subject=self.compose_subject,
            body="\n".join(self.compose_lines),
            in_reply_to=self.compose_reply_to,
        )
        try:
            self.engine.save_draft(raw, self.compose_thread)
        except Offline as exc:
            self.notice = str(exc) or "offline"
            return
        except GmailError as exc:
            self.notice = str(exc) or "draft failed"
            return
        self.notice = "saved draft"
        self.load_reader(fetch=False)

    def prompt_type(self, text: str) -> None:
        if not self.prompt or not text:
            return
        if self.prompt == "quit":
            return
        if self.prompt == "compose":
            self._set_compose_field(self._compose_field() + text)
            self._compose_matches = []
            self.prompt_hint = ""
            return
        if self.prompt == "send":
            if text == "j":
                self.prompt_move(1)
            elif text == "k":
                self.prompt_move(-1)
            elif text.strip().lower() == "y":
                self.prompt_cursor = 0
                self.confirm_send()
            elif text.strip().lower() == "n":
                self.prompt_cancel()
            return
        if self.prompt == "save":
            self.prompt_text += text
            self._clear_save_complete()
            self.prompt_hint = ""
            return
        if self.prompt == "snooze":
            if text == "j":
                self.prompt_move(1)
            elif text == "k":
                self.prompt_move(-1)
            elif text.lower() in "0123456789mhd":
                self.prompt_text += text.lower()
            return
        if self.prompt == "holding":
            if text in ("d", "D"):
                self.release_holding()
            elif text == "j":
                self.prompt_move(1)
            elif text == "k":
                self.prompt_move(-1)
            return
        self.prompt_text += text
        self.prompt_cursor = 0
        self._refresh_prompt()

    def prompt_backspace(self) -> None:
        if self.prompt == "compose":
            self._set_compose_field(self._compose_field()[:-1])
            self._compose_matches = []
            self.prompt_hint = ""
            return
        if self.prompt == "save":
            self.prompt_text = self.prompt_text[:-1]
            self._clear_save_complete()
            self.prompt_hint = ""
            return
        if self.prompt == "snooze":
            if self.prompt_text:
                self.prompt_text = self.prompt_text[:-1]
            return
        if not self.prompt:
            return
        if not self.prompt_text:
            # Empty search field + backspace leaves search mode (like Escape).
            if self.prompt == "search":
                self.prompt_cancel()
            return
        self.prompt_text = self.prompt_text[:-1]
        self.prompt_cursor = 0
        self._refresh_prompt()

    def _clear_save_complete(self) -> None:
        self._save_matches = []
        self._save_match_i = -1
        self._save_cycle_dir = ""
        self._save_cycle_tilde = False
        self._save_listing = []

    def prompt_list_folders(self) -> None:
        """Show or hide the directories under the save path."""
        if self.prompt != "save":
            return
        if self._save_listing:
            self._save_listing = []
            self.prompt_hint = ""
            return
        directory, prefix, _tilde = _save_path_parts(self.prompt_text)
        folders = _save_path_folders(directory, prefix)
        if not folders:
            self._save_listing = []
            self.prompt_hint = "no folders"
            return
        self._save_listing = folders
        self.prompt_hint = f"{len(folders)} folders"

    def prompt_complete(self, *, reverse: bool = False) -> None:
        if self.prompt != "save":
            return
        self._save_listing = []
        if len(self._save_matches) > 1:
            step = -1 if reverse else 1
            self._save_match_i = (self._save_match_i + step) % len(self._save_matches)
            name = self._save_matches[self._save_match_i]
            self.prompt_text = _join_save_path(
                self._save_cycle_dir,
                name,
                self._save_cycle_tilde,
                directory_ok=True,
            )
            self.prompt_hint = f"{self._save_match_i + 1}/{len(self._save_matches)}"
            return
        directory, prefix, tilde = _save_path_parts(self.prompt_text)
        matches = _save_path_matches(directory, prefix)
        if not matches:
            self.prompt_hint = "no match"
            self._clear_save_complete()
            return
        common = os.path.commonprefix(matches)
        if len(matches) == 1:
            self.prompt_text = _join_save_path(directory, matches[0], tilde, directory_ok=True)
            self._clear_save_complete()
            self.prompt_hint = ""
            return
        if common and common != prefix:
            self.prompt_text = _join_save_path(directory, common, tilde, directory_ok=False)
            self._save_matches = matches
            self._save_match_i = -1
            self._save_cycle_dir = directory
            self._save_cycle_tilde = tilde
            self.prompt_hint = f"{len(matches)} matches"
            return
        self._save_matches = matches
        self._save_match_i = -1
        self._save_cycle_dir = directory
        self._save_cycle_tilde = tilde
        step = -1 if reverse else 1
        self._save_match_i = (self._save_match_i + step) % len(matches)
        name = matches[self._save_match_i]
        self.prompt_text = _join_save_path(directory, name, tilde, directory_ok=True)
        self.prompt_hint = f"{self._save_match_i + 1}/{len(matches)}"

    def prompt_move(self, delta: int, *, wrap: bool = False) -> None:
        if self.prompt == "save":
            count = len(_SAVE_CHOICES)
            if count <= 0 or delta == 0:
                return
            if wrap:
                self.prompt_cursor = (self.prompt_cursor + delta) % count
            else:
                self.prompt_cursor = _clamp(self.prompt_cursor + delta, count)
            return
        if self.prompt == "compose":
            self.prompt_cursor = _clamp(self.prompt_cursor + delta, 3)
            self._compose_matches = []
            self.prompt_hint = ""
            return
        if self.prompt == "send":
            if delta == 0:
                return
            self.prompt_cursor = _clamp(self.prompt_cursor + delta, len(_COMPOSE_CHOICES))
            return
        if not self.prompt or delta == 0 or self.prompt == "quit":
            return
        if self.prompt == "snooze":
            self.prompt_cursor = _clamp(self.prompt_cursor + delta, len(self._snooze_choices))
            return
        if self.prompt == "holding":
            self.prompt_cursor = _clamp(self.prompt_cursor + delta, len(self.held))
            return
        if self.prompt == "label":
            self.prompt_cursor = _clamp(self.prompt_cursor + delta, len(self.completions))
            return
        if self._prompt_focus == "labels":
            self.label_cursor = _clamp(self.label_cursor + delta, len(self.visible))
            return
        if self._prompt_focus == "list":
            self.message_cursor = _clamp(self.message_cursor + delta, len(self.messages))
            self.load_reader(fetch=False)
            return
        if not self._find_lines:
            return
        self.prompt_cursor = _clamp(self.prompt_cursor + delta, len(self._find_lines))
        self._show_find()

    def prompt_cancel(self) -> None:
        if not self.prompt:
            return
        if self.prompt == "compose":
            self.prompt = None
            self.prompt_text = ""
            self.prompt_hint = ""
            self._reset_compose()
            return
        if self.prompt == "send":
            self.prompt = None
            self.prompt_text = ""
            self.notice = "not sent"
            if self.messages:
                self.load_reader(fetch=False)
            return
        if self.prompt == "holding":
            self.prompt = None
            self.prompt_text = ""
            self.held = []
            return
        if self.prompt in {"save", "snooze"}:
            self.prompt = None
            self.prompt_text = ""
            self.prompt_hint = ""
            self._snooze_choices = []
            return
        if self.prompt == "quit":
            self.prompt = None
            self.prompt_text = ""
            return
        self.prompt = None
        self.prompt_text = ""
        self.prompt_hint = ""
        self.completions = []
        self.visible = list(self._saved_visible)
        self.label_cursor = self._saved_label_cursor
        self.messages = list(self._saved_messages)
        self.message_cursor = self._saved_message_cursor
        self.reader_scroll = self._saved_reader_scroll
        self.find_line = None
        self.find_query = ""
        self._find_lines = []
        if self.messages:
            self.load_reader(fetch=False)
            self.reader_scroll = self._saved_reader_scroll

    def prompt_accept(self) -> None:
        if self.prompt == "compose":
            self.prompt = None
            self.prompt_hint = ""
            self._compose_matches = []
            if not self.begin_external_edit():
                self.prompt = "compose"
            return
        if self.prompt == "send":
            choice = _COMPOSE_CHOICES[_clamp(self.prompt_cursor, len(_COMPOSE_CHOICES))]
            if choice == "Send":
                self.confirm_send()
            else:
                self.save_draft()
            return
        if self.prompt == "holding":
            self.release_holding()
            return
        if self.prompt == "save":
            choice = _SAVE_CHOICES[_clamp(self.prompt_cursor, len(_SAVE_CHOICES))]
            if choice == "open":
                self._open_attachment()
            else:
                self._accept_save()
            return
        if self.prompt == "snooze":
            self._accept_snooze()
            return
        if self.prompt == "quit":
            self.prompt = None
            self.prompt_text = ""
            self.quitting = True
            return
        if self.prompt == "label":
            self._accept_label()
        elif self._prompt_focus == "labels":
            self._accept_folder_search()
        elif self._prompt_focus == "list":
            self._accept_mail_search()
        else:
            self.prompt = None
            self.prompt_text = ""
            self.completions = []

    def _accept_snooze(self) -> None:
        targets = self._targets()
        if not targets:
            self.prompt = None
            self.prompt_text = ""
            self._snooze_choices = []
            self.notice = "no message selected"
            return
        now = datetime.now().astimezone()
        custom = parse_duration(self.prompt_text, now=now)
        if custom is not None:
            until = custom
        elif self._snooze_choices:
            until = self._snooze_choices[
                _clamp(self.prompt_cursor, len(self._snooze_choices))
            ].until
        else:
            self.notice = "pick a time"
            return
        if until <= now:
            self.notice = "snooze time is in the past"
            return
        until_ms = int(until.timestamp() * 1000)
        for gmail_id in targets:
            self.engine.store.schedule_snooze(gmail_id, until_ms)
        self.prompt = None
        self.prompt_text = ""
        self.prompt_hint = ""
        self._snooze_choices = []
        # Archive out of the inbox; wake_due_snoozes puts INBOX back later.
        self._change_selected(remove=["INBOX"])
        # Snooze already writes the wake-up time on this key, so land the
        # leave-view label change with it instead of reporting INBOX forever
        # while the account thread waits for a turn.
        self.flush_label_todo()
        hour = until.strftime("%I").lstrip("0") or "12"
        stamp = f"{until.strftime('%a')} {hour}:{until.strftime('%M%p')}"
        count = len(targets)
        self.notice = (
            f"snoozed until {stamp}"
            if count == 1
            else f"snoozed {count} until {stamp}"
        )

    def _remember(self) -> None:
        self._saved_visible = list(self.visible)
        self._saved_label_cursor = self.label_cursor
        self._saved_messages = list(self.messages)
        self._saved_message_cursor = self.message_cursor
        self._saved_reader_scroll = self.reader_scroll

    def _refresh_prompt(self) -> None:
        if self.prompt == "label":
            self._refresh_label_prompt()
        elif self._prompt_focus == "labels":
            self._refresh_folder_search()
        elif self._prompt_focus == "list":
            self._refresh_mail_search()
        else:
            self._apply_reader_find()

    def _refresh_folder_search(self) -> None:
        query = self.prompt_text.strip()
        if not query:
            self.visible = list(self._saved_visible)
            self.label_cursor = min(self._saved_label_cursor, max(0, len(self.visible) - 1))
            self.prompt_hint = ""
            return
        ranked = _rank_labels(self._all_labels(), query, require_id=False)
        self.visible = [
            FlatLabel(
                key=item.key,
                depth=0,
                name=_label_title(item),
                path=item.path,
                gmail_id=item.gmail_id,
                unread=item.unread,
                expandable=False,
                expanded=False,
                total=item.total,
            )
            for item in ranked
        ]
        self.label_cursor = 0
        self.prompt_hint = str(len(self.visible))

    def _refresh_mail_search(self) -> None:
        query = self.prompt_text.strip()
        if not query:
            self.messages = list(self._saved_messages)
            self.message_cursor = self._saved_message_cursor
            self.load_reader(fetch=False)
            self.reader_scroll = self._saved_reader_scroll
            self.prompt_hint = ""
            return
        try:
            found = self.engine.store.search_messages(parse_query(query), 100)
        except sqlite3.OperationalError:
            found = []
            self.prompt_hint = "bad query"
        else:
            self.prompt_hint = str(len(found))
        self.messages = found
        self._more = False
        self.message_cursor = 0
        self.message_scroll = 0
        self.load_reader(fetch=False)

    def _apply_reader_find(self) -> None:
        query = self.prompt_text.strip()
        self.find_query = query
        if not query:
            self._find_lines = []
            self.find_line = None
            self.prompt_hint = ""
            self.reader_scroll = self._saved_reader_scroll
            return
        lowered = query.lower()
        self._find_lines = [
            index
            for index, line in enumerate(self.reader)
            if lowered in self._find_text(line).lower()
        ]
        if not self._find_lines:
            self.find_line = None
            self.prompt_hint = "0"
            return
        self.prompt_cursor = 0
        self._show_find()

    def _show_find(self) -> None:
        if not self._find_lines:
            return
        self.prompt_cursor = _clamp(self.prompt_cursor, len(self._find_lines))
        self.find_line = self._find_lines[self.prompt_cursor]
        if self._reader_starts and self.find_line < len(self._reader_starts):
            self.reader_scroll = self._reader_starts[self.find_line]
        else:
            self.reader_scroll = self.find_line
        self.prompt_hint = f"{self.prompt_cursor + 1}/{len(self._find_lines)}"

    def _refresh_label_prompt(self) -> None:
        current = self.messages[self.message_cursor].gmail_id if self.messages else ""
        already = self.engine.store.message_label_ids(current) if current else set()
        catalog = self._all_labels()
        query = self.prompt_text.strip()
        ranked = _rank_labels(catalog, query, require_id=True)
        choices: list[LabelChoice] = []
        for item in ranked:
            title = _label_title(item)
            if item.gmail_id in already:
                choices.append(LabelChoice(f"- {title}", item.gmail_id, remove=True))
            else:
                choices.append(LabelChoice(title, item.gmail_id))
        if query and not _exact_label(catalog, query):
            create = LabelChoice(f'create "{query}"', None, query)
            strong = any(
                (score := _label_score(item, query)) is not None and score <= 1
                for item in ranked
            )
            if strong:
                choices.append(create)
            else:
                choices.insert(0, create)
        self.completions = choices
        self.prompt_cursor = _clamp(self.prompt_cursor, len(self.completions))
        self.prompt_hint = str(len(self.completions))

    def _accept_label(self) -> None:
        targets = self._targets()
        if not self.completions or not targets:
            self.prompt_hint = "no matching label"
            return
        chosen = self.completions[self.prompt_cursor]
        if chosen.create_name:
            gmail_id = self.engine.store.create_user_label(chosen.create_name)
        else:
            gmail_id = chosen.gmail_id or ""
        if not gmail_id:
            self.prompt_hint = "no matching label"
            return
        self.prompt = None
        self.prompt_text = ""
        self.completions = []
        if chosen.remove:
            self._change_selected(remove=[gmail_id])
        else:
            self._change_selected(add=[gmail_id])

    def _accept_folder_search(self) -> None:
        query = self.prompt_text.strip()
        if not query:
            self.prompt_cancel()
            return
        if not self.visible:
            self.prompt_hint = "no match"
            return
        chosen = self.visible[self.label_cursor]
        self.prompt = None
        self.prompt_text = ""
        self.completions = []
        self._expand_to(chosen.key)
        for index, item in enumerate(self.visible):
            if item.key == chosen.key:
                self.label_cursor = index
                break
        if chosen.gmail_id is not None:
            self.load_messages(fetch=False)
            self.focus = "list"

    def _accept_mail_search(self) -> None:
        query = self.prompt_text.strip()
        self.prompt = None
        self.prompt_text = ""
        self.completions = []
        if not query:
            self.messages = list(self._saved_messages)
            self.message_cursor = self._saved_message_cursor
            self.load_reader(fetch=False)
            return
        kept_id = self.messages[self.message_cursor].gmail_id if self.messages else None
        response = self.engine.search(query, 100)
        self.messages = self._in_order(
            self.engine.store.summaries_for([hit.gmail_id for hit in response.hits])
        )
        self._more = False
        self.search_text = query
        self.message_cursor = 0
        if kept_id:
            for index, item in enumerate(self.messages):
                if item.gmail_id == kept_id:
                    self.message_cursor = index
                    break
        self.message_scroll = _reveal_list(
            self.message_cursor,
            self.message_scroll,
            max(1, self.message_window),
            len(self.messages),
            more=self._more,
        )
        self.focus = "list"
        self.load_reader(fetch=False)
        if response.remote_error:
            self.sync_phase = str(response.remote_error)

    def _show_saved_search(self) -> None:
        message_id = self.messages[self.message_cursor].gmail_id if self.messages else None
        reader_scroll = self.reader_scroll
        try:
            found = self.engine.store.search_messages(
                parse_query(self.search_text), 100, newest_first=self.newest_first
            )
        except sqlite3.OperationalError:
            return
        self.messages = found
        self._more = False
        if message_id:
            for index, item in enumerate(self.messages):
                if _row_identity(item) == message_id:
                    self.message_cursor = index
                    break
        self.message_scroll = _reveal_list(
            self.message_cursor,
            self.message_scroll,
            max(1, self.message_window),
            len(self.messages),
            more=self._more,
        )
        self.load_reader(fetch=False)
        self.reader_scroll = reader_scroll

    def _reload_current_message(self) -> None:
        if not self.messages:
            return
        message_id = _row_identity(self.messages[self.message_cursor])
        cursor = self.message_cursor
        scroll = self.reader_scroll
        if self.search_text:
            self._show_saved_search()
        else:
            current = self.current_label()
            label_id = current.gmail_id if current is not None else None
            self.reload_labels()
            if label_id is not None:
                for index, item in enumerate(self.visible):
                    if item.gmail_id == label_id:
                        self.label_cursor = index
                        break
            self.load_messages(fetch=False)
        for index, item in enumerate(self.messages):
            if _row_identity(item) == message_id:
                self.message_cursor = index
                break
        else:
            self.message_cursor = min(cursor, max(0, len(self.messages) - 1))
        self.load_reader(fetch=False)
        self.reader_scroll = scroll

    def _in_order(self, messages: list[MessageSummary]) -> list[MessageSummary]:
        return sorted(
            messages,
            key=lambda item: (item.internal_date_ms, item.message_id),
            reverse=self.newest_first,
        )

    def _label_tree(self):
        # Startup used to build this twice (reload_labels + open_inbox). Reuse
        # the tree until reload_labels clears the cache.
        cached = getattr(self, "_label_tree_cache", None)
        if cached is not None:
            return cached
        tree = build_label_tree(self.engine.store.list_label_info())
        if tree and tree[0].gmail_id == "":
            tree[0].total, tree[0].unread = self.engine.store.mailbox_counts()
        self._label_tree_cache = tree
        return tree

    def _all_labels(self) -> list[FlatLabel]:
        tree = self._label_tree()
        return flatten(tree, expandable_keys(tree))

    def _expand_to(self, key: str) -> None:
        tree = self._label_tree()
        ancestors = _ancestor_keys(tree, key)
        if ancestors:
            self.expanded |= ancestors
        self.visible = flatten(tree, self.expanded)

    def _viewing_trash(self) -> bool:
        current = self.current_label()
        return current is not None and current.gmail_id == "TRASH"

    def _row_ids(self, item: MessageSummary) -> list[str]:
        if item.thread_role == "head" and item.member_ids:
            return list(item.member_ids)
        return [item.gmail_id]

    def _nudge_thread(self, right: bool) -> bool:
        """Open a collapsed thread to the right, or close an open one to the left."""
        if not self.messages:
            return False
        current = self.messages[_clamp(self.message_cursor, len(self.messages))]
        if current.thread_count <= 1 or current.thread_role not in {"head", "child"}:
            return False
        opened = current.thread_id in self.expanded_threads
        if right and current.thread_role == "head" and not opened:
            self.expanded_threads.add(current.thread_id)
            self._show_threads()
            self._restore_row(current.row_key)
            self.load_reader(fetch=False)
            return True
        if not right and opened:
            self.expanded_threads.discard(current.thread_id)
            key = f"t:{current.thread_id}"
            self._show_threads()
            self._restore_row(key)
            self.load_reader(fetch=False)
            return True
        return False

    def _restore_row(self, key: str) -> None:
        for index, item in enumerate(self.messages):
            if _row_identity(item) == key:
                self.message_cursor = index
                return

    def _show_threads(self) -> None:
        rows: list[MessageSummary] = []
        for head in self._mail_threads:
            shown = self._thread_head(head)
            if self._row_hidden(shown):
                continue
            rows.append(shown)
            if shown.expanded:
                rows.extend(
                    child for child in self._thread_children(shown) if not self._row_hidden(child)
                )
        self.messages = rows

    def _thread_head(self, head: MessageSummary) -> MessageSummary:
        if head.thread_count <= 1:
            return head
        return replace(
            head,
            subject=bare_subject(head.subject),
            expanded=head.thread_id in self.expanded_threads,
        )

    def _thread_children(self, head: MessageSummary) -> list[MessageSummary]:
        messages = self.engine.store.messages_in_thread(
            head.thread_id, hide_trash=not self._viewing_trash()
        )
        return [
            replace(
                message,
                subject=f"{prefix}{bare_subject(message.subject)}",
                thread_role="child",
                thread_count=head.thread_count,
                thread_id=head.thread_id,
                row_key=message.gmail_id,
            )
            for message, prefix in arrange(messages)
        ]

    def _refresh_thread_unread(self) -> None:
        hide_trash = not self._viewing_trash()
        fresh: dict[int, dict[str, bool]] = {}
        for head in self._mail_threads:
            if head.thread_count <= 1:
                continue
            fresh[head.thread_id] = {
                message.gmail_id: message.unread
                for message in self.engine.store.messages_in_thread(
                    head.thread_id, hide_trash=hide_trash
                )
            }

        def unread_of(item: MessageSummary) -> bool:
            flags = fresh.get(item.thread_id)
            if not flags:
                return item.unread
            if item.member_ids:
                return any(flags.get(gmail_id, False) for gmail_id in item.member_ids)
            if item.gmail_id in flags:
                return flags[item.gmail_id]
            return item.unread

        self.messages = [
            replace(item, unread=unread_of(item)) if item.thread_id in fresh else item
            for item in self.messages
        ]
        self._mail_threads = [
            replace(item, unread=unread_of(item)) if item.thread_id in fresh else item
            for item in self._mail_threads
        ]

    def _select_initial(self) -> None:
        self.open_inbox()

    def _fill_page(self, *, force: bool = False) -> None:
        if not self._more or not self._mail_threads:
            return
        if not force and self.message_cursor < len(self.messages) - 15:
            return
        current = self.current_label()
        if current is None or current.gmail_id is None:
            return
        last = self._mail_threads[-1]
        label = None if current.gmail_id == "" else current.gmail_id
        extra = self.engine.store.list_mail_threads(
            label,
            ThreadCursor(last.internal_date_ms, last.thread_id),
            PAGE,
            newest_first=self.newest_first,
        )
        if len(extra) < PAGE:
            self._more = False
        self._mail_threads.extend(extra)
        self._show_threads()

    def _refresh_attachment_flag(self, gmail_id: str) -> None:
        preview = self.engine.store.preview_message(gmail_id)
        if preview is None:
            return
        current = self.messages[self.message_cursor]
        if current.gmail_id != gmail_id or current.has_attachment == preview.has_attachment:
            return
        self.messages[self.message_cursor] = replace(current, has_attachment=preview.has_attachment)


def wrap_lines(lines: list[str], width: int) -> tuple[list[str], list[int]]:
    """Break each line on a space so it fits ``width`` columns.

    A token wider than the pane is split at the last character that fits.
    The second list is the wrapped row where each source line begins.
    """
    view: list[str] = []
    starts: list[int] = []
    for line in lines:
        starts.append(len(view))
        view.extend(piece for _start, _end, piece in _wrap_spans(line, width))
    return view, starts


def _wrap_spans(text: str, width: int) -> list[tuple[int, int, str]]:
    text = _plain(text)
    if width <= 0 or not text:
        return [(0, len(text), text)]
    if text.isascii():
        if len(text) <= width:
            return [(0, len(text), text)]
        return _wrap_columns(text, width, ascii_text=True)
    if _display_width(text) <= width:
        return [(0, len(text), text)]
    return _wrap_columns(text, width, ascii_text=False)


def _wrap_columns(text: str, width: int, *, ascii_text: bool) -> list[tuple[int, int, str]]:
    """One pass. A long line used to remeasure its tail on every row."""
    spans: list[tuple[int, int, str]] = []
    start = 0
    size = len(text)
    while start < size:
        used = 0
        index = start
        last_space = -1
        while index < size:
            char_width = 1 if ascii_text else _char_width(text[index])
            if used + char_width > width and index > start:
                break
            if text[index] == " " and used > 0:
                last_space = index
            used += char_width
            index += 1
        if index >= size:
            spans.append((start, size, text[start:]))
            break
        if last_space > start:
            piece_end = last_space
            while piece_end > start and text[piece_end - 1] == " ":
                piece_end -= 1
            if piece_end > start:
                spans.append((start, piece_end, text[start:piece_end]))
            nxt = last_space + 1
            while nxt < size and text[nxt] == " ":
                nxt += 1
            start = nxt if nxt > start else index
            continue
        spans.append((start, index, text[start:index]))
        start = index
    return spans or [(0, 0, "")]


def layout_reader(
    lines: list[str],
    pane: int,
    term: int = 0,
    left: int = 0,
    *,
    hide_urls: bool = False,
) -> tuple[list[str], list[int], list[ReaderMark]]:
    """Wrap reader lines inside the pane and note each ``link`` word.

    ``term`` and ``left`` are unused. Long URLs wrap inside the pane like any
    other line. The word link still carries the whole address. With
    ``hide_urls`` only that word is drawn; the address lives in the hyperlink.
    """
    del term, left
    pane = max(1, pane)
    view: list[str] = []
    starts: list[int] = []
    marks: list[ReaderMark] = []
    for line in lines:
        starts.append(len(view))
        plain = _plain(line)
        urls: list[tuple[int, str]] = []
        if "link " in plain:
            if hide_urls:
                plain, urls = strip_link_urls(plain)
            else:
                urls = [(start, url) for start, _end, url in link_spans(plain)]
        pieces = _wrap_spans(plain, pane)
        for begin, _end, piece in pieces:
            row_links: list[tuple[int, str]] = []
            for start, url in urls:
                local = start - begin
                if local < 0 or local >= len(piece):
                    continue
                if piece[local : local + len(LABEL)] != LABEL:
                    continue
                row_links.append((_display_width(piece[:local]), url))
            view.append(piece)
            marks.append(ReaderMark(links=tuple(row_links)))
    return view, starts, marks


def reader_hyperlinks(
    view: list[str],
    marks: list[ReaderMark],
    scroll: int,
    top: int,
    pane_left: int,
    rows: int,
    term: int,
) -> list[tuple[int, int, str, str]]:
    """``(y, x, link, url)`` for each visible ``link`` word inside the reader."""
    del term
    writes: list[tuple[int, int, str, str]] = []
    last = min(len(view), len(marks), scroll + max(0, rows))
    for index in range(max(0, scroll), last):
        shown = view[index]
        y = top + index - scroll
        for column, url in marks[index].links:
            if shown[column : column + len(LABEL)] != LABEL:
                continue
            writes.append((y, pane_left + column, LABEL, url))
    return writes


def link_at(
    view: list[str],
    marks: list[ReaderMark],
    scroll: int,
    y: int,
    x: int,
    *,
    top: int,
    pane_left: int,
    rows: int,
) -> str | None:
    """URL when ``(y, x)`` lands on a visible ``link`` word."""
    for ly, lx, text, url in reader_hyperlinks(view, marks, scroll, top, pane_left, rows, 0):
        if y == ly and lx <= x < lx + len(text):
            return url
    return None


def reader_match(session: MailSession, width: int) -> tuple[int, int, str] | None:
    """Wrapped row, display column, and the visible slice of the current find."""
    if session.find_line is None or not session.find_query:
        return None
    if session.find_line < 0 or session.find_line >= len(session.reader):
        return None
    line = _plain(session._find_text(session.reader[session.find_line]))
    start = line.lower().find(session.find_query.lower())
    if start < 0:
        return None
    end = start + len(session.find_query)
    spans = _wrap_spans(line, max(1, width))
    origin = 0
    if session._reader_starts and session.find_line < len(session._reader_starts):
        origin = session._reader_starts[session.find_line]
    for offset, (left, right, piece) in enumerate(spans):
        if start < left:
            continue
        if start >= right and offset + 1 < len(spans):
            continue
        column = _display_width(piece[: max(0, start - left)])
        shown = piece[max(0, start - left) : max(0, end - left)]
        if not shown:
            continue
        return origin + offset, column, shown
    return None


def format_reader(
    preview: MessagePreview | None,
    attachment: int | None = None,
    mime: int | None = None,
    *,
    mime_focus: bool = False,
    headers: list[tuple[str, str]] | None = None,
) -> list[str]:
    if preview is None:
        return ["(no message)"]
    labels = ", ".join(preview.label_names) or "(none)"
    if headers:
        top = [f"{name}: {value}" for name, value in headers]
    else:
        top = [
            f"From: {preview.from_addr}",
            f"To: {preview.to_addr}",
            f"Date: {format_when(preview.internal_date_ms)}",
            f"Subject: {preview.subject}",
        ]
    lines = [
        *top,
        f"Labels: {labels}",
        *_mime_lines(preview, mime, mime_focus=mime_focus),
        *_attachment_lines(preview, attachment),
        "",
    ]
    if preview.body_cached:
        # Same text with or without MIME focus: the preferred (or selected)
        # part. Stored body_text is only a fallback before parts are known.
        body_text = preview.body_text
        if preview.mime_parts:
            chosen = mime
            if chosen is None:
                chosen = preview.mime_parts[preferred_mime_index(preview.mime_parts)].index
            for part in preview.mime_parts:
                if part.index == chosen:
                    body_text = part.text
                    break
        body = present_links(body_text).splitlines() or ["(empty message)"]
        lines.extend(body)
    elif preview.snippet:
        lines.append(present_links(preview.snippet))
        lines.append("")
        lines.append("(body not downloaded)")
    else:
        lines.append("(body not downloaded)")
    return lines


def format_source(raw: bytes) -> list[str]:
    """The saved message as text, line for line. Nothing is decoded or hidden."""
    return raw.decode("utf-8", errors="replace").splitlines() or ["(empty message)"]


def _mime_lines(
    preview: MessagePreview,
    selected: int | None = None,
    *,
    mime_focus: bool = False,
) -> list[str]:
    if not preview.body_cached:
        return []
    if not preview.mime_parts:
        return ["MIME: none"]
    lines = ["MIME:"]
    for item in preview.mime_parts:
        mark = ">" if mime_focus and selected == item.index else " "
        lines.append(f"{mark} {item.index}  {item.content_type}  {item.size} B")
    return lines


def _attachment_lines(preview: MessagePreview, selected: int | None = None) -> list[str]:
    if preview.attachments:
        lines = ["Attachments:"]
        for item in preview.attachments:
            mark = ">" if selected == item.index else " "
            lines.append(f"{mark} {item.index}  {item.filename}  {item.content_type}  {item.size} B")
        return lines
    if preview.has_attachment and not preview.body_cached:
        return ["Attachments: yes"]
    return ["Attachments: none"]


def format_when(millis: int) -> str:
    from gmail_tui.formats import format_millis

    return format_millis(millis)


def geometry(height: int, width: int) -> Geometry:
    height = max(height, 9)
    width = max(width, 40)
    top = 1
    mail_h = height - top
    sidebar_w = min(36, max(22, width // 4))
    if sidebar_w >= width - 20:
        sidebar_w = max(16, width // 3)
    list_span = max(7, (mail_h - 1) * 45 // 100)
    if list_span > mail_h - 6:
        list_span = max(5, mail_h // 2)
    list_h = top + list_span
    bottom = height - 2
    if list_h >= bottom:
        list_h = bottom - 1
    # The folder list stops at the mail list. The reader is the full width under both.
    label_rows = max(1, list_h - top - 1)
    # The list pane uses one row for the column names and one for the rule under them.
    message_rows = max(1, list_h - top - 3)
    reader_rows = max(1, bottom - list_h - 1)
    return Geometry(height, width, sidebar_w, list_h, label_rows, message_rows, reader_rows, top)


def render_lines(
    session: MailSession,
    height: int,
    width: int,
    accounts: list[Account] | None = None,
    active: int = 0,
) -> list[str]:
    geo = geometry(height, width)
    session.popup = None
    session.popup_choices = ()
    session.reveal(geo)
    accounts = _accounts_for(session, accounts, active)
    canvas = _Canvas(geo.height, geo.width)
    _write_tabs(canvas, session, accounts, active, geo.width)
    _frame(canvas, geo, _list_title(session), session.focus)
    _write_labels(canvas, session, geo)
    _write_messages(canvas, session, geo)
    _write_scrollbar(canvas, session, geo)
    _write_reader(canvas, session, geo)
    _write_completions(canvas, session, geo)
    _write_holding(canvas, session, geo)
    _write_snooze(canvas, session, geo)
    _write_save(canvas, session, geo)
    _write_quit(canvas, session, geo)
    _write_compose(canvas, session, geo)
    _write_compose_addresses(canvas, session, geo)
    _write_help(canvas, session, geo)
    canvas.write(geo.height - 1, 0, status_bar(session, geo.width))
    return canvas.lines()


def _row_identity(item: MessageSummary) -> str:
    return item.row_key or item.gmail_id


def _list_index(
    messages: list[MessageSummary],
    row_id: str | None,
    gmail_id: str,
    thread_id: int,
) -> int | None:
    if row_id:
        for index, item in enumerate(messages):
            if _row_identity(item) == row_id:
                return index
    if gmail_id:
        for index, item in enumerate(messages):
            if item.gmail_id == gmail_id:
                return index
            if gmail_id in (item.member_ids or ()):
                return index
    if thread_id:
        for index, item in enumerate(messages):
            if item.thread_id == thread_id and item.thread_role != "child":
                return index
    return None


def _list_cursor_for(
    messages: list[MessageSummary],
    row_id: str | None,
    gmail_id: str,
    thread_id: int,
    kept_cursor: int,
) -> int:
    """Keep the open conversation selected after a mailbox reload."""
    found = _list_index(messages, row_id, gmail_id, thread_id)
    if found is not None:
        return found
    if not messages:
        return 0
    return min(max(0, kept_cursor), len(messages) - 1)


def _merge_mail_page(
    fresh: list[MessageSummary],
    fresh_threads: list[MessageSummary],
    kept: list[MessageSummary],
    kept_threads: list[MessageSummary],
    kept_more: bool,
) -> tuple[list[MessageSummary], list[MessageSummary], bool]:
    """Keep a deep paged window while letting the newest page replace overlaps."""
    fresh_thread_ids = {item.thread_id for item in fresh}
    trimmed = [item for item in kept if item.thread_id not in fresh_thread_ids]
    trimmed_threads = [item for item in kept_threads if item.thread_id not in fresh_thread_ids]
    return list(fresh) + trimmed, list(fresh_threads) + trimmed_threads, kept_more


def _known_mail_ids(messages: list[MessageSummary]) -> set[str]:
    known: set[str] = set()
    for item in messages:
        if item.gmail_id:
            known.add(item.gmail_id)
        known.update(item.member_ids or ())
    return known


def _new_mail_indexes(
    previous: list[MessageSummary],
    current: list[MessageSummary],
) -> list[int]:
    """Indexes of list rows that introduce mail the previous view did not have."""
    known = _known_mail_ids(previous)
    if not known:
        return []
    found: list[int] = []
    for index, item in enumerate(current):
        members = item.member_ids or ((item.gmail_id,) if item.gmail_id else ())
        if any(member not in known for member in members):
            found.append(index)
    return found


def _row_marked(item: MessageSummary, selected: set[str]) -> bool:
    if item.thread_role == "head" and item.member_ids:
        return all(gmail_id in selected for gmail_id in item.member_ids)
    return item.gmail_id in selected


def _thread_subject(message: MessageSummary) -> str:
    if message.thread_role == "head" and message.thread_count > 1:
        glyph = "v" if message.expanded else ">"
        return f"{glyph} {message.thread_count}  {message.subject}"
    return message.subject


def message_row(
    message: MessageSummary,
    width: int,
    selected: bool = False,
    columns: tuple[int, int, int, int] | None = None,
) -> str:
    mark = "x " if selected else "  "
    if width <= len(mark):
        return _clip(mark, width)
    return mark + _message_line(
        message.from_addr,
        message.to_addr,
        _thread_subject(message),
        ", ".join(message.label_names),
        "*" if message.has_attachment else " ",
        width - len(mark),
        format_when(message.internal_date_ms),
        columns,
        "U" if message.unread else " ",
    )


def _list_title(session: MailSession) -> str:
    if session.prompt == "search" and session._prompt_focus == "list" and session.prompt_text.strip():
        return f"Search: {session.prompt_text.strip()}  {len(session.messages)}"
    if session.search_text:
        return f"Search: {session.search_text}  {len(session.messages)}"
    current = session.current_label()
    if current is None:
        return "Mail"
    count = len(session.messages)
    place = session.message_cursor + 1 if count else 0
    suffix = "+" if session._more else ""
    order = "newest" if session.newest_first else "oldest"
    return f"{current.name}  {place}/{count}{suffix}  {order}"


def _accounts_for(session: MailSession, accounts: list[Account] | None, active: int) -> list[Account]:
    if not accounts:
        email = session.engine.store.account_email() or session.engine.store.root.name
        return [Account(email, session.engine.store.root, session.inbox_count, session.inbox_unread)]
    if 0 <= active < len(accounts):
        accounts[active].inbox = session.inbox_count
        accounts[active].unread = session.inbox_unread
        email = session.engine.store.account_email()
        if email:
            accounts[active].name = email
    return accounts


def format_check_age(checked_at: float | None, *, now: float | None = None) -> str:
    """Compact age for the account tab, e.g. ``12s``, ``3m``, ``2h``, ``1d``."""
    if checked_at is None:
        return ""
    seconds = max(0, int((time.time() if now is None else now) - checked_at))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def render_account_tabs(
    session: MailSession,
    accounts: list[Account] | None,
    active: int,
    width: int,
) -> str:
    """Account tab row only. Updates ``session.account_spans`` for clicks."""
    accounts = _accounts_for(session, accounts, active)
    canvas = _Canvas(1, max(1, width))
    _write_tabs(canvas, session, accounts, active, max(1, width))
    return canvas.lines()[0]


def _write_tabs(canvas: _Canvas, session: MailSession, accounts: list[Account], active: int, width: int) -> None:
    spans: list[tuple[int, int, int]] = []
    column = 0
    for index, account in enumerate(accounts):
        mark = "* " if index == active else "  "
        age = format_check_age(account.checked_at)
        suffix = f"  {age}" if age else ""
        text = f"{mark}{account.name}  {account.unread} [{account.inbox}]{suffix}"
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
        spans.append((column, column + len(shown), index))
        column += len(shown)
    session.account_spans = spans


def _write_labels(canvas: _Canvas, session: MailSession, geo: Geometry) -> None:
    top = geo.top + 1
    width = geo.sidebar_w - 2
    total_w, unread_w = _folder_count_widths(session.visible)
    rows = session.visible[session.label_scroll : session.label_scroll + geo.label_rows]
    for offset, item in enumerate(rows):
        badge = _folder_count(item.total, item.unread, total_w, unread_w)
        body = f"{item.prefix}{item.name}"
        if not badge:
            canvas.write(top + offset, 1, _clip(body, width))
            continue
        badge_w = _display_width(badge)
        room = max(0, width - badge_w)
        clipped = _clip(body, room)
        pad = max(0, room - _display_width(clipped))
        canvas.write(top + offset, 1, clipped + (" " * pad) + badge)


def _folder_count_widths(items: list[FlatLabel]) -> tuple[int, int]:
    totals = [item.total for item in items if item.total or item.unread]
    unreads = [item.unread for item in items if item.total or item.unread]
    total_w = max((len(str(value)) for value in totals), default=1)
    unread_w = max((len(str(value)) for value in unreads), default=1)
    return total_w, unread_w


def _folder_count(total: int, unread: int, total_w: int, unread_w: int) -> str:
    if total <= 0 and unread <= 0:
        return ""
    return f" {unread:>{unread_w}} [{total:>{total_w}}]"


def _write_messages(canvas: _Canvas, session: MailSession, geo: Geometry) -> None:
    left = geo.sidebar_w
    width = geo.width - geo.sidebar_w - 1
    mark = "  "
    inner = max(0, width - len(mark))
    samples = [("From", "To", "Subject", "Labels")]
    samples.extend(
        (
            _mailboxes(message.from_addr),
            _mailboxes(message.to_addr),
            _thread_subject(message),
            ", ".join(message.label_names),
        )
        for message in session.messages
    )
    columns = _fit_columns(inner, samples)
    header = mark + _message_line(
        "From", "To", "Subject", "Labels", "A", inner, "Date", columns, "U"
    )
    shown = _clip(header, width)
    canvas.write(geo.top + 1, left, shown)
    _hline(canvas, geo.top + 2, geo.sidebar_w - 1, geo.width - 1, "├", "─", "┤")
    for column in _separator_columns(shown):
        place = left + column
        if place <= geo.sidebar_w - 1 or place >= geo.width - 1:
            continue
        if canvas.grid[geo.top][place] == "─":
            canvas.write(geo.top, place, "┬")
        canvas.write(geo.top + 2, place, "┼")
    rows = session.messages[session.message_scroll : session.message_scroll + geo.message_rows]
    for offset, message in enumerate(rows):
        line = message_row(message, width, _row_marked(message, session.selected), columns)
        canvas.write(geo.top + 3 + offset, left, line)


def _separator_columns(text: str) -> list[int]:
    """Display columns where a field separator sits in a mail-list line."""
    columns: list[int] = []
    used = 0
    for char in text:
        if char == "│":
            columns.append(used)
        used += _char_width(char)
    return columns


def _write_scrollbar(canvas: _Canvas, session: MailSession, geo: Geometry) -> None:
    marks = _scrollbar_column(
        geo.message_rows,
        len(session.messages),
        session.message_cursor,
        session._more,
    )
    if not marks:
        return
    column = geo.width - 1
    for offset, mark in enumerate(marks):
        canvas.write(geo.top + 3 + offset, column, mark)


def _scrollbar_column(rows: int, count: int, cursor: int, more: bool) -> list[str]:
    """Thumb on the message-list edge. Empty when every loaded message is on screen."""
    if rows <= 0 or count <= 0 or (count <= rows and not more):
        return []
    span = count + (1 if more else 0)
    thumb = max(1, min(rows, max(1, rows * rows // span)))
    usable = rows - thumb
    if span <= 1 or usable <= 0:
        start = 0
    else:
        start = min(usable, max(0, cursor) * usable // (span - 1))
    marks = ["░"] * rows
    for offset in range(start, start + thumb):
        marks[offset] = "█"
    return marks


def _write_completions(canvas: _Canvas, session: MailSession, geo: Geometry) -> None:
    if session.prompt != "label":
        return
    titles = [item.title for item in session.completions] or ["(type a name)"]
    shown = min(8, len(titles))
    hint = "enter applies    esc cancels"
    longest = max(len(title) for title in titles)
    longest = max(longest, len(hint), len("Label"))
    right_w = max(1, geo.width - geo.sidebar_w)
    width = min(right_w - 1, geo.width - 2, max(28, longest + 4))
    if width < 20:
        width = min(geo.width - 2, max(20, longest + 4))
        left = max(0, (geo.width - width) // 2)
    else:
        left = geo.sidebar_w + max(0, (right_w - width) // 2)
    height = shown + 3
    top = max(0, (geo.height - 1 - height) // 2)
    if top + height > geo.height - 1:
        top = max(0, geo.height - 1 - height)
    for row in range(height):
        canvas.write(top + row, left, " " * width)
    _dialog(canvas, top, left, height, width, "Label")
    start = 0
    if session.completions and session.prompt_cursor >= shown:
        start = session.prompt_cursor - shown + 1
    window = titles[start : start + shown]
    for offset, title in enumerate(window):
        selected = bool(session.completions) and start + offset == session.prompt_cursor
        marker = "> " if selected else "  "
        canvas.write(top + 1 + offset, left + 1, _clip(marker + title, width - 2))
    canvas.write(top + height - 2, left + 1, _clip(hint, width - 2))
    session.popup = (top, left, height, width)


def _write_holding(canvas: _Canvas, session: MailSession, geo: Geometry) -> None:
    if session.prompt != "holding":
        return
    titles = [
        f"{item.subject or item.gmail_id}  {'+' if item.op == 'add' else '-'}{item.label}"
        for item in session.held
    ] or ["(nothing holding)"]
    shown = min(8, len(titles))
    hint = "enter releases    esc closes"
    longest = max(_display_width(title) for title in titles)
    longest = max(longest, _display_width(hint), _display_width("Holding"))
    right_w = max(1, geo.width - geo.sidebar_w)
    width = min(right_w - 1, geo.width - 2, max(36, longest + 4))
    if width < 20:
        width = min(geo.width - 2, max(20, longest + 4))
        left = max(0, (geo.width - width) // 2)
    else:
        left = geo.sidebar_w + max(0, (right_w - width) // 2)
    height = shown + 3
    top = max(0, (geo.height - 1 - height) // 2)
    if top + height > geo.height - 1:
        top = max(0, geo.height - 1 - height)
    for row in range(height):
        canvas.write(top + row, left, " " * width)
    _dialog(canvas, top, left, height, width, "Holding")
    start = 0
    if session.held and session.prompt_cursor >= shown:
        start = session.prompt_cursor - shown + 1
    window = titles[start : start + shown]
    for offset, title in enumerate(window):
        selected = bool(session.held) and start + offset == session.prompt_cursor
        marker = "> " if selected else "  "
        canvas.write(top + 1 + offset, left + 1, _clip(marker + title, width - 2))
    canvas.write(top + height - 2, left + 1, _clip(hint, width - 2))
    session.popup = (top, left, height, width)


def help_lines(
    focus: str,
    bindings: dict[str, tuple[str, ...]],
    *,
    filtering: bool = False,
) -> list[str]:
    """Commands for the pane that has the keyboard, with the keys actually bound."""
    rows: list[tuple[str, str] | None] = []
    for action, text in _HELP[focus if focus in _HELP else "list"]:
        if filtering and action == "quit" and focus in {"labels", "list"}:
            text = "clear the search"
        if not action:
            rows.append(None)
            continue
        if action == "escape":
            rows.append(("esc", text))
            continue
        names = bindings.get(action, ())
        if not names:
            continue
        rows.append((" ".join(names), text))
    present = [row for row in rows if row is not None]
    width = max((len(row[0]) for row in present), default=0)
    lines: list[str] = []
    for row in rows:
        if row is None:
            lines.append("")
            continue
        key, text = row
        lines.append(f"{key.ljust(width)}  {text}")
    return lines


def _write_help(canvas: _Canvas, session: MailSession, geo: Geometry) -> None:
    if not session.help_open:
        return
    lines = session.help_lines or ["(?)"]
    title = f"Help · {_HELP_TITLE.get(session.focus, 'Mail')}"
    room = max(4, geo.height - 3)
    visible = min(len(lines), room - 2)
    session.help_scroll = min(max(0, session.help_scroll), max(0, len(lines) - visible))
    window = lines[session.help_scroll : session.help_scroll + visible]
    longest = max(_display_width(line) for line in lines + [title])
    width = min(geo.width - 4, max(28, longest + 2))
    height = visible + 2
    left = max(0, (geo.width - width) // 2)
    top = max(0, (geo.height - 2 - height) // 2)
    for row in range(height):
        canvas.write(top + row, left, " " * width)
    _dialog(canvas, top, left, height, width, title)
    for offset, line in enumerate(window):
        canvas.write(top + 1 + offset, left + 1, _clip(line, width - 2))
    session.popup = (top, left, height, width)


_HELP_TITLE = {
    "labels": "Folders",
    "list": "Mail",
    "reader": "Message",
    "attachments": "Attachments",
}

_SHARED_HELP: tuple[tuple[str, str], ...] = (
    ("", ""),
    ("next-pane", "next pane"),
    ("prev-pane", "previous pane"),
    ("next-account", "next account"),
    ("prev-account", "previous account"),
    ("sync", "check this account for new mail"),
    ("sync-all", "check every account for new mail"),
    ("cache", "pause or resume downloading"),
    ("snooze", "snooze selected mail"),
    ("inbox", "open the inbox"),
    ("calendar", "open the calendar"),
    ("help", "close this list"),
)

_HELP: dict[str, tuple[tuple[str, str], ...]] = {
    "labels": (
        ("down", "next folder"),
        ("up", "previous folder"),
        ("page-down", "page down"),
        ("page-up", "page up"),
        ("top", "first folder"),
        ("bottom", "last folder"),
        ("right", "expand a folder, or open the mail list"),
        ("left", "collapse a folder"),
        ("open", "expand a folder"),
        ("close", "collapse a folder"),
        ("toggle-fold", "toggle a folder"),
        ("activate", "list that folder's mail"),
        ("search", "filter folders"),
        ("compose", "address a new message, then edit it"),
        ("quit", "quit"),
        *_SHARED_HELP,
    ),
    "list": (
        ("down", "next mail"),
        ("up", "previous mail"),
        ("page-down", "page down"),
        ("page-up", "page up"),
        ("top", "first mail"),
        ("bottom", "last mail"),
        ("activate", "read the message"),
        ("right", "open a thread, or read the message"),
        ("left", "close a thread, or go to folders"),
        ("open", "open a thread"),
        ("close", "close a thread"),
        ("toggle-fold", "toggle a thread"),
        ("search", "search mail"),
        ("select", "mark the mail, then move down"),
        ("archive", "archive"),
        ("snooze", "snooze until later"),
        ("delete", "delete"),
        ("add-label", "add or remove a label"),
        ("toggle-unread", "toggle unread"),
        ("next-unread", "next unread"),
        ("order", "newest or oldest first"),
        ("compose", "address a new message, then edit it"),
        ("reply-all", "reply to everyone"),
        ("reply", "reply to the sender"),
        ("attachments", "list attachments"),
        ("mime-parts", "choose a MIME part"),
        ("raw-source", "show the raw message source"),
        ("headers", "show all header fields"),
        ("urls", "hide or show link addresses"),
        ("open-url", "open the next link"),
        ("pick-url", "label visible links and open one"),
        ("holding", "mail waiting to be sent"),
        ("undo", "undo the last change"),
        ("quit", "quit"),
        *_SHARED_HELP,
    ),
    "reader": (
        ("down", "scroll down"),
        ("up", "scroll up"),
        ("page-down", "scroll a page down"),
        ("page-up", "scroll a page up"),
        ("top", "start of the message"),
        ("bottom", "end of the message"),
        ("quit", "back to the mail list"),
        ("left", "back to the mail list"),
        ("search", "find in this message"),
        ("compose", "address a new message, then edit it"),
        ("reply-all", "reply to everyone"),
        ("reply", "reply to the sender"),
        ("attachments", "list attachments"),
        ("mime-parts", "choose a MIME part"),
        ("view-image", "show the selected picture"),
        ("raw-source", "show or hide the raw message source"),
        ("headers", "show all header fields, or only the short set"),
        ("urls", "hide or show link addresses"),
        ("open-url", "open the next link"),
        ("pick-url", "label visible links and open one"),
        ("escape", "leave the raw source"),
        ("archive", "archive"),
        ("snooze", "snooze until later"),
        ("delete", "delete"),
        ("add-label", "add or remove a label"),
        ("toggle-unread", "toggle unread"),
        ("next-unread", "next unread"),
        ("undo", "undo the last change"),
        ("holding", "mail waiting to be sent"),
        *_SHARED_HELP,
    ),
    "attachments": (
        ("down", "next file"),
        ("up", "previous file"),
        ("activate", "save or open the file"),
        ("view-image", "show the selected picture"),
        ("escape", "back to the message"),
        ("quit", "back to the mail list"),
        ("left", "back to the mail list"),
        ("compose", "address a new message, then edit it"),
        ("reply-all", "reply to everyone"),
        ("reply", "reply to the sender"),
        ("archive", "archive"),
        ("snooze", "snooze until later"),
        ("delete", "delete"),
        ("undo", "undo the last change"),
        *_SHARED_HELP,
    ),
    "mime": (
        ("down", "next MIME part"),
        ("up", "previous MIME part"),
        ("activate", "open HTML with the open command"),
        ("escape", "back to the message"),
        ("quit", "back to the mail list"),
        ("left", "back to the mail list"),
        ("attachments", "list attachments"),
        ("compose", "address a new message, then edit it"),
        ("reply-all", "reply to everyone"),
        ("reply", "reply to the sender"),
        ("archive", "archive"),
        ("snooze", "snooze until later"),
        ("delete", "delete"),
        ("undo", "undo the last change"),
        *_SHARED_HELP,
    ),
}


def _write_snooze(canvas: _Canvas, session: MailSession, geo: Geometry) -> None:
    if session.prompt != "snooze":
        return
    choices = session._snooze_choices
    titles = [item.label for item in choices] or ["(no choices)"]
    custom = f"custom {session.prompt_text}_"
    hint = "j/k pick    type 30m 2h 3d    enter snoozes    esc cancels"
    longest = max(_display_width(title) for title in titles)
    longest = max(longest, _display_width(hint), _display_width(custom), _display_width("Snooze"))
    right_w = max(1, geo.width - geo.sidebar_w)
    width = min(right_w - 1, geo.width - 2, max(40, longest + 4))
    if width < 20:
        width = min(geo.width - 2, max(20, longest + 4))
        left = max(0, (geo.width - width) // 2)
    else:
        left = geo.sidebar_w + max(0, (right_w - width) // 2)
    shown = min(len(titles), max(1, geo.height - 6))
    height = shown + 5
    top = max(0, (geo.height - 1 - height) // 2)
    if top + height > geo.height - 1:
        top = max(0, geo.height - 1 - height)
    for row in range(height):
        canvas.write(top + row, left, " " * width)
    _dialog(canvas, top, left, height, width, "Snooze")
    start = 0
    if choices and session.prompt_cursor >= shown:
        start = session.prompt_cursor - shown + 1
    window = titles[start : start + shown]
    for offset, title in enumerate(window):
        selected = bool(choices) and start + offset == session.prompt_cursor
        marker = "> " if selected else "  "
        canvas.write(top + 1 + offset, left + 1, _clip(marker + title, width - 2))
    canvas.write(top + 1 + shown, left + 1, _clip(custom, width - 2))
    canvas.write(top + 2 + shown, left + 1, _clip(hint, width - 2))
    session.popup = (top, left, height, width)


def _write_save(canvas: _Canvas, session: MailSession, geo: Geometry) -> None:
    if session.prompt != "save":
        return
    path = f"{session.prompt_text}_"
    hint = "tab completes    ? folders    up/down pick    enter runs    esc closes"
    if session.prompt_hint:
        hint = f"{session.prompt_hint}    {hint}"
    title = "Download"
    labels = [
        f"{'>' if index == session.prompt_cursor else ' '} {label}"
        for index, label in enumerate(_SAVE_CHOICES)
    ]
    folder_room = max(0, geo.height - 2 - 4 - len(labels))
    listing = _save_folder_lines(session._save_listing, folder_room)
    rows = [path, *labels, *listing, hint, title]
    longest = max(_display_width(line) for line in rows)
    right_w = max(1, geo.width - geo.sidebar_w)
    width = min(right_w - 1, geo.width - 2, max(36, longest + 4))
    if width < 20:
        width = min(geo.width - 2, max(20, longest + 4))
        left = max(0, (geo.width - width) // 2)
    else:
        left = geo.sidebar_w + max(0, (right_w - width) // 2)
    height = 4 + len(labels) + len(listing)
    top = max(0, (geo.height - 1 - height) // 2)
    if top + height > geo.height - 1:
        top = max(0, geo.height - 1 - height)
    for row in range(height):
        canvas.write(top + row, left, " " * width)
    _dialog(canvas, top, left, height, width, title)
    canvas.write(top + 1, left + 1, _clip(path, width - 2))
    for offset, label in enumerate(labels):
        canvas.write(top + 2 + offset, left + 1, _clip(label, width - 2))
    for offset, line in enumerate(listing):
        canvas.write(top + 2 + len(labels) + offset, left + 1, _clip(line, width - 2))
    canvas.write(top + 2 + len(labels) + len(listing), left + 1, _clip(hint, width - 2))
    session.popup = (top, left, height, width)


def _save_folder_lines(folders: list[str], room: int) -> list[str]:
    if not folders or room <= 0:
        return []
    if room == 1:
        return [f"Folders: {len(folders)}"]
    lines = ["Folders:"]
    show = min(len(folders), room - 1)
    if show < len(folders):
        show = max(0, room - 2)
        lines.extend(f"  {name}" for name in folders[:show])
        lines.append(f"  … +{len(folders) - show} more")
    else:
        lines.extend(f"  {name}" for name in folders[:show])
    return lines


def _save_path_parts(text: str) -> tuple[str, str, bool]:
    raw = text or ""
    tilde = raw.startswith("~")
    expanded = os.path.expanduser(raw)
    if raw.endswith(("/", os.sep)):
        stripped = expanded.rstrip("/\\")
        if not stripped or expanded in ("/", os.sep):
            return "/", "", tilde
        return stripped, "", tilde
    directory, prefix = os.path.split(expanded)
    if not directory:
        directory = "."
    return directory, prefix, tilde


def _save_path_matches(directory: str, prefix: str) -> list[str]:
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    matches = sorted(name for name in names if name.startswith(prefix))
    if not prefix.startswith("."):
        matches = [name for name in matches if not name.startswith(".")]
    return matches


def _save_path_folders(directory: str, prefix: str) -> list[str]:
    folders: list[str] = []
    for name in _save_path_matches(directory, prefix):
        try:
            if Path(directory, name).is_dir():
                folders.append(name + os.sep)
        except OSError:
            continue
    return folders


def _join_save_path(directory: str, name: str, tilde: bool, *, directory_ok: bool) -> str:
    if directory in ("/", ""):
        joined = f"/{name}" if name else "/"
    else:
        joined = os.path.join(directory, name) if name else directory
    if directory_ok and name and Path(joined).is_dir():
        joined = joined.rstrip("/\\") + os.sep
    home = str(Path.home())
    if tilde and (joined == home or joined.startswith(home + os.sep) or joined.startswith(home + "/")):
        return "~" + joined[len(home) :]
    return joined


def _write_compose(canvas: _Canvas, session: MailSession, geo: Geometry) -> None:
    if session.prompt != "send":
        return
    subject = session.compose_subject.strip() or "(no subject)"
    choices = [
        f"{'>' if index == session.prompt_cursor else ' '} {label}"
        for index, label in enumerate(_COMPOSE_CHOICES)
    ]
    hint = "j/k chooses    enter confirms    esc discards"
    title = "Message"
    longest = max(
        _display_width(subject),
        _display_width(hint),
        _display_width(title),
        *(_display_width(choice) for choice in choices),
    )
    width = min(geo.width - 2, max(42, longest + 4))
    left = max(0, (geo.width - width) // 2)
    height = 6
    top = max(0, (geo.height - 1 - height) // 2)
    if top + height > geo.height - 1:
        top = max(0, geo.height - 1 - height)
    for row in range(height):
        canvas.write(top + row, left, " " * width)
    _dialog(canvas, top, left, height, width, title)
    canvas.write(top + 1, left + 1, _clip(subject, width - 2))
    canvas.write(top + 2, left + 1, _clip(choices[0], width - 2))
    canvas.write(top + 3, left + 1, _clip(choices[1], width - 2))
    canvas.write(top + 4, left + 1, _clip(hint, width - 2))
    session.popup = (top, left, height, width)
    session.popup_choices = (top + 2, top + 3)


def _write_compose_addresses(canvas: _Canvas, session: MailSession, geo: Geometry) -> None:
    if session.prompt != "compose":
        return
    labels = ("To: ", "Cc: ", "Bcc: ")
    values = (session.compose_to, session.compose_cc, session.compose_bcc)
    rows = [
        label + value + ("_" if index == session.prompt_cursor else "")
        for index, (label, value) in enumerate(zip(labels, values, strict=True))
    ]
    hint = session.prompt_hint or "up/down moves    tab completes    enter edits"
    title = "Compose"
    longest = max(_display_width(title), _display_width(hint), *(_display_width(row) for row in rows))
    width = min(geo.width - 2, max(48, longest + 4))
    left = max(0, (geo.width - width) // 2)
    height = 6
    top = max(0, (geo.height - 1 - height) // 2)
    if top + height > geo.height - 1:
        top = max(0, geo.height - 1 - height)
    for row in range(height):
        canvas.write(top + row, left, " " * width)
    _dialog(canvas, top, left, height, width, title)
    for offset, row in enumerate(rows):
        canvas.write(top + 1 + offset, left + 1, _clip(row, width - 2))
    canvas.write(top + 4, left + 1, _clip(hint, width - 2))
    session.popup = (top, left, height, width)


def _write_quit(canvas: _Canvas, session: MailSession, geo: Geometry) -> None:
    if session.prompt != "quit":
        return
    question = "Really quit?"
    hint = "enter quits    esc cancels"
    title = "Quit"
    longest = max(_display_width(question), _display_width(hint), _display_width(title))
    width = min(geo.width - 2, max(36, longest + 4))
    left = max(0, (geo.width - width) // 2)
    height = 4
    top = max(0, (geo.height - 1 - height) // 2)
    if top + height > geo.height - 1:
        top = max(0, geo.height - 1 - height)
    for row in range(height):
        canvas.write(top + row, left, " " * width)
    _dialog(canvas, top, left, height, width, title)
    canvas.write(top + 1, left + 1, _clip(question, width - 2))
    canvas.write(top + 2, left + 1, _clip(hint, width - 2))
    session.popup = (top, left, height, width)


def _write_reader(canvas: _Canvas, session: MailSession, geo: Geometry) -> None:
    top = geo.list_h + 1
    left = 1
    width = geo.width - 2
    rows = session.reader_view[session.reader_scroll : session.reader_scroll + geo.reader_rows]
    for offset, line in enumerate(rows):
        canvas.write(top + offset, left, _clip(line, width))


def status_bar(session: MailSession, width: int) -> str:
    """The bottom line, padded so its highlight reaches the right edge."""
    line = format_activity(session, width)
    gap = max(0, width - 1 - _display_width(line))
    return line + (" " * gap)


def format_activity(session: MailSession, width: int) -> str:
    if session.prompt == "search":
        hint = f"  {session.prompt_hint}" if session.prompt_hint else ""
        return _clip(f"/{session.prompt_text}_{hint}", max(0, width - 1))
    if session.prompt == "label":
        hint = f"  {session.prompt_hint}" if session.prompt_hint else ""
        return _clip(f"label {session.prompt_text}_{hint}", max(0, width - 1))
    if session.prompt == "compose":
        return _clip("up/down moves    tab completes    enter edits", max(0, width - 1))
    if session.prompt == "send":
        return _clip("send or save as draft", max(0, width - 1))
    if session.prompt == "holding":
        return _clip("enter releases    esc closes", max(0, width - 1))
    accepted, waiting = session.engine.store.queue_counts()
    inbound = _bar(session.sync_done, session.sync_total, 12, session.sync_tick)
    in_label = session.sync_phase or "idle"
    out_total = accepted + waiting
    if out_total == 0:
        outbound = _bar(1, 1, 12, 0)
        out_label = "to Gmail: none"
    else:
        outbound = _bar(accepted, out_total, 12, session.sync_tick if waiting else 0)
        if waiting:
            out_label = _pending_text(session.engine.store.pending_changes())
            if session.urgent_phase:
                out_label = f"{session.urgent_phase}  {out_label}"
        else:
            out_label = f"holding {accepted}  H"
    parts = [f"in {inbound} {in_label}", f"out {outbound} {out_label}"]
    if session.focus == "attachments":
        parts.append("enter saves")
    if session.focus == "mime":
        parts.append("j/k switches MIME  enter opens HTML")
    if session.raw_source:
        parts.append("raw source  esc returns")
    if session.notice:
        parts.append(session.notice)
    return _clip("  ".join(parts), max(0, width - 1))


def _pending_text(changes) -> str:
    if not changes:
        return "to Gmail: none"
    grouped: dict[str, list[str]] = {}
    subjects: dict[str, str] = {}
    order: list[str] = []
    for change in changes:
        if change.gmail_id not in grouped:
            grouped[change.gmail_id] = []
            order.append(change.gmail_id)
            subjects[change.gmail_id] = change.subject.strip() or change.gmail_id
        sign = "+" if change.op == "add" else "-"
        grouped[change.gmail_id].append(f"{sign}{change.label}")
    parts: list[str] = []
    for gmail_id in order[:2]:
        title = subjects[gmail_id]
        if len(title) > 28:
            title = title[:27] + "~"
        parts.append(f"{title} ({' '.join(grouped[gmail_id])})")
    extra = f" +{len(order) - 2}" if len(order) > 2 else ""
    return f"to Gmail: {len(changes)}  " + "; ".join(parts) + extra


def _bar(done: int, total: int, width: int, tick: int) -> str:
    if total <= 0:
        # Wall clock so the bar keeps moving while one Gmail call is open.
        pulse = int(time.monotonic() * 4)
        cells = ["-"] * width
        cells[pulse % width] = "#"
        return "[" + "".join(cells) + "]"
    filled = int(width * min(max(done, 0), total) / total)
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"


DATE_WIDTH = 16
ATT_WIDTH = 1
READ_WIDTH = 1
FIELD_GAP = " │ "


def _mailboxes(value: str) -> str:
    """Email addresses from a From or To header, without display names."""
    found = _address_list(value)
    if found:
        return ", ".join(found)
    return value.strip()


def _address_list(value: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for _name, addr in getaddresses([value or ""]):
        addr = addr.strip()
        if "@" not in addr:
            continue
        key = addr.casefold()
        if key in seen:
            continue
        seen.add(key)
        found.append(addr)
    return found


def reply_draft(
    *,
    from_addr: str,
    to_addr: str,
    cc_addr: str,
    subject: str,
    body: str,
    me: str,
    everyone: bool,
) -> tuple[str, str, str, list[str]]:
    """To, Cc, subject, and body lines for a reply or a reply to all."""
    senders = _address_list(from_addr)
    tos = _address_list(to_addr)
    ccs = _address_list(cc_addr)
    mine = me.strip().casefold()
    skip = {mine} if mine else set()
    sender = senders[0] if senders else ""
    if sender and sender.casefold() not in skip:
        to = sender
    elif tos:
        to = tos[0]
    else:
        to = ""
    cc = ""
    if everyone:
        skip_cc = set(skip)
        if to:
            skip_cc.add(to.casefold())
        extra = [addr for addr in tos + ccs if addr.casefold() not in skip_cc]
        cc = ", ".join(extra)
    title = subject.strip()
    if not title.casefold().startswith("re:"):
        title = f"Re: {title}" if title else "Re:"
    quoted = ["> " + line for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    if not quoted:
        quoted = ["> "]
    return to, cc, title, ["", *quoted]


def editor_command() -> list[str]:
    """Program used to edit a reply. ``$EDITOR``, then ``$VISUAL``, then vim."""
    for name in ("EDITOR", "VISUAL"):
        chosen = os.environ.get(name, "").strip()
        if not chosen:
            continue
        parts = shlex.split(chosen)
        if parts:
            return parts
    return ["vim"]


def edit_draft(text: str, run) -> tuple[str | None, int]:
    """Write ``text`` to a temp file, call ``run(path)``, and read the file back."""
    handle, name = tempfile.mkstemp(prefix="gmail-tui-reply-", suffix=".eml")
    os.close(handle)
    path = Path(name)
    try:
        path.write_text(text, encoding="utf-8")
        try:
            status = int(run(path))
        except OSError:
            return None, 127
        try:
            return path.read_text(encoding="utf-8"), status
        except OSError:
            return None, status
    finally:
        path.unlink(missing_ok=True)


def _address_fragment(value: str) -> str:
    """The address still being typed, after the last comma."""
    return value.rpartition(",")[2].strip()


def _join_address(value: str, address: str) -> str:
    head, sep, _fragment = value.rpartition(",")
    if not sep:
        return address
    return f"{head}, {address}"


_DRAFT_HEADERS = {"to", "cc", "bcc", "subject"}


def parse_draft(text: str) -> tuple[str, str, str, str, str]:
    """To, Cc, Bcc, Subject, and the message body.

    Only a leading To, Cc, Bcc, or Subject line is a header. The line typed under
    Subject is the start of the body, including when it contains a colon.
    One blank line after those headers is the separator and is left out.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if normalized.endswith("\n"):
        normalized = normalized[:-1]
    lines = normalized.split("\n")
    to = cc = bcc = subject = ""
    index = 0
    while index < len(lines):
        name, colon, value = lines[index].partition(":")
        key = name.strip().casefold()
        if not colon or key not in _DRAFT_HEADERS:
            break
        value = value.strip()
        if key == "to":
            to = value
        elif key == "cc":
            cc = value
        elif key == "bcc":
            bcc = value
        else:
            subject = value
        index += 1
    if index < len(lines) and lines[index] == "":
        index += 1
    return to, cc, bcc, subject, "\n".join(lines[index:])


def compose_message(
    *,
    from_addr: str,
    to_addr: str,
    cc_addr: str,
    subject: str,
    body: str,
    in_reply_to: str,
    bcc_addr: str = "",
) -> bytes:
    message = EmailMessage()
    if from_addr.strip():
        message["From"] = from_addr.strip()
    message["To"] = to_addr.strip()
    if cc_addr.strip():
        message["Cc"] = cc_addr.strip()
    if bcc_addr.strip():
        message["Bcc"] = bcc_addr.strip()
    message["Subject"] = subject
    token = in_reply_to.strip()
    if token:
        if not token.startswith("<"):
            token = f"<{token.strip('<>')}>"
        message["In-Reply-To"] = token
        message["References"] = token
    message.set_content(body if body.endswith("\n") else body + "\n")
    return message.as_bytes()


def _message_line(
    sender: str,
    recipient: str,
    subject: str,
    labels: str,
    attachment: str,
    width: int,
    when: str,
    columns: tuple[int, int, int, int] | None = None,
    unread: str = " ",
) -> str:
    if width <= 0:
        return ""
    sender = _mailboxes(sender)
    recipient = _mailboxes(recipient)
    if columns is None:
        columns = _fit_columns(width, [(sender, recipient, subject, labels)])
    flag = _pad(_clip(unread, READ_WIDTH), READ_WIDTH)
    mark = _pad(_clip(attachment, ATT_WIDTH), ATT_WIDTH)
    if columns is None:
        stamp = _pad(when, min(DATE_WIDTH, max(0, width - ATT_WIDTH - READ_WIDTH - 1)))
        return _pad(FIELD_GAP.join((flag, mark, stamp, sender, subject)), width)
    from_w, to_w, subject_w, labels_w = columns
    stamp = _pad(when, DATE_WIDTH)
    line = FIELD_GAP.join(
        (
            flag,
            mark,
            stamp,
            _pad(sender, from_w),
            _pad(recipient, to_w),
            _pad(subject, subject_w),
            _pad(labels, labels_w),
        )
    )
    return _pad(line, width)


def _fit_columns(
    width: int, samples: list[tuple[str, str, str, str]]
) -> tuple[int, int, int, int] | None:
    """Size From and To to the longest address.

    Labels stay whole when the subjects on screen leave enough room.
    Subject receives whatever remains after that.
    """
    gaps = 6 * _display_width(FIELD_GAP)
    minimum = DATE_WIDTH + ATT_WIDTH + READ_WIDTH + gaps + 8
    if width < minimum:
        return None
    rest = width - DATE_WIDTH - ATT_WIDTH - READ_WIDTH - gaps
    min_subject = len("Subject")
    min_labels = len("Labels")
    min_from = len("From")
    min_to = len("To")

    def longest(index: int, floor: int) -> int:
        size = floor
        for sample in samples:
            size = max(size, _display_width(_plain(sample[index])))
        return size

    from_need = longest(0, min_from)
    to_need = longest(1, min_to)
    subject_need = longest(2, min_subject)
    labels_need = longest(3, min_labels)
    room = rest - min_labels
    if from_need + to_need + min_subject <= room:
        from_w = from_need
        to_w = to_need
    else:
        share = max(0, room - min_subject)
        total = max(1, from_need + to_need)
        from_w = min(from_need, max(0, share * from_need // total))
        to_w = share - from_w
        if to_w > to_need:
            from_w += to_w - to_need
            to_w = to_need
        if from_w > from_need:
            to_w += from_w - from_need
            from_w = from_need
    leftover = rest - from_w - to_w
    if labels_need + subject_need <= leftover:
        labels_w = labels_need
    else:
        labels_w = min(labels_need, max(min_labels, leftover - min_subject))
    subject_w = leftover - labels_w
    if subject_w < 0:
        subject_w = 0
    return from_w, to_w, subject_w, labels_w


def _dialog(canvas: _Canvas, top: int, left: int, height: int, width: int, title: str) -> None:
    if height < 2 or width < 2:
        return
    _hline(canvas, top, left, left + width - 1, "┌", "─", "┐")
    _hline(canvas, top + height - 1, left, left + width - 1, "└", "─", "┘")
    for row in range(top + 1, top + height - 1):
        canvas.write(row, left, "│")
        canvas.write(row, left + width - 1, "│")
    canvas.write(top, left + 1, _clip(f" {title} ", max(0, width - 2)))


def _caption(text: str, active: bool) -> str:
    if active:
        return f"[{text}]"
    return f" {text} "


def _frame(canvas: _Canvas, geo: Geometry, list_title: str, focus: str) -> None:
    top = geo.top
    bottom = geo.height - 2
    right = geo.width - 1
    split_x = geo.sidebar_w - 1
    split_y = geo.list_h
    if bottom <= top + 1 or right <= split_x + 1 or split_y <= top or split_y >= bottom:
        return
    _hline(canvas, top, 0, right, "┌", "─", "┐")
    _hline(canvas, bottom, 0, right, "└", "─", "┘")
    _hline(canvas, split_y, 0, right, "├", "─", "┤")
    for row in range(top + 1, bottom):
        if row == split_y:
            continue
        canvas.write(row, 0, "│")
        canvas.write(row, right, "│")
        if row < split_y:
            canvas.write(row, split_x, "│")
    canvas.write(top, split_x, "┬")
    reading = focus in {"reader", "attachments"}
    canvas.write(top, 1, _clip(_caption("Labels", focus == "labels"), max(0, split_x - 1)))
    pane = max(0, right - split_x - 1)
    canvas.write(top, split_x + 1, _clip(_caption(list_title, focus == "list"), pane))
    canvas.write(split_y, 1, _clip(_caption("Message", reading), max(0, right - 1)))
    canvas.write(split_y, split_x, "┴")


def _hline(canvas: _Canvas, y: int, left: int, right: int, west: str, fill: str, east: str) -> None:
    if right - left < 1:
        return
    canvas.write(y, left, west + (fill * (right - left - 1)) + east)


def _pad(text: str, width: int) -> str:
    clipped = _clip(text, width)
    return clipped + (" " * max(0, width - _display_width(clipped)))


def selected_text(lines: list[str], y0: int, x0: int, y1: int, x1: int) -> str:
    """Text covered by a drag, inclusive of both ends. Coordinates are columns."""
    if (y1, x1) < (y0, x0):
        y0, x0, y1, x1 = y1, x1, y0, x0
    if y0 < 0:
        y0 = 0
    chunks: list[str] = []
    for y in range(y0, y1 + 1):
        if y >= len(lines):
            break
        start = x0 if y == y0 else 0
        end = x1 + 1 if y == y1 else _display_width(lines[y])
        chunks.append(_slice_columns(lines[y], start, end))
    return "\n".join(chunks).rstrip()


def mark_spans(start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int, int]]:
    """Screen runs ``(row, column, length)`` covered by a drag, inclusive."""
    y0, x0 = start
    y1, x1 = end
    if (y1, x1) < (y0, x0):
        y0, x0, y1, x1 = y1, x1, y0, x0
    spans: list[tuple[int, int, int]] = []
    for y in range(y0, y1 + 1):
        if y == y0 and y == y1:
            left, right = x0, x1
        elif y == y0:
            left, right = x0, x0 + 10000
        elif y == y1:
            left, right = 0, x1
        else:
            left, right = 0, 10000
        spans.append((y, left, right - left + 1))
    return spans


def _slice_columns(text: str, start: int, end: int) -> str:
    if end <= start:
        return ""
    kept: list[str] = []
    column = 0
    for char in text:
        width = _char_width(char)
        if width == 0:
            if start <= column < end:
                kept.append(char)
            continue
        if column >= end:
            break
        if column >= start:
            kept.append(char)
        column += width
    return "".join(kept)


def _clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    text = _plain(text)
    if _display_width(text) <= width:
        return text
    if width == 1:
        first = text[:1]
        return first if _display_width(first) <= 1 else "~"
    kept: list[str] = []
    used = 0
    for char in text:
        char_width = _char_width(char)
        if used + char_width > width - 1:
            break
        kept.append(char)
        used += char_width
    return "".join(kept) + "~"


def _plain(text: str) -> str:
    """Drop characters that move the cursor instead of occupying a cell."""
    if text.isascii():
        if "\t" not in text and "\n" not in text and "\r" not in text:
            return text
        return text.replace("\t", " ").replace("\n", " ").replace("\r", " ")
    kept: list[str] = []
    for char in text:
        if char in "\n\r\t":
            kept.append(" ")
            continue
        category = unicodedata.category(char)
        if category in {"Cc", "Cs"}:
            continue
        kept.append(char)
    return "".join(kept)


def _display_width(text: str) -> int:
    return sum(_char_width(char) for char in text)


def _char_width(char: str) -> int:
    # Variation selectors are nonspacing marks with combining class 0, so
    # unicodedata.combining() does not catch them. Format characters (ZWJ,
    # zero-width space, bidi marks) and enclosing marks also take no cell.
    if unicodedata.combining(char) or unicodedata.category(char) in {"Mn", "Me", "Cf"}:
        return 0
    if unicodedata.east_asian_width(char) in {"W", "F"}:
        return 2
    return 1


def _exact_label(items: list[FlatLabel], query: str) -> bool:
    needle = query.casefold()
    for item in items:
        if not item.gmail_id:
            continue
        fields = (item.name, item.path or "", _label_title(item), item.gmail_id)
        if needle in {field.casefold() for field in fields if field}:
            return True
    return False


def _label_title(item: FlatLabel) -> str:
    path = item.path or item.name
    if path and path not in (item.name, item.gmail_id or ""):
        return path
    return item.name


def _rank_labels(items: list[FlatLabel], query: str, *, require_id: bool) -> list[FlatLabel]:
    ranked: list[tuple[int, str, FlatLabel]] = []
    for item in items:
        if require_id and not item.gmail_id:
            continue
        score = _label_score(item, query)
        if score is None:
            continue
        ranked.append((score, _label_title(item).lower(), item))
    ranked.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in ranked]


def _label_score(item: FlatLabel, query: str) -> int | None:
    if not query:
        return 1
    needle = query.lower()
    title = _label_title(item).lower()
    name = item.name.lower()
    path = (item.path or item.name).lower()
    if title.startswith(needle) or name.startswith(needle) or path.startswith(needle):
        return 0
    if any(part.startswith(needle) for part in path.split("/")):
        return 1
    if needle in title or needle in name or needle in path:
        return 2
    return None


def _ancestor_keys(nodes: list[TreeNode], key: str, parents: tuple[str, ...] = ()) -> set[str] | None:
    for node in nodes:
        if node.key == key:
            return set(parents)
        if node.children:
            found = _ancestor_keys(node.children, key, parents + (node.key,))
            if found is not None:
                return found
    return None


def _clamp(index: int, size: int) -> int:
    if size <= 0:
        return 0
    return max(0, min(index, size - 1))


def _reveal(cursor: int, scroll: int, window: int) -> int:
    if window <= 0:
        return 0
    if cursor < scroll:
        return cursor
    if cursor >= scroll + window:
        return cursor - window + 1
    return scroll


def _scroll_margin(window: int) -> int:
    if window <= 1:
        return 0
    return min(SCROLL_MARGIN, (window - 1) // 2)


def _reveal_list(
    cursor: int,
    scroll: int,
    window: int,
    total: int,
    *,
    more: bool = False,
) -> int:
    """Keep the selection on screen, with a margin while more mail remains."""
    if window <= 0 or total <= 0:
        return 0
    limit = max(0, total - window)
    scroll = _reveal(cursor, scroll, window)
    margin = _scroll_margin(window)
    if margin <= 0:
        return max(0, min(scroll, limit))
    below = max(0, total - 1 - cursor)
    if more or below > 0:
        want = margin if more else min(margin, below)
        want = min(want, window - 1)
        row = cursor - scroll
        if row > window - 1 - want:
            scroll = min(limit, cursor - (window - 1 - want))
    above = cursor
    if above > 0:
        want = min(margin, above, window - 1)
        row = cursor - scroll
        if row < want:
            scroll = max(0, cursor - want)
    return max(0, min(scroll, limit))


# Second cell of a wide character. Omitted when the row is drawn, so curses
# advances two columns for the character and the scrollbar stays on screen.
_WIDE = "\x00"


class _Canvas:
    def __init__(self, height: int, width: int) -> None:
        self.height = height
        self.width = width
        self.grid = [[" "] * width for _ in range(height)]

    def write(self, y: int, x: int, text: str) -> None:
        if y < 0 or y >= self.height or x >= self.width:
            return
        column = x
        for char in text:
            width = _char_width(char)
            if width == 0:
                self._attach(y, column, char)
                continue
            if column >= self.width:
                break
            if y == self.height - 1 and column >= self.width - 1:
                break
            if width == 2 and column + 1 >= self.width:
                break
            if width == 2 and y == self.height - 1 and column + 1 >= self.width - 1:
                break
            self._clear(y, column)
            if width == 2:
                self._clear(y, column + 1)
            self.grid[y][column] = char
            column += 1
            if width == 2:
                self.grid[y][column] = _WIDE
                column += 1

    def _attach(self, y: int, column: int, char: str) -> None:
        if column <= 0:
            return
        owner = column - 1
        if self.grid[y][owner] == _WIDE and owner > 0:
            owner -= 1
        if self.grid[y][owner] == _WIDE:
            return
        self.grid[y][owner] += char

    def _clear(self, y: int, column: int) -> None:
        if self.grid[y][column] == _WIDE and column > 0:
            self.grid[y][column - 1] = " "
            self.grid[y][column] = " "
            return
        if column + 1 < self.width and self.grid[y][column + 1] == _WIDE:
            self.grid[y][column + 1] = " "

    def lines(self) -> list[str]:
        return ["".join(cell for cell in row if cell != _WIDE).rstrip() for row in self.grid]

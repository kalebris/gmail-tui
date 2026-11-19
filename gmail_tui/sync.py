"""Sync loops.

On startup the inbox is listed, then any inbox message that is not saved yet
is downloaded in the largest batch the remaining query-cost minute can pay
for, before the rest of the mailbox. After that, a periodic history check
fetches new messages ahead of the cache.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from gmail_tui.gmail import (
    QUOTA_PER_MINUTE,
    UNITS_GET,
    UNITS_GET_RAW,
    UNITS_HISTORY,
    UNITS_LABELS,
    UNITS_LIST,
    UNITS_MODIFY,
    UNITS_PROFILE,
    UNITS_DRAFT,
    UNITS_SEND,
    Conflict,
    GmailClient,
    GmailError,
    HistoryExpired,
    HistoryRecord,
    IncomingMessage,
    Label,
    MessageFormat,
    NotFound,
    Offline,
    RateLimited,
)
from gmail_tui import profile
from gmail_tui.http_gmail import HttpStatus, is_precondition_failed
from gmail_tui.oauth import OAuthError
from gmail_tui.quota import Quota, SyncStopped
from gmail_tui.search import ParsedQuery, parse_query
from gmail_tui.store import (
    QueueItem,
    SearchHit,
    Store,
    ThreadCursor,
    ThreadSummary,
    defers_body,
)
from gmail_tui.textutil import parse_rfc822

_POLL_SECONDS = 60.0
# After suspend the first DNS lookup often fails once. Wait, then try again.
_RETRY_SECONDS = 30.0
# Wi-Fi / DHCP after hibernate often needs longer than a single DNS blip.
_NETWORK_RETRY_SECONDS = 60.0
# Hold network work this long after the wall clock jumps (suspend/resume).
_RESUME_GRACE_SECONDS = 15.0
# Inbox RAW batches used to spend a whole Gmail minute in one step. Chunking
# yields between steps so a second account can download while the first is busy.
_INBOX_BODY_CHUNK = 10


@dataclass
class SyncReport:
    open_label: str
    history_records: int = 0
    metadata_upserted: int = 0
    bodies_downloaded: int = 0
    mutations_pushed: int = 0
    mutations_rebased: int = 0
    backfill_downloaded: int = 0
    bootstrapped: bool = False


@dataclass
class SearchResponse:
    hits: list[SearchHit]
    remote_error: str | None = None


@dataclass
class OpenedMessage:
    gmail_id: str
    thread_gmail_id: str
    subject: str
    from_addr: str
    to_addr: str
    cc_addr: str
    snippet: str
    internal_date_ms: int
    label_ids: tuple[str, ...]
    body_text: str
    body_cached: bool


@dataclass
class Engine:
    store: Store
    gmail: GmailClient
    quota: Quota = field(default_factory=Quota)
    # Background caching of message bodies. Opening a message, search, and
    # folder changes still fetch when this is off.
    background_bodies: bool = True
    # The open reader is waiting on a body. Skip quota pacing so UrgentWork
    # can run on the next account-thread turn instead of sleeping ~0.6s.
    open_waiting: bool = False

    @classmethod
    def open(
        cls,
        root: Path | str,
        gmail: GmailClient,
        *,
        per_minute: float = QUOTA_PER_MINUTE,
        sleeper=None,
        now=None,
    ) -> Engine:
        return cls(Store.open(root), gmail, Quota(per_minute, sleeper=sleeper, now=now))

    def local_threads(
        self,
        label: str | None = None,
        cursor: ThreadCursor | None = None,
        limit: int = 50,
    ) -> tuple[str, list[ThreadSummary]]:
        chosen = label or self.store.open_label()
        if label is not None:
            self.store.set_open_label(chosen)
        return chosen, self.store.list_threads(chosen, cursor, limit)

    def startup(self) -> SyncReport:
        self.wake_due_snoozes()
        job = BackgroundSync(self)
        while job.step():
            pass
        if job.exc is not None:
            raise job.exc
        return job.report

    def wake_due_snoozes(self, *, now_ms: int | None = None) -> int:
        """Put snoozed messages back in the inbox when their timer ends."""
        when = int(time.time() * 1000) if now_ms is None else now_ms
        due = self.store.due_snooze_ids(when)
        for gmail_id in due:
            if self.store.has_metadata(gmail_id):
                self.store.apply_local_labels(gmail_id, add=["INBOX"])
            self.store.clear_snooze(gmail_id)
        return len(due)

    def sync_metadata(self, report: SyncReport | None = None) -> SyncReport:
        report = report or SyncReport(open_label=self.store.open_label())
        self._refresh_labels()
        if self.store.bootstrap_phase() != "done":
            self._bootstrap(report)
            return report
        history_id = self.store.history_id()
        if not history_id:
            self.ensure_token_matches_mailbox()
            self.store.begin_resync()
            self._bootstrap(report)
            return report
        try:
            self.pull_history(history_id, report)
        except HistoryExpired:
            self.ensure_token_matches_mailbox()
            self.store.begin_resync()
            self._bootstrap(report)
        return report

    def ensure_token_matches_mailbox(self) -> None:
        """Refuse a rebuild when oauth belongs to a different Google account.

        A mismatched token used to expire history and prune the local replica,
        wiping tens of thousands of cached messages. Fail soft instead.
        """
        email = self.store.account_email().strip()
        if not email:
            return
        profile = self._call(UNITS_PROFILE, self.gmail.get_profile)
        remote = (profile.email or "").strip()
        if remote and remote.casefold() != email.casefold():
            raise Offline(
                f"token is for {remote}, but this mailbox is {email}; "
                "run gmail-tui login with that Google account"
            )

    def download_new_bodies(self) -> int:
        count = 0
        for gmail_id in self.store.pending_body_ids(self.store.open_label()):
            self._download_body(gmail_id)
            count += 1
        return count

    def flush_queue(self) -> tuple[int, int]:
        pushed = 0
        rebased = 0
        for gmail_id, items in self.store.queue_groups().items():
            items, did_rebase, remote = self._push_one(gmail_id, items)
            if remote is None:
                continue
            self.store.complete_push(gmail_id, items, remote.label_ids, remote.history_id)
            pushed += 1
            rebased += int(did_rebase)
        return pushed, rebased

    def backfill(self) -> int:
        count = 0
        seen: set[str] = set()
        while self.store.is_idle():
            missing = self.store.missing_body_ids(limit=1, open_label=self.store.open_label())
            if not missing or missing[0] in seen:
                break
            seen.add(missing[0])
            self._download_body(missing[0])
            count += 1
        return count

    def search(self, query: str, limit: int = 50) -> SearchResponse:
        limit = max(1, min(limit, 200))
        if not query.strip():
            return SearchResponse([])
        parsed = parse_query(query)
        hits = {hit.gmail_id: hit for hit in self.store.search_local(parsed, limit)}
        remote_error: str | None = None
        try:
            self._merge_remote(query, parsed, hits, limit)
        except Offline as exc:
            remote_error = str(exc)
        except GmailError as exc:
            remote_error = str(exc)
        ordered = sorted(hits.values(), key=lambda hit: (hit.internal_date_ms, hit.gmail_id), reverse=True)
        return SearchResponse(ordered[:limit], remote_error)

    def open_message(self, gmail_id: str) -> OpenedMessage:
        if self.store.opened_message(gmail_id) is None:
            message = self._call(UNITS_GET, lambda: self.gmail.get_message(gmail_id, MessageFormat.METADATA))
            self.store.upsert_metadata(message, "missing")
        if not self.store.body_readable(gmail_id):
            self._download_body(gmail_id)
        return _opened(self.store.opened_message(gmail_id))

    def send_reply(self, raw: bytes, thread_id: str) -> str:
        sent = self._call(UNITS_SEND, lambda: self.gmail.send_message(raw, thread_id))
        return self._store_outgoing(sent, raw, thread_id, ["SENT"])

    def save_draft(self, raw: bytes, thread_id: str) -> str:
        saved = self._call(UNITS_DRAFT, lambda: self.gmail.create_draft(raw, thread_id))
        return self._store_outgoing(saved, raw, thread_id, ["DRAFT"])

    def _store_outgoing(
        self,
        sent: IncomingMessage,
        raw: bytes,
        thread_id: str,
        fallback_labels: list[str],
    ) -> str:
        # Gmail adds Message-ID (and may adjust threading) on the stored copy.
        # Keep that, not our pre-send bytes, so later replies can cite the id.
        try:
            message = self._call(
                UNITS_GET_RAW,
                lambda: self.gmail.get_message(sent.gmail_id, MessageFormat.RAW),
            )
        except (GmailError, Offline, OAuthError):
            message = sent
            message.raw = raw
        body = message.raw or raw
        parsed = parse_rfc822(body)
        if not message.subject:
            message.subject = parsed.subject
            message.from_addr = parsed.from_addr
            message.to_addr = parsed.to_addr
            message.cc_addr = parsed.cc_addr
        if not message.message_id_header:
            message.message_id_header = parsed.message_id_header
        if not message.in_reply_to:
            message.in_reply_to = parsed.in_reply_to
        if not message.references_header:
            message.references_header = parsed.references
        if not message.label_ids:
            message.label_ids = list(sent.label_ids) or list(fallback_labels)
        if not message.thread_id:
            message.thread_id = sent.thread_id or thread_id or sent.gmail_id
        if not message.internal_date_ms:
            message.internal_date_ms = int(time.time() * 1000)
        message.has_attachment = parsed.has_attachment
        message.raw = body
        self.store.upsert_metadata(message, "missing")
        self.store.write_body(message, body, parsed.text)
        return message.gmail_id

    def pull_history(self, start: str, report: SyncReport) -> None:
        if not start:
            return
        resume_start, resume_token = self.store.history_resume()
        token = resume_token if resume_start == start else None
        while True:
            page = self._call(
                UNITS_HISTORY,
                lambda token=token: self.gmail.list_history(start, token),
            )
            headers = self._headers_for(page.records)
            finished = page.history_id if not page.next_page_token else None
            if not page.next_page_token and not finished:
                finished = start
            paths = self.store.apply_history_page(
                page.records,
                headers,
                start=start,
                next_page_token=page.next_page_token,
                finished_history_id=finished,
            )
            _unlink(paths)
            report.history_records += len(page.records)
            if not page.next_page_token:
                break
            token = page.next_page_token

    def _bootstrap(self, report: SyncReport) -> None:
        report.bootstrapped = True
        while True:
            phase = self.store.bootstrap_phase()
            if phase == "need_profile":
                profile = self._call(UNITS_PROFILE, self.gmail.get_profile)
                self.store.set_profile(profile.email, profile.history_id, profile.messages_total)
            elif phase == "inbox":
                self._sync_list("INBOX", report)
                self.store.advance_phase("all")
            elif phase == "all":
                self._sync_list(None, report)
                self.store.advance_phase("catchup")
            elif phase == "catchup":
                start = self.store.history_id() or self.store.bootstrap_history_id()
                try:
                    if start:
                        self.pull_history(start, report)
                except HistoryExpired:
                    if start:
                        self.store.set_history_id(start)
                if self.store.resyncing():
                    while True:
                        paths = self.store.prune_unseen()
                        if not paths:
                            break
                        _unlink(paths)
                self.store.finish_bootstrap()
            elif phase == "done":
                return
            else:
                raise RuntimeError(f"unknown bootstrap phase {phase}")

    def _sync_list(self, label: str | None, report: SyncReport) -> None:
        open_label = self.store.open_label()
        while True:
            token = self.store.page_token()
            page = self._call(
                UNITS_LIST,
                lambda token=token: self.gmail.list_messages(label, None, token),
            )
            batch: list[tuple[IncomingMessage, str]] = []
            for item in page.messages:
                if self.store.already_visited(item.id):
                    continue
                message = self._call(
                    UNITS_GET,
                    lambda mid=item.id: self.gmail.get_message(mid, MessageFormat.METADATA),
                )
                schedule = (
                    "pending_now" if _wants_body_now(message, label, open_label) else "missing"
                )
                batch.append((message, schedule))
            self.store.commit_metadata_page(batch, page.next_page_token)
            report.metadata_upserted += len(batch)
            if not page.next_page_token:
                break

    def _refresh_labels(self) -> None:
        labels = self._call(UNITS_LABELS, self.gmail.list_labels)
        self.store.replace_labels(labels)

    def _headers_for(self, records: list[HistoryRecord]) -> dict[str, IncomingMessage | None]:
        wanted: list[str] = []
        for record in records:
            for message in record.messages_added:
                if message.id and not self.store.has_metadata(message.id):
                    wanted.append(message.id)
            for change in (*record.labels_added, *record.labels_removed):
                if change.message.id and not self.store.has_metadata(change.message.id):
                    wanted.append(change.message.id)
        headers: dict[str, IncomingMessage | None] = {}
        for message_id in dict.fromkeys(wanted):
            try:
                headers[message_id] = self._call(
                    UNITS_GET,
                    lambda message_id=message_id: self.gmail.get_message(
                        message_id, MessageFormat.METADATA
                    ),
                )
            except NotFound:
                headers[message_id] = None
        return headers

    def _download_body(self, gmail_id: str) -> bool:
        """Save one raw message. False when Gmail no longer has it."""
        tracing = profile.recording()
        if tracing:
            profile.begin_message()
        try:
            message = self._call(
                UNITS_GET_RAW,
                lambda: self.gmail.get_message(gmail_id, MessageFormat.RAW),
            )
        except NotFound:
            if tracing:
                profile.discard_message()
            _unlink(self.store.delete_message(gmail_id))
            return False
        except GmailError as exc:
            # A stubborn FAILED_PRECONDITION on one body must not park sync.
            if tracing:
                profile.discard_message()
            if "temporary error" in str(exc).lower() or "precondition" in str(exc).lower():
                return False
            raise
        self._save_body(message)
        if tracing:
            profile.finish_message(gmail_id, len(message.raw or b""))
        return True

    def _save_body(self, message: IncomingMessage) -> None:
        raw = message.raw or b""
        # A raw Gmail response is parsed while it is read. Parsing it again
        # walked every attachment a second time.
        if message.body_text is None:
            parsed = parse_rfc822(raw)
            if not message.subject:
                message.subject = parsed.subject
            if not message.from_addr:
                message.from_addr = parsed.from_addr
            if not message.to_addr:
                message.to_addr = parsed.to_addr
            if not message.cc_addr:
                message.cc_addr = parsed.cc_addr
            if not message.message_id_header:
                message.message_id_header = parsed.message_id_header
            if not message.in_reply_to:
                message.in_reply_to = parsed.in_reply_to
            if not message.references_header:
                message.references_header = parsed.references
            message.has_attachment = parsed.has_attachment
            message.body_text = parsed.text
        if profile.recording():
            started = time.perf_counter()
            self.store.write_body(message, raw, message.body_text)
            profile.add("save", time.perf_counter() - started)
        else:
            self.store.write_body(message, raw, message.body_text)

    def _merge_remote(
        self,
        query: str,
        parsed: ParsedQuery,
        hits: dict[str, SearchHit],
        limit: int,
    ) -> None:
        token: str | None = None
        seen = 0
        for _page in range(5):
            page = self._call(
                UNITS_LIST,
                lambda token=token: self.gmail.list_messages(None, query, token),
            )
            if not page.messages:
                return
            for item in page.messages:
                seen += 1
                if item.id not in hits:
                    if not self.store.has_metadata(item.id):
                        try:
                            meta = self._call(
                                UNITS_GET,
                                lambda mid=item.id: self.gmail.get_message(mid, MessageFormat.METADATA),
                            )
                        except NotFound:
                            continue
                        self.store.upsert_metadata(meta, "missing")
                    hit = self.store.search_hit(item.id)
                    if hit is not None and _hit_matches(self.store, hit, parsed):
                        hits[item.id] = hit
                if seen >= limit:
                    return
            token = page.next_page_token
            if not token:
                return

    def _push_one(
        self,
        gmail_id: str,
        items: list[QueueItem],
    ) -> tuple[list[QueueItem], bool, IncomingMessage | None]:
        items = self._resolve_created_labels(items)
        try:
            remote = self._send(gmail_id, items)
            return items, False, remote
        except Conflict:
            meta = self._call(UNITS_GET, lambda: self.gmail.get_message(gmail_id, MessageFormat.METADATA))
            items = self.store.rebase_and_snapshot(gmail_id, meta.label_ids)
            remote = self._send(gmail_id, items)
            return items, True, remote
        except NotFound:
            _unlink(self.store.delete_message(gmail_id))
            return items, False, None

    def _resolve_created_labels(self, items: list[QueueItem]) -> list[QueueItem]:
        resolved: list[QueueItem] = []
        for item in items:
            label = self._materialize_label(item.label)
            if label == item.label:
                resolved.append(item)
            else:
                resolved.append(QueueItem(item.id, item.seq, item.gmail_id, label, item.op))
        return resolved

    def _materialize_label(self, label_id: str) -> str:
        created = getattr(self, "_created_labels", None)
        if created is None:
            created = {}
            self._created_labels = created
        if label_id in created:
            return created[label_id]
        name = self.store.pending_label_name(label_id)
        if name is None:
            return label_id
        try:
            remote = self._call(UNITS_MODIFY, lambda: self.gmail.create_label(name))
        except Conflict:
            remote = self._label_named(name)
            if remote is None:
                raise
        self.store.adopt_label_id(label_id, remote.id, remote.name)
        created[label_id] = remote.id
        return remote.id

    def _label_named(self, name: str) -> Label | None:
        for label in self._call(UNITS_LABELS, self.gmail.list_labels):
            if label.name.casefold() == name.casefold():
                return label
        return None

    def _send(self, gmail_id: str, items: list[QueueItem]) -> IncomingMessage:
        add = [item.label for item in items if item.op == "add"]
        remove = [item.label for item in items if item.op == "remove"]
        if not add and not remove:
            return self._call(UNITS_GET, lambda: self.gmail.get_message(gmail_id, MessageFormat.METADATA))
        return self._call(
            UNITS_MODIFY,
            lambda: self.gmail.modify_message(gmail_id, add, remove),
        )

    def refresh_auth(self, *, force: bool = False) -> bool:
        """Reload the Gmail bearer token after sleep or a 401."""
        reauth = getattr(self.gmail, "reauthorize", None)
        if not callable(reauth):
            return False
        try:
            return bool(reauth(force=force))
        except OAuthError as exc:
            raise Offline(str(exc)) from exc

    def _call(self, cost: int, fn):
        attempts = 0
        offline_attempts = 0
        refreshed = False
        while True:
            self.quota.acquire(cost)
            try:
                return fn()
            except Offline:
                # After sleep the first connect/DNS try often fails before the
                # NIC has an address. A short retry beats parking the mailbox.
                offline_attempts += 1
                if offline_attempts <= 3:
                    closer = getattr(self.gmail, "close", None)
                    if callable(closer):
                        closer()
                    self.quota.backoff(offline_attempts)
                    continue
                raise
            except HttpStatus as exc:
                if not refreshed and _unauthorized(exc) and self.refresh_auth(force=True):
                    refreshed = True
                    continue
                if is_precondition_failed(exc.code, str(exc)):
                    attempts += 1
                    if attempts <= 3:
                        self.quota.backoff(attempts)
                        continue
                    raise GmailError(
                        "Gmail had a temporary error (precondition); will retry"
                    ) from exc
                raise
            except RateLimited as exc:
                attempts += 1
                if _minute_quota(str(exc)):
                    self.quota.hold()
                    if attempts > 3:
                        raise RateLimited("Gmail quota reached") from exc
                    continue
                if attempts > 5:
                    raise
                self.quota.backoff(attempts)


class UrgentWork:
    """The open message, then queued label changes. One Gmail call at a time.

    The network call runs on the account thread. The screen only records the
    result, so a key never waits on Gmail.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.phase = ""
        self.dirty = False
        self.paused = False
        self._mu = threading.Lock()
        self._inflight: tuple | None = None
        self._running = False
        self._ready: list[tuple] = []

    def has_work(self, session) -> bool:
        if getattr(session, "label_todo", None):
            return True
        if self.paused:
            return False
        with self._mu:
            if self._inflight is not None or self._running or self._ready:
                return True
        return bool(session.needs_fetch) or self.engine.store.has_pending_mutations()

    def take(self, session) -> bool:
        """Snapshot the next call. This does not talk to Gmail."""
        with self._mu:
            if self._inflight is not None or self._running:
                return False
            todo = getattr(session, "label_todo", None)
            if todo:
                # Persist deferred archive/delete before any open/push.
                session.label_todo = []
                self._inflight = ("local", todo)
                self.phase = ""
                return True
            if self.paused:
                return False
            if session.needs_fetch:
                if not session.messages:
                    session.needs_fetch = False
                    return False
                gmail_id = session.messages[session.message_cursor].gmail_id
                self._inflight = ("open", gmail_id)
                self.phase = "opening mail"
                return True
            groups = self.engine.store.queue_groups()
            if not groups:
                self.phase = ""
                return False
            gmail_id, items = next(iter(groups.items()))
            self._inflight = ("push", gmail_id, items)
            self.phase = "sending to Gmail"
            return True

    def run(self) -> None:
        """Perform the snapshotted Gmail call. Safe off the UI thread."""
        with self._mu:
            job = self._inflight
            if job is None or self._running:
                return
            self._inflight = None
            self._running = True
        ready: tuple = ("error", "interrupted", job)
        try:
            if job[0] == "local":
                changes: list[tuple[str, list[str], list[str]]] = []
                done: set[str] = set()
                for targets, add_labels, remove_labels in job[1]:
                    for gmail_id in targets:
                        changes.append((gmail_id, add_labels, remove_labels))
                        done.add(gmail_id)
                try:
                    self.engine.store.apply_local_labels_batch(changes)
                except Exception as exc:  # noqa: BLE001 - surface on the status line
                    ready = ("error", str(exc), job)
                else:
                    ready = ("local", done)
            elif job[0] == "open":
                try:
                    self.engine.open_message(job[1])
                except Offline as exc:
                    ready = ("offline", str(exc), job)
                except GmailError as exc:
                    ready = ("error", str(exc), job)
                else:
                    ready = ("open", job[1])
            else:
                gmail_id, items = job[1], job[2]
                try:
                    items, _rebased, remote = self.engine._push_one(gmail_id, items)
                except Offline as exc:
                    ready = ("offline", str(exc), job)
                except GmailError as exc:
                    ready = ("error", str(exc), job)
                else:
                    if remote is not None:
                        self.engine.store.complete_push(
                            gmail_id, items, remote.label_ids, remote.history_id
                        )
                    self.dirty = True
                    ready = ("push", gmail_id)
        finally:
            with self._mu:
                self._running = False
                self._ready.append(ready)

    def apply(self, session) -> bool:
        """Fold finished calls into the screen. No network."""
        with self._mu:
            ready = self._ready
            self._ready = []
        if not ready:
            return False
        saw_fault = False
        for item in ready:
            kind = item[0]
            if kind == "local":
                hidden = getattr(session, "_hidden", None)
                if isinstance(hidden, set):
                    hidden -= item[1]
                # Folder counts waited for this write; refresh them off the key.
                session.labels_stale = True
                continue
            if kind in {"error", "offline"}:
                # Offline after sleep is transient; keep the next open/push armed.
                saw_fault = True
                self.paused = kind == "error"
                self.phase = item[1]
                job = item[2]
                if job[0] == "open":
                    self._clear_fetch(session, job[1])
                continue
            if kind == "open":
                self._clear_fetch(session, item[1])
        if (
            not saw_fault
            and not self.paused
            and not session.needs_fetch
            and not getattr(session, "label_todo", None)
            and not self.engine.store.has_pending_mutations()
        ):
            self.phase = ""
        session.urgent_phase = self.phase
        return True

    def step(self, session) -> bool:
        """Run one call to completion. Tests and the startup path use this."""
        started = self.take(session)
        if started:
            self.run()
        return self.apply(session)

    def _clear_fetch(self, session, gmail_id: str) -> None:
        current = ""
        if session.messages:
            current = session.messages[session.message_cursor].gmail_id
        if current == gmail_id:
            session.note_body()
        elif getattr(session, "_fetch_gmail_id", None) == gmail_id:
            session.needs_fetch = False
            session._fetch_gmail_id = None
            if hasattr(session, "_set_open_waiting"):
                session._set_open_waiting()
            else:
                self.engine.open_waiting = False


class BackgroundSync:
    """One network step at a time.

    ``startup`` drains every step. The screen reads the mailbox while this
    runs on the account thread, so a keypress does not wait on a download.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.report = SyncReport(open_label=engine.store.open_label())
        self.mode = "labels"
        self.exc: Exception | None = None
        self.phase = "refreshing labels"
        self.done = 0
        self.total = 0
        self.tick = 0
        self.dirty = False
        self._step_lock = threading.Lock()
        self._list_items: list | None = None
        self._list_index = 0
        self._list_batch: list[tuple[IncomingMessage, str]] = []
        self._list_next: str | None = None
        self._hist_start: str | None = None
        self._hist_token: str | None = None
        self._hist_page = None
        self._hist_epoch = 0
        self._list_epoch = 0
        self._list_estimate = 0
        self._hist_wanted: list[str] = []
        self._hist_headers: dict[str, IncomingMessage | None] = {}
        self._hist_in_bootstrap = False
        self._ids: list[str] = []
        self._index = 0
        self._groups: list[tuple[str, list[QueueItem]]] = []
        self._seen: set[str] = set()
        self._inbox_catchup = False
        self._polling = False
        self._poll_new = 0
        self.mail_alert = 0
        self.list_bump = False
        # Wall time of the last history page that finished without error.
        self.checked_at: float | None = None
        self._resume_mode = ""
        self._poll_at = 0.0
        self._retry_at = 0.0
        self._cache_done = 0
        self._cache_total = -1
        self._auth_pending = False
        self._network_until = 0.0
        self._wall = time.time()
        self._mono = time.monotonic()
        # A finished replica only needs history. Re-listing the whole inbox on
        # every launch made a warm start feel like the first sync again.
        if (
            self.engine.store.bootstrap_phase() != "done"
            or not self.engine.store.history_id()
        ):
            self._arm_inbox()

    @property
    def running(self) -> bool:
        return self.mode not in {"idle", "offline", "error"}

    def restart(self, *, refresh_auth: bool = False) -> None:
        with self._step_lock:
            fresh = BackgroundSync(self.engine)
            fresh._step_lock = self._step_lock
            self.__dict__.clear()
            self.__dict__.update(fresh.__dict__)
            if refresh_auth:
                self._auth_pending = True
                self.phase = "signing in"
                self.done = 0
                self.total = 0
                self.dirty = True

    def note_clock(self) -> bool:
        """Detect suspend/resume via wall-clock jump. True when network is deferred."""
        wall = time.time()
        mono = time.monotonic()
        jumped = (wall - self._wall) - (mono - self._mono) >= 3.0
        self._wall = wall
        self._mono = mono
        if not jumped:
            return False
        self.defer_network(_RESUME_GRACE_SECONDS)
        return True

    def defer_network(self, seconds: float = _RESUME_GRACE_SECONDS) -> None:
        """Pause Gmail calls until DHCP/DNS usually catch up after wake."""
        until = time.monotonic() + max(0.0, seconds)
        self._network_until = max(self._network_until, until)
        closer = getattr(self.engine.gmail, "close", None)
        if callable(closer):
            closer()
        if self._poll_at > 0:
            self._poll_at = max(self._poll_at, until)
        if self.running or self.mode in {"offline", "error"}:
            # Park mid-sync work so the account thread sleeps the grace out
            # instead of spinning on a NIC that still has no address.
            self.mode = "offline" if self.mode != "error" else self.mode
            self._retry_at = max(self._retry_at, until)
            self._auth_pending = False
            self._polling = False
            self.phase = "waiting for network"
            self.done = 0
            self.total = 1
            self.dirty = True

    def _network_ready(self) -> bool:
        return time.monotonic() >= self._network_until

    def set_background_bodies(self, enabled: bool) -> None:
        """Stop or resume downloading bodies the user has not asked for."""
        self.engine.background_bodies = enabled
        if enabled:
            if self.phase == "cache paused":
                self.phase = "caching mail"
            return
        if self.mode not in {"offline", "error"}:
            self.phase = "cache paused"

    def cache_due(self) -> bool:
        """True when an idle mailbox should resume background body downloads."""
        if not self._network_ready():
            return False
        if not self.engine.background_bodies or self.running or self.mode in {"offline", "error"}:
            return False
        return self._uncached_bodies()

    def step(self) -> bool:
        with self._step_lock:
            self.engine.store.checkpoint()
            if self.engine.quota.stopped():
                return False
            if self._auth_pending:
                if not self._network_ready():
                    return False
                return self._refresh_auth_step()
            if not self._network_ready():
                return False
            if self.mode == "idle" and self.engine.background_bodies and self._uncached_bodies():
                self._begin_backfill()
            if self.mode == "idle" and self.poll_due():
                self._begin_poll()
            if not self.running:
                return False
            self.tick += 1
            try:
                self._dispatch()
            except SyncStopped:
                return False
            except HistoryExpired:
                self._expire_history()
            except Offline as exc:
                self._stop(exc)
            except GmailError as exc:
                self._stop(exc)
            except Exception as exc:
                # Unexpected failures still need a scheduled retry, or the
                # mailbox sits mid-step forever until the process restarts.
                self._stop(exc)
            return self.running

    def poll_due(self) -> bool:
        """True when an idle mailbox should look for new mail."""
        if not self._network_ready():
            return False
        if self.running or self.mode in {"offline", "error"}:
            return False
        return self._poll_wait() == 0.0

    def retry_due(self) -> bool:
        """True when an offline mailbox should start again."""
        if not self._network_ready():
            return False
        if self.mode not in {"offline", "error"}:
            return False
        if self._retry_at <= 0:
            return False
        return time.monotonic() >= self._retry_at

    def check_mail(self, *, refresh_auth: bool = False) -> None:
        """Ask this mailbox to look for new mail as soon as it can.

        Offline or errored jobs resume immediately. Idle jobs start a history
        poll. A job already mid-check is left alone.
        """
        with self._step_lock:
            if self.engine.wake_due_snoozes():
                self.list_bump = True
                self.dirty = True
            if refresh_auth:
                self._auth_pending = True
                self.phase = "signing in"
                self.done = 0
                self.total = 0
                self.dirty = True
            if self.mode in {"offline", "error"}:
                # A manual check overrides the post-sleep DHCP grace.
                self._network_until = 0.0
                self._retry_at = time.monotonic()
                self.dirty = True
                return
            self._poll_at = time.monotonic()
            if self.mode == "idle" and not self._auth_pending:
                self._begin_poll()
                self.dirty = True

    def recover_if_due(self) -> bool:
        """Resume after an offline wait. True when the job should run again."""
        if not self.retry_due():
            return False
        # First boot never finished: rebuild from scratch like the sync key.
        if self._history_start() is None:
            self.restart(refresh_auth=True)
            return True
        with self._step_lock:
            if self.mode not in {"offline", "error"}:
                return False
            self.exc = None
            self.mode = "labels"
            self.phase = "signing in"
            self.done = 0
            self.total = 0
            self._retry_at = 0.0
            self._polling = False
            self._hist_page = None
            self._auth_pending = True
            # Poll as soon as labels finish — do not wait another full minute.
            self._poll_at = time.monotonic()
            self.dirty = True
        return True

    def seconds_until_poll(self) -> float | None:
        grace = max(0.0, self._network_until - time.monotonic())
        if self.mode in {"offline", "error"}:
            if self._retry_at <= 0:
                return max(_RETRY_SECONDS, grace)
            return max(0.0, self._retry_at - time.monotonic(), grace)
        if self.running:
            return None
        wait = self._poll_wait()
        if wait is None:
            return None if grace <= 0 else grace
        return max(wait, grace)

    def _history_start(self) -> str | None:
        return self.engine.store.history_id() or self.engine.store.bootstrap_history_id()

    def _poll_wait(self) -> float | None:
        if not self._history_start():
            return None
        # Arm on first ask. A warm, fully-cached mailbox used to sit idle with
        # `_poll_at == 0` forever, so the 60s check never ran until the user
        # pressed s/S.
        if self._poll_at <= 0:
            self._poll_at = time.monotonic() + _POLL_SECONDS
        return max(0.0, self._poll_at - time.monotonic())

    def _poll_due(self) -> bool:
        if self._polling or self.mode in {
            "labels",
            "history",
            "history-bootstrap",
            "catchup-finish",
            "bodies",
            "push",
            "priority",
            "idle",
            "offline",
            "error",
        }:
            return False
        if self._inbox_catchup or self.engine.store.next_priority_bodies(
            self.engine.store.open_label(), 1
        ):
            return False
        if not self._history_start():
            return False
        wait = self._poll_wait()
        return wait is not None and wait <= 0.0

    def _begin_poll(self) -> None:
        if self.engine.wake_due_snoozes():
            self.list_bump = True
            self.dirty = True
        start = self._history_start()
        if not start:
            self._poll_at = time.monotonic() + _POLL_SECONDS
            return
        self._polling = True
        self._poll_new = 0
        self._resume_mode = self.mode
        self._prepare_history(start, in_bootstrap=False)
        self.mode = "history"
        self.phase = "checking for new mail"
        self.done = 0
        self.total = 0
        self._poll_at = time.monotonic() + _POLL_SECONDS

    def _finish_poll(self) -> None:
        self._polling = False
        if self._poll_new:
            self.mail_alert += self._poll_new
            self.list_bump = True
            self.dirty = True
        self._poll_new = 0
        if not self.engine.background_bodies:
            self._pause_cache()
            return
        pending = [
            gmail_id
            for gmail_id in self.engine.store.pending_body_ids(self.engine.store.open_label())
            if not self.engine.store.body_readable(gmail_id)
        ]
        resume = self._resume_mode or "idle"
        if pending and self.engine.background_bodies:
            self._ids = pending
            self._index = 0
            self.done = 0
            self.total = len(pending)
            self._resume_mode = resume
            self.mode = "priority"
            self.phase = f"fetching new mail 0/{self.total}"
            return
        self._resume_mode = ""
        if resume in {"", "idle"}:
            self._finish_idle()
        else:
            self.mode = resume

    def _priority_step(self) -> None:
        if not self.engine.background_bodies:
            self._pause_cache()
            return
        if self._index >= len(self._ids):
            resume = self._resume_mode or "idle"
            self._resume_mode = ""
            if resume in {"", "idle"}:
                self._finish_idle()
            else:
                self.mode = resume
            return
        gmail_id = self._ids[self._index]
        self._index += 1
        self.done = self._index
        self.total = len(self._ids)
        self.phase = f"fetching new mail {self.done}/{self.total}"
        if self.engine.store.body_readable(gmail_id):
            return
        self.engine._download_body(gmail_id)
        self.report.bodies_downloaded += 1
        # The open reader notices the file on its own. Dirty would rebuild
        # the mailbox between keypresses.

    def _pace_body(self) -> None:
        """Space out background raw fetches unless the open reader is waiting."""
        if self.engine.open_waiting:
            return
        self.engine.quota.spread(UNITS_GET_RAW)

    def _stop(self, exc: Exception) -> None:
        self.exc = exc
        self.mode = "offline" if isinstance(exc, Offline) else "error"
        self.phase = str(exc)
        self.done = 0
        self.total = 1
        self._auth_pending = False
        self._polling = False
        self._hist_page = None
        wait = _offline_retry_seconds(exc)
        self._retry_at = time.monotonic() + wait
        if self._network_until > self._retry_at:
            self._retry_at = self._network_until
        # No address yet: give DHCP another full grace window before the retry.
        if isinstance(exc, Offline) and _network_not_ready(str(exc)):
            self.defer_network(max(_RESUME_GRACE_SECONDS, wait))
        self.dirty = True
        closer = getattr(self.engine.gmail, "close", None)
        if callable(closer):
            closer()

    def _refresh_auth_step(self) -> bool:
        """Ask Google for a bearer token. The status line already says so."""
        self.phase = "signing in"
        self.done = 0
        self.total = 0
        self.tick += 1
        self.dirty = True
        try:
            self.engine.refresh_auth(force=True)
        except Offline as exc:
            self._auth_pending = False
            self._stop(exc)
            return False
        self._auth_pending = False
        if self.mode == "idle":
            self.mode = "labels"
        if self.mode == "labels":
            self.phase = "refreshing labels"
        return True

    def _dispatch(self) -> None:
        if self.mode != "labels" and self._serve_inbox():
            return
        if self._poll_due():
            self._begin_poll()
        if self.mode == "labels":
            self.phase = "refreshing labels"
            self.done = 0
            self.total = 0
            self.engine._refresh_labels()
            self.dirty = True
            self._enter_after_labels()
        elif self.mode == "bootstrap":
            self._bootstrap_step()
        elif self.mode in ("history", "history-bootstrap"):
            self._history_step()
        elif self.mode == "catchup-finish":
            self._finish_catchup()
            self._begin_bodies()
        elif self.mode == "bodies":
            self._body_step()
        elif self.mode == "push":
            self._push_step()
        elif self.mode == "backfill":
            self._backfill_step()
        elif self.mode == "priority":
            self._priority_step()

    def _enter_after_labels(self) -> None:
        phase = self.engine.store.bootstrap_phase()
        if phase != "done":
            self.report.bootstrapped = True
            self.mode = "bootstrap"
            self.phase = "listing mail"
            return
        history_id = self.engine.store.history_id()
        if not history_id:
            self.engine.ensure_token_matches_mailbox()
            self.engine.store.begin_resync()
            self.dirty = True
            self.report.bootstrapped = True
            self.mode = "bootstrap"
            self.phase = "rebuilding mailbox"
            return
        self._prepare_history(history_id, in_bootstrap=False)
        self.mode = "history"
        self.phase = "checking for new mail"
        self.done = 0
        self.total = 0

    def _bootstrap_step(self) -> None:
        store = self.engine.store
        phase = store.bootstrap_phase()
        if phase == "need_profile":
            self.phase = "reading mailbox size"
            self.done = 0
            self.total = 0
            self.engine.ensure_token_matches_mailbox()
            profile = self.engine._call(UNITS_PROFILE, self.engine.gmail.get_profile)
            store.set_profile(profile.email, profile.history_id, profile.messages_total)
            self._arm_inbox()
            self.dirty = True
        elif phase == "inbox":
            if self._list_items is None:
                self.phase = "listing Inbox"
                self.done = 0
                self.total = 0
            if self._list_step("INBOX"):
                return
            store.advance_phase("all")
            self._reset_list()
        elif phase == "all":
            if self._list_items is None:
                self.phase = "listing all mail"
                self.done = 0
                self.total = 0
            if self._list_step(None):
                return
            store.advance_phase("catchup")
            self._reset_list()
        elif phase == "catchup":
            start = store.history_id() or store.bootstrap_history_id()
            self._prepare_history(start, in_bootstrap=True)
            if start:
                self.mode = "history-bootstrap"
                self.phase = "catching up with Gmail"
                self.done = 0
                self.total = 0
            else:
                self.mode = "catchup-finish"
        elif phase == "done":
            self._begin_bodies()
        else:
            raise RuntimeError(f"unknown bootstrap phase {phase}")

    def _list_step(self, label: str | None) -> bool:
        store = self.engine.store
        if self._list_items is None:
            token = store.page_token()
            page = self.engine._call(
                UNITS_LIST,
                lambda token=token: self.engine.gmail.list_messages(label, None, token),
            )
            self._list_items = list(page.messages)
            self._list_index = 0
            self._list_batch = []
            self._list_next = page.next_page_token
            self._list_epoch = store.label_epoch
            if token is None:
                self._list_estimate = 0
            if page.result_size_estimate:
                self._list_estimate = page.result_size_estimate
            self._publish_list_progress(label)
            return True
        if self._list_index < len(self._list_items):
            item = self._list_items[self._list_index]
            self._list_index += 1
            self._publish_list_progress(label)
            message_id = item.id
            if message_id and not store.already_visited(message_id):
                message = self.engine._call(
                    UNITS_GET,
                    lambda message_id=message_id: self.engine.gmail.get_message(
                        message_id, MessageFormat.METADATA
                    ),
                )
                schedule = (
                    "pending_now"
                    if _wants_body_now(message, label, store.open_label())
                    else "missing"
                )
                store.stage_message(message, schedule)
                self._list_batch.append((message, schedule))
                self.dirty = True
            return True
        if store.label_epoch != self._list_epoch:
            self._reset_list()
            return True
        store.commit_metadata_page(self._list_batch, self._list_next)
        store.note_listed(len(self._list_items))
        self.report.metadata_upserted += len(self._list_batch)
        self.dirty = True
        more = self._list_next is not None
        self._reset_list()
        return more

    def _arm_inbox(self) -> None:
        self._inbox_catchup = True
        self._inbox_items = None
        self._inbox_index = 0
        self._inbox_token: str | None = None
        self._inbox_next: str | None = None
        self._inbox_seen: set[str] = set()
        self._inbox_total: int | None = None
        self._inbox_scan: int | None = None

    def _serve_inbox(self) -> bool:
        """Catch up the inbox list, then download the open folder and inbox first."""
        if self._inbox_catchup and self.engine.store.bootstrap_phase() != "need_profile":
            self._inbox_catchup_step()
            return True
        if not self.engine.background_bodies:
            return False
        open_label = self.engine.store.open_label()
        ids = self.engine.store.next_priority_bodies(
            open_label,
            self.engine.quota.available(UNITS_GET_RAW, _INBOX_BODY_CHUNK),
        )
        if ids:
            self._download_priority(ids, open_label)
            return True
        return False

    def _download_priority(self, ids: list[str], open_label: str) -> None:
        done, total = self._priority_body_counts(open_label)
        self.done = done
        self.total = max(total, 1)
        self.phase = f"downloading mail {done}/{self.total}"
        saved = 0
        if len(ids) == 1:
            before = self.engine.store.body_readable(ids[0])
            self.engine._download_body(ids[0])
            saved = int(self.engine.store.body_readable(ids[0]) and not before)
        else:
            messages, missing = self.engine._call(
                UNITS_GET_RAW * len(ids),
                lambda: self.engine.gmail.get_messages(ids, MessageFormat.RAW),
            )
            for message in messages:
                self.engine._save_body(message)
            for gmail_id in missing:
                _unlink(self.engine.store.delete_message(gmail_id))
            saved = len(messages)
        done, total = self._priority_body_counts(open_label)
        self.done = done
        self.total = max(total, 1)
        self.phase = f"downloading mail {done}/{self.total}"
        self.report.bodies_downloaded += saved
        # Rows are already listed. Marking the list dirty here rebuilt the
        # mailbox on the UI thread between each j/k while the bodies saved.
        # Same pacing as backfill: leave room for other accounts on this machine.
        for _ in ids:
            self._pace_body()

    def _priority_body_counts(self, open_label: str) -> tuple[int, int]:
        """Saved vs total for the open folder and inbox (the priority set)."""
        if not open_label or open_label == "INBOX":
            return self.engine.store.inbox_body_counts()
        # Count distinct messages that carry either label.
        row = self.engine.store.conn.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN m.body_state = 'present' THEN 1 ELSE 0 END), 0),
              COUNT(*)
            FROM messages m
            WHERE EXISTS (
              SELECT 1 FROM message_labels ml
              JOIN labels l ON l.id = ml.label_id
              WHERE ml.message_id = m.id AND l.gmail_id IN (?, 'INBOX')
            )
            """,
            (open_label,),
        ).fetchone()
        return int(row[0]), int(row[1])

    def _inbox_catchup_step(self) -> None:
        store = self.engine.store
        if self._inbox_total is None:
            label = self.engine._call(UNITS_LABELS, lambda: self.engine.gmail.get_label("INBOX"))
            self._inbox_total = label.messages_total
            self._publish_inbox_listing()
            return
        if self._inbox_items is None:
            token = self._inbox_token
            page = self.engine._call(
                UNITS_LIST,
                lambda token=token: self.engine.gmail.list_messages("INBOX", None, token),
            )
            self._inbox_items = list(page.messages)
            self._inbox_index = 0
            self._inbox_next = page.next_page_token
            self._publish_inbox_listing()
            return
        self._ingest_inbox_page()
        next_token = self._inbox_next
        self._inbox_items = None
        self._inbox_index = 0
        if next_token:
            self._inbox_token = next_token
            self._publish_inbox_listing()
            return
        if self._inbox_scan is None:
            self._inbox_scan = 0
        self.phase = "updating inbox"
        nxt = store.retain_inbox(self._inbox_seen, self._inbox_scan)
        self.dirty = True
        if nxt is not None:
            self._inbox_scan = nxt
            return
        self._inbox_scan = None
        if store.bootstrap_phase() == "inbox":
            store.advance_phase("all")
            self._reset_list()
        self._inbox_catchup = False
        if self._poll_at <= 0:
            self._poll_at = time.monotonic() + _POLL_SECONDS
        self._publish_inbox_listing()

    def _ingest_inbox_page(self) -> None:
        store = self.engine.store
        pending: list[str] = []
        for item in self._inbox_items or []:
            if not item.id:
                continue
            self._inbox_seen.add(item.id)
            if not store.has_metadata(item.id):
                pending.append(item.id)
                continue
            if "INBOX" not in store.message_label_ids(item.id) and not store.note_inbox(item.id):
                self._inbox_seen.discard(item.id)
        if not pending:
            self._publish_inbox_listing()
            return
        messages, missing = self.engine._call(
            UNITS_GET * len(pending),
            lambda pending=pending: self.engine.gmail.get_messages(pending, MessageFormat.METADATA),
        )
        for gmail_id in missing:
            self._inbox_seen.discard(gmail_id)
            _unlink(store.delete_message(gmail_id))
        for message in messages:
            if "INBOX" not in message.label_ids:
                message.label_ids = [*message.label_ids, "INBOX"]
            store.stage_message(message, "pending_now")
        self.dirty = True
        self._publish_inbox_listing()

    def _publish_inbox_listing(self) -> None:
        walked = len(self._inbox_seen)
        known = self._inbox_total or 0
        total = known if known > 0 else max(walked, 1)
        self.done = walked
        self.total = total
        self.phase = f"listing Inbox {walked}/{total}"

    def _publish_list_progress(self, label: str | None) -> None:
        store = self.engine.store
        walked = store.listed_count() + self._list_index
        page_len = len(self._list_items or [])
        if label is None:
            total = self._account_size() or self._list_estimate
            title = "listing all mail"
            approximate = False
        else:
            total = self._list_estimate or (store.listed_count() + page_len)
            title = "listing Inbox"
            approximate = bool(self._list_estimate)
        if total <= 0:
            total = max(walked, 1)
        self.done = walked
        self.total = total
        shown = f"~{total}" if approximate and total >= walked else str(total)
        self.phase = f"{title} {walked}/{shown}"

    def _account_size(self) -> int:
        total = self.engine.store.messages_total()
        if total > 0:
            return total
        profile = self.engine._call(UNITS_PROFILE, self.engine.gmail.get_profile)
        if profile.messages_total:
            self.engine.store.set_messages_total(profile.messages_total)
        return profile.messages_total

    def _prepare_history(self, start: str | None, *, in_bootstrap: bool) -> None:
        self._hist_start = start
        self._hist_in_bootstrap = in_bootstrap
        self._hist_page = None
        self._hist_wanted = []
        self._hist_headers = {}
        resume_start, resume_token = self.engine.store.history_resume()
        self._hist_token = resume_token if start and resume_start == start else None

    def _history_step(self) -> None:
        if self._hist_page is None:
            start = self._hist_start or ""
            token = self._hist_token
            page = self.engine._call(
                UNITS_HISTORY,
                lambda token=token: self.engine.gmail.list_history(start, token),
            )
            self._hist_page = page
            self._hist_epoch = self.engine.store.label_epoch
            self._hist_wanted = self._wanted(page.records)
            self._hist_headers = {}
            if self._polling:
                arrived = _inbox_arrivals(page.records)
                if arrived:
                    self._poll_new += arrived
                    # Pin on the first refresh that includes these rows, not
                    # only after the poll finishes (when the list already has them).
                    self.list_bump = True
            self.phase = f"history {self.report.history_records + len(page.records)}"
            self.done = 0
            self.total = 0
            return
        if self._hist_wanted:
            message_id = self._hist_wanted.pop(0)
            try:
                self._hist_headers[message_id] = self.engine._call(
                    UNITS_GET,
                    lambda message_id=message_id: self.engine.gmail.get_message(
                        message_id, MessageFormat.METADATA
                    ),
                )
            except NotFound:
                self._hist_headers[message_id] = None
            return
        if self.engine.store.label_epoch != self._hist_epoch:
            self.engine.store.clear_history_page()
            self._prepare_history(self.engine.store.history_id(), in_bootstrap=self._hist_in_bootstrap)
            self.phase = "history"
            return
        page = self._hist_page
        start = self._hist_start or ""
        finished = page.history_id if not page.next_page_token else None
        if not page.next_page_token and not finished:
            finished = start
        paths = self.engine.store.apply_history_page(
            page.records,
            self._hist_headers,
            start=start,
            next_page_token=page.next_page_token,
            finished_history_id=finished,
        )
        _unlink(paths)
        self.report.history_records += len(page.records)
        self.dirty = True
        next_token = page.next_page_token
        self._hist_page = None
        self._hist_headers = {}
        self._hist_wanted = []
        if next_token:
            self._hist_token = next_token
            self.phase = f"history {self.report.history_records}"
            return
        # Full history catch-up finished; the account tab can show how fresh it is.
        self.checked_at = time.time()
        if self._polling:
            self._finish_poll()
            return
        if self._hist_in_bootstrap:
            self.mode = "catchup-finish"
        else:
            self._begin_bodies()

    def _wanted(self, records: list[HistoryRecord]) -> list[str]:
        wanted: list[str] = []
        for record in records:
            for message in record.messages_added:
                if message.id and not self.engine.store.has_metadata(message.id):
                    wanted.append(message.id)
            for change in (*record.labels_added, *record.labels_removed):
                if change.message.id and not self.engine.store.has_metadata(change.message.id):
                    wanted.append(change.message.id)
        return list(dict.fromkeys(wanted))

    def _expire_history(self) -> None:
        if self._hist_in_bootstrap:
            if self._hist_start:
                self.engine.store.set_history_id(self._hist_start)
            self._hist_page = None
            self._hist_wanted = []
            self._hist_headers = {}
            self.mode = "catchup-finish"
            return
        # Wrong-account tokens often look like an expired history cursor.
        # Confirm identity before begin_resync or prune_unseen can wipe mail.
        self.engine.ensure_token_matches_mailbox()
        self.engine.store.begin_resync()
        self._polling = False
        self._poll_new = 0
        self.list_bump = False
        self._resume_mode = ""
        self._arm_inbox()
        self.dirty = True
        self.report.bootstrapped = True
        self._hist_page = None
        self._hist_wanted = []
        self._hist_headers = {}
        self._hist_in_bootstrap = False
        self._reset_list()
        self.mode = "bootstrap"
        self.phase = "rebuilding mailbox"

    def _finish_catchup(self) -> None:
        if self.engine.store.resyncing():
            paths = self.engine.store.prune_unseen()
            if paths:
                _unlink(paths)
                self.dirty = True
                self.phase = "removing old mail"
                return
        self.engine.store.finish_bootstrap()

    def _begin_bodies(self) -> None:
        self._ids = self.engine.store.pending_body_ids(self.engine.store.open_label())
        self._index = 0
        self.done = 0
        self.total = len(self._ids)
        if self._ids and self.engine.background_bodies:
            self.mode = "bodies"
            self.phase = f"downloading new mail 0/{self.total}"
            return
        self._begin_push()

    def _body_step(self) -> None:
        if not self.engine.background_bodies:
            self._begin_push()
            return
        if self._index >= len(self._ids):
            self._begin_push()
            return
        gmail_id = self._ids[self._index]
        self._index += 1
        self.done = self._index
        self.total = len(self._ids)
        self.phase = f"downloading new mail {self.done}/{self.total}"
        if self.engine.store.body_readable(gmail_id):
            return
        self.engine._download_body(gmail_id)
        self.report.bodies_downloaded += 1
        # Same as a priority fetch: the reader updates, the list does not.
        self._pace_body()

    def _begin_push(self) -> None:
        self._groups = list(self.engine.store.queue_groups().items())
        self._index = 0
        self.done = 0
        self.total = len(self._groups)
        if self._groups:
            self.mode = "push"
            self.phase = f"sending to Gmail 0/{self.total}"
            return
        self._begin_backfill()

    def _push_step(self) -> None:
        if self._index >= len(self._groups):
            self._begin_backfill()
            return
        gmail_id, items = self._groups[self._index]
        self._index += 1
        self.done = self._index
        self.total = len(self._groups)
        self.phase = f"sending to Gmail {self.done}/{self.total}"
        items = [item for item in items if self.engine.store.mutation_pending(item.id, item.seq)]
        if not items:
            return
        items, did_rebase, remote = self.engine._push_one(gmail_id, items)
        self.dirty = True
        if remote is None:
            return
        self.engine.store.complete_push(gmail_id, items, remote.label_ids, remote.history_id)
        self.report.mutations_pushed += 1
        self.report.mutations_rebased += int(did_rebase)

    def _begin_backfill(self) -> None:
        if not self.engine.background_bodies:
            self._pause_cache()
            return
        if not self.engine.store.is_idle():
            self._finish_idle()
            return
        if not self._uncached_bodies():
            self._finish_idle()
            return
        self.mode = "backfill"
        self._seen = set()
        self._cache_total = -1
        self._publish_cache()

    def _backfill_step(self) -> None:
        if not self.engine.background_bodies:
            self._pause_cache()
            return
        if not self.engine.store.is_idle():
            self._finish_idle()
            return
        missing = self.engine.store.missing_body_ids(
            limit=1, open_label=self.engine.store.open_label()
        )
        if not missing or missing[0] in self._seen:
            self._finish_idle()
            return
        self._seen.add(missing[0])
        if self.engine._download_body(missing[0]):
            self.report.backfill_downloaded += 1
            self._cache_done += 1
        elif self._cache_total >= 0:
            self._cache_total = max(0, self._cache_total - 1)
        self._publish_cache()
        # One raw fetch is 60 of the 6,000 units, so it owns 0.6s of the minute.
        # Sleeping the rest keeps a reused connection from running the parser
        # and the index back to back until the budget is gone.
        self._pace_body()

    def _publish_cache(self) -> None:
        if not self.engine.background_bodies:
            self.phase = "cache paused"
            return
        if self._cache_total < 0:
            self._cache_done, self._cache_total = self.engine.store.body_cache_counts()
        self.done = self._cache_done
        self.total = max(self._cache_total, 1)
        self.phase = f"caching mail {self._cache_done}/{self._cache_total}"

    def _pause_cache(self) -> None:
        self._finish_idle()
        self.phase = "cache paused"

    def _uncached_bodies(self) -> bool:
        store = self.engine.store
        return store.missing_body_count() > 0 or bool(
            store.next_priority_bodies(store.open_label(), 1)
        )

    def _finish_idle(self) -> None:
        if self.engine.wake_due_snoozes():
            self.list_bump = True
            self.dirty = True
        self.mode = "idle"
        self.phase = "idle"
        self.done = 1
        self.total = 1

    def _reset_list(self) -> None:
        self._list_items = None
        self._list_index = 0
        self._list_batch = []
        self._list_next = None


def _minute_quota(detail: str) -> bool:
    text = detail.lower()
    return "per minute" in text or "userratelimitexceeded" in text


def _network_not_ready(detail: str) -> bool:
    """True when the failure looks like no link/DNS yet (common after hibernate)."""
    text = detail.lower()
    needles = (
        "network is unreachable",
        "network unreachable",
        "no route to host",
        "name resolution",
        "name or service not known",
        "temporary failure",
        "errno 101",
        "errno 99",
        "errno 113",
        "enetunreach",
        "ehostunreach",
        "enetdown",
        "network down",
        "cannot assign requested address",
        "network is down",
        "host is down",
    )
    return any(item in text for item in needles)


def _offline_retry_seconds(exc: Exception) -> float:
    if isinstance(exc, Offline) and _network_not_ready(str(exc)):
        return _NETWORK_RETRY_SECONDS
    return _RETRY_SECONDS


def _unauthorized(exc: BaseException) -> bool:
    if isinstance(exc, HttpStatus):
        if exc.code == 401:
            return True
        text = f"{exc.body} {exc}".lower()
    else:
        text = str(exc).lower()
    return "invalid authentication credentials" in text or "invalid credentials" in text


def _wants_body_now(message: IncomingMessage, listed_label: str | None, open_label: str) -> bool:
    # On-demand folders (config ``on-demand``): metadata only until open/viewed.
    if defers_body(message.label_ids, open_label):
        return False
    if "INBOX" in message.label_ids:
        return True
    return _in_view(message, listed_label, open_label)


def _in_view(message: IncomingMessage, listed_label: str | None, open_label: str) -> bool:
    if listed_label is not None and listed_label == open_label:
        return True
    return open_label in message.label_ids


def _hit_matches(store: Store, hit: SearchHit, parsed: ParsedQuery) -> bool:
    labels = set(hit.label_ids)
    if parsed.unread is True and "UNREAD" not in labels:
        return False
    if parsed.unread is False and "UNREAD" in labels:
        return False
    if parsed.starred is True and "STARRED" not in labels:
        return False
    if parsed.label:
        keys = store.message_label_keys(hit.gmail_id)
        if parsed.label not in keys and parsed.label.lower() not in keys:
            return False
    return True


def _opened(row: dict[str, object] | None) -> OpenedMessage:
    if row is None:
        raise NotFound("message disappeared while opening")
    csv = str(row.get("label_ids") or "")
    return OpenedMessage(
        gmail_id=str(row["gmail_id"]),
        thread_gmail_id=str(row["thread_gmail_id"]),
        subject=str(row["subject"]),
        from_addr=str(row["from_addr"]),
        to_addr=str(row["to_addr"]),
        cc_addr=str(row["cc_addr"]),
        snippet=str(row["snippet"]),
        internal_date_ms=int(row["internal_date"]),
        label_ids=tuple(part for part in csv.split(" ") if part),
        body_text=str(row["body_text"]),
        body_cached=row["body_state"] == "present",
    )


def _inbox_arrivals(records: list[HistoryRecord]) -> int:
    """Count history adds that land in INBOX (desktop notify candidates)."""
    return sum(
        1
        for record in records
        for message in record.messages_added
        if message.id and "INBOX" in message.label_ids
    )


def _unlink(paths: list[Path]) -> None:
    for path in paths:
        if path.exists():
            path.unlink()


"""In-memory Gmail used by the sync tests."""

from __future__ import annotations

from dataclasses import dataclass

from gmail_tui.gmail import (
    Conflict,
    HistoryExpired,
    HistoryMessage,
    HistoryPage,
    HistoryRecord,
    IncomingMessage,
    Label,
    LabelChange,
    ListedMessage,
    MessageFormat,
    MessageListPage,
    NotFound,
    Offline,
    Profile,
    RateLimited,
)


def make_raw(
    *,
    subject: str,
    from_addr: str,
    to_addr: str = "me@gmail.com",
    cc_addr: str = "",
    body: str,
    message_id: str = "m@example.com",
) -> bytes:
    lines = [
        f"Subject: {subject}",
        f"From: {from_addr}",
        f"To: {to_addr}",
        f"Cc: {cc_addr}",
        f"Message-ID: <{message_id}>",
        "MIME-Version: 1.0",
        "Content-Type: text/plain; charset=utf-8",
        "",
        body,
    ]
    return "\r\n".join(lines).encode()


@dataclass
class FakeMessage:
    id: str
    thread_id: str
    label_ids: list[str]
    subject: str
    from_addr: str
    body: str
    internal_date_ms: int
    snippet: str = ""
    to_addr: str = "me@gmail.com"
    cc_addr: str = ""
    history_id: str = "1"
    raw_bytes: bytes | None = None

    def raw(self) -> bytes:
        if self.raw_bytes is not None:
            return self.raw_bytes
        return make_raw(
            subject=self.subject,
            from_addr=self.from_addr,
            to_addr=self.to_addr,
            cc_addr=self.cc_addr,
            body=self.body,
            message_id=f"{self.id}@example.com",
        )


class FakeGmail:
    def __init__(self) -> None:
        self.email = "user@gmail.com"
        self.profile_history_id = "100"
        self.labels = [
            Label("INBOX", "INBOX", "system"),
            Label("UNREAD", "UNREAD", "system"),
            Label("STARRED", "STARRED", "system"),
            Label("TRASH", "TRASH", "system"),
            Label("IMPORTANT", "IMPORTANT", "system"),
            Label("Label_1", "Archive", "user"),
        ]
        self.messages: dict[str, FakeMessage] = {}
        self.records: list[HistoryRecord] = []
        self.expired_ids: set[str] = set()
        self.conflict_once: set[str] = set()
        self.modify_offline = False
        self.history_offline = False
        self.profile_rate_limits = 0
        self.raw_quota_once = 0
        self.page_size = 0
        self.reported_messages_total = 0
        self.estimate_pages = False
        self.calls: list[str] = []
        self.sent: list[bytes] = []
        self.drafts: list[bytes] = []

    def add(self, message: FakeMessage) -> None:
        self.messages[message.id] = message

    def add_history(self, record: HistoryRecord) -> HistoryRecord:
        new_id = str(int(self.profile_history_id) + 1)
        self.profile_history_id = new_id
        record.id = new_id
        self.records.append(record)
        return record

    def get_profile(self) -> Profile:
        self.calls.append("profile")
        if self.profile_rate_limits:
            self.profile_rate_limits -= 1
            raise RateLimited("slow down")
        total = self.reported_messages_total or len(self.messages)
        return Profile(self.email, self.profile_history_id, total)

    def list_labels(self) -> list[Label]:
        self.calls.append("labels.list")
        return list(self.labels)

    def get_label(self, label_id: str) -> Label:
        self.calls.append(f"labels.get:{label_id}")
        for label in self.labels:
            if label.id == label_id:
                count = sum(1 for message in self.messages.values() if label_id in message.label_ids)
                return Label(label.id, label.name, label.type, count)
        raise NotFound(label_id)

    def get_messages(
        self, message_ids: list[str], format: MessageFormat
    ) -> tuple[list[IncomingMessage], list[str]]:
        self.calls.append(f"messages.batch:{format.value}:{len(message_ids)}")
        found: list[IncomingMessage] = []
        missing: list[str] = []
        for message_id in message_ids:
            try:
                found.append(self.get_message(message_id, format))
            except NotFound:
                missing.append(message_id)
        return found, missing

    def list_history(self, start_history_id: str, page_token: str | None = None) -> HistoryPage:
        self.calls.append(f"history.list:{start_history_id}")
        if self.history_offline:
            raise Offline("temporary failure in name resolution")
        if start_history_id in self.expired_ids:
            raise HistoryExpired()
        newer = [record for record in self.records if int(record.id) > int(start_history_id)]
        newer.sort(key=lambda record: int(record.id))
        return HistoryPage(self.profile_history_id, None, newer)

    def list_messages(
        self,
        label_id: str | None = None,
        query: str | None = None,
        page_token: str | None = None,
    ) -> MessageListPage:
        self.calls.append(f"messages.list:label={label_id or ''}:q={query or ''}")
        chosen = [
            message
            for message in self.messages.values()
            if (label_id is None or label_id in message.label_ids) and (query is None or self._matches(message, query))
        ]
        chosen.sort(key=lambda message: message.internal_date_ms, reverse=True)
        offset = int(page_token or "0") if self.page_size else 0
        window = chosen[offset : offset + self.page_size] if self.page_size else chosen
        next_offset = offset + len(window)
        next_token = str(next_offset) if self.page_size and next_offset < len(chosen) else None
        estimate = len(window) if self.estimate_pages else len(chosen)
        return MessageListPage(
            [ListedMessage(message.id, message.thread_id) for message in window],
            next_token,
            estimate,
        )

    def get_message(self, message_id: str, format: MessageFormat) -> IncomingMessage:
        self.calls.append(f"messages.get:{message_id}:{format.value}")
        if format is MessageFormat.RAW and self.raw_quota_once:
            self.raw_quota_once -= 1
            raise RateLimited(
                "Quota exceeded for quota metric 'Total Query Cost' "
                "and limit 'Units per minute per user'"
            )
        message = self.messages.get(message_id)
        if message is None:
            raise NotFound(message_id)
        incoming = self._incoming(message)
        if format is MessageFormat.RAW:
            incoming.raw = message.raw()
        return incoming

    def modify_message(self, message_id: str, add: list[str], remove: list[str]) -> IncomingMessage:
        if self.modify_offline:
            self.calls.append(f"messages.modify:{message_id}:offline")
            raise Offline("offline")
        if message_id in self.conflict_once:
            self.conflict_once.remove(message_id)
            self.calls.append(f"messages.modify:{message_id}:conflict")
            raise Conflict(message_id)
        message = self.messages.get(message_id)
        if message is None:
            self.calls.append(f"messages.modify:{message_id}:missing")
            raise NotFound(message_id)
        message.label_ids = [label for label in message.label_ids if label not in remove]
        for label in add:
            if label not in message.label_ids:
                message.label_ids.append(label)
        self.calls.append(
            f"messages.modify:{message_id}:add={','.join(add)}:remove={','.join(remove)}"
        )
        if add or remove:
            record = self.add_history(
                HistoryRecord(
                    id="0",
                    labels_added=[
                        LabelChange(
                            HistoryMessage(message.id, message.thread_id, list(message.label_ids)),
                            list(add),
                        )
                    ]
                    if add
                    else [],
                    labels_removed=[
                        LabelChange(
                            HistoryMessage(message.id, message.thread_id, list(message.label_ids)),
                            list(remove),
                        )
                    ]
                    if remove
                    else [],
                )
            )
            message.history_id = record.id
        return self._incoming(message)

    def batch_modify(self, message_ids: list[str], add: list[str], remove: list[str]) -> None:
        if self.modify_offline:
            self.calls.append(f"messages.batchModify:{','.join(message_ids)}:offline")
            raise Offline("offline")
        missing = [message_id for message_id in message_ids if message_id not in self.messages]
        if missing:
            self.calls.append(f"messages.batchModify:missing={','.join(missing)}")
            raise NotFound(missing[0])
        added: list[LabelChange] = []
        removed: list[LabelChange] = []
        for message_id in message_ids:
            message = self.messages[message_id]
            message.label_ids = [label for label in message.label_ids if label not in remove]
            for label in add:
                if label not in message.label_ids:
                    message.label_ids.append(label)
            change = HistoryMessage(message.id, message.thread_id, list(message.label_ids))
            if add:
                added.append(LabelChange(change, list(add)))
            if remove:
                removed.append(LabelChange(change, list(remove)))
        self.calls.append(
            "messages.batchModify:"
            f"{','.join(message_ids)}:add={','.join(add)}:remove={','.join(remove)}"
        )
        if add or remove:
            record = self.add_history(
                HistoryRecord(id="0", labels_added=added, labels_removed=removed)
            )
            for message_id in message_ids:
                self.messages[message_id].history_id = record.id

    def send_message(self, raw: bytes, thread_id: str) -> IncomingMessage:
        self.sent.append(raw)
        return self._outgoing(raw, thread_id, f"sent-{len(self.sent)}", ["SENT"], f"messages.send:{thread_id}")

    def create_draft(self, raw: bytes, thread_id: str) -> IncomingMessage:
        self.drafts.append(raw)
        return self._outgoing(
            raw,
            thread_id,
            f"draft-{len(self.drafts)}",
            ["DRAFT"],
            f"drafts.create:{thread_id}",
        )

    def _outgoing(
        self,
        raw: bytes,
        thread_id: str,
        gmail_id: str,
        label_ids: list[str],
        call: str,
    ) -> IncomingMessage:
        self.calls.append(call)
        thread = thread_id or gmail_id
        from gmail_tui.textutil import parse_rfc822

        parsed = parse_rfc822(raw)
        # Mimic Gmail assigning a Message-ID when the client omitted one.
        mid = parsed.message_id_header.strip() or f"<{gmail_id}@mail.gmail.com>"
        stamped = raw
        if not parsed.message_id_header.strip():
            stamped = raw.replace(b"\r\n\r\n", f"\r\nMessage-ID: {mid}\r\n\r\n".encode(), 1)
            if stamped == raw:
                stamped = raw.replace(b"\n\n", f"\nMessage-ID: {mid}\n\n".encode(), 1)
            parsed = parse_rfc822(stamped)
        self.messages[gmail_id] = FakeMessage(
            id=gmail_id,
            thread_id=thread,
            label_ids=list(label_ids),
            subject=parsed.subject,
            from_addr=parsed.from_addr,
            to_addr=parsed.to_addr or "me@gmail.com",
            cc_addr=parsed.cc_addr,
            body=parsed.text,
            internal_date_ms=int(__import__("time").time() * 1000),
            snippet=parsed.text[:80],
            raw_bytes=stamped,
        )
        return IncomingMessage(
            gmail_id=gmail_id,
            thread_id=thread,
            label_ids=list(label_ids),
            subject=parsed.subject,
            from_addr=parsed.from_addr,
            to_addr=parsed.to_addr,
            cc_addr=parsed.cc_addr,
            message_id_header=parsed.message_id_header or mid,
            in_reply_to=parsed.in_reply_to,
            references_header=parsed.references,
        )

    def create_label(self, name: str) -> Label:
        self.calls.append(f"labels.create:{name}")
        for label in self.labels:
            if label.name.lower() == name.lower():
                raise Conflict(name)
        new_id = f"Label_{len(self.labels) + 1}"
        created = Label(new_id, name, "user")
        self.labels.append(created)
        return created

    def _incoming(self, message: FakeMessage) -> IncomingMessage:
        return IncomingMessage(
            gmail_id=message.id,
            thread_id=message.thread_id,
            label_ids=list(message.label_ids),
            history_id=message.history_id,
            internal_date_ms=message.internal_date_ms,
            snippet=message.snippet or message.body[:80],
            subject=message.subject,
            from_addr=message.from_addr,
            to_addr=message.to_addr,
            cc_addr=message.cc_addr,
            message_id_header=f"<{message.id}@example.com>",
            raw=None,
        )

    def _matches(self, message: FakeMessage, query: str) -> bool:
        text = " ".join(
            [message.subject, message.from_addr, message.to_addr, message.snippet, message.body]
        ).lower()
        unread: bool | None = None
        terms: list[str] = []
        for token in query.split():
            lowered = token.lower()
            if lowered == "is:unread":
                unread = True
            elif lowered == "is:read":
                unread = False
            elif lowered.startswith("from:"):
                if token.split(":", 1)[1].lower() not in message.from_addr.lower():
                    return False
            elif lowered.startswith("subject:"):
                if token.split(":", 1)[1].lower() not in message.subject.lower():
                    return False
            else:
                terms.append(lowered)
        if unread is True and "UNREAD" not in message.label_ids:
            return False
        if unread is False and "UNREAD" in message.label_ids:
            return False
        return all(term in text for term in terms)


def added(message_id: str, thread_id: str, labels: list[str]) -> HistoryRecord:
    return HistoryRecord(
        id="0",
        messages_added=[HistoryMessage(message_id, thread_id, list(labels))],
    )


def label_change(message_id: str, thread_id: str, labels: list[str], *, added_labels: bool) -> HistoryRecord:
    change = LabelChange(HistoryMessage(message_id, thread_id, list(labels)), list(labels))
    if added_labels:
        return HistoryRecord(id="0", labels_added=[change])
    return HistoryRecord(id="0", labels_removed=[change])

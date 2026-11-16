"""Gmail API types and the client interface the sync loops call."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class GmailError(Exception):
    """Base error for Gmail API calls."""


class HistoryExpired(GmailError):
    """The stored history id is too old. A full metadata rebuild is required."""


class NotFound(GmailError):
    """The message or thread is gone."""


class Conflict(GmailError):
    """The modify lost a race with another client."""


class RateLimited(GmailError):
    """Gmail returned 429. The caller should wait and retry."""


class Offline(GmailError):
    """The network is unreachable."""


class MessageFormat(Enum):
    METADATA = "metadata"
    RAW = "raw"


# Query costs from the Gmail API usage table. messages.get is 20, and 60
# when the format is raw. A user may spend 6,000 of these units per minute.
UNITS_PROFILE = 1
UNITS_LABELS = 1
UNITS_HISTORY = 2
UNITS_LIST = 5
UNITS_GET = 20
UNITS_GET_RAW = 60
UNITS_MODIFY = 5
UNITS_SEND = 100
UNITS_DRAFT = 10
QUOTA_PER_MINUTE = 6000
BATCH_LIMIT = 100


@dataclass
class Profile:
    email: str
    history_id: str
    messages_total: int = 0


@dataclass
class Label:
    id: str
    name: str
    type: str = ""
    messages_total: int = 0


@dataclass
class HistoryMessage:
    id: str
    thread_id: str
    label_ids: list[str] = field(default_factory=list)


@dataclass
class LabelChange:
    message: HistoryMessage
    label_ids: list[str] = field(default_factory=list)


@dataclass
class HistoryRecord:
    id: str
    messages_added: list[HistoryMessage] = field(default_factory=list)
    messages_deleted: list[HistoryMessage] = field(default_factory=list)
    labels_added: list[LabelChange] = field(default_factory=list)
    labels_removed: list[LabelChange] = field(default_factory=list)


@dataclass
class HistoryPage:
    history_id: str
    next_page_token: str | None
    records: list[HistoryRecord] = field(default_factory=list)


@dataclass
class ListedMessage:
    id: str
    thread_id: str


@dataclass
class MessageListPage:
    messages: list[ListedMessage] = field(default_factory=list)
    next_page_token: str | None = None
    result_size_estimate: int = 0


@dataclass
class IncomingMessage:
    gmail_id: str
    thread_id: str
    label_ids: list[str] = field(default_factory=list)
    history_id: str | None = None
    internal_date_ms: int = 0
    snippet: str = ""
    subject: str = ""
    from_addr: str = ""
    to_addr: str = ""
    cc_addr: str = ""
    message_id_header: str = ""
    in_reply_to: str = ""
    references_header: str = ""
    size_estimate: int | None = None
    raw: bytes | None = None
    has_attachment: bool | None = None
    # Set when the raw message was already parsed. None means the body text
    # still has to be read from ``raw``.
    body_text: str | None = None


class GmailClient(Protocol):
    """The subset of the Gmail API the sync loops use."""

    def get_profile(self) -> Profile:
        """Return the account address and the current mailbox history id."""

    def list_labels(self) -> list[Label]:
        """Return every label in the mailbox."""

    def get_label(self, label_id: str) -> Label:
        """Return one label, including how many messages wear it."""

    def get_messages(
        self, message_ids: list[str], format: MessageFormat
    ) -> tuple[list[IncomingMessage], list[str]]:
        """Fetch many messages in one round trip. Missing ids are the second list."""

    def list_history(self, start_history_id: str, page_token: str | None = None) -> HistoryPage:
        """Return changes strictly after ``start_history_id``."""

    def list_messages(
        self,
        label_id: str | None = None,
        query: str | None = None,
        page_token: str | None = None,
    ) -> MessageListPage:
        """List message ids, optionally restricted to a label or a Gmail query."""

    def get_message(self, message_id: str, format: MessageFormat) -> IncomingMessage:
        """Fetch one message as metadata or as raw RFC822."""

    def modify_message(self, message_id: str, add: list[str], remove: list[str]) -> IncomingMessage:
        """Add and remove label ids. This is an intent, not a full label snapshot."""

    def create_label(self, name: str) -> Label:
        """Create a user label and return the id Gmail assigned."""

    def send_message(self, raw: bytes, thread_id: str) -> IncomingMessage:
        """Send one RFC822 message, keeping it on ``thread_id`` when that is set."""

    def create_draft(self, raw: bytes, thread_id: str) -> IncomingMessage:
        """Store one RFC822 message as a draft, on ``thread_id`` when that is set."""


class NullGmail:
    """Client used when the caller has no token. Every call reports offline."""

    def get_profile(self) -> Profile:
        raise Offline("no Gmail token")

    def list_labels(self) -> list[Label]:
        raise Offline("no Gmail token")

    def get_label(self, label_id: str) -> Label:
        raise Offline("no Gmail token")

    def get_messages(
        self, message_ids: list[str], format: MessageFormat
    ) -> tuple[list[IncomingMessage], list[str]]:
        raise Offline("no Gmail token")

    def list_history(self, start_history_id: str, page_token: str | None = None) -> HistoryPage:
        raise Offline("no Gmail token")

    def list_messages(
        self,
        label_id: str | None = None,
        query: str | None = None,
        page_token: str | None = None,
    ) -> MessageListPage:
        raise Offline("no Gmail token")

    def get_message(self, message_id: str, format: MessageFormat) -> IncomingMessage:
        raise Offline("no Gmail token")

    def modify_message(self, message_id: str, add: list[str], remove: list[str]) -> IncomingMessage:
        raise Offline("no Gmail token")

    def create_label(self, name: str) -> Label:
        raise Offline("no Gmail token")

    def send_message(self, raw: bytes, thread_id: str) -> IncomingMessage:
        raise Offline("no Gmail token")

    def create_draft(self, raw: bytes, thread_id: str) -> IncomingMessage:
        raise Offline("no Gmail token")

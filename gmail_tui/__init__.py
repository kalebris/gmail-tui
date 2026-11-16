"""Online-first Gmail client core.

Gmail is the source of truth. A private SQLite database replicates labels,
threads, and headers. Message bodies live in a separate cache. Label changes
are applied locally, queued, and pushed when the network is up.
"""

from gmail_tui.gmail import (
    Conflict,
    GmailError,
    HistoryExpired,
    NotFound,
    Offline,
    RateLimited,
)
from gmail_tui.http_gmail import HttpGmail
from gmail_tui.store import Store, ThreadCursor, ThreadSummary
from gmail_tui.sync import Engine, OpenedMessage, SearchHit, SearchResponse, SyncReport

__all__ = [
    "Conflict",
    "Engine",
    "GmailError",
    "HistoryExpired",
    "HttpGmail",
    "NotFound",
    "Offline",
    "OpenedMessage",
    "RateLimited",
    "SearchHit",
    "SearchResponse",
    "Store",
    "SyncReport",
    "ThreadCursor",
    "ThreadSummary",
]

"""Gmail REST client.

The sync engine applies the quota budget. This module only speaks HTTP.
"""

from __future__ import annotations

import base64
import gzip
import http.client
import json
import socket
import threading
import time
import urllib.parse
from typing import Any

from gmail_tui import profile as trace
from gmail_tui.gmail import (
    Conflict,
    GmailError,
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
from gmail_tui.textutil import parse_rfc822

_BASE = "https://gmail.googleapis.com"
_HEADER_NAMES = ("From", "To", "Cc", "Subject", "Message-ID", "In-Reply-To", "References")


class HttpStatus(GmailError):
    def __init__(self, code: int, body: str) -> None:
        super().__init__(f"Gmail HTTP {code}: {body[:300]}")
        self.code = code
        self.body = body


class HttpGmail:
    def __init__(
        self,
        token: str,
        base: str = _BASE,
        timeout: float = 60.0,
        refresh=None,
    ) -> None:
        if not token:
            raise ValueError("Gmail access token is empty")
        self.token = token
        self.base = base.rstrip("/")
        self.timeout = timeout
        # Optional callable(force: bool) -> str | None. Used after sleep when
        # Google rejects the access token that was loaded at startup.
        self._refresh = refresh
        # One socket per host. Message downloads used to open a new TCP
        # session, and a new TLS handshake, for every request.
        self._connections: dict[tuple[str, str, int], http.client.HTTPConnection] = {}
        self._lock = threading.Lock()
        self._aborted = False

    def close(self) -> None:
        """Drop kept sockets. Resume after sleep needs a fresh TCP session."""
        with self._lock:
            keys = list(self._connections)
        for key in keys:
            self._drop(key)

    def abort(self) -> None:
        """Unblock an in-flight download and refuse further calls."""
        self._aborted = True
        self.close()

    def reauthorize(self, *, force: bool = False) -> bool:
        """Load a bearer token again. True when one is ready to use."""
        if self._refresh is None:
            return False
        token = self._refresh(force=force)
        if not token:
            return False
        self.token = token
        self.close()
        return True

    def get_profile(self) -> Profile:
        data = self._json("GET", "/gmail/v1/users/me/profile")
        return Profile(
            email=_str(data.get("emailAddress")),
            history_id=_str(data.get("historyId")),
            messages_total=_int(data.get("messagesTotal")),
        )

    def list_labels(self) -> list[Label]:
        data = self._json("GET", "/gmail/v1/users/me/labels")
        labels: list[Label] = []
        for item in data.get("labels") or []:
            labels.append(
                Label(
                    id=_str(item.get("id")),
                    name=_str(item.get("name")),
                    type=_str(item.get("type")),
                )
            )
        return labels

    def get_label(self, label_id: str) -> Label:
        try:
            data = self._json("GET", f"/gmail/v1/users/me/labels/{_quote(label_id)}")
        except HttpStatus as exc:
            if exc.code == 404:
                raise NotFound(label_id) from exc
            raise
        return Label(
            id=_str(data.get("id")) or label_id,
            name=_str(data.get("name")) or label_id,
            type=_str(data.get("type")),
            messages_total=_int(data.get("messagesTotal")),
        )

    def get_messages(
        self, message_ids: list[str], format: MessageFormat
    ) -> tuple[list[IncomingMessage], list[str]]:
        if not message_ids:
            return [], []
        if len(message_ids) == 1:
            try:
                return [self.get_message(message_ids[0], format)], []
            except NotFound:
                return [], [message_ids[0]]
        return self._batch_messages(message_ids, format)

    def list_history(self, start_history_id: str, page_token: str | None = None) -> HistoryPage:
        query: list[tuple[str, str]] = [
            ("startHistoryId", start_history_id),
            ("maxResults", "100"),
            ("historyTypes", "messageAdded"),
            ("historyTypes", "messageDeleted"),
            ("historyTypes", "labelAdded"),
            ("historyTypes", "labelRemoved"),
        ]
        if page_token:
            query.append(("pageToken", page_token))
        try:
            data = self._json("GET", "/gmail/v1/users/me/history", query)
        except HttpStatus as exc:
            if exc.code == 404:
                raise HistoryExpired(str(exc)) from exc
            raise
        return parse_history_page(data)

    def list_messages(
        self,
        label_id: str | None = None,
        query: str | None = None,
        page_token: str | None = None,
    ) -> MessageListPage:
        params: list[tuple[str, str]] = [("maxResults", "100")]
        if label_id:
            params.append(("labelIds", label_id))
        if query:
            params.append(("q", query))
        if page_token:
            params.append(("pageToken", page_token))
        data = self._json("GET", "/gmail/v1/users/me/messages", params)
        return parse_list_page(data)

    def get_message(self, message_id: str, format: MessageFormat) -> IncomingMessage:
        params: list[tuple[str, str]] = [("format", format.value)]
        if format is MessageFormat.METADATA:
            for name in _HEADER_NAMES:
                params.append(("metadataHeaders", name))
        try:
            data = self._json("GET", f"/gmail/v1/users/me/messages/{_quote(message_id)}", params)
        except HttpStatus as exc:
            if exc.code == 404:
                raise NotFound(message_id) from exc
            raise
        return parse_message(data)

    def modify_message(self, message_id: str, add: list[str], remove: list[str]) -> IncomingMessage:
        body = {"addLabelIds": list(add), "removeLabelIds": list(remove)}
        try:
            data = self._json(
                "POST",
                f"/gmail/v1/users/me/messages/{_quote(message_id)}/modify",
                body=body,
            )
        except HttpStatus as exc:
            if exc.code == 404:
                raise NotFound(message_id) from exc
            if exc.code in (409, 412):
                raise Conflict(str(exc)) from exc
            raise
        return parse_message(data)

    def batch_modify(self, message_ids: list[str], add: list[str], remove: list[str]) -> None:
        body = {
            "ids": list(message_ids),
            "addLabelIds": list(add),
            "removeLabelIds": list(remove),
        }
        self._json("POST", "/gmail/v1/users/me/messages/batchModify", body=body)

    def send_message(self, raw: bytes, thread_id: str) -> IncomingMessage:
        encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        body: dict[str, Any] = {"raw": encoded}
        if thread_id:
            body["threadId"] = thread_id
        data = self._json("POST", "/gmail/v1/users/me/messages/send", body=body)
        return parse_message(data)

    def create_draft(self, raw: bytes, thread_id: str) -> IncomingMessage:
        encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        message: dict[str, Any] = {"raw": encoded}
        if thread_id:
            message["threadId"] = thread_id
        data = self._json("POST", "/gmail/v1/users/me/drafts", body={"message": message})
        inner = data.get("message")
        if not isinstance(inner, dict):
            inner = data
        return parse_message(inner)

    def create_label(self, name: str) -> Label:
        body = {
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
        try:
            data = self._json("POST", "/gmail/v1/users/me/labels", body=body)
        except HttpStatus as exc:
            if exc.code == 409:
                raise Conflict(name) from exc
            raise
        return Label(
            id=_str(data.get("id")),
            name=_str(data.get("name")) or name,
            type=_str(data.get("type")) or "user",
        )

    def _json(
        self,
        method: str,
        path: str,
        query: list[tuple[str, str]] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self.base + path
        if query:
            url = url + "?" + urllib.parse.urlencode(query)
        payload = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        status, _content_type, raw = self._request(method, url, headers, payload)
        if status >= 400:
            detail = raw.decode("utf-8", errors="replace")
            if is_rate_limited(status, detail):
                raise RateLimited(detail)
            if status in (409, 412):
                raise Conflict(detail)
            raise HttpStatus(status, detail)
        if not raw:
            return {}
        if trace.recording():
            started = time.perf_counter()
            parsed = json.loads(raw)
            trace.add("json", time.perf_counter() - started)
        else:
            parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise GmailError("Gmail response was not an object")
        return parsed

    def _request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: bytes | None,
    ) -> tuple[int, str, bytes]:
        current = url
        current_method = method
        current_payload = payload
        current_headers = headers
        for _redirect in range(4):
            status, content_type, raw, location = self._exchange(
                current_method, current, current_headers, current_payload
            )
            if status not in (301, 302, 303, 307, 308) or not location:
                return status, content_type, raw
            nxt = urllib.parse.urljoin(current, location)
            if urllib.parse.urlsplit(nxt).hostname != urllib.parse.urlsplit(current).hostname:
                raise GmailError("Gmail redirected to another host")
            current = nxt
            if status == 303:
                current_method = "GET"
                current_payload = None
                current_headers = {
                    key: value
                    for key, value in current_headers.items()
                    if key.lower() != "content-type"
                }
        raise GmailError("Gmail redirected too many times")

    def _exchange(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        payload: bytes | None,
    ) -> tuple[int, str, bytes, str]:
        if self._aborted:
            raise Offline("stopped")
        scheme, host, port, path = _endpoint(url)
        key = (scheme, host, port)
        for attempt in (1, 2):
            if self._aborted:
                raise Offline("stopped")
            reused = key in self._connections
            conn = self._open(scheme, host, port)
            try:
                conn.request(method, path, body=payload, headers=headers)
                response = conn.getresponse()
                raw = _read_body(response)
                status = response.status
                content_type = response.getheader("Content-Type") or ""
                location = response.getheader("Location") or ""
                closing = response.will_close
            except (TimeoutError, OSError, http.client.HTTPException) as exc:
                self._drop(key)
                if reused and attempt == 1:
                    continue
                raise Offline(str(exc)) from exc
            if closing:
                self._drop(key)
            return status, content_type, raw, location
        raise Offline("connection failed")

    def _open(self, scheme: str, host: str, port: int) -> http.client.HTTPConnection:
        key = (scheme, host, port)
        with self._lock:
            if self._aborted:
                raise Offline("stopped")
            existing = self._connections.get(key)
            if existing is not None:
                return existing
        if scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                host, port, timeout=self.timeout
            )
        elif scheme == "http":
            conn = http.client.HTTPConnection(host, port, timeout=self.timeout)
        else:
            raise Offline(f"unsupported URL scheme {scheme}")
        with self._lock:
            if self._aborted:
                _close_conn(conn)
                raise Offline("stopped")
            current = self._connections.get(key)
            if current is not None:
                _close_conn(conn)
                return current
            self._connections[key] = conn
            return conn

    def _drop(self, key: tuple[str, str, int]) -> None:
        with self._lock:
            conn = self._connections.pop(key, None)
        if conn is not None:
            _close_conn(conn)

    def _batch_messages(
        self, message_ids: list[str], format: MessageFormat
    ) -> tuple[list[IncomingMessage], list[str]]:
        boundary = "gmail_tui_batch"
        chunks: list[str] = []
        for index, message_id in enumerate(message_ids):
            query = [("format", format.value)]
            if format is MessageFormat.METADATA:
                for name in _HEADER_NAMES:
                    query.append(("metadataHeaders", name))
            path = "/gmail/v1/users/me/messages/" + _quote(message_id)
            path = path + "?" + urllib.parse.urlencode(query)
            chunks.append(
                f"--{boundary}\r\n"
                "Content-Type: application/http\r\n"
                f"Content-ID: <{index}>\r\n"
                "\r\n"
                f"GET {path} HTTP/1.1\r\n"
                "\r\n"
            )
        payload = ("".join(chunks) + f"--{boundary}--\r\n").encode()
        status, content_type, raw = self._request(
            "POST",
            "https://www.googleapis.com/batch/gmail/v1",
            {
                "Authorization": f"Bearer {self.token}",
                "Accept": "multipart/mixed",
                "Accept-Encoding": "gzip",
                "Content-Type": f"multipart/mixed; boundary={boundary}",
            },
            payload,
        )
        if status >= 400:
            detail = raw.decode("utf-8", errors="replace")
            if is_rate_limited(status, detail):
                raise RateLimited(detail)
            raise HttpStatus(status, detail)
        parts = parse_batch_response(content_type, raw)
        if len(parts) != len(message_ids):
            raise GmailError("Gmail batch response did not include every message")
        found: list[IncomingMessage] = []
        missing: list[str] = []
        for message_id, (status, data) in zip(message_ids, parts, strict=True):
            if status == 404:
                missing.append(message_id)
                continue
            detail = _error_detail(data)
            if is_rate_limited(status, detail):
                raise RateLimited(detail or message_id)
            if is_precondition_failed(status, detail):
                # Same intermittent fault as a lone messages.get; skip this id.
                missing.append(message_id)
                continue
            if status >= 400 or data is None:
                raise GmailError(f"Gmail batch failed for {message_id} ({status})")
            if not data.get("id"):
                data["id"] = message_id
            found.append(parse_message(data))
        return found, missing


def _endpoint(url: str) -> tuple[str, str, int, str]:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if not parsed.scheme or not host:
        raise Offline(f"unsupported URL {url}")
    if parsed.port is not None:
        port = parsed.port
    elif parsed.scheme == "https":
        port = 443
    else:
        port = 80
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return parsed.scheme, host, port, path


def _close_conn(conn: http.client.HTTPConnection) -> None:
    """Shut the socket down so a blocked read in another thread returns."""
    sock = getattr(conn, "sock", None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    try:
        conn.close()
    except OSError:
        pass


def _read_body(response: http.client.HTTPResponse) -> bytes:
    if trace.recording():
        started = time.perf_counter()
        raw = response.read()
        trace.add("read", time.perf_counter() - started, wire=len(raw))
    else:
        raw = response.read()
    encoding = (response.getheader("Content-Encoding") or "").lower()
    if encoding in ("gzip", "x-gzip"):
        try:
            return gzip.decompress(raw)
        except gzip.BadGzipFile as exc:
            raise GmailError("Gmail gzip response could not be read") from exc
    return raw


def is_rate_limited(code: int, detail: str) -> bool:
    if code == 429:
        return True
    text = detail.lower()
    return code == 403 and ("per minute" in text or "userratelimitexceeded" in text)


def is_precondition_failed(code: int, detail: str) -> bool:
    """True for Gmail's intermittent ``FAILED_PRECONDITION`` responses.

    Google often returns HTTP 400 with only ``Precondition check failed.``
    Retries usually succeed; treating it as fatal parked the sync loop.
    """
    if code != 400:
        return False
    text = detail.lower().replace("_", " ").replace(".", " ")
    return "precondition check failed" in text or "failed precondition" in text


def _error_detail(data: dict[str, Any] | None) -> str:
    if not isinstance(data, dict):
        return ""
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or "")
    return ""


def parse_batch_response(content_type: str, payload: bytes) -> list[tuple[int, dict[str, Any] | None]]:
    boundary = _boundary(content_type)
    if not boundary:
        raise GmailError("Gmail batch response had no boundary")
    marker = b"--" + boundary.encode("utf-8")
    parsed: list[tuple[int, dict[str, Any] | None]] = []
    for part in payload.split(marker):
        piece = part.strip(b"\r\n")
        if not piece or piece == b"--":
            continue
        status, data = _batch_part(piece)
        if status:
            parsed.append((status, data))
    return parsed


def _boundary(content_type: str) -> str:
    for piece in content_type.split(";"):
        item = piece.strip()
        if item.lower().startswith("boundary="):
            value = item.split("=", 1)[1].strip().strip('"')
            return value
    return ""


def _batch_part(part: bytes) -> tuple[int, dict[str, Any] | None]:
    start = part.find(b"HTTP/1.1")
    if start < 0:
        return 0, None
    rest = part[start:]
    header, _, body = rest.partition(b"\r\n\r\n")
    if not body:
        header, _, body = rest.partition(b"\n\n")
    status_line = header.splitlines()[0].decode("utf-8", errors="replace")
    try:
        status = int(status_line.split()[1])
    except (IndexError, ValueError):
        return 0, None
    body = body.strip()
    if not body:
        return status, None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return status, None
    return status, data if isinstance(data, dict) else None


def parse_history_page(data: dict[str, Any]) -> HistoryPage:
    records: list[HistoryRecord] = []
    for item in data.get("history") or []:
        records.append(
            HistoryRecord(
                id=_str(item.get("id")),
                messages_added=[_history_message(entry.get("message") or {}) for entry in item.get("messagesAdded") or []],
                messages_deleted=[
                    _history_message(entry.get("message") or {}) for entry in item.get("messagesDeleted") or []
                ],
                labels_added=[_label_change(entry) for entry in item.get("labelsAdded") or []],
                labels_removed=[_label_change(entry) for entry in item.get("labelsRemoved") or []],
            )
        )
    return HistoryPage(
        history_id=_str(data.get("historyId")),
        next_page_token=_optional_str(data.get("nextPageToken")),
        records=records,
    )


def parse_list_page(data: dict[str, Any]) -> MessageListPage:
    messages = [
        ListedMessage(id=_str(item.get("id")), thread_id=_str(item.get("threadId")))
        for item in data.get("messages") or []
        if item.get("id")
    ]
    return MessageListPage(
        messages=messages,
        next_page_token=_optional_str(data.get("nextPageToken")),
        result_size_estimate=_int(data.get("resultSizeEstimate")),
    )


def parse_message(data: dict[str, Any]) -> IncomingMessage:
    headers = _headers(data.get("payload") or {})
    raw = _decode_raw(data.get("raw"))
    parsed = None
    if raw:
        if trace.recording():
            started = time.perf_counter()
            parsed = parse_rfc822(raw)
            trace.add("parse", time.perf_counter() - started)
        else:
            parsed = parse_rfc822(raw)
    if parsed is not None and not headers.get("subject"):
        headers = {
            "subject": parsed.subject,
            "from": parsed.from_addr,
            "to": parsed.to_addr,
            "cc": parsed.cc_addr,
            "message_id": parsed.message_id_header,
            "in_reply_to": parsed.in_reply_to,
            "references": parsed.references,
        }
    elif parsed is not None:
        headers.setdefault("in_reply_to", parsed.in_reply_to)
        headers.setdefault("references", parsed.references)
        headers.setdefault("message_id", parsed.message_id_header)
    size = data.get("sizeEstimate")
    has_attachment: bool | None = None
    if data.get("payload") or raw:
        has_attachment = _payload_has_attachment(data.get("payload") or {})
        if parsed is not None:
            has_attachment = has_attachment or parsed.has_attachment
    return IncomingMessage(
        gmail_id=_str(data.get("id")),
        thread_id=_str(data.get("threadId")) or _str(data.get("id")),
        label_ids=[_str(label) for label in data.get("labelIds") or []],
        history_id=_optional_str(data.get("historyId")),
        internal_date_ms=_int(data.get("internalDate")),
        snippet=_str(data.get("snippet")),
        subject=headers.get("subject", ""),
        from_addr=headers.get("from", ""),
        to_addr=headers.get("to", ""),
        cc_addr=headers.get("cc", ""),
        message_id_header=headers.get("message_id", ""),
        in_reply_to=headers.get("in_reply_to", ""),
        references_header=headers.get("references", ""),
        size_estimate=int(size) if isinstance(size, int) else None,
        raw=raw,
        has_attachment=has_attachment,
        body_text=None if parsed is None else parsed.text,
    )


def _payload_has_attachment(payload: dict[str, Any]) -> bool:
    if payload.get("filename"):
        return True
    body = payload.get("body") or {}
    if isinstance(body, dict) and body.get("attachmentId"):
        return True
    return any(_payload_has_attachment(part) for part in payload.get("parts") or [])


def _history_message(data: dict[str, Any]) -> HistoryMessage:
    return HistoryMessage(
        id=_str(data.get("id")),
        thread_id=_str(data.get("threadId")) or _str(data.get("id")),
        label_ids=[_str(label) for label in data.get("labelIds") or []],
    )


def _label_change(data: dict[str, Any]) -> LabelChange:
    return LabelChange(
        message=_history_message(data.get("message") or {}),
        label_ids=[_str(label) for label in data.get("labelIds") or []],
    )


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for header in payload.get("headers") or []:
        name = _str(header.get("name")).lower()
        if name == "message-id":
            found["message_id"] = _str(header.get("value"))
        elif name == "in-reply-to":
            found["in_reply_to"] = _str(header.get("value"))
        elif name == "references":
            found["references"] = _str(header.get("value"))
        elif name in ("subject", "from", "to", "cc"):
            found[name] = _str(header.get("value"))
    return found


def _decode_raw(value: Any) -> bytes | None:
    if not value or not isinstance(value, str):
        return None
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _optional_str(value: Any) -> str | None:
    text = _str(value)
    return text or None


def _int(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0

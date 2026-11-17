"""Extract headers and searchable text from a raw RFC822 message."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.parser import BytesHeaderParser, BytesParser
from html.parser import HTMLParser
from pathlib import Path

from gmail_tui.links import present_links

_TAG = re.compile(r"(?s)<[^>]+>")
_BLOCK = re.compile(r"(?is)<(script|style)\b[^>]*>.*?</\1>")
_SPACE = re.compile(r"[ \t\f\v]+")
_SKIP_TAGS = {"script", "style", "head"}
_BREAK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "header",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "blockquote",
    "ul",
    "ol",
    "li",
    "table",
    "tr",
    "pre",
    "br",
    "hr",
}
_BLANK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"}


@dataclass(frozen=True)
class Attachment:
    """One downloadable part. ``index`` is its place in the saved message."""

    index: int
    filename: str
    content_type: str
    size: int


@dataclass(frozen=True)
class MimePart:
    """One readable body part. ``index`` is its place among body parts.

    ``text`` is what the reader shows (HTML already converted). ``source`` is
    the original part body, used when opening HTML in a browser.
    """

    index: int
    content_type: str
    size: int
    text: str
    source: str = ""


@dataclass
class ParsedMail:
    subject: str = ""
    from_addr: str = ""
    to_addr: str = ""
    cc_addr: str = ""
    message_id_header: str = ""
    in_reply_to: str = ""
    references: str = ""
    text: str = ""
    has_attachment: bool = False
    attachments: tuple[Attachment, ...] = ()
    mime_parts: tuple[MimePart, ...] = ()


# Below this, a multipart message is usually large because of an attachment.
# The search index only keeps the text, so those bodies are not parsed.
_LARGE_MESSAGE = 48_000
_FILENAME_PARAM = re.compile(r"(?:^|;)\s*filename\s*\*?=", re.IGNORECASE)
_NAME_PARAM = re.compile(r"(?:^|;)\s*name\s*\*?=", re.IGNORECASE)
_HEADER_PARAM = re.compile(
    r"""(?P<name>[A-Za-z0-9_*\-]+)\s*=\s*(?:"(?P<quoted>[^"]*)"|(?P<plain>[^;\s]+))"""
)


def parse_rfc822(raw: bytes) -> ParsedMail:
    if not raw:
        return ParsedMail()
    if len(raw) >= _LARGE_MESSAGE:
        prepared = _without_heavy_parts(raw)
        if prepared is not None:
            shrunk, sizes = prepared
            message = _bytes_message(shrunk)
            if message is not None:
                parsed = _parsed_mail(message)
                if len(parsed.attachments) == len(sizes):
                    parsed.attachments = tuple(
                        Attachment(
                            item.index,
                            item.filename,
                            item.content_type,
                            item.size if size is None else size,
                        )
                        for item, size in zip(parsed.attachments, sizes)
                    )
                    return parsed
    message = _bytes_message(raw)
    if message is None:
        return ParsedMail(text=raw.decode("utf-8", errors="replace"))
    return _parsed_mail(message)


def header_fields(raw: bytes) -> list[tuple[str, str]]:
    """Every header of ``raw`` in file order, decoded and unfolded for display."""
    try:
        message = BytesHeaderParser(policy=policy.default).parsebytes(raw)
    except Exception:
        return []
    fields: list[tuple[str, str]] = []
    for name, value in message.raw_items():
        try:
            shown = str(policy.default.header_fetch_parse(name, value))
        except Exception:
            shown = str(value)
        # 8-bit header bytes arrive as surrogate escapes, which curses cannot draw.
        shown = shown.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
        fields.append((name, " ".join(shown.split())))
    return fields


def _bytes_message(raw: bytes) -> EmailMessage | None:
    try:
        return BytesParser(policy=policy.default).parsebytes(raw)
    except Exception:
        return None


def _parsed_mail(message: EmailMessage) -> ParsedMail:
    mime_parts = tuple(_mime_parts(message))
    # Prefer real plain text. Use HTML only when that field is missing or blank.
    text = "\n".join(
        part.text for part in mime_parts if part.content_type == "text/plain" and part.text.strip()
    )
    if not text.strip():
        text = "\n".join(
            part.text for part in mime_parts if part.content_type == "text/html" and part.text.strip()
        )
    attachments = tuple(_attachments(message))
    return ParsedMail(
        subject=_header(message, "Subject"),
        from_addr=_header(message, "From"),
        to_addr=_header(message, "To"),
        cc_addr=_header(message, "Cc"),
        message_id_header=_header(message, "Message-ID"),
        in_reply_to=_header(message, "In-Reply-To"),
        references=_header(message, "References"),
        text=text,
        has_attachment=bool(attachments),
        attachments=attachments,
        mime_parts=mime_parts,
    )


def _without_heavy_parts(raw: bytes) -> tuple[bytes, list[int | None]] | None:
    """Drop non-text bodies. Sizes line up with the attachments the parser will list.

    ``None`` in that list means the parser still saw the body and its own size stands.
    The attachment bytes stay in the original buffer; only the text is copied out.
    """
    rewritten = _rewrite_entity(raw, 0, len(raw), 0)
    if rewritten is None:
        return None
    new_raw, sizes, dropped = rewritten
    if not dropped:
        return None
    return new_raw, sizes


def _rewrite_entity(
    buf: bytes, start: int, end: int, depth: int
) -> tuple[bytes, list[int | None], bool] | None:
    if depth > 8:
        return None
    split = _header_split(buf, start, end)
    if split is None:
        return b"", [], False
    head_end, body_start = split
    ctype = _header_map(buf[start:head_end]).get("content-type", "")
    if not _media_type(ctype).startswith("multipart/"):
        return b"", [], False
    boundary = _header_param(ctype, "boundary")
    if not boundary:
        return None
    try:
        token = b"--" + boundary.encode("ascii")
    except UnicodeEncodeError:
        return None
    marks = _boundary_marks(buf, body_start, end, token)
    if not marks:
        return None
    sizes: list[int | None] = []
    dropped = False
    pieces = [buf[start:body_start], buf[body_start : marks[0][0]]]
    for index, (mark_start, mark_end, closing) in enumerate(marks):
        pieces.append(buf[mark_start:mark_end])
        if closing:
            pieces.append(buf[mark_end:end])
            break
        next_start = marks[index + 1][0] if index + 1 < len(marks) else end
        rewritten = _rewrite_part(buf, mark_end, next_start, depth)
        if rewritten is None:
            return None
        new_part, extra, part_dropped = rewritten
        sizes.extend(extra)
        dropped = dropped or part_dropped
        pieces.append(new_part)
    if not dropped:
        return b"", [], False
    return b"".join(pieces), sizes, True


def _rewrite_part(
    buf: bytes, start: int, end: int, depth: int
) -> tuple[bytes, list[int | None], bool] | None:
    part_end = end
    suffix = 0
    if end - start >= 2 and buf[end - 2 : end] == b"\r\n":
        suffix = 2
        part_end = end - 2
    elif end > start and buf[end - 1] == 10:
        suffix = 1
        part_end = end - 1
    split = _header_split(buf, start, part_end)
    if split is None:
        return buf[start:end], [], False
    head_end, body_start = split
    headers = _header_map(buf[start:head_end])
    media = _media_type(headers.get("content-type", "text/plain"))
    if media.startswith("multipart/"):
        rewritten = _rewrite_entity(buf, start, end, depth + 1)
        if rewritten is None:
            return None
        new_raw, sizes, dropped = rewritten
        if not dropped:
            return buf[start:end], sizes, False
        return new_raw, sizes, True
    if media == "message/rfc822":
        sizes: list[int | None] = [None] if _listed(headers) else []
        inner = _rewrite_entity(buf, body_start, part_end, depth + 1)
        if inner is None or not inner[2]:
            return buf[start:end], sizes, False
        new_body, inner_sizes, _dropped = inner
        tail = buf[end - suffix : end] if suffix else b""
        return buf[start:body_start] + new_body + tail, sizes + inner_sizes, True
    if media.startswith("text/"):
        return buf[start:end], ([None] if _listed(headers) else []), False
    sizes = [_body_size_at(headers, buf, body_start, part_end)] if _listed(headers) else []
    tail = buf[end - suffix : end] if suffix else b""
    return buf[start:body_start] + tail, sizes, True


def _boundary_marks(
    buf: bytes, start: int, end: int, token: bytes
) -> list[tuple[int, int, bool]]:
    """Delimiter lines: ``--boundary`` or the closing ``--boundary--``."""
    marks: list[tuple[int, int, bool]] = []
    cursor = start
    while cursor < end:
        found = buf.find(token, cursor, end)
        if found < 0:
            break
        if found > start and buf[found - 1] not in (10, 13):
            cursor = found + 1
            continue
        pos = found + len(token)
        closing = buf.startswith(b"--", pos)
        if closing:
            pos += 2
        line_end = buf.find(b"\n", pos, end)
        if line_end < 0:
            if end - pos > 100 or buf[pos:end].strip(b"\r\n\t ") != b"":
                cursor = found + 1
                continue
            marks.append((found, end, closing))
            break
        if buf[pos:line_end].strip(b"\r\n\t ") != b"":
            cursor = found + 1
            continue
        marks.append((found, line_end + 1, closing))
        cursor = line_end + 1
    return marks


def _header_split(buf: bytes, start: int, end: int) -> tuple[int, int] | None:
    """Return the end of the headers and the start of the body, within ``buf``."""
    for sep in (b"\r\n\r\n", b"\n\n"):
        index = buf.find(sep, start, end)
        if index >= 0:
            return index, index + len(sep)
    return None


def _header_map(head: bytes) -> dict[str, str]:
    text = head.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    folded: list[bytearray] = []
    for line in text.split(b"\n"):
        if line.startswith((b" ", b"\t")) and folded:
            folded[-1].extend(b" ")
            folded[-1].extend(line.strip())
        else:
            folded.append(bytearray(line))
    found: dict[str, str] = {}
    for line in folded:
        if b":" not in line:
            continue
        name, value = bytes(line).split(b":", 1)
        key = name.decode("ascii", "ignore").lower()
        if key not in found:
            found[key] = value.strip().decode("utf-8", "replace")
    return found


def _media_type(value: str) -> str:
    return value.split(";", 1)[0].strip().lower()


def _header_param(value: str, name: str) -> str:
    _media, _sep, rest = value.partition(";")
    wanted = name.lower()
    for match in _HEADER_PARAM.finditer(rest):
        if match.group("name").lower() == wanted:
            quoted = match.group("quoted")
            return quoted if quoted is not None else match.group("plain")
    return ""


def _listed(headers: dict[str, str]) -> bool:
    """True when the email parser would put this part in the attachment list."""
    if _FILENAME_PARAM.search(headers.get("content-disposition", "")):
        return True
    if _NAME_PARAM.search(headers.get("content-type", "")):
        return True
    disposition = _media_type(headers.get("content-disposition", ""))
    return disposition == "attachment"


def _body_size_at(headers: dict[str, str], buf: bytes, start: int, end: int) -> int:
    encoding = headers.get("content-transfer-encoding", "").lower()
    if "base64" in encoding:
        return _base64_size_at(buf, start, end)
    if "quoted-printable" in encoding:
        return len(_decode_quoted(buf[start:end]))
    return max(0, end - start)


def _base64_size_at(buf: bytes, start: int, end: int) -> int:
    """Decoded length of a base64 span. Wrapped lines are measured without a copy."""
    while end > start and buf[end - 1] in (9, 10, 13, 32):
        end -= 1
    if end <= start:
        return 0
    pad = 0
    if buf[end - 1] == 61:
        pad = 2 if end - start > 1 and buf[end - 2] == 61 else 1
    newline = buf.find(b"\n", start, min(end, start + 120))
    if newline < 0:
        return max(0, (end - start) * 3 // 4 - pad)
    crlf = newline > start and buf[newline - 1] == 13
    width = newline - start - (1 if crlf else 0)
    stride = width + (2 if crlf else 1)
    first = buf[start:newline]
    if width <= 0 or b" " in first or b"\t" in first:
        return _base64_size_scan(buf, start, end, pad)
    full, remainder = divmod(end - start, stride)
    if remainder > width:
        return _base64_size_scan(buf, start, end, pad)
    if full >= 2:
        mid = start + (full // 2) * stride
        if mid + stride > end or buf[mid + stride - 1] != 10:
            return _base64_size_scan(buf, start, end, pad)
        if crlf and buf[mid + stride - 2] != 13:
            return _base64_size_scan(buf, start, end, pad)
    return max(0, (full * width + remainder) * 3 // 4 - pad)


def _base64_size_scan(buf: bytes, start: int, end: int, pad: int) -> int:
    length = end - start
    length -= buf.count(b"\n", start, end)
    length -= buf.count(b"\r", start, end)
    length -= buf.count(b" ", start, end)
    length -= buf.count(b"\t", start, end)
    return max(0, length * 3 // 4 - pad)


def html_to_text(value: str) -> str:
    """Turn an HTML body into short lines: paragraphs, lists, and links."""
    parser = _HTMLText()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        without_blocks = _BLOCK.sub(" ", value)
        without_tags = _TAG.sub(" ", without_blocks)
        return present_links(_SPACE.sub(" ", without_tags).strip())
    return present_links(parser.text())


def safe_filename(name: str) -> str:
    cleaned = Path(name).name.replace("\x00", "").strip()
    if cleaned in {"", ".", ".."}:
        return "attachment"
    return cleaned


def reserve_download(folder: Path, filename: str) -> Path:
    """Create an empty file under ``folder`` without replacing an existing name."""
    folder.mkdir(parents=True, exist_ok=True)
    name = safe_filename(filename)
    stem = Path(name).stem
    suffix = Path(name).suffix
    for number in range(1000):
        candidate = folder / (name if number == 0 else f"{stem}-{number}{suffix}")
        try:
            descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(descriptor)
        return candidate
    raise FileExistsError(name)


def read_attachment(raw: bytes, index: int) -> tuple[str, bytes] | None:
    """Return the filename and bytes of the 1-based attachment ``index``."""
    if not raw or index < 1:
        return None
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception:
        return None
    for number, part, filename in _attachment_parts(message):
        if number != index:
            continue
        payload = part.get_payload(decode=True)
        data = bytes(payload) if isinstance(payload, (bytes, bytearray)) else b""
        return safe_filename(filename or f"attachment-{number}"), data
    return None


def _header(message: EmailMessage, name: str) -> str:
    value = message.get(name)
    if value is None:
        return ""
    return str(value)


def _attachment_parts(message: EmailMessage):
    number = 0
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = (part.get_content_disposition() or "").lower()
        if not filename and disposition != "attachment":
            continue
        number += 1
        if not filename:
            subtype = part.get_content_subtype() or "bin"
            filename = f"attachment-{number}.{subtype}"
        yield number, part, str(filename)


def _attachments(message: EmailMessage) -> list[Attachment]:
    found: list[Attachment] = []
    for number, part, filename in _attachment_parts(message):
        found.append(
            Attachment(
                index=number,
                filename=filename,
                content_type=part.get_content_type(),
                size=_part_size(part),
            )
        )
    return found


def _part_size(part: EmailMessage) -> int:
    """Decoded size without building a second copy of the file."""
    payload = part.get_payload(decode=False)
    if isinstance(payload, str):
        data = payload.encode("ascii", errors="ignore")
    elif isinstance(payload, (bytes, bytearray)):
        data = bytes(payload)
    else:
        return 0
    encoding = (part.get("Content-Transfer-Encoding") or "").lower()
    if "base64" in encoding:
        return _base64_size(data)
    if "quoted-printable" in encoding:
        return len(_decode_quoted(data))
    if isinstance(payload, str):
        return len(payload.encode(_codec_name(part.get_content_charset()), errors="replace"))
    return len(data)


def _base64_size(data: bytes) -> int:
    end = data.rstrip(b"\r\n\t ")
    pad = 2 if end.endswith(b"==") else 1 if end.endswith(b"=") else 0
    length = len(data) - data.count(b"\n") - data.count(b"\r") - data.count(b" ") - data.count(b"\t")
    return max(0, length * 3 // 4 - pad)


def _decode_quoted(data: bytes) -> bytes:
    import binascii

    try:
        return binascii.a2b_qp(data)
    except binascii.Error:
        return data


def _mime_parts(message: EmailMessage) -> list[MimePart]:
    found: list[MimePart] = []
    _collect_mime(message, found)
    return found


def _collect_mime(message: EmailMessage, found: list[MimePart]) -> None:
    if message.is_multipart():
        for part in message.iter_parts():
            _collect_mime(part, found)
        return
    if (message.get_content_disposition() or "").lower() == "attachment":
        return
    content_type = message.get_content_type()
    if content_type not in ("text/plain", "text/html"):
        return
    payload = _part_text(message)
    if payload is None:
        return
    text = payload if content_type == "text/plain" else html_to_text(payload)
    found.append(
        MimePart(
            index=len(found) + 1,
            content_type=content_type,
            size=len(payload.encode("utf-8", errors="replace")),
            text=text,
            source=payload,
        )
    )


def preferred_mime_index(parts: tuple[MimePart, ...] | list[MimePart]) -> int:
    """Index of the part the reader should open first."""
    if not parts:
        return 0
    for index, part in enumerate(parts):
        if part.content_type == "text/plain" and part.text.strip():
            return index
    for index, part in enumerate(parts):
        if part.content_type == "text/html" and part.text.strip():
            return index
    return 0


def _part_text(message: EmailMessage) -> str | None:
    """Decode a text part. Unknown charsets still yield a readable string."""
    try:
        payload = message.get_content()
    except Exception:
        payload = None
    if isinstance(payload, str):
        return payload
    raw = message.get_payload(decode=True)
    if not isinstance(raw, (bytes, bytearray)):
        return None
    return bytes(raw).decode(_codec_name(message.get_content_charset()), errors="replace")


def _codec_name(value: str | None) -> str:
    """Return a usable codec. Broken MIME often glues the next header onto charset."""
    raw = (value or "utf-8").strip().strip("\"'")
    if not raw:
        return "utf-8"
    # charset=utf-8Content-Type:… — the newline before the next header was lost.
    for marker in ("content-", "Content-", "CONTENT-"):
        cut = raw.find(marker)
        if cut > 0:
            raw = raw[:cut]
            break
    cleaned: list[str] = []
    for char in raw.strip().rstrip(";"):
        if char.isalnum() or char in {"-", "_", "."}:
            cleaned.append(char)
        else:
            break
    name = "".join(cleaned) or "utf-8"
    try:
        "".encode(name)
    except LookupError:
        return "utf-8"
    return name


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.buf: list[str] = []
        self.skip = 0
        self.pre = 0
        self.cell = 0
        self.href: str | None = None
        # Text captured since the open ``<a>``. Joining the whole buffer on
        # every link or break made a long HTML letter use a full CPU core.
        self._link_parts: list[str] | None = None
        self._trailing = 0

    def _append(self, text: str) -> None:
        if not text:
            return
        self.buf.append(text)
        if self._link_parts is not None:
            self._link_parts.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in _BREAK_TAGS:
            self._break(blank=tag in _BLANK_TAGS)
        if tag == "li":
            self._append("- ")
        if tag == "tr":
            self.cell = 0
        if tag in {"td", "th"}:
            if self.cell:
                self._append(" | ")
            self.cell += 1
        if tag == "pre":
            self.pre += 1
        if tag == "a":
            self.href = dict(attrs).get("href")
            self._link_parts = []
        if tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self._append(alt)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "a" and self.href:
            href = self.href.strip()
            shown = "".join(self._link_parts or ())
            self._link_parts = None
            self.href = None
            if href and href not in shown:
                if href.lower().startswith(("http://", "https://", "mailto:")):
                    self._append(f" link {href}")
                else:
                    self._append(f" ({href})")
        if tag == "pre":
            self._flush()
            self.pre = max(0, self.pre - 1)
        if tag in _BREAK_TAGS:
            self._break(blank=tag in _BLANK_TAGS)

    def handle_data(self, data: str) -> None:
        if self.skip or not data:
            return
        self._append(data)

    def _flush(self) -> None:
        if not self.buf:
            return
        text = "".join(self.buf).replace("\xa0", " ")
        self.buf.clear()
        if not self.pre:
            text = _SPACE.sub(" ", text)
        self.out.append(text)
        self._trailing = len(text) - len(text.rstrip("\n"))

    def _break(self, blank: bool = False) -> None:
        self._flush()
        if blank:
            if self._trailing >= 2:
                return
            extra = "\n" * (2 - self._trailing)
        elif self._trailing >= 1:
            return
        else:
            extra = "\n"
        self.out.append(extra)
        self._trailing += len(extra)

    def text(self) -> str:
        self._flush()
        raw = "".join(self.out).replace("\r\n", "\n").replace("\r", "\n")
        lines: list[str] = []
        blanks = 0
        for line in raw.split("\n"):
            if not self.pre:
                line = _SPACE.sub(" ", line.replace("\xa0", " ")).strip()
            else:
                line = line.rstrip()
            if not line:
                blanks += 1
                if blanks == 1 and lines:
                    lines.append("")
                continue
            blanks = 0
            lines.append(line)
        return "\n".join(lines).strip()

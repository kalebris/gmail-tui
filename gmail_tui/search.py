"""Translate a Gmail-like query into an FTS5 expression plus local filters.

The same raw string is sent to Gmail. Local search only understands a
subset: bare terms, from/to/cc/subject, is:unread, is:read, is:starred,
and in:/label:.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedQuery:
    fts: str | None
    unread: bool | None
    starred: bool | None
    label: str | None


def parse_query(query: str) -> ParsedQuery:
    fts_parts: list[str] = []
    unread: bool | None = None
    starred: bool | None = None
    label: str | None = None
    for kind, value in _tokenize(query):
        if not value:
            continue
        if kind == "from":
            fts_parts.append(f"from_addr : {_quote(value)}")
        elif kind == "to":
            fts_parts.append(f"to_addr : {_quote(value)}")
        elif kind == "cc":
            fts_parts.append(f"cc_addr : {_quote(value)}")
        elif kind == "subject":
            fts_parts.append(f"subject : {_quote(value)}")
        elif kind == "term":
            fts_parts.append(_quote(value))
        elif kind == "is":
            lowered = value.lower()
            if lowered == "unread":
                unread = True
            elif lowered == "read":
                unread = False
            elif lowered == "starred":
                starred = True
        elif kind in ("in", "label"):
            normalized = _label(value)
            if normalized:
                label = normalized
    fts = " AND ".join(fts_parts) if fts_parts else None
    return ParsedQuery(fts=fts, unread=unread, starred=starred, label=label)


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _label(value: str) -> str:
    aliases = {
        "inbox": "INBOX",
        "trash": "TRASH",
        "spam": "SPAM",
        "anywhere": "",
        "all": "",
    }
    return aliases.get(value.lower(), value)


def _tokenize(query: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    index = 0
    length = len(query)
    while index < length:
        while index < length and query[index].isspace():
            index += 1
        if index >= length:
            break
        if query[index] == '"':
            word, index = _read_quoted(query, index)
            tokens.append(("term", word))
            continue
        start = index
        while index < length and not query[index].isspace():
            index += 1
        word = query[start:index]
        op, sep, rest = word.partition(":")
        if sep and op.isalpha():
            if rest.startswith('"'):
                if rest.endswith('"') and len(rest) > 1:
                    tokens.append((op.lower(), rest[1:-1]))
                else:
                    quoted, index = _read_quoted(query, start + len(op) + 1)
                    tokens.append((op.lower(), quoted))
            else:
                tokens.append((op.lower(), rest))
        else:
            tokens.append(("term", word))
    return tokens


def _read_quoted(query: str, index: int) -> tuple[str, int]:
    index += 1
    chars: list[str] = []
    while index < len(query):
        char = query[index]
        if char == '"':
            return "".join(chars), index + 1
        chars.append(char)
        index += 1
    return "".join(chars), index

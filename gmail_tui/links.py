"""Show a URL in full, with the word ``link`` in front of it.

The word is an OSC 8 hyperlink, which foot and Alacritty already open.
The address itself stays ordinary text, so a wrapped URL is still one string
to copy and the destination is visible before it is opened.
"""

from __future__ import annotations

import re

LABEL = "link"

_URL = re.compile(r"(?i)(?<![A-Za-z0-9+.-])(?:https?://|mailto:)\S+")
_MARK = re.compile(r"(?<![A-Za-z0-9])link ((?:https?://|mailto:)\S+)")
_OPEN = re.compile(r"(?i)https?://\S+$")
_TRAIL = ".,;:!?)]}>\"'"
_STOP = frozenset(
    {
        "about",
        "and",
        "cheers",
        "could",
        "for",
        "from",
        "have",
        "hello",
        "here",
        "http",
        "https",
        "just",
        "please",
        "regards",
        "see",
        "should",
        "sincerely",
        "thanks",
        "that",
        "the",
        "there",
        "this",
        "today",
        "with",
        "would",
        "you",
        "your",
    }
)


def osc8(label: str, url: str) -> str:
    """Wrap ``label`` so a terminal opens ``url`` from those cells."""
    return f"\033]8;;{url}\033\\{label}\033]8;;\033\\"


def present_links(text: str) -> str:
    """Put ``link`` in front of each http(s) or mailto URL, rejoining wrapped ones."""
    text = _join_wrapped(text)
    pieces: list[str] = []
    last = 0
    for start, end, url in _urls(text):
        pieces.append(text[last:start])
        if text[max(0, start - len(LABEL) - 1) : start] == f"{LABEL} ":
            pieces.append(url)
        else:
            pieces.append(f"{LABEL} {url}")
        last = end
    pieces.append(text[last:])
    return "".join(pieces)


def link_spans(line: str) -> list[tuple[int, int, str]]:
    """``(start, end, url)`` for each ``link <url>`` already present on ``line``."""
    found: list[tuple[int, int, str]] = []
    for match in _MARK.finditer(line):
        url = _trim(match.group(1))
        if not _usable(url):
            continue
        end = match.start(1) + len(url)
        found.append((match.start(), end, url))
    return found


def _urls(text: str) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str]] = []
    for match in _URL.finditer(text):
        url = _trim(match.group(0))
        if not _usable(url):
            continue
        found.append((match.start(), match.start() + len(url), url))
    return found


def _trim(url: str) -> str:
    while url and url[-1] in _TRAIL:
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        url = url[:-1]
    return url


def _usable(url: str) -> bool:
    if any(char in url for char in "\x1b\x07\n\r"):
        return False
    body = url.split(":", 1)[-1].lstrip("/")
    return bool(body) and any(char in url for char in ".@/")


def _join_wrapped(text: str) -> str:
    """Glue a long URL that a mailer broke onto the following line."""
    lines = text.splitlines()
    if len(lines) < 2:
        return text
    joined: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        while index + 1 < len(lines) and _continues(line, lines[index + 1]):
            index += 1
            line += lines[index].strip()
        joined.append(line)
        index += 1
    return "\n".join(joined)


def _continues(line: str, nxt: str) -> bool:
    token = nxt.strip()
    if not token or any(char.isspace() for char in token) or "://" in token:
        return False
    if token[:1] in {">", "-", "*", "[", "|"} or token[:1].isupper():
        return False
    if token.casefold() in _STOP:
        return False
    stripped = line.rstrip()
    if len(stripped) < 70:
        return False
    match = _OPEN.search(stripped)
    return match is not None and len(match.group(0)) >= 40

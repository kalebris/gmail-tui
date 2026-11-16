"""Conversation subjects and the reply tree inside one thread."""

from __future__ import annotations

import re

from gmail_tui.store import MessageSummary

_REPLY_PREFIX = re.compile(
    r"^(?:(?:re|fwd|fw)\s*(?:\[\d+\])?\s*:\s*)+",
    re.IGNORECASE,
)
_MESSAGE_ID = re.compile(r"<[^>]+>")


def bare_subject(subject: str) -> str:
    """Drop leading Re:/Fwd: prefixes, including repeats and Re[2]:."""
    cleaned = _REPLY_PREFIX.sub("", subject).strip()
    return cleaned or subject.strip()


def arrange(messages: list[MessageSummary]) -> list[tuple[MessageSummary, str]]:
    """Oldest-first tree. Each prefix is the branch drawn in front of the subject."""
    by_id: dict[str, MessageSummary] = {}
    for message in messages:
        key = _msgid(message.message_id_header)
        if key and key not in by_id:
            by_id[key] = message
    parent: dict[str, str] = {}
    for message in messages:
        earlier = _parent(message, by_id)
        if earlier is None or earlier.gmail_id == message.gmail_id:
            continue
        if (earlier.internal_date_ms, earlier.message_id) > (
            message.internal_date_ms,
            message.message_id,
        ):
            continue
        parent[message.gmail_id] = earlier.gmail_id
    parent = _acyclic(parent)
    children: dict[str, list[MessageSummary]] = {message.gmail_id: [] for message in messages}
    roots: list[MessageSummary] = []
    for message in messages:
        owner = parent.get(message.gmail_id)
        if owner and owner in children:
            children[owner].append(message)
        else:
            roots.append(message)
    def order(message: MessageSummary) -> tuple[int, int]:
        return (message.internal_date_ms, message.message_id)

    roots.sort(key=order)
    for kids in children.values():
        kids.sort(key=order)
    placed: list[tuple[MessageSummary, str]] = []

    def walk(node: MessageSummary, ancestors: list[bool], is_last: bool) -> None:
        prefix = "".join("  " if done else "│ " for done in ancestors)
        prefix += "└ " if is_last else "├ "
        placed.append((node, prefix))
        kids = children[node.gmail_id]
        for index, kid in enumerate(kids):
            walk(kid, ancestors + [is_last], index == len(kids) - 1)

    for index, root in enumerate(roots):
        walk(root, [], index == len(roots) - 1)
    return placed


def _parent(message: MessageSummary, by_id: dict[str, MessageSummary]) -> MessageSummary | None:
    reply = _msgid(message.in_reply_to)
    if reply and reply in by_id:
        return by_id[reply]
    for token in reversed(_MESSAGE_ID.findall(message.references_header)):
        key = _msgid(token)
        if key and key in by_id and by_id[key].gmail_id != message.gmail_id:
            return by_id[key]
    return None


def _msgid(value: str) -> str:
    token = value.strip()
    if not token or token == "-":
        return ""
    found = _MESSAGE_ID.search(token)
    if found:
        token = found.group(0)
    return token.strip("<>").lower()


def _acyclic(parent: dict[str, str]) -> dict[str, str]:
    kept: dict[str, str] = {}
    for start in parent:
        seen: set[str] = set()
        node = start
        while node in parent and node not in seen:
            seen.add(node)
            node = parent[node]
        if node not in seen:
            kept[start] = parent[start]
    return kept

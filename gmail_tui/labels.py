"""Turn Gmail labels into a folder tree.

User labels nest on ``/`` in the name (``Work/Clients``). System labels stay
in a fixed order. Categories sit under one group.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from gmail_tui.store import LabelInfo

SYSTEM_LABELS: tuple[tuple[str, str], ...] = (
    ("INBOX", "Inbox"),
    ("STARRED", "Starred"),
    ("IMPORTANT", "Important"),
    ("UNREAD", "Unread"),
    ("SENT", "Sent"),
    ("DRAFT", "Drafts"),
    ("SPAM", "Spam"),
    ("TRASH", "Trash"),
)

_CATEGORY_NAMES = {
    "CATEGORY_PERSONAL": "Personal",
    "CATEGORY_SOCIAL": "Social",
    "CATEGORY_PROMOTIONS": "Promotions",
    "CATEGORY_UPDATES": "Updates",
    "CATEGORY_FORUMS": "Forums",
}


@dataclass
class TreeNode:
    name: str
    gmail_id: str | None
    path: str
    unread: int = 0
    total: int = 0
    children: list[TreeNode] = field(default_factory=list)

    @property
    def key(self) -> str:
        if self.gmail_id == "":
            return "all"
        if self.gmail_id:
            return f"id:{self.gmail_id}"
        return f"path:{self.path}"


@dataclass(frozen=True)
class FlatLabel:
    key: str
    depth: int
    name: str
    path: str
    gmail_id: str | None
    unread: int
    expandable: bool
    expanded: bool
    total: int = 0
    # Same branch drawing as expanded message threads (│ ├ └ and >/v).
    prefix: str = ""


def build_label_tree(rows: list[LabelInfo]) -> list[TreeNode]:
    by_id = {row.gmail_id: row for row in rows}
    roots: list[TreeNode] = [TreeNode(name="All mail", gmail_id="", path="")]
    used: set[str] = set()
    for gmail_id, title in SYSTEM_LABELS:
        row = by_id.get(gmail_id)
        if row is None:
            continue
        roots.append(_leaf(title, row))
        used.add(gmail_id)

    categories: list[LabelInfo] = []
    user_rows: list[LabelInfo] = []
    for row in rows:
        if row.gmail_id in used:
            continue
        if row.gmail_id.startswith("CATEGORY_"):
            categories.append(row)
        elif row.type == "system":
            roots.append(_leaf(row.name or row.gmail_id, row))
        else:
            user_rows.append(row)

    if categories:
        group = TreeNode(name="Categories", gmail_id=None, path="Categories")
        for row in sorted(categories, key=lambda item: item.name.lower()):
            title = _CATEGORY_NAMES.get(row.gmail_id, row.name or row.gmail_id)
            group.children.append(_leaf(title, row))
        roots.append(group)
    roots.extend(_nest_user_labels(user_rows))
    return roots


def flatten(
    nodes: list[TreeNode],
    expanded: set[str],
    depth: int = 0,
    ancestors: list[bool] | None = None,
) -> list[FlatLabel]:
    """Flatten the tree with the same branch prefixes message threads use.

    Top-level expandable folders are fold heads (``v`` / ``>``), like a thread
    head. Their children start in that same column (``├`` / ``└``), not one
    indent further under the name — otherwise the spine is drawn twice. Deeper
    levels use the same ancestor ``│`` / ``├`` / ``└`` formula as
    ``threads.arrange``. A collapsed nested folder gets a trailing ``>``.
    """
    visible: list[FlatLabel] = []
    trail = ancestors or []
    for index, node in enumerate(nodes):
        is_last = index == len(nodes) - 1
        is_open = node.key in expanded
        has_kids = bool(node.children)
        if depth == 0:
            # Fold head, same role as ``v N  subject`` on a thread.
            prefix = ("v " if is_open else "> ") if has_kids else "  "
            # Indent children under the name. Always treat the head as "last" so
            # we never draw a second spine under the arrow toward later siblings.
            child_trail: list[bool] = [True]
        else:
            # Same formula as threads.arrange.
            prefix = "".join("  " if done else "│ " for done in trail)
            prefix += "└ " if is_last else "├ "
            if has_kids and not is_open:
                prefix += "> "
            child_trail = trail + [is_last]
        visible.append(
            FlatLabel(
                key=node.key,
                depth=depth,
                name=node.name,
                path=node.path or node.name,
                gmail_id=node.gmail_id,
                unread=node.unread,
                expandable=has_kids,
                expanded=is_open,
                total=node.total,
                prefix=prefix,
            )
        )
        if has_kids and is_open:
            visible.extend(flatten(node.children, expanded, depth + 1, child_trail))
    return visible


def expandable_keys(nodes: list[TreeNode]) -> set[str]:
    keys: set[str] = set()
    for node in nodes:
        if node.children:
            keys.add(node.key)
            keys.update(expandable_keys(node.children))
    return keys


def _leaf(name: str, row: LabelInfo) -> TreeNode:
    return TreeNode(
        name=name,
        gmail_id=row.gmail_id,
        path=row.name or name,
        unread=row.unread,
        total=row.total,
    )


def _nest_user_labels(rows: list[LabelInfo]) -> list[TreeNode]:
    index: dict[tuple[str, ...], TreeNode] = {}
    roots: list[TreeNode] = []
    for row in sorted(rows, key=lambda item: item.name.lower()):
        parts = [part for part in row.name.split("/") if part] or [row.name or row.gmail_id]
        for depth, part in enumerate(parts):
            path = tuple(parts[: depth + 1])
            node = index.get(path)
            if node is None:
                node = TreeNode(name=part, gmail_id=None, path="/".join(path))
                index[path] = node
                if depth == 0:
                    roots.append(node)
                else:
                    index[path[:-1]].children.append(node)
            if depth == len(parts) - 1:
                node.gmail_id = row.gmail_id
                node.unread = row.unread
                node.total = row.total
    return roots

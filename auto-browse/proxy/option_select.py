"""Pure helpers for the composite `select_options` dropdown tool.

ARIA comboboxes/listboxes render their option nodes only AFTER the control is
opened, so picking a value is a multi-step browser dance (open -> read options ->
click the matching option). These functions do the deterministic, browser-free
parts — locating the rendered options in a snapshot and matching a requested
human-readable label to one of them — so the async orchestration in `server.py`
stays thin. No LLM, no upstream calls.
"""

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from .tree_parser import TreeNode, collect_all, parse

# Roles that represent a selectable choice inside an opened dropdown/listbox/menu.
_OPTION_ROLES: frozenset[str] = frozenset(
    {"option", "menuitem", "menuitemradio", "menuitemcheckbox", "radio", "treeitem"}
)
# Containers that hold the rendered options once a combobox is opened. When one is
# present we scope option collection to its descendants (avoids matching unrelated
# option-role nodes elsewhere on the page).
_LISTBOX_ROLES: frozenset[str] = frozenset({"listbox", "menu", "tree"})
# A free-text filter inside an opened combobox => type-ahead widget. Only a real
# textbox/searchbox counts — not the combobox button itself (typing into a closed
# button is wrong).
_SEARCH_ROLES: frozenset[str] = frozenset({"textbox", "searchbox"})

# Workday renders each ALREADY-SELECTED value as a `listbox "items selected"` holding
# `option "X, press delete to clear value."` pills. These are state, not choices — a
# permanent tray that stays in the DOM whether the dropdown is open or closed. Both
# the tray (by name) and the pill nodes (by this marker) must be excluded from the
# selectable-option pool, else a closed widget looks "open" and values match pills.
_PILL_RE = re.compile(r"press delete to clear value", re.I)
_PILL_TRAY_RE = re.compile(r"items selected", re.I)

# Fuzzy ratio below which we refuse to pick (report back rather than guess wrong).
MATCH_THRESHOLD = 0.6

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]")


@dataclass(frozen=True)
class Option:
    ref: str
    label: str


def _normalize(text: str) -> str:
    """Lowercase, drop a leading required `*`, strip punctuation, collapse spaces."""
    text = text.strip().lstrip("*").strip()
    text = _PUNCT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip().lower()


def _option_nodes(root: TreeNode) -> list[TreeNode]:
    """Option-role nodes (with a ref + name) under `root`, scoped to the opened
    listbox/menu when one exists, else the whole tree."""
    nodes = collect_all(root)
    # Exclude the selected-pills tray from the scope — it is not an opened menu.
    containers = [
        n
        for n in nodes
        if n.role in _LISTBOX_ROLES and not _PILL_TRAY_RE.search(n.name or "")
    ]
    scope: list[TreeNode] = []
    for c in containers:
        scope.extend(collect_all(c))
    pool = scope or nodes
    seen: set[str] = set()
    out: list[TreeNode] = []
    for n in pool:
        if n.role in _OPTION_ROLES and n.ref and n.name and n.ref not in seen:
            if _PILL_RE.search(n.name):  # an already-selected pill, not a choice
                continue
            seen.add(n.ref)
            out.append(n)
    return out


def collect_options(tree_text: str) -> list[Option]:
    """Rendered selectable options in a snapshot, in document order."""
    root = parse(tree_text)
    if root is None:
        return []
    return [Option(ref=n.ref, label=n.name) for n in _option_nodes(root)]  # type: ignore[arg-type]


def find_searchbox(tree_text: str) -> str | None:
    """Ref of a free-text filter inside an opened combobox (type-ahead), if any.

    Skips the very first node (the combobox container itself can carry a
    `combobox` role) and returns the first inner search/textbox ref.
    """
    root = parse(tree_text)
    if root is None:
        return None
    nodes = collect_all(root)
    for n in nodes:
        if n.role in _SEARCH_ROLES and n.ref and n is not root:
            return n.ref
    return None


# Roles a combobox/dropdown control can carry — used to re-find it by label after
# a pick re-renders the tree and its ref changes.
_CONTROL_ROLES: frozenset[str] = frozenset(
    {"combobox", "button", "listbox", "textbox", "searchbox"}
)


def label_of_ref(tree_text: str, ref: str) -> str | None:
    """Accessible name of the node with `ref`, if present in the tree."""
    root = parse(tree_text)
    if root is None:
        return None
    for n in collect_all(root):
        if n.ref == ref:
            return n.name
    return None


def find_ref_by_label(tree_text: str, label: str) -> str | None:
    """Ref of a combobox/dropdown control whose name matches `label` (for
    re-opening a multiselect after its ref went stale). Best normalized match."""
    root = parse(tree_text)
    if root is None or not label:
        return None
    nlabel = _normalize(label)
    best_ref: str | None = None
    best_score = 0.0
    for n in collect_all(root):
        if n.role in _CONTROL_ROLES and n.ref and n.name:
            s = _score(nlabel, _normalize(n.name))
            if s > best_score:
                best_ref, best_score = n.ref, s
    return best_ref if best_score >= MATCH_THRESHOLD else None


def _score(query: str, candidate: str) -> float:
    """Similarity in [0, 1]. Exact == 1.0; substring either way ranks high; else a
    fuzzy ratio."""
    if not query or not candidate:
        return 0.0
    if query == candidate:
        return 1.0
    if query in candidate or candidate in query:
        # Longer overlap relative to the candidate ranks higher, but always above
        # the fuzzy band so a clean substring beats a noisy fuzzy near-match.
        return 0.9 + 0.1 * (len(query) / max(len(candidate), 1))
    return SequenceMatcher(None, query, candidate).ratio()


def best_match(value: str, options: list[Option]) -> Option | None:
    """Closest option to `value`, or None if nothing clears MATCH_THRESHOLD."""
    nval = _normalize(value)
    best: Option | None = None
    best_score = 0.0
    for opt in options:
        s = _score(nval, _normalize(opt.label))
        if s > best_score:
            best, best_score = opt, s
    return best if best_score >= MATCH_THRESHOLD else None

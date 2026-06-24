import re

from .keyword_extractor import extract
from .tree_parser import LANDMARK_ROLES, TreeNode, collect_all, parse

INTERACTIVE_ROLES: frozenset[str] = frozenset(
    {
        "button",
        "link",
        "textbox",
        "searchbox",
        "combobox",
        "checkbox",
        "radio",
        "menuitem",
        "option",
        "switch",
        "tab",
        "slider",
    }
)

# Primary form-action buttons. These are always surfaced (they live in the form's
# action bar, not the page footer) so the model never has to fish around the
# footer to find "Save and Continue" / "Next" / "Submit".
_ACTION_BUTTON_RE = re.compile(
    r"\b(save|continue|next|submit|review|apply|finish|proceed|back)\b", re.I
)
# Page footer landmark — demoted so its social/legal links don't dominate.
_FOOTER_LANDMARK = "contentinfo"


def _action_buttons(nodes: list[TreeNode]) -> set[TreeNode]:
    return {
        n
        for n in nodes
        if n.role == "button"
        and n.landmark != _FOOTER_LANDMARK
        and n.name
        and _ACTION_BUTTON_RE.search(n.name)
    }


def prune(raw_tree: str, goal: str, min_results: int = 3) -> str:
    root = parse(raw_tree)
    if root is None:
        return raw_tree

    all_nodes = collect_all(root)
    non_meta = [n for n in all_nodes if not n.is_url and not n.is_text]

    keywords, _ = extract(goal)

    pass1 = _keyword_match(non_meta, keywords)
    actions = _action_buttons(non_meta)

    if not pass1:
        # No keyword match: surface just the form action buttons if present,
        # else fall back to landmarks only (don't flood with all interactives).
        if not actions:
            return _landmarks_only(all_nodes, keywords)
        pass2: set[TreeNode] = set()
    elif len(pass1) < min_results:
        pass2 = _interactive_fallback(non_meta, pass1)
    else:
        pass2 = set()

    # Always include the form's action buttons (Save/Continue/Next/Submit/...).
    kept = pass1 | pass2 | actions

    ancestors = _ancestor_chains(kept)
    url_children = {child for node in kept for child in node.children if child.is_url}

    return _reconstruct(kept, ancestors, url_children, keywords, len(non_meta), "none")


EXPAND_MAX_NODES = 250


def expand_region(ref: str, goal: str = "", max_nodes: int = EXPAND_MAX_NODES) -> str:
    """Dump the FULL content of a subtree from the cached raw tree, verbatim.

    This is the READ path: pruned snapshots tell the model *where* to look, then
    expand_region reveals *everything* inside a region — including the text/value
    leaf nodes (dates, times, prices, status) that keyword pruning strips out.
    No keyword filtering; refs and indentation preserved exactly. Capped at
    ``max_nodes`` to bound context; drill into a deeper ref if truncated.

    ``goal`` is accepted for API compatibility but ignored — expansion is total.
    """
    from . import snapshot_cache

    raw = snapshot_cache.get()
    if not raw:
        return "Error: no snapshot in cache. Call snapshot or navigate first."

    root = parse(raw)
    if root is None:
        return "Error: could not parse cached snapshot."

    target = next((n for n in collect_all(root) if n.ref == ref), None)
    if target is None:
        return f"Error: ref={ref} not found in cached snapshot."

    subtree = collect_all(target)
    shown = subtree[:max_nodes]

    lines = [
        f"[expanded: {len(shown)} of {len(subtree)} nodes under {ref} "
        f"— full content, refs preserved]",
        "",
    ]
    lines.extend(node.raw_line for node in shown)
    if len(subtree) > max_nodes:
        lines.append(
            f"  ... ({len(subtree) - max_nodes} more nodes truncated — "
            f"expand a deeper ref to read them)"
        )
    return "\n".join(lines)


def _keyword_match(nodes: list[TreeNode], keywords: set[str]) -> set[TreeNode]:
    if not keywords:
        return set()
    matched: set[TreeNode] = set()
    for node in nodes:
        haystack = f"{node.role} {node.name or ''}".lower()
        if any(kw in haystack for kw in keywords):
            matched.add(node)
    return matched


def _interactive_fallback(nodes: list[TreeNode], pass1: set[TreeNode]) -> set[TreeNode]:
    if pass1:
        best = min(pass1, key=lambda n: n.line_index)
        target_landmark = best.landmark
        candidates = [n for n in nodes if n.landmark == target_landmark]
    else:
        candidates = nodes
    # Demote the page footer: its social/legal links shouldn't flood the result.
    return {
        n
        for n in candidates
        if n.role in INTERACTIVE_ROLES and n.landmark != _FOOTER_LANDMARK
    }


def _ancestor_chains(kept: set[TreeNode]) -> set[TreeNode]:
    ancestors: set[TreeNode] = set()
    for node in kept:
        current = node.parent
        while current is not None:
            if current not in kept:
                ancestors.add(current)
            current = current.parent
    return ancestors


def _landmarks_only(all_nodes: list[TreeNode], keywords: set[str]) -> str:
    kw_str = ", ".join(sorted(keywords)) if keywords else "(none)"
    lines = [
        f"[pruned: 0 of {len(all_nodes)} nodes | keywords: {{{kw_str}}} | fallback: landmarks_only]"
    ]
    for n in all_nodes:
        if n.role in LANDMARK_ROLES:
            indent = "  " * n.depth
            ref_part = f" [ref={n.ref}]" if n.ref else ""
            name_part = f' "{n.name}"' if n.name else ""
            lines.append(f"{indent}- {n.role}{name_part}{ref_part}")
    return "\n".join(lines)


def _reconstruct(
    kept: set[TreeNode],
    ancestors: set[TreeNode],
    url_children: set[TreeNode],
    keywords: set[str],
    total_nodes: int,
    fallback_flag: str,
) -> str:
    all_output = kept | ancestors | url_children
    sorted_nodes = sorted(all_output, key=lambda n: n.line_index)

    kw_str = ", ".join(sorted(keywords)) if keywords else "(none)"
    lines = [
        f"[pruned: {len(kept)} of {total_nodes} nodes | keywords: {{{kw_str}}} | fallback: {fallback_flag}]",
        "",
    ]

    for node in sorted_nodes:
        if node in kept or node in url_children:
            lines.append(node.raw_line)
        else:
            # ancestor only: simplified line
            indent = "  " * node.depth
            ref_part = f" [ref={node.ref}]" if node.ref else ""
            name_part = f' "{node.name}"' if node.name else ""
            has_children_in_output = any(c for c in node.children if c in all_output)
            colon = ":" if has_children_in_output else ""
            lines.append(f"{indent}- {node.role}{name_part}{ref_part}{colon}")

    return "\n".join(lines)

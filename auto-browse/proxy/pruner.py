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

# Entry-action controls on a landing / gateway page. Surfaced regardless of goal
# because the agent's first move on a fresh job page is almost always one of
# these (data: first orientation goal is nearly always apply/sign-in/autofill).
# Plain "apply"/"submit"/"continue" are already covered by _ACTION_BUTTON_RE.
_ENTRY_ACTION_RE = re.compile(
    r"\b(sign[\s-]?in|log[\s-]?in|sign[\s-]?up|create account|register|autofill"
    r"|apply with resume|easy apply|upload (your )?resume|attach resume"
    r"|use my resume|continue with (google|linkedin|resume))\b",
    re.I,
)
# Always-surface nodes regardless of goal: a form won't submit while these are
# present, and their keywords never appear in the step goal.
_ALWAYS_ROLES: frozenset[str] = frozenset({"alert", "status", "alertdialog"})
# Node *name* signalling an error state — ATS portals render errors as
# `img "Error"` icons, not text or `alert`.
_ERROR_NAME_RE = re.compile(
    r"\b(error|invalid)\b|is required|please (enter|select|complete|fill)"
    r"|cannot be blank|must be (completed|selected|provided)",
    re.I,
)
# Required field: a leading "*" in the accessible name (e.g.
# `combobox "*How did you hear..."`, `group "*Are you 18..."`).
_REQUIRED_NAME_RE = re.compile(r"^\s*\*\s*\S")

# Choice containers whose option/radio children are auto-included once the
# container is kept, so the model sees the actual choices (the pruned tree used
# to keep a `*How did you hear` combobox but drop every option under it).
_CHOICE_CONTAINERS: frozenset[str] = frozenset(
    {"combobox", "radiogroup", "listbox", "group", "menu"}
)
_CHOICE_ITEMS: frozenset[str] = frozenset(
    {"option", "radio", "menuitemradio", "checkbox", "switch"}
)
_MAX_CHOICES_PER_CONTAINER = 30  # targeted prune; overview uses a smaller cap

# Roles that count as a form field for the structured overview.
_ENTRY_ACTION_ROLES: frozenset[str] = frozenset({"button", "link", "menuitem"})


def _action_buttons(nodes: list[TreeNode]) -> set[TreeNode]:
    return {
        n
        for n in nodes
        if n.role == "button"
        and n.landmark != _FOOTER_LANDMARK
        and n.name
        and _ACTION_BUTTON_RE.search(n.name)
    }


def _entry_actions(nodes: list[TreeNode]) -> set[TreeNode]:
    """Sign-in / create-account / apply-with-resume / autofill / upload-resume
    controls — surfaced regardless of goal (footer demoted)."""
    return {
        n
        for n in nodes
        if n.role in _ENTRY_ACTION_ROLES
        and n.landmark != _FOOTER_LANDMARK
        and n.name
        and _ENTRY_ACTION_RE.search(n.name)
    }


def _always_keep(nodes: list[TreeNode]) -> set[TreeNode]:
    """Validation errors, alerts, and required (`*`) fields — always surfaced so
    the model never misses why a form won't advance."""
    out: set[TreeNode] = set()
    for n in nodes:
        if n.role in _ALWAYS_ROLES:
            out.add(n)
        elif n.name and (
            _ERROR_NAME_RE.search(n.name) or _REQUIRED_NAME_RE.search(n.name)
        ):
            out.add(n)
    return out


def _choice_children(kept: set[TreeNode], cap: int) -> set[TreeNode]:
    """Option/radio/checkbox children of any kept choice container, so the model
    sees the actual choices. Capped per container to bound size."""
    out: set[TreeNode] = set()
    for node in kept:
        if node.role not in _CHOICE_CONTAINERS:
            continue
        items = [
            d for d in collect_all(node) if d is not node and d.role in _CHOICE_ITEMS
        ]
        out.update(items[:cap])
    return out


def prune(raw_tree: str, goal: str, min_results: int = 3) -> str:
    root = parse(raw_tree)
    if root is None:
        return raw_tree

    all_nodes = collect_all(root)
    non_meta = [n for n in all_nodes if not n.is_url and not n.is_text]

    keywords, _ = extract(goal)

    pass1 = _keyword_match(non_meta, keywords)

    # Always-surfaced sets, independent of the step goal: form-action buttons,
    # entry actions (sign-in/apply-with-resume/...), and errors/required fields.
    extras = (
        _action_buttons(non_meta) | _entry_actions(non_meta) | _always_keep(non_meta)
    )

    if pass1 and len(pass1) < min_results:
        pass2 = _interactive_fallback(non_meta, pass1)
    else:
        pass2 = set()

    kept = pass1 | pass2 | extras
    if not kept:
        # Nothing matched and nothing to always-surface: landmarks skeleton.
        return _landmarks_only(all_nodes, keywords)

    # Reveal the choices inside any kept dropdown / radio group.
    kept |= _choice_children(kept, _MAX_CHOICES_PER_CONTAINER)

    ancestors = _ancestor_chains(kept)
    url_children = {child for node in kept for child in node.children if child.is_url}

    return _reconstruct(kept, ancestors, url_children, keywords, len(non_meta), "none")


_OVERVIEW_CHOICE_CAP = 8  # hint options in the overview without flooding


def overview(raw_tree: str) -> str:
    """Structured first-view of a freshly-loaded page (no keyword goal).

    Returns the page's actionable skeleton: every interactive field/button/link
    (footer demoted), plus entry actions, form-action buttons, validation errors,
    required (`*`) fields, and a few choices per dropdown/radio group — across ALL
    sections of a multi-section form, so the agent never has a blind spot. Far
    smaller than the raw tree on a landing page (drops the job description /
    related-jobs / legal noise), complete on a form page.
    """
    root = parse(raw_tree)
    if root is None:
        return raw_tree

    all_nodes = collect_all(root)
    non_meta = [n for n in all_nodes if not n.is_url and not n.is_text]

    interactive = {
        n
        for n in non_meta
        if n.role in INTERACTIVE_ROLES and n.landmark != _FOOTER_LANDMARK
    }
    kept = (
        interactive
        | _entry_actions(non_meta)
        | _action_buttons(non_meta)
        | _always_keep(non_meta)
    )
    if not kept:
        return _landmarks_only(all_nodes, set())

    kept |= _choice_children(kept, _OVERVIEW_CHOICE_CAP)

    ancestors = _ancestor_chains(kept)
    url_children = {child for node in kept for child in node.children if child.is_url}

    return _reconstruct(kept, ancestors, url_children, set(), len(non_meta), "overview")


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

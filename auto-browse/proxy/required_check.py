"""Required-field validation, computed deterministically from a snapshot tree.

ATS forms mark required fields with a leading ``*`` in the accessible name
(``textbox "*First Name"``, ``combobox "*How did you hear..."``,
``group "*Are you 18..."``). A field can read back as "filled" or "empty" from
the tree itself — text value present, an option ``[selected]`` (not a
placeholder), or a child radio/checkbox ``[checked]``. The proxy appends this
status to every form result so the agent (and the evidence gate) sees instantly
when a fill did not take — e.g. a ``fill_form`` on a radio that silently no-ops.
"""

import re

from .tree_parser import collect_all, parse

# Field is required when its name starts with "*".
_REQUIRED_NAME_RE = re.compile(r"^\s*\*\s*\S")

# Roles that count as a fillable field.
_FIELD_ROLES = frozenset(
    {
        "textbox",
        "searchbox",
        "spinbutton",
        "combobox",
        "listbox",
        "radiogroup",
        "group",
        "checkbox",
        "switch",
        "slider",
    }
)
# Roles whose fill state is an inline text value.
_VALUE_ROLES = frozenset({"textbox", "searchbox", "spinbutton", "slider"})
# Selected option names that don't count as a real choice.
_PLACEHOLDER = frozenset(
    {"", "choose...", "choose", "select...", "select", "select one",
     "select an option", "none", "--", "- select -"}
)
# Inline value after the ref, e.g. `... [ref=e132]: +1 510-...`.
_INLINE_VALUE_RE = re.compile(r"\]:\s*(\S.*)$")

# Stable marker the evidence gate matches (lowercased) to block a premature finish.
INCOMPLETE_MARKER = "required field(s) still empty"


def _has_inline_value(raw_line: str) -> bool:
    m = _INLINE_VALUE_RE.search(raw_line)
    return bool(m and m.group(1).strip())


def _is_placeholder(name: str | None) -> bool:
    return (name or "").strip().lower() in _PLACEHOLDER


def _is_filled(node) -> bool:
    """True if a required field node reads back as answered in the tree."""
    if node.role in _VALUE_ROLES:
        if _has_inline_value(node.raw_line):
            return True
    if node.role in ("checkbox", "switch") and "[checked]" in node.raw_line:
        return True
    # Container/choice fields: any descendant checked, or a non-placeholder option
    # selected.
    for d in collect_all(node):
        rl = d.raw_line
        if "[checked]" in rl:
            return True
        if "[selected]" in rl and not _is_placeholder(d.name):
            return True
    return False


def required_status(raw_tree: str) -> tuple[list, list]:
    """Return (filled, empty) lists of required (`*`) field nodes."""
    root = parse(raw_tree)
    if root is None:
        return [], []
    filled, empty = [], []
    for n in collect_all(root):
        if n.role in _FIELD_ROLES and n.name and _REQUIRED_NAME_RE.match(n.name):
            (filled if _is_filled(n) else empty).append(n)
    return filled, empty


def _hint(role: str) -> str:
    if role in ("radiogroup", "group"):
        return " — click the Yes/No radio ref directly"
    if role == "checkbox":
        return " — click the checkbox ref"
    if role in ("combobox", "listbox"):
        return " — pick an option"
    return ""


def format_required_check(raw_tree: str, max_list: int = 12) -> str:
    """One compact status block for the result footer. Empty string when the page
    has no required (`*`) fields (landing / sign-in pages self-suppress)."""
    filled, empty = required_status(raw_tree)
    total = len(filled) + len(empty)
    if total == 0:
        return ""
    if not empty:
        return f"REQUIRED CHECK — all {total} required field(s) filled."
    lines = [f"REQUIRED CHECK — {len(empty)} of {total} required field(s) still EMPTY:"]
    for n in empty[:max_list]:
        ref = f", ref {n.ref}" if n.ref else ""
        lines.append(f"  - {n.name} ({n.role}{ref}){_hint(n.role)}")
    if len(empty) > max_list:
        lines.append(f"  ... (+{len(empty) - max_list} more)")
    lines.append(
        "Fill every one of these before Save/Submit. For radio/checkbox questions "
        "CLICK the specific option ref; do not use fill_form on them."
    )
    return "\n".join(lines)

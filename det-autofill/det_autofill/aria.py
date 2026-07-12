"""ARIA snapshot parser: playwright-style snapshot text -> FormField objects.

Input format (what playwright MCP / the proxy's fetch_aria emit), one node per line:

    - textbox "First Name*" [ref=e161] [cursor=pointer]:
      - /placeholder: Type here...
    - button "State Select One Required" [ref=e302]: Select One
    - radio "Male" [ref=e33] [checked]
    - generic [ref=e395]: Are you Hispanic/Latino?

The parser is deliberately line-based and forgiving: trace fragments are often
truncated mid-tree, and real snapshots mix indentation styles. It extracts every
interactive node, associates a best-effort question label, and marks required-ness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Roles a filler can act on. `button` and `link` are included because many ATSes
# render dropdown triggers and nav ("Next", "Apply") as buttons/links.
INTERACTIVE_ROLES = {
    "textbox",
    "searchbox",
    "combobox",
    "listbox",
    "radio",
    "checkbox",
    "switch",
    "spinbutton",
    "slider",
    "option",
    "button",
    "link",
    "menuitem",
}

# Nodes that carry question text for a nearby widget.
_TEXTY_ROLES = {"generic", "paragraph", "text", "strong", "emphasis", "heading", "legend", "LabelText"}

_LINE_RE = re.compile(
    r"""^(?P<indent>\s*)-\s+
        (?P<role>[A-Za-z]+)
        (?:\s+"(?P<name>[^"]*)")?
        (?P<flags>(?:\s+\[[^\]]+\])*)
        (?::\s*(?P<text>.*))?
        \s*$""",
    re.VERBOSE,
)

_REQUIRED_MARKS = ("*", "✱", "required")


@dataclass
class FormField:
    ref: str
    role: str
    label: str  # accessible name or associated question text
    required: bool = False
    checked: bool | None = None
    selected: bool | None = None
    expanded: bool | None = None
    disabled: bool = False
    value: str = ""  # trailing text (current value / button caption)
    options: list[str] = field(default_factory=list)  # options seen under a combobox/listbox
    context: str = ""  # nearby question text when the accessible name is weak


def _flags(raw: str) -> dict[str, str | bool]:
    out: dict[str, str | bool] = {}
    for m in re.finditer(r"\[([^\]]+)\]", raw or ""):
        token = m.group(1)
        if "=" in token:
            k, v = token.split("=", 1)
            out[k.strip()] = v.strip()
        else:
            out[token.strip()] = True
    return out


def _is_required(label: str, value: str) -> bool:
    hay = f"{label} {value}".lower()
    return any(mark in hay for mark in ("*", "✱")) or "required" in hay


def parse_snapshot(text: str) -> list[FormField]:
    """All interactive fields in a snapshot/fragment, in document order."""
    fields: list[FormField] = []
    # (indent, question-text) trail: the most recent texty lines above the widget.
    trail: list[tuple[int, str]] = []

    for raw in text.splitlines():
        m = _LINE_RE.match(raw)
        if not m:
            continue
        indent = len(m.group("indent"))
        role = m.group("role")
        name = (m.group("name") or "").strip()
        flags = _flags(m.group("flags"))
        tail = (m.group("text") or "").strip()

        if role in _TEXTY_ROLES:
            label_text = (tail or name).strip('"')
            if label_text:
                # visual caption echoing the widget it names (Workday renders
                # `- radio "Yes"` followed by `- generic: "Yes"`) — not question
                # text; letting it onto the trail poisons the next sibling's context
                if fields and fields[-1].label == label_text:
                    continue
                # keep only trail entries at shallower indents (ancestors/siblings above)
                trail = [(i, t) for i, t in trail if i < indent] + [(indent, label_text)]
            continue

        if role not in INTERACTIVE_ROLES:
            continue

        ref = str(flags.get("ref", ""))
        if not ref:
            continue

        context = trail[-1][1] if trail else ""
        label = name or context
        checked_flag = flags.get("checked")
        f = FormField(
            ref=ref,
            role=role,
            label=label,
            required=_is_required(label, tail),
            checked=(
                True
                if checked_flag is True
                else (False if str(checked_flag).lower() == "false" else None)
            ),
            selected=True if flags.get("selected") is True else None,
            expanded=True if flags.get("expanded") is True else None,
            disabled=flags.get("disabled") is True,
            value=tail,
            context=context,
        )
        # playwright renders unchecked as [checked] absent; explicit "not checked"
        # sometimes appears in the name/tail — normalize:
        if f.checked is None and re.search(r"\bnot checked\b", f"{name} {tail}".lower()):
            f.checked = False
        # Workday renders empty dropdowns as buttons captioned "Select One" —
        # they behave as click-open comboboxes, not nav
        if role == "button" and "select one" in f"{name} {tail}".lower():
            f.role = "combobox"
        if role == "option":
            # attach to the most recent combobox/listbox field
            for prev in reversed(fields):
                if prev.role in ("combobox", "listbox"):
                    prev.options.append(name or tail)
                    break
        fields.append(f)
    return fields


# Buttons are normally nav chrome, EXCEPT file-upload triggers ("Attach"
# under a Resume/CV group) — those are the only way to reach the upload.
# Label only: sibling source buttons (Dropbox, Google Drive, Enter manually)
# share the resume group's context but must not trigger uploads.
_UPLOAD_HINT = re.compile(
    r"attach|upload|resume|\bcv\b|choose file|select file|browse", re.I
)


def fillable(fields: list[FormField]) -> list[FormField]:
    """The subset a filler answers (excludes nav buttons/links and options)."""
    out = []
    for f in fields:
        if f.role in ("button", "link", "menuitem", "option"):
            if f.role == "button" and _UPLOAD_HINT.search(f.label):
                out.append(f)
            continue
        if f.disabled:
            continue
        out.append(f)
    return out

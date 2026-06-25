"""Deterministic page-model extractor.

Replaces the old keyword-pruning approach. Instead of returning a goal-filtered
slice of the accessibility tree, we parse the FULL tree once and return a typed,
complete model of the page: its kind, every form field (with type / required /
current value / options / error), the actionable buttons, and any validation
errors. The cheap LLM no longer searches with a goal — it receives exactly what
matters to fill and advance a job application.

Field-type and error signatures were derived from a corpus of human-recorded
applications across Workday, Greenhouse, Lever, Oracle, Taleo and UltiPro (see
proxy/tests/fixtures/). Everything here is pure / deterministic — no LLM.

Live snapshots from Playwright MCP carry `[ref=eN]` ids (preserved through
`tree_parser`); recorder fixtures do not, so `ref` may be None — render() omits it
gracefully.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .tree_parser import TreeNode, collect_all, parse

MIN_MEANINGFUL_NODES = 3
RAW_CAP = 400  # max raw lines when falling back on an unclassifiable page

# --- field type detection ------------------------------------------------------

_TEXT_ROLES = frozenset({"textbox", "searchbox"})
_REQ_RE = re.compile(r"[*✱]|\brequired\b", re.IGNORECASE)
_SELECT_ONE_RE = re.compile(r"select one|select\.\.\.|choose one|please select", re.IGNORECASE)
# Workday multiselect: chosen values render as `option "X, press delete to clear
# value."` pills inside a permanent `listbox "items selected"` tray. The tray is the
# field's *value*, not a separate dropdown — see _multiselect_tray_fields.
_PILL_RE = re.compile(r",?\s*press delete to clear value\.?\s*$", re.IGNORECASE)
_PILL_TRAY_RE = re.compile(r"items selected", re.IGNORECASE)
_ITEMS_SELECTED_PREFIX_RE = re.compile(r"^\d+\s+items?\s+selected,\s*", re.IGNORECASE)

# --- action button classification ---------------------------------------------

_ACTION_KINDS: list[tuple[str, re.Pattern]] = [
    ("apply", re.compile(r"\bapply\b", re.I)),
    ("signup", re.compile(r"create account|sign up|register|new account", re.I)),
    ("signin", re.compile(r"sign in|log in|login", re.I)),
    ("submit", re.compile(r"\bsubmit\b", re.I)),
    ("review", re.compile(r"\breview\b", re.I)),
    ("save", re.compile(r"save and continue|save & continue|\bsave\b", re.I)),
    ("continue", re.compile(r"continue|next|proceed", re.I)),
    ("upload", re.compile(r"upload|attach|choose file|autofill", re.I)),
    ("add", re.compile(r"add another|add more|\badd\b", re.I)),
    ("back", re.compile(r"\bback\b|previous", re.I)),
]
# obvious page chrome we never surface as an action
_CHROME_RE = re.compile(
    r"skip to|careers home|privacy|cookie|terms|toggle flyout|view more jobs|"
    r"back to job|powered by|home$|search for jobs|introduce yourself|menu",
    re.I,
)

# --- error / status signatures -------------------------------------------------

_ERROR_SUMMARY_RE = re.compile(r"errors? found|please (correct|fix|complete)", re.I)
# Broad — used only inside alert/status nodes (a deliberate error channel).
_ERROR_FIELD_RE = re.compile(
    r"is required|field is required|must have a value|please enter|please select|"
    r"invalid|enter a valid|you must select|cannot be (blank|empty)",
    re.I,
)
# Strong — used for bare text/paragraph nodes, where "please enter X" is usually a
# section helper and ". Required" is a label marker, not a validation error.
_STRONG_ERR_RE = re.compile(
    r"is required|must have a value|invalid|enter a valid|you must select all|"
    r"cannot be (blank|empty)",
    re.I,
)
# alert/status text we DROP (informational, not validation)
_INFO_RE = re.compile(
    r"loading|successfully|uploaded|imported|pre-?fill|dependency-fields|"
    r"verification code|email has been sent|you're all set|done$|sent to",
    re.I,
)
# long legal/EEO boilerplate that contains "required" but is not an error
_BOILERPLATE_RE = re.compile(
    r"affirmative|OMB|paperwork reduction|notify all applicants|federal contractor|"
    r"VEVRAA|readjustment|equal (employment )?opportunity|decline to say",
    re.I,
)

_CONFIRM_RE = re.compile(
    r"thank you for applying|application (has been )?(received|submitted|complete)|"
    r"your application|we('| ha)ve received|application submitted|already applied|"
    r"successfully submitted",
    re.I,
)
_PASSWORD_RE = re.compile(r"password|passcode", re.I)
_STEP_RE = re.compile(r"current step (\d+) of (\d+)\s*(.*)", re.I)


@dataclass
class Field:
    ref: str | None
    label: str
    type: str  # text|textarea|dropdown-aria|select-native|radio|checkbox|file|date|unknown
    required: bool
    value: str  # "" == empty
    options: list[tuple[str | None, str]] = field(default_factory=list)
    error: str | None = None

    @property
    def empty(self) -> bool:
        if self.type in ("radio", "checkbox"):
            return not self.value
        if self.type in ("dropdown-aria", "select-native"):
            return not self.value or bool(_SELECT_ONE_RE.search(self.value))
        return not self.value


@dataclass
class Action:
    ref: str | None
    label: str
    kind: str


@dataclass
class PageModel:
    page_type: str
    step: str | None = None
    fields: list[Field] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    raw_fallback: str | None = None


# --- helpers -------------------------------------------------------------------


def _value_and_state(raw_line: str) -> tuple[str, bool, bool]:
    """Return (value, checked, selected) parsed from a node's raw line.

    Lines look like: `- textbox "Job Title": Founding Engineer`,
    `- checkbox "X" [checked]`, `- button "Degree Select One Required": Select One`.
    """
    s = raw_line.strip()
    if s.startswith("- "):
        s = s[2:]
    checked = "[checked]" in s
    selected = "[selected]" in s
    q1 = s.find('"')
    rest = s
    if q1 != -1:
        q2 = s.find('"', q1 + 1)
        rest = s[q2 + 1 :] if q2 != -1 else ""
    val = ""
    if ": " in rest:
        val = rest.split(": ", 1)[1].strip().strip('"')
    elif rest.endswith(":"):
        val = ""
    return val, checked, selected


def _required(label: str) -> bool:
    return bool(label and _REQ_RE.search(label))


def _dedupe_doubled(s: str) -> str:
    """ARIA names sometimes repeat the label ("*First Name *First Name"). Collapse."""
    half = len(s) // 2
    if len(s) % 2 == 1 and s[half] == " " and s[:half] == s[half + 1 :]:
        return s[:half]
    return s


def _clean_label(label: str) -> str:
    """Normalize whitespace, unify the heavy asterisk, drop doubled labels."""
    return _dedupe_doubled(re.sub(r"\s+", " ", label.replace("✱", "*")).strip())


def _option_children(node: TreeNode) -> list[TreeNode]:
    return [c for c in node.children if c.role == "option"]


def _text_of(n: TreeNode) -> str:
    if n.name:
        return n.name
    s = n.raw_line.strip()
    if s.startswith("- "):
        s = s[2:]
    if s.startswith("text:"):
        return s[5:].strip().strip('"')
    return ""


def _subtree_text(n: TreeNode, cap: int = 160) -> str:
    """All visible text under `n` (node names, `text:` nodes, inline values like a
    required `✱`), in document order, collapsed and capped. Used to read a label
    that lives nested inside a preceding sibling container, not as a bare sibling."""
    parts: list[str] = []
    for d in collect_all(n):
        t = _text_of(d) or _value_and_state(d.raw_line)[0]
        if t:
            parts.append(t)
    full = re.sub(r"\s+", " ", " ".join(parts)).strip()
    out = full[:cap]
    # The required ✱ is a trailing sibling node; on a long legal question it falls
    # past the cap. Re-attach it so the label stays detectable as required.
    if len(full) > cap and _REQ_RE.search(full) and not _REQ_RE.search(out):
        out = out.rstrip() + " ✱"
    return out


def _preceding_label(node: TreeNode, max_up: int = 2) -> str:
    """Label text from the nearest preceding sibling, walking up to `max_up` levels.

    Lever/Greenhouse/Workday put a field or choice group's question in a `text:`
    node — often nested inside a sibling container — BEFORE the field rather than
    naming the field itself. We read the full text of that preceding sibling.
    """
    cur = node
    for _ in range(max_up + 1):
        p = cur.parent
        if p is None:
            break
        try:
            idx = p.children.index(cur)
        except ValueError:
            idx = len(p.children)
        for j in range(idx - 1, -1, -1):
            t = _subtree_text(p.children[j])
            if t and len(t) > 1:
                return t
        cur = p
    return ""


def _label_for(node: TreeNode) -> str:
    return _clean_label(node.name or _preceding_label(node))


def _member_label(m: TreeNode) -> str:
    """Label for a radio/checkbox option — its name, else the following text sibling."""
    if m.name:
        return _clean_label(m.name)
    p = m.parent
    if p is not None:
        try:
            idx = p.children.index(m)
        except ValueError:
            idx = -1
        for j in range(idx + 1, len(p.children)):
            t = _text_of(p.children[j])
            if t:
                return _clean_label(t)
    return ""


def _choice_container(n: TreeNode) -> TreeNode | None:
    """Nearest ancestor that groups choices (group/radiogroup/list)."""
    anc = n.parent
    while anc is not None:
        if anc.role in ("group", "radiogroup", "list"):
            return anc
        anc = anc.parent
    return None


def _adopt_click_ref(n: TreeNode) -> str | None:
    """A clickable ref for a checkbox/radio that Playwright gave no ref of its own.

    Oracle/Taleo render an "I agree" control as a 0×0 hidden <input> wrapped in a
    `<label>`; the input is omitted from the snapshot's `[ref=…]` set, but the
    visible label renders as a sibling `[cursor=pointer]` node that DOES carry a
    ref. Adopt that label ref so the agent has something to click (the proxy
    redirects such clicks to toggle the underlying input — see server.py)."""
    p = n.parent
    if p is None:
        return None
    for sib in p.children:
        if sib is not n and sib.ref and "[cursor=pointer]" in sib.raw_line:
            return sib.ref
    if p.ref and "[cursor=pointer]" in p.raw_line:
        return p.ref
    return None


def _is_pill_tray(n: TreeNode) -> bool:
    """True if `n` is a Workday selected-values tray: a `listbox "items selected"`.
    When populated its option children are "…, press delete to clear value." pills;
    when empty it has no options (still a tray, an unfilled multiselect)."""
    if n.role != "listbox" or not _PILL_TRAY_RE.search(n.name or ""):
        return False
    opts = [c for c in n.children if c.role == "option"]
    return all(_PILL_RE.search(o.name or "") for o in opts)  # all([]) -> empty tray ok


def _pill_value(opt: TreeNode) -> str:
    """The selected value text of one pill option — its child paragraph if present
    (cleanest), else the option name with the pill suffix stripped."""
    for d in collect_all(opt):
        if d is not opt and d.role == "paragraph" and (d.name or _text_of(d)):
            return _clean_label(d.name or _text_of(d))
    return _clean_label(_PILL_RE.sub("", opt.name or ""))


def _multiselect_tray_fields(
    nodes: list[TreeNode], consumed: set[int]
) -> list[tuple[int, "Field"]]:
    """One dropdown Field per Workday multiselect widget, read from its pill tray.

    The widget is a `[cursor=pointer]` wrapper holding a search `textbox` (the click
    target) plus a `listbox "items selected"` tray of selected-value pills. Without
    this, the textbox renders as an empty `text` field and the tray as a bogus empty
    `items selected` dropdown — so a field that is ALREADY filled looks empty and the
    agent retries forever. We fold both into one filled dropdown field and consume
    them so pass 2 skips both."""
    out: list[tuple[int, Field]] = []
    for tray in nodes:
        if not _is_pill_tray(tray):
            continue
        wrapper = tray.parent
        if wrapper is None:
            continue
        # the click target: the search textbox/button beside the tray
        control = next(
            (
                d
                for d in collect_all(wrapper)
                if d is not tray and d.role in ("textbox", "searchbox", "button") and d.ref
            ),
            None,
        )
        value = ", ".join(
            _pill_value(o) for o in tray.children if o.role == "option"
        )
        preceding = _preceding_label(wrapper)
        label = _clean_label(
            (control.name if control and control.name else "") or preceding
        )
        if _required(preceding) and not _required(label):
            label = label + " *" if label else preceding
        ref = control.ref if control else tray.ref
        consumed.add(id(tray))
        if control is not None:
            consumed.add(id(control))
        out.append(
            (
                tray.line_index,
                Field(
                    ref=ref,
                    label=label or "(question)",
                    type="dropdown-aria",
                    required=_required(label),
                    value=value,
                ),
            )
        )
    return out


# --- field extraction ----------------------------------------------------------


def _extract_fields(nodes: list[TreeNode]) -> list[Field]:
    from collections import defaultdict

    consumed: set[int] = set()  # id(node) folded into a group field
    indexed: list[tuple[int, Field]] = []  # (line_index, field) for doc-order sort

    # Pass 1: choice groups. Group radios/checkboxes by their nearest choice
    # container so repeated-section checkboxes with identical labels (Workday's
    # "I currently work here") are NOT merged, while real Yes/No sets are.
    groups: dict[int, list[TreeNode]] = defaultdict(list)
    keynode: dict[int, TreeNode | None] = {}
    for n in nodes:
        if n.role in ("radio", "checkbox"):
            c = _choice_container(n)
            k = id(c) if c is not None else id(n)
            groups[k].append(n)
            keynode[k] = c

    for k, members in groups.items():
        radios = [m for m in members if m.role == "radio"]
        checks = [m for m in members if m.role == "checkbox"]
        labels = {_member_label(m) for m in checks}
        container = keynode[k]
        if len(radios) >= 2:
            use = radios
        elif len(checks) >= 2 and len(labels) == len(checks):
            use = checks  # distinct labels => single-select set (Yes/No)
        elif len(members) == 1 and container is not None and _required(
            _label_for(container)
        ):
            # Single-option acknowledgment inside a real choice container (Lever's
            # "I have read and understand…" radio under a required ✱ question). The
            # lone control names itself, so pass 2 would miss the required question
            # in the preceding sibling — group it here to inherit that label.
            use = members
        else:
            continue  # leave singles / identical-label checkboxes for pass 2
        for m in use:
            consumed.add(id(m))
        label = _label_for(container) if container is not None else _member_label(use[0])
        selected = ""
        for m in use:
            _, checked, _ = _value_and_state(m.raw_line)
            if checked:
                selected = _member_label(m)
        required = _required(label) or any(_required(_member_label(m)) for m in use)
        idx = min(m.line_index for m in use)
        indexed.append(
            (
                idx,
                Field(
                    ref=container.ref if container is not None else use[0].ref,
                    label=label or "(question)",
                    type="radio",
                    required=required,
                    value=selected,
                    options=[(m.ref, _member_label(m)) for m in use],
                ),
            )
        )

    # Pass 1b: Workday multiselect widgets (search box + selected-pills tray) — one
    # filled dropdown field each; consumes the box + tray so pass 2 skips both.
    indexed.extend(_multiselect_tray_fields(nodes, consumed))

    # Pass 2: individual inputs.
    for n in nodes:
        if id(n) in consumed:
            continue
        ftype = _classify(n)
        if ftype is None:
            continue
        val, checked, _ = _value_and_state(n.raw_line)
        ref = n.ref
        label = _label_for(n)
        required = _required(label)
        if ftype in ("checkbox", "radio"):
            # a standalone checkbox/radio (not part of a multi-option group)
            val = "checked" if checked else ""
            if ref is None:
                ref = _adopt_click_ref(n)
                # the required ✱ is a sibling marker on the wrapping label, not in
                # the control's own name — scan the parent row's nodes for it.
                if not required and n.parent is not None:
                    required = any(
                        _REQ_RE.search(d.raw_line) for d in collect_all(n.parent)
                    )
        opts: list[tuple[str | None, str]] = []
        if ftype == "select-native":
            for o in _option_children(n):
                opts.append((o.ref, _clean_label(o.name or "")))
                _, _, osel = _value_and_state(o.raw_line)
                if osel:
                    val = _clean_label(o.name or "")
        indexed.append(
            (
                n.line_index,
                Field(
                    ref=ref,
                    label=label,
                    type=ftype,
                    required=required,
                    value="" if _SELECT_ONE_RE.search(val) else val,
                    options=opts,
                ),
            )
        )

    indexed.sort(key=lambda t: t[0])
    return [f for _, f in indexed]


def _classify(n: TreeNode) -> str | None:
    """Map a node to a field type, or None if it is not an input."""
    role = n.role
    name = n.name or ""
    if role in _TEXT_ROLES:
        return "text"
    if role == "spinbutton":
        # Workday dates render as Month/Year spinbuttons under a From/To group.
        if n.parent is not None and re.search(r"\b(from|to|date)\b", n.parent.name or "", re.I):
            return "date"
        return "text"
    if role == "combobox":
        return "select-native" if _option_children(n) else "dropdown-aria"
    if role == "listbox":
        return "dropdown-aria"
    if role == "checkbox":
        return "checkbox"
    if role == "radio":
        return "radio"  # stray radio not inside a group
    if role == "button":
        # Workday ARIA dropdown: a button carrying a "Select One" value/name.
        val, _, _ = _value_and_state(n.raw_line)
        if _SELECT_ONE_RE.search(name) or _SELECT_ONE_RE.search(val):
            return "dropdown-aria"
    return None


# --- action + error extraction -------------------------------------------------


def _extract_actions(nodes: list[TreeNode]) -> list[Action]:
    actions: list[Action] = []
    seen: set[str] = set()
    for n in nodes:
        if n.role not in ("button", "link"):
            continue
        name = _clean_label(n.name or "")
        if not name or _CHROME_RE.search(name):
            continue
        kind = next((k for k, rx in _ACTION_KINDS if rx.search(name)), None)
        if kind is None:
            continue
        key = name.lower()[:60]
        if key in seen:
            continue
        seen.add(key)
        actions.append(Action(ref=n.ref, label=name[:80], kind=kind))
    return actions


def _extract_errors(nodes: list[TreeNode]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()

    def add(text: str) -> None:
        t = re.sub(r"\s+", " ", text).strip().lstrip("*").strip()
        if not t or len(t) > 200:
            return
        if _BOILERPLATE_RE.search(t):
            return
        # drop only PURE info (an info phrase with no validation signal)
        if _INFO_RE.search(t) and not _ERROR_FIELD_RE.search(t):
            return
        key = t.lower()
        if key not in seen:
            seen.add(key)
            errors.append(t)

    for n in nodes:
        # content can live in the name, the inline value (`alert: text`), or a
        # bare text node (`- text: The field X is required.`).
        content = n.name or _value_and_state(n.raw_line)[0] or _text_of(n)
        if not content:
            continue
        if n.role in ("alert", "status"):
            if _ERROR_FIELD_RE.search(content) or _ERROR_SUMMARY_RE.search(content):
                # may aggregate "Profile imported.The X field is required." — split
                # on sentence boundaries even without a space after the period.
                for part in re.split(r"(?<=[.!])\s*(?=[A-Z])", content):
                    if _ERROR_FIELD_RE.search(part) or _ERROR_SUMMARY_RE.search(part):
                        add(part)
            continue
        if _ERROR_SUMMARY_RE.search(content):
            add(content)
        elif content.startswith("Error") or "Error:" in content:
            add(re.sub(r"^error:?\s*", "", content, flags=re.I))
        elif (
            _STRONG_ERR_RE.search(content)
            and len(content) < 160
            and not _BOILERPLATE_RE.search(content)
            and n.role in ("paragraph", "text", "/text")
        ):
            add(content)

    # drop any error that is a strict substring of a longer one (keeps the full
    # "The field Degree is required..." over a bare "Degree").
    return [
        e for e in errors if not any(e != o and e.lower() in o.lower() for o in errors)
    ]


def _attach_field_errors(fields: list[Field], errors: list[str]) -> None:
    """Best-effort: link an error mentioning a field's label to that field."""
    for f in fields:
        toks = [w for w in re.findall(r"\w+", f.label.lower()) if len(w) > 3][:3]
        if not toks:
            continue
        for e in errors:
            el = e.lower()
            if all(t in el for t in toks):
                f.error = e
                break


# --- page classification -------------------------------------------------------


def _detect_step(nodes: list[TreeNode]) -> str | None:
    for n in nodes:
        m = _STEP_RE.search(n.raw_line)
        if m:
            name = m.group(3).strip()
            return f"step {m.group(1)} of {m.group(2)}" + (f": {name}" if name else "")
    return None


def _job_link_count(nodes: list[TreeNode]) -> int:
    return sum(
        1
        for n in nodes
        if n.role == "link" and n.is_url is False and re.search(r"\bjob\b", n.name or "", re.I)
    )


def extract(tree_text: str) -> PageModel:
    root = parse(tree_text)
    if root is None:
        return PageModel(page_type="loading")
    nodes = collect_all(root)
    if len(nodes) <= MIN_MEANINGFUL_NODES:
        return PageModel(page_type="loading")

    step = _detect_step(nodes)
    fields = _extract_fields(nodes)
    actions = _extract_actions(nodes)
    errors = _extract_errors(nodes)
    _attach_field_errors(fields, errors)

    all_text = " ".join((n.name or "") for n in nodes)
    has_password = any(
        f.type == "text" and _PASSWORD_RE.search(f.label) for f in fields
    )

    # classification order matters
    if _CONFIRM_RE.search(all_text) and not fields:
        ptype = "confirmation"
    elif has_password and len(fields) <= 6:
        ptype = "signin_signup"
    elif fields:
        ptype = "form"
    elif any(a.kind == "apply" for a in actions):
        ptype = "job_posting"
    elif _job_link_count(nodes) >= 5:
        ptype = "job_board"
    else:
        return PageModel(
            page_type="unknown",
            step=step,
            actions=actions,
            errors=errors,
            raw_fallback="\n".join(tree_text.splitlines()[:RAW_CAP]),
        )

    return PageModel(
        page_type=ptype, step=step, fields=fields, actions=actions, errors=errors
    )


# --- rendering -----------------------------------------------------------------

_TYPE_HINT = {
    "text": "text",
    "textarea": "textarea",
    "dropdown-aria": "dropdown(use select_options)",
    "select-native": "select(use select_option)",
    "radio": "radio(click option ref)",
    "checkbox": "checkbox(click ref)",
    "file": "file(open chooser+file_upload)",
    "date": "date",
    "unknown": "field",
}


def _ref(r: str | None) -> str:
    return f"[{r}]" if r else "[-]"


def render(model: PageModel) -> str:
    if model.page_type == "loading":
        return "[empty: page not loaded — call wait_for, then snapshot again]"
    if model.page_type == "unknown" and model.raw_fallback:
        head = "PAGE: unknown (structural extraction failed — raw tree below)\n"
        return head + model.raw_fallback

    lines: list[str] = []
    head = f"PAGE: {model.page_type}"
    if model.step:
        head += f" ({model.step})"
    lines.append(head)

    if model.errors:
        lines.append("ERRORS: " + " | ".join(model.errors))

    if model.fields:
        lines.append("FIELDS:")
        for f in model.fields:
            req = " *req" if f.required else ""
            hint = _TYPE_HINT.get(f.type, f.type)
            val = f.value if f.value else "(empty)"
            if len(val) > 80:  # keep long textarea values from bloating the model
                val = val[:80].rstrip() + "…"
            line = f"  {_ref(f.ref)} \"{f.label}\"{req} {hint} = {val}"
            if f.options:
                opts = "  ".join(f"{lbl}{_ref(r)}" for r, lbl in f.options)
                line += f"  options: {opts}"
            flag = ""
            if f.error:
                flag = f"  ⚠ {f.error}"
            elif f.required and f.empty:
                flag = "  ⚠ required, empty"
            lines.append(line + flag)

    if model.actions:
        acts = "  ".join(f"{_ref(a.ref)}\"{a.label}\"" for a in model.actions)
        lines.append("ACTIONS: " + acts)

    return "\n".join(lines)

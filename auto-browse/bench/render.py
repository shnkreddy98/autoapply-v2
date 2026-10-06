"""Site spec -> HTML. Markup follows the ATS the spec names (label wiring, ids,
data-automation-id, where the required star sits), because that is what the engines
key on; behaviour lives in static/runtime.js.

The page gets the field list without `expect`: the answer key never reaches the
browser, or an engine reading the DOM could copy it.

Markup knobs that copy real-site quirks (each spec's `evidence` says where it was seen):
  labels       for (default) | detached: the label is a sibling text node with no link
               to the control, so the control has no accessible name (Paylocity,
               Dayforce) | doubled: the name reads "*City *City" (UKG Pro) | heading:
               an <h3> before the control, text boxes named only by their placeholder,
               selects unnamed (Breezy)
  star         the required marker ("*", Lever's "✱"); star_before puts it first (UKG)
  option_aria  radio names that repeat the question: "{option}, {label}" (Eightfold)
  option_label detached: radios whose text sits outside any label, so each radio is
               named "" (Jobvite)
  step.shadow  fields and buttons inside open shadow roots (SmartRecruiters)
  flow.frame   the application inside a same-origin iframe (iCIMS)
  flow.button_tag  Next/Submit as a custom element with no button role (UKG <ukg-button>)
  flow.overlay a modal over the page until it is dismissed (cookie banners, notices)
  section.repeat   an Add / Save entry list: fields count only once saved
"""

from __future__ import annotations

import html
import json

WIDGET_KEYS = ("id", "widget", "label", "required", "show_if", "options", "tree", "display", "async", "single",
               "parts", "copies_to", "section", "placeholder")


def esc(text) -> str:
    return html.escape(str(text), quote=True)


def sections_of(spec: dict):
    """(step number, section number, section) in page order."""
    for n, step in enumerate(spec["steps"]):
        for k, section in enumerate(step["sections"]):
            yield n, k, section


def section_id(n: int, k: int) -> str:
    return f"s{n}-{k}"


def page_fields(spec: dict) -> list[dict]:
    out = []
    for n, k, section in sections_of(spec):
        for f in section["fields"]:
            extra = {"step": n}
            if section.get("repeat"):
                extra["section"] = section_id(n, k)
            out.append({kk: f[kk] for kk in WIDGET_KEYS if kk in f} | extra)
    return out


def repeat_sections(spec: dict) -> list[dict]:
    return [{"id": section_id(n, k), "step": n, "heading": s.get("heading", ""), "required": bool(s["repeat"].get("required")),
             "summary": s["repeat"].get("summary"), "noun": s["repeat"].get("noun", "entry")}
            for n, k, s in sections_of(spec) if s.get("repeat")]


class Style:
    """Per-site markup conventions, from the spec's top level (`labels`, `star`, ...)."""

    def __init__(self, spec: dict):
        self.name = spec["style"]
        self.labels = spec.get("labels", "for")
        self.star = spec.get("star", "*")
        self.star_before = bool(spec.get("star_before"))
        self.option_label = spec.get("option_label", "label")
        self.workday = self.name == "workday"


def _dom_id(f: dict) -> str:
    return f.get("dom_id") or f["id"]


def _star_html(st: Style) -> str:
    if st.workday:
        return '<abbr class="req" title="required">*</abbr>'
    return f'<span class="req">{esc(st.star)}</span>'


def _label_text(f: dict, st: Style) -> str:
    if not f.get("required"):
        return esc(f["label"])
    return (_star_html(st) + esc(f["label"])) if st.star_before else (esc(f["label"]) + _star_html(st))


def _plain_label(f: dict, st: Style) -> str:
    star = st.star if f.get("required") else ""
    return f"{star}{f['label']}" if st.star_before else f"{f['label']}{star}"


def _mode(f: dict, st: Style) -> str:
    return f.get("labels", st.labels)


def _label(f: dict, st: Style, *, for_control: bool = True) -> str:
    fid = _dom_id(f)
    if _mode(f, st) == "detached":
        return f'<div class="label-text">{_label_text(f, st)}</div>'
    if _mode(f, st) == "heading":
        return f'<h3 class="q-heading">{_label_text(f, st)}</h3>'
    target = f' for="{esc(fid)}"' if for_control else ""
    return f'<label id="{esc(fid)}-label"{target}>{_label_text(f, st)}</label>'


def _name_attrs(f: dict, st: Style) -> str:
    """How the control gets its accessible name, beyond <label for>."""
    fid, mode = _dom_id(f), _mode(f, st)
    if mode in ("detached", "heading"):
        return ""
    if mode == "doubled":
        return f' aria-labelledby="{esc(fid)}-label {esc(fid)}-label2"'
    if st.workday:
        return f' aria-labelledby="{esc(fid)}-label"'
    return ""


def _doubled(f: dict, st: Style) -> str:
    if _mode(f, st) != "doubled":
        return ""
    return f'<span id="{esc(_dom_id(f))}-label2" class="sr-only">{esc(_plain_label(f, st))}</span>'


def _aid(f: dict, st: Style) -> str:
    return f' data-automation-id="{esc(f.get("aid") or f["id"])}"' if st.workday else ""


def _required_attr(f: dict) -> str:
    return ' aria-required="true"' if f.get("required") else ""


def _hint(f: dict) -> str:
    return f'<p class="hint">{esc(f["hint"])}</p>' if f.get("hint") else ""


def _choices(f: dict, st: Style, kind: str) -> str:
    out = []
    for n, o in enumerate(f["options"]):
        oid = f"{_dom_id(f)}-{n}"
        aria = ""
        if f.get("option_aria"):
            aria = f' aria-label="{esc(f["option_aria"].format(option=o, label=f["label"]))}"'
        req = _required_attr(f) if kind == "radio" else ""
        box = f'<input type="{kind}" id="{esc(oid)}" name="{esc(f["id"])}" value="{esc(o)}"{aria}{req}>'
        if f.get("option_label", st.option_label) == "detached":
            out.append(f'<div class="choice">{box} <span class="choice-text">{esc(o)}</span></div>')
        else:
            out.append(f'<label class="choice" for="{esc(oid)}">{box} <span>{esc(o)}</span></label>')
    return "".join(out)


def render_field(f: dict, st: Style) -> str:
    w, fid = f["widget"], _dom_id(f)
    common = f'id="{esc(fid)}" name="{esc(f["id"])}"{_aid(f, st)}{_required_attr(f)}{_name_attrs(f, st)}'
    placeholder = f.get("placeholder") or (f["label"] if _mode(f, st) == "heading" else "")
    ph = f' placeholder="{esc(placeholder)}"' if placeholder else ""

    if w == "text":
        body = _label(f, st) + _hint(f) + f'<input type="{esc(f.get("input_type", "text"))}" {common}{ph}>'
    elif w == "textarea":
        body = _label(f, st) + _hint(f) + f'<textarea rows="4" {common}{ph}></textarea>'
    elif w == "select":
        blank = esc(f.get("blank_option", "Select..."))
        opts = "".join(f"<option>{esc(o)}</option>" for o in f["options"])
        body = _label(f, st) + _hint(f) + f'<select {common}><option value="">{blank}</option>{opts}</select>'
    elif w in ("radio", "checkbox_group"):
        kind = "radio" if w == "radio" else "checkbox"
        if _mode(f, st) in ("detached", "heading"):
            head = f'{_label(f, st)}{_hint(f)}'
            body = f'{head}<div role="radiogroup" id="{esc(fid)}" class="choices">{_choices(f, st, kind)}</div>' if kind == "radio" \
                else f'{head}<div id="{esc(fid)}" class="choices">{_choices(f, st, kind)}</div>'
        else:
            body = (f'<fieldset id="{esc(fid)}"{_aid(f, st)}><legend id="{esc(fid)}-label">{_label_text(f, st)}</legend>'
                    f'{_hint(f)}{_choices(f, st, kind)}</fieldset>')
    elif w == "checkbox":
        body = (f'<div class="single-check"><input type="checkbox" id="{esc(fid)}" name="{esc(f["id"])}"{_aid(f, st)}'
                f'{_required_attr(f)}> <label id="{esc(fid)}-label" for="{esc(fid)}">{_label_text(f, st)}</label></div>{_hint(f)}')
    elif w == "react_select":
        # Greenhouse job-boards react-select: the input is the combobox, the menu
        # renders outside .select__control, the shown value can be a slice of the option
        body = (_label(f, st) +
                f'<div class="select__container" data-rs="{esc(f["id"])}"><div class="select__control">'
                f'<div class="select__value-container"><div class="select__placeholder">Select...</div>'
                f'<div class="select__input-container"><input class="select__input" id="{esc(fid)}" role="combobox" type="text"'
                f' aria-expanded="false" aria-haspopup="true" aria-autocomplete="list" aria-labelledby="{esc(fid)}-label"'
                f'{_required_attr(f)} autocomplete="off" value=""></div></div>'
                f'<div class="select__indicators"><button type="button" class="select__toggle" tabindex="-1" aria-label="Toggle flyout">&#9662;</button></div>'
                f'</div></div>')
    elif w == "typeahead":
        # Ashby / Oracle / SmartRecruiters: a combobox you type into; only a pick counts
        named = _mode(f, st) == "for" and f.get("named", False)
        name = f' aria-labelledby="{esc(fid)}-label"' if named else ""
        body = (_label(f, st, for_control=False) +
                f'<div class="typeahead"><input type="text" role="combobox" id="{esc(fid)}" name="{esc(f["id"])}"'
                f' placeholder="{esc(f.get("placeholder", "Start typing..."))}" aria-expanded="false" aria-autocomplete="list"'
                f' autocomplete="off"{_required_attr(f)}{name}>'
                f'<button type="button" class="ta-toggle" tabindex="-1" aria-label="{esc(f.get("toggle_label", ""))}">&#9662;</button></div>')
    elif w == "yesno":
        # as live Ashby draws it (dailypay, 2026-10-05): the label's `for` names the
        # checkbox's name (it has no id), the star is CSS on a "required" class, and the
        # display:none checkbox is ticked only for Yes, so No reads like unanswered
        req = " _required" if f.get("required") else ""
        body = (f'<label class="yn-q{req}" for="{esc(f["id"])}">{esc(f["label"])}</label>' + _hint(f) +
                f'<div class="yesno" id="{esc(fid)}">'
                f'<button type="button" class="yn" aria-pressed="false" data-option="yes" data-v="Yes">Yes</button>'
                f'<button type="button" class="yn" aria-pressed="false" data-option="no" data-v="No">No</button>'
                f'<input type="checkbox" class="yn-input" tabindex="-1" name="{esc(f["id"])}"></div>')
    elif w == "buttons":
        # Oracle pill buttons: a group of toggle buttons, one pressed at a time
        items = "".join(f'<button type="button" class="pill" aria-pressed="false" data-v="{esc(o)}">{esc(o)}</button>'
                        for o in f["options"])
        body = (_label(f, st, for_control=False) + _hint(f) +
                f'<div class="pills" role="group" id="{esc(fid)}" aria-labelledby="{esc(fid)}-label">{items}</div>')
    elif w == "search_select":
        # iCIMS: the native <select> stays in the form, visually hidden, behind a
        # "— Make a Selection —" toggle that opens a "— Type to Search —" box
        opts = "".join(f"<option>{esc(o)}</option>" for o in f["options"])
        body = (_label(f, st) +
                f'<div class="ss"><select {common} class="visually-hidden ss-native" tabindex="-1">'
                f'<option value="">— Make a Selection —</option>{opts}</select>'
                f'<a href="#" role="combobox" class="ss-toggle" id="{esc(fid)}-toggle" aria-expanded="false" aria-haspopup="listbox"'
                f' aria-labelledby="{esc(fid)}-label">— Make a Selection —</a></div>')
    elif w == "multi_combo":
        # SmartRecruiters "Select all that apply": each pick adds a chip, the list stays open
        body = (_label(f, st, for_control=False) +
                f'<div class="mc"><ul class="mc-chips"></ul><input type="text" role="combobox" id="{esc(fid)}"'
                f' aria-labelledby="{esc(fid)}-label" aria-expanded="false" autocomplete="off"{_required_attr(f)}>'
                f'<button type="button" class="mc-clear" aria-label="Clear value">&times;</button></div>')
    elif w == "wd_prompt":
        req = " Required" if f.get("required") else ""
        body = (_label(f, st, for_control=False) +
                f'<button type="button" class="wd-prompt" id="{esc(fid)}"{_aid(f, st)} aria-haspopup="listbox" aria-expanded="false"'
                f' aria-label="{esc(f["label"])} Select One{req}">Select One</button>')
    elif w == "wd_multiselect":
        body = (_label(f, st, for_control=False) +
                f'<div class="wd-multi" id="{esc(fid)}-container" data-automation-id="multiSelectContainer">'
                f'<ul class="chips" data-automation-id="selectedItemList"></ul>'
                f'<input type="text" id="{esc(fid)}" data-automation-id="searchBox" placeholder="Search" autocomplete="off"'
                f' aria-labelledby="{esc(fid)}-label"{_required_attr(f)}></div>')
    elif w == "wd_date":
        segs = {"Month": ("MM", 2, "dateSectionMonth"), "Day": ("DD", 2, "dateSectionDay"), "Year": ("YYYY", 4, "dateSectionYear")}
        role = f.get("segment_role", "spinbutton")
        inputs = '<span class="sep">/</span>'.join(
            f'<input type="text" role="{role}" aria-label="{p}" placeholder="{segs[p][0]}" maxlength="{segs[p][1]}"'
            f' data-automation-id="{segs[p][2]}-input" data-part="{p}" inputmode="numeric" class="seg seg-{p.lower()}">'
            for p in f.get("parts", ["Month", "Year"]))
        body = (_label(f, st, for_control=False) +
                f'<div role="group" class="wd-date" id="{esc(fid)}"{_aid(f, st)} aria-labelledby="{esc(fid)}-label">{inputs}'
                f'<button type="button" class="cal" aria-label="{esc(f.get("calendar_label", "Calendar"))}" tabindex="-1">&#128197;</button></div>')
    elif w == "file":
        trigger = f.get("trigger")
        cls = ' class="visually-hidden"' if trigger else ""
        btn = f'<button type="button" class="file-trigger">{esc(trigger)}</button>' if trigger else ""
        body = (_label(f, st) + _hint(f) +
                f'<div class="file-box"><input type="file" {common}{cls} accept=".pdf,.doc,.docx,.txt,.rtf">{btn}'
                f'<span class="file-name"></span></div>')
    else:
        raise ValueError(f"unknown widget {w!r} on field {f['id']!r}")

    body += _doubled(f, st)
    hidden = ' style="display:none"' if f.get("show_if") else ""
    wrap_aid = f' data-automation-id="formField-{esc(f.get("aid") or f["id"])}"' if st.workday else ""
    inner = f'{body}<div class="field-error" aria-live="polite"></div>'
    if f.get("_shadow"):
        inner = f'<template shadowrootmode="open"><link rel="stylesheet" href="/static/bench.css"><div class="shadow-field">{inner}</div></template>'
    return f'<div class="field field-{w}" data-bench-field="{esc(f["id"])}" data-widget="{w}"{wrap_aid}{hidden}>{inner}</div>'


def render_section(section: dict, st: Style, sid: str, shadow: bool) -> str:
    out = [f'<section class="form-section" data-section="{sid}">']
    if section.get("heading"):
        out.append(f'<h3>{esc(section["heading"])}{_star_html(st) if section.get("repeat", {}).get("required") else ""}</h3>')
    if section.get("note"):
        out.append(f'<p class="note">{esc(section["note"])}</p>')
    groups: dict[str, list[str]] = {}
    body = []
    for f in section["fields"]:
        f = f | ({"_shadow": True} if shadow else {})
        if f.get("group"):
            if f["group"] not in groups:
                groups[f["group"]] = []
                body.append(f"@@group:{f['group']}@@")
            groups[f["group"]].append(render_field(f, st))
        else:
            body.append(render_field(f, st))
    inner = "".join(body)
    for name, parts in groups.items():
        inner = inner.replace(
            f"@@group:{name}@@",
            f'<div role="group" aria-label="{esc(name)}" class="field-group"><span class="group-label">{esc(name)}</span>{"".join(parts)}</div>')
    rep = section.get("repeat")
    if rep:
        # entries are added with a button and only count once saved (SmartRecruiters
        # Experience, Workday Add): the subform is not in view until Add is pressed
        inner = (f'<div class="repeat-list"></div>'
                 f'<button type="button" class="btn-add" data-section="{sid}">{esc(rep.get("add", "Add"))}</button>'
                 f'<div class="repeat-form" style="display:none">{inner}'
                 f'<div class="repeat-actions"><button type="button" class="btn-cancel">{esc(rep.get("cancel", "Cancel"))}</button>'
                 f'<button type="button" class="btn-save">{esc(rep.get("save", "Save"))}</button></div></div>'
                 f'<div class="field-error repeat-error" aria-live="polite"></div>')
    out.append(inner)
    return "".join(out) + "</section>"


def _shell(spec: dict, title: str, body: str, boot: dict | None) -> str:
    boot_js = f"<script>window.__BENCH = {json.dumps(boot)};</script><script src=\"/static/runtime.js\"></script>" if boot else ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{esc(title)}</title><link rel="stylesheet" href="/static/bench.css"></head>'
            f'<body class="ats-{esc(spec["style"])}">{body}{boot_js}</body></html>')


def _description(spec: dict) -> str:
    paras = "".join(f"<p>{esc(p)}</p>" for p in spec["job"]["description"].split("\n\n"))
    return f'<div class="job-description">{paras}</div>'


def _job_header(spec: dict) -> str:
    job = spec["job"]
    return (f'<header class="job-header"><div class="company">{esc(spec["company"])}</div>'
            f'<h1>{esc(job["title"])}</h1><div class="job-location">{esc(job["location"])}</div></header>')


def boot_data(spec: dict, base: str) -> dict:
    return {
        "site": spec["site"], "style": spec["style"], "base": base,
        "fields": page_fields(spec), "repeats": repeat_sections(spec),
        "steps": [s["title"] for s in spec["steps"]],
        "next": spec.get("next", "Next"), "submit": spec["submit"], "confirmation": spec["confirmation"],
        "overlay": (spec["flow"].get("overlay") or None),
    }


def _button(spec: dict, text: str, cls: str, aid: str, shadow: bool) -> str:
    tag = spec["flow"].get("button_tag")
    if shadow:
        return (f'<spl-button class="{cls}"><template shadowrootmode="open"><link rel="stylesheet" href="/static/bench.css">'
                f'<button type="button" class="{cls.split()[0]}">{esc(text)}</button></template></spl-button>')
    if tag:
        # UKG Pro's <ukg-button>: a custom element, focusable, with no button role
        return f'<{tag} class="{cls}" tabindex="0">{esc(text)}</{tag}>'
    return f'<button type="button" class="{cls}" data-automation-id="{aid}">{esc(text)}</button>'


def form_html(spec: dict, base: str) -> str:
    steps = spec["steps"]
    st = Style(spec)
    parts = []
    multi = len(steps) > 1
    if multi:
        crumbs = "".join(f'<li data-step="{n}">{esc(s["title"])}</li>' for n, s in enumerate(steps))
        parts.append(f'<div class="progress" data-automation-id="progressBar"><ol>{crumbs}</ol>'
                     f'<span class="step-count" data-automation-id="progressBarActiveStep">current step 1 of {len(steps)}</span></div>')
    parts.append('<div class="error-banner" role="alert" style="display:none" data-automation-id="errorBanner"></div>')
    step_divs = []
    for n, step in enumerate(steps):
        on = " on" if n == 0 else ""
        shadow = bool(step.get("shadow"))
        heading = f'<h2 class="step-title">{esc(step["title"])}</h2>' if multi else ""
        req_note = f'<p class="req-note">{esc(spec.get("required_note", "* Indicates a required field"))}</p>' if spec.get("required_note") or st.workday else ""
        sections = "".join(render_section(s, st, section_id(n, k), shadow) for k, s in enumerate(step["sections"]))
        last = n == len(steps) - 1
        buttons = []
        if multi and n > 0:
            buttons.append(_button(spec, spec.get("back", "Back"), "btn-back", "pageFooterBackButton", shadow))
        if last:
            buttons.append(_button(spec, spec["submit"], "btn-primary bench-submit", "pageFooterNextButton", shadow))
        else:
            buttons.append(_button(spec, step.get("next", spec.get("next", "Next")), "btn-primary bench-next", "pageFooterNextButton", shadow))
        step_divs.append(f'<div class="step{on}" data-step="{n}">{heading}{req_note}{sections}'
                         f'<div class="form-footer">{"".join(buttons)}</div></div>')
    # runtime.js keeps only the current step in the DOM, as Workday does
    parts.append(f'<div class="steps">{"".join(step_divs)}</div>')
    return f'<form id="application-form" novalidate onsubmit="return false">{"".join(parts)}</form>'


def _apply_control(spec: dict, base: str) -> str:
    flow = spec["flow"]
    if flow.get("chooser"):
        choices = "".join(
            f'<a class="btn-choice" href="{esc(base)}/apply" data-automation-id="{esc(c.replace(" ", "").lower())}">{esc(c)}</a>'
            for c in flow["chooser"])
        title = flow.get("chooser_title", "Start Your Application")
        return (f'<button type="button" class="btn-primary" data-automation-id="adventureButton"'
                f' onclick="document.getElementById(\'chooser\').style.display=\'flex\'">{esc(flow["apply_button"])}</button>'
                f'<div id="chooser" class="modal-wrap" style="display:none"><div role="dialog" aria-modal="true" aria-labelledby="chooser-title" class="modal">'
                f'<h2 id="chooser-title">{esc(title)}</h2>{flow.get("chooser_note") and "<p>" + esc(flow["chooser_note"]) + "</p>" or ""}{choices}'
                f'<button type="button" class="modal-close" aria-label="Close" onclick="document.getElementById(\'chooser\').style.display=\'none\'">&times;</button>'
                f'</div></div>')
    tag = flow.get("button_tag")
    if tag:
        return f'<{tag} class="btn-primary" tabindex="0" onclick="location.href=\'{esc(base)}/apply\'">{esc(flow["apply_button"])}</{tag}>'
    if flow.get("apply_as") == "button":
        return f'<button type="button" class="btn-primary" onclick="location.href=\'{esc(base)}/apply\'">{esc(flow["apply_button"])}</button>'
    return f'<a class="btn-primary" href="{esc(base)}/apply" role="button">{esc(flow["apply_button"])}</a>'


def posting_page(spec: dict, base: str) -> str:
    """The job posting. same_page (Greenhouse, JazzHR) carries the form under the
    posting; separate postings link to /apply, optionally through a chooser dialog."""
    flow, style = spec["flow"], spec["style"]
    if flow["posting"] == "same_page":
        body = (f'<main class="posting">{_job_header(spec)}'
                f'<button type="button" class="btn-primary apply-scroll" onclick="document.getElementById(\'application\').scrollIntoView()">{esc(flow["apply_button"])}</button>'
                f'{_description(spec)}<div id="application"><h2>{esc(flow.get("form_heading", "Apply for this job"))}</h2>'
                f'<p class="req-note">* indicates a required field</p>{form_html(spec, base)}</div></main>')
        return _shell(spec, f'Job Application for {spec["job"]["title"]} at {spec["company"]}', body, boot_data(spec, base))
    tabs = ""
    if style == "ashby":
        tabs = (f'<div role="tablist" class="tabs"><a role="tab" aria-selected="true" href="{esc(base)}/job">Overview</a>'
                f'<a role="tab" aria-selected="false" href="{esc(base)}/apply">Application</a></div>')
    body = f'<main class="posting">{_job_header(spec)}{tabs}{_description(spec)}<div class="apply-bar">{_apply_control(spec, base)}</div></main>'
    return _shell(spec, f'{spec["job"]["title"]} - {spec["company"]}', body, None)


def application_page(spec: dict, base: str, *, in_frame: bool = False) -> str:
    """The application. With flow.frame (iCIMS) the page is a shell around an iframe
    whose document (in_frame) holds the form and the runtime."""
    flow = spec["flow"]
    if flow.get("frame") and not in_frame:
        body = (f'<main class="application">{_job_header(spec)}'
                f'<iframe id="icims_content_iframe" name="icims_content_iframe" title="Job application" src="{esc(base)}/apply?in_iframe=1"'
                f' style="width:100%;height:2400px;border:0"></iframe></main>')
        return _shell(spec, f'Apply - {spec["job"]["title"]} - {spec["company"]}', body, None)
    tabs = ""
    if spec["style"] == "ashby":
        tabs = (f'<div role="tablist" class="tabs"><a role="tab" aria-selected="false" href="{esc(base)}/job">Overview</a>'
                f'<a role="tab" aria-selected="true" href="{esc(base)}/apply">Application</a></div>')
    header = "" if in_frame else _job_header(spec)
    body = f'<main class="application">{header}{tabs}{form_html(spec, base)}</main>'
    return _shell(spec, f'Apply - {spec["job"]["title"]} - {spec["company"]}', body, boot_data(spec, base))

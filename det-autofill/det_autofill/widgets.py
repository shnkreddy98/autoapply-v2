"""Fill strategies per ARIA role. Every strategy returns the page's new
snapshot text so the caller can re-locate fields (refs go stale after any
mutation — hard rule from the traces).

All strategies take the CURRENT field object (fresh refs) + the resolved
Answer, act via mcpio.Browser, and report what they did.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from det_autofill.answers import NO, OPTION_TEXT, YES, Answer
from det_autofill.aria import FormField, parse_snapshot
from det_autofill.ats import ATSQuirks
from det_autofill.mcpio import Browser


@dataclass
class FillResult:
    ok: bool
    note: str
    snapshot: str  # page text after the last action ("" = page untouched)


# profiles store 2-letter codes; option lists (Workday "Region") spell them out
US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}


def _pick_option(options: list[str], answer: Answer) -> str | None:
    """Choose the option wording for an answer: preference regexes for kind
    'option', yes/no wording for bools, case-insensitive contains for text."""
    if not options:
        return None
    if answer.kind == "option":
        for pref in answer.value:
            for o in options:
                if re.search(pref, o, re.I):
                    return o
    elif answer.kind == "bool":
        want = YES if answer.value else NO
        for o in options:
            if re.search(want, o.strip(), re.I):
                return o
    else:
        val = str(answer.value).strip().lower()
        if not val:
            return None
        full = US_STATES.get(val.upper())
        if full:
            val = full.lower()
        for o in options:
            if val == o.strip().lower():
                return o
        for o in options:
            if val in o.lower() or o.strip().lower() in val:
                return o
    return None


async def fill_text(br: Browser, f: FormField, answer: Answer) -> FillResult:
    text = str(answer.value)
    if not text:
        return FillResult(True, "blank-ok", "")
    snap = await br.type(f.ref, text, element=f.label[:60])
    return FillResult(True, f"typed {text[:30]!r}", snap)


async def fill_checkbox(br: Browser, f: FormField, answer: Answer) -> FillResult:
    want = bool(answer.value)
    if f.checked is want:
        return FillResult(True, "already-set", "")
    if not want and f.checked in (None, False):
        return FillResult(True, "leave-unchecked", "")
    snap = await br.click(f.ref, element=f.label[:60])
    return FillResult(True, "checked" if want else "unchecked", snap)


async def fill_radio(
    br: Browser, f: FormField, answer: Answer, group: list[FormField]
) -> FillResult:
    """`group` = radios sharing this field's question context (fresh refs)."""
    labels = [g.label for g in group]
    pick = _pick_option(labels, answer)
    if pick is None:
        return FillResult(False, f"no radio matches answer in {labels[:6]}", "")
    target = next(g for g in group if g.label == pick)
    if target.checked:
        return FillResult(True, "already-selected", "")
    snap = await br.click(target.ref, element=pick[:60])
    return FillResult(True, f"picked {pick!r}", snap)


async def fill_combobox(
    br: Browser, f: FormField, answer: Answer, quirks: ATSQuirks
) -> FillResult:
    """The hard one. Strategy per ATS; every path re-reads the page after
    acting and confirms the field shows a value."""
    if quirks.combobox == "typeahead":
        # Type-ahead only suits literal text (city, school, ...). Enumerated
        # answers (option/bool) carry preference REGEXES — typing those is
        # garbage; open the list and pick from the real option wording.
        if answer.kind in ("option", "bool"):
            return await _combobox_click(br, f, answer)
        return await _combobox_typeahead(br, f, answer)
    if quirks.combobox == "click":
        return await _combobox_click(br, f, answer)
    # auto: native select first, then click-open, then typeahead
    pick = _pick_option(f.options, answer)
    if pick is not None:
        try:
            snap = await br.select_option(f.ref, [pick], element=f.label[:60])
            return FillResult(True, f"selected {pick!r}", snap)
        except Exception:
            pass
    res = await _combobox_click(br, f, answer)
    if res.ok:
        return res
    return await _combobox_typeahead(br, f, answer)


def _combobox_value_shown(snap: str, label: str, expected: str) -> bool:
    """Loose verify: some line near the label mentions the chosen value, or the
    ATS's 'N item(s) selected' marker appears."""
    low = snap.lower()
    return expected.lower()[:25] in low or "item selected" in low


async def _combobox_typeahead(br: Browser, f: FormField, answer: Answer) -> FillResult:
    """Greenhouse/Ashby: type the value, ArrowDown, Enter. Never click options."""
    value = _answer_text_for_combobox(f, answer)
    if not value:
        return FillResult(False, "no value to type", "")
    await br.type(f.ref, value, element=f.label[:60])
    await br.press("ArrowDown")
    snap = await br.press("Enter")
    ok = _combobox_value_shown(snap, f.label, value)
    return FillResult(ok, f"typeahead {value!r}" + ("" if ok else " (unverified)"), snap)


async def _combobox_click(br: Browser, f: FormField, answer: Answer) -> FillResult:
    """Workday-style: click to open, click the matching option ref. One level of
    cascade: if the click revealed more options instead of committing, pick again."""
    snap = await br.click(f.ref, element=f.label[:60])
    for _hop in range(3):  # initial pick + up to 2 cascade levels
        fields = parse_snapshot(snap)
        options = [x for x in fields if x.role == "option"]
        if not options:
            return FillResult(False, "no options rendered", snap)
        pick = _pick_option([o.label for o in options], answer)
        if pick is None:
            return FillResult(False, f"no option match ({len(options)} options)", snap)
        target = next(o for o in options if o.label == pick)
        snap = await br.click(target.ref, element=pick[:60])
        if _combobox_value_shown(snap, f.label, pick) and not _still_expanded(
            snap, f.label
        ):
            return FillResult(True, f"clicked {pick!r}", snap)
        # else: cascade — options re-rendered, loop picks at the next level
    return FillResult(False, "cascade did not commit", snap)


async def fill_search_multiselect(
    br: Browser, f: FormField, answer: Answer, quirks: ATSQuirks
) -> FillResult:
    """Workday multiselect search textbox: typed text does NOT commit — options
    render async below the box and one must be clicked."""
    value = OPTION_TEXT.get(answer.key) or _answer_text_for_combobox(f, answer)
    if not value:
        return FillResult(False, "no value to type", "")
    # options already on the page (committed chips like "…press delete to clear
    # value") are not dropdown choices — only options that APPEAR after typing are
    base = {x.label for x in parse_snapshot(await br.snapshot()) if x.role == "option"}
    snap = await br.type(f.ref, value, element=f.label[:60], slowly=True)

    def fresh(s: str) -> list[FormField]:
        return [
            x for x in parse_snapshot(s) if x.role == "option" and x.label not in base
        ]

    for _hop in range(3):  # first pick + category cascade
        options = fresh(snap)
        if not options:
            await asyncio.sleep(2)
            snap = await br.snapshot()
            options = fresh(snap)
        if not options:  # some boxes only open the list on a key navigation
            snap = await br.press("ArrowDown")
            await asyncio.sleep(1)
            snap = await br.snapshot()
            options = fresh(snap)
        if not options:
            return FillResult(False, f"no options after typing {value!r}", snap)
        pick = _pick_option([o.label for o in options], answer)
        if pick is None:
            return FillResult(False, f"no option match ({len(options)} options)", snap)
        target = next(o for o in options if o.label == pick)
        snap = await br.click(target.ref, element=pick[:60])
        if _combobox_value_shown(snap, f.label, pick):
            return FillResult(True, f"picked {pick!r}", snap)
    return FillResult(False, "pick did not commit", snap)


def _still_expanded(snap: str, label: str) -> bool:
    for line in snap.splitlines():
        if label[:30] in line and "[expanded]" in line:
            return True
    return False


def _answer_text_for_combobox(f: FormField, answer: Answer) -> str:
    if answer.kind == "bool":
        return "Yes" if answer.value else "No"
    if answer.kind == "option":
        # typeahead needs literal text: use the first preference stripped of regex
        return re.sub(r"[\^\$\\\|\(\)\[\]\.\*\+\?]", " ", answer.value[0]).split("|")[0].strip()
    return str(answer.value)


async def fill_date(
    br: Browser, f: FormField, answer: Answer, quirks: ATSQuirks
) -> FillResult:
    from datetime import date

    v = answer.value
    if not v:
        return FillResult(True, "blank-ok", "")
    try:
        d = date.fromisoformat(str(v))
        text = d.strftime(quirks.date_format)
    except ValueError:
        text = str(v)
    snap = await br.type(f.ref, text, element=f.label[:60])
    return FillResult(True, f"typed date {text}", snap)


async def upload_resume(br: Browser, f: FormField, answer: Answer) -> FillResult:
    """Attach control first, then the modal upload. A blank page after the click
    is EXPECTED (native chooser open) — proceed straight to upload.

    Verified by the filename appearing in the page afterwards: a direct
    browser_file_upload can report success without the ATS registering the file
    (Workday), so an unverified direct path falls through to click-then-upload."""
    path = str(answer.value)
    if not path:
        return FillResult(False, "no resume path", "")
    fname = path.rsplit("/", 1)[-1].lower()
    if fname in (await br.snapshot()).lower():
        return FillResult(True, "already-attached", "")
    try:
        snap = await br.upload([path])  # a chooser may already be armed
        if fname in snap.lower():
            return FillResult(True, "uploaded (direct)", snap)
    except Exception:
        pass
    await br.click(f.ref, element=f.label[:60])
    snap = await br.upload([path])
    ok = fname in snap.lower()
    return FillResult(ok, "uploaded (via attach click)" + ("" if ok else " — file not shown"), snap)

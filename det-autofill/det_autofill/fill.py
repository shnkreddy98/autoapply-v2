"""Execute a plan row against a live page — one strategy per widget, every write
verified by reading the control back.

Drivers: anything with the `Driver` methods. `PlaywrightDriver` wraps a Playwright
page (tests, local runs); `McpDriver` sends the same actions to Playwright MCP with
CSS selectors as targets (the container browser). Both are real browser input, so
react-select opens on a plain click — the React-prop and synthetic-event tricks
browser extensions need do not apply here.

Strategies by widget (behaviour observed on live forms, 2026-09-13):

  select     choose via the option ladder, select by value, read back selected text
  radio      click the matched member, read back :checked
  checkbox   click only when the state differs, read back
  combobox   open by click; find the listbox through aria-controls (set only while
             open), a Workday wd-popup, or the only visible listbox; pick from the
             rendered options, else type search terms and pick from the filtered
             ones (long lists render only a slice until typed into); follow nested
             categories up to 3 levels; committed when the menu is closed and the
             control shows a value compatible with the option (Greenhouse shows
             "+1" for "United States +1")
  search     Workday multiselect: type, Enter, pick, committed when a chip appears
  text       type, read back; phone digits and maxlength variants
  file       upload, confirmed by the input's file list or the file name on page

A failure returns the reason and leaves the menu closed; nothing is guessed.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from det_autofill.answers import AnswerStore
from det_autofill.options import Choice, hispanic_only, match_option, nv, text_forms

MAX_HOPS = 3  # nested prompt levels ("Job Board" -> "LinkedIn")
LONG_LIST = 50  # at or above this, a rendered list is likely a virtualised slice


class Driver(Protocol):
    async def click(self, selector: str) -> None: ...
    async def type(self, selector: str, text: str, *, slowly: bool = False) -> None: ...
    async def press(self, key: str) -> None: ...
    async def select_option(self, selector: str, value: str) -> None: ...
    async def evaluate(self, js: str, arg: Any = None) -> Any: ...
    async def upload(self, selector: str, paths: list[str]) -> None: ...
    async def wait(self, ms: int) -> None: ...


class CaptchaCover(RuntimeError):
    """A captcha challenge sits over the form (Lever's hCaptcha opens full-viewport
    mid-fill): the run hands off instead of recording every widget as broken."""


@dataclass
class FillResult:
    ok: bool
    how: str
    wrote: str | None = None


# ---------------------------------------------------------------------------
# in-page probes (read-only)

_SELECTOR_FOR = r"""
  const esc = (s) => CSS.escape(s);
  const unique = (sel) => { try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; } };
  function selectorFor(el) {
    if (el.id && unique('#' + esc(el.id))) return '#' + esc(el.id);
    const parts = [];
    for (let node = el; node && node.nodeType === 1 && node !== document.documentElement; node = node.parentElement) {
      if (node !== el && node.id && unique('#' + esc(node.id))) { parts.unshift('#' + esc(node.id)); break; }
      let part = node.tagName.toLowerCase();
      const parent = node.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter((c) => c.tagName === node.tagName);
        if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
      }
      parts.unshift(part);
      if (unique(parts.join(' > '))) break;
    }
    return parts.join(' > ');
  }
"""

READ_FIELD = r"""(selector) => {
  const el = document.querySelector(selector);
  if (!el) return JSON.stringify({ missing: true });
  const text = (n) => (n ? (n.innerText || n.textContent || '') : '').replace(/\s+/g, ' ').trim();
  // Walk up to the first ancestor that holds a chosen value, stopping before one that
  // also holds another dropdown: live react-select wraps the input in its own
  // "select__input-container", so the nearest "container" is not the right scope.
  let single = null;
  for (let n = el.parentElement, d = 0; n && d < 6; n = n.parentElement, d++) {
    if (Array.from(n.querySelectorAll('[role="combobox"], select')).some((c) => c !== el)) break;
    single = n.querySelector('[class*="singleValue" i], [class*="single-value" i], [data-automation-id="selectedItem"]');
    if (single) break;
  }
  const field = el.closest('[data-automation-id^="formField-"], [data-automation-id="multiSelectContainer"]');
  const chips = field ? Array.from(field.querySelectorAll('[data-automation-id="selectedItemList"] li')).map(text) : [];
  return JSON.stringify({
    tag: el.tagName.toLowerCase(),
    value: el.tagName === 'SELECT' ? text(el.options[el.selectedIndex]) : (el.value || ''),
    // react-select shows the pick in a sibling; buttons and div-based comboboxes in
    // their own text (inputs have none)
    display: single ? text(single) : (/^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName) ? '' : text(el)),
    chips,
    checked: !!(el.checked || el.getAttribute('aria-checked') === 'true' || /--checked\b/i.test(String(el.className))),
    expanded: el.getAttribute('aria-expanded') === 'true',
    files: el.files ? Array.from(el.files).map((f) => f.name) : [],
    invalid: el.getAttribute('aria-invalid') === 'true',
  });
}"""

LIST_OPTIONS = "(selector) => {" + _SELECTOR_FOR + r"""
  const el = document.querySelector(selector);
  if (!el) return JSON.stringify({ listbox: false, options: [] });
  const visible = (n) => { const r = n.getBoundingClientRect(); const s = getComputedStyle(n); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  let lb = null;
  const ctl = el.getAttribute('aria-controls') || el.getAttribute('aria-owns');
  if (ctl) lb = document.getElementById(ctl);
  if (!lb) {
    const field = el.closest('[data-automation-id^="formField-"], [data-automation-id="multiSelectContainer"]');
    const ids = [el.id, field && field.id, el.closest('[data-automation-id="multiSelectContainer"]') && el.closest('[data-automation-id="multiSelectContainer"]').id].filter(Boolean);
    for (const id of ids) {
      const popup = document.querySelector('[data-automation-widget="wd-popup"][data-associated-widget="' + CSS.escape(id) + '"]');
      if (popup) { lb = popup; break; }
    }
  }
  if (!lb) {
    const open = Array.from(document.querySelectorAll('[role="listbox"]')).filter(visible);
    if (open.length === 1) lb = open[0];
  }
  if (!lb) return JSON.stringify({ listbox: false, options: [] });
  // menuitem: BambooHR's select toggles open a role=menu
  const nodes = Array.from(lb.querySelectorAll('[role="option"], [role="menuitem"], [role="menuitemradio"], [data-automation-id="promptOption"]'))
    .filter((n, i, all) => all.indexOf(n.closest('[role="option"]') || n) === i || !n.closest('[role="option"]'));
  const options = nodes.filter((n) => n.getAttribute('aria-disabled') !== 'true').map((n) => ({
    text: (n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim(),
    value: n.getAttribute('data-value') || n.getAttribute('value') || '',
    selector: selectorFor(n),
    nested: n.getAttribute('aria-haspopup') === 'true' || !!n.querySelector('[data-automation-id="promptLeafNode"] + *, [class*="chevron" i]'),
  }));
  return JSON.stringify({ listbox: true, options });
}"""

# The visible control that opens a file input's chooser: its <label for>, or an
# Attach/Upload/Choose button inside the same field. '' when there is none.
FILE_TRIGGER = "(selector) => {" + _SELECTOR_FOR + r"""
  const input = document.querySelector(selector);
  if (!input) return '';
  const visible = (n) => { const r = n.getBoundingClientRect(); const s = getComputedStyle(n); return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none'; };
  // Jobvite: the resume field is a "Select" menu button; once open, its "File" item
  // (a <label for> a file input parked under <body>) opens the chooser
  if (input.tagName !== 'INPUT') {
    if (input.getAttribute('aria-expanded') !== 'true') return selectorFor(input);
    const item = Array.from(document.querySelectorAll('label[for], [role=menuitem], button, a, li'))
      .find((n) => visible(n) && n !== input && /^(file|my (computer|device)|(upload )?from (my )?(computer|device)|upload( a)? file|browse|attach( a)? file)$/i.test((n.innerText || '').trim()));
    return item ? selectorFor(item) : '';
  }
  const words = /attach|upload|choose file|select file|browse|add file|drag and drop|drop (a |your )?file/i;
  // Recruitee lays the real input over its drop zone at opacity 0: the input itself
  // is the click target, and no button with upload wording exists outside it
  const box = input.getBoundingClientRect();
  if (box.width > 8 && box.height > 8 && getComputedStyle(input).visibility !== 'hidden' && getComputedStyle(input).display !== 'none') return selectorFor(input);
  if (input.id) {
    const lbl = document.querySelector('label[for="' + CSS.escape(input.id) + '"]');
    if (lbl && visible(lbl) && words.test(lbl.textContent || '')) return selectorFor(lbl);
  }
  for (let scope = input.parentElement, d = 0; scope && d < 5; scope = scope.parentElement, d++) {
    if (scope.querySelectorAll('input[type=file]').length > 1) break;  // never another field's button
    const btn = Array.from(scope.querySelectorAll('button, [role=button], label, a')).find((b) => visible(b) && words.test(b.textContent || b.getAttribute('aria-label') || ''));
    if (btn) return selectorFor(btn);
  }
  return '';
}"""

# Cookie / consent banners cover the bottom of the page and swallow clicks meant for
# the form (a live Lever page's banner made radios and checkboxes "not take").
# Only an explicit accept/close button inside a container marked as such is clicked.
OVERLAY_JS = r"""() => {
  const visible = (n) => { const r = n.getBoundingClientRect(); const s = getComputedStyle(n); return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const words = /^(accept( all)?( cookies)?|allow( all)?( cookies)?|i agree|agree|got it|ok(ay)?|dismiss|close)[!.]?$/i;
  const marked = Array.from(document.querySelectorAll('[id*="cookie" i], [class*="cookie" i], [id*="consent" i], [class*="consent" i], [id*="onetrust" i], [class*="gdpr" i], [aria-label*="cookie" i], [class*="cc-window" i]'));
  // Lever's banner is "div.cc-window.cc-banner": nothing says cookie but its text, so
  // fixed/sticky boxes that talk about cookies count too
  const floating = Array.from(document.querySelectorAll('body div, body section, body aside'))
    .filter((n) => /fixed|sticky/.test(getComputedStyle(n).position) && /cookie/i.test((n.innerText || '').slice(0, 400)));
  // OneTrust's preference centre dims the page (#onetrust-pc-dark-filter, no buttons of
  // its own) and names its buttons by id; ADP opened it over the whole posting
  const known = Array.from(document.querySelectorAll('#onetrust-accept-btn-handler, #accept-recommended-btn-handler, .save-preference-btn-handler, .onetrust-close-btn-handler')).find(visible);
  if (known) { known.setAttribute('data-det-overlay', '1'); return true; }
  // Eightfold opens a "Data Privacy Agreement" dialog after the resume upload and
  // blocks the form until "I Agree" — the same terms the application's own consent
  // box asks for
  const agreements = Array.from(document.querySelectorAll('[role=dialog], [aria-modal="true"], dialog[open]'))
    .filter((n) => /privacy (statement|agreement|policy|notice)|terms (of use|and conditions)|data (privacy|protection)/i.test((n.innerText || '').slice(0, 600)));
  const boxes = marked.concat(floating, agreements).filter((n) => visible(n) && !(n.closest('form') && !n.matches('[role=dialog], [aria-modal="true"], dialog')));
  for (const box of boxes) {
    // "I Agree" over the dialog's "Close" (Eightfold lists Close first)
    const label = (b) => (b.innerText || b.getAttribute('aria-label') || '').trim();
    const btn = Array.from(box.querySelectorAll('button, [role=button], a'))
      .filter((b) => visible(b) && words.test(label(b)))
      .sort((x, y) => /^(close|dismiss)/i.test(label(x)) - /^(close|dismiss)/i.test(label(y)))[0];
    if (btn) { btn.setAttribute('data-det-overlay', '1'); return true; }
  }
  return false;
}"""


# Click through the page's own hit test: only when the point at the control's centre
# resolves to the control (or its label); otherwise say what covers it.
HIT_CLICK = "({selector}) => {" + _SELECTOR_FOR + r"""
  const el = document.querySelector(selector);
  if (!el) return 'missing';
  el.scrollIntoView({block: 'center', inline: 'nearest'});
  const r = el.getBoundingClientRect();
  const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
  const label = el.closest('label') || (el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null);
  if (!top || !(top === el || el.contains(top) || (label && label.contains(top)))) {
    // a challenge the page raised is the human's to solve, never to click around
    if (top && top.tagName === 'IFRAME' && /captcha|challenge/i.test(`${top.title} ${top.src}`)) return `captcha: ${top.title.slice(0, 60)} (visibility ${getComputedStyle(top).visibility})`;
    // Eightfold floats a <p> label over its combobox inputs: a cover inside the
    // control's own field (no other control there) is part of the widget, and a real
    // click on it is what a user's click does
    for (let box = el.parentElement, d = 0; top && box && d < 4; box = box.parentElement, d++) {
      const others = Array.from(box.querySelectorAll('input:not([type=hidden]), select, textarea, [role=combobox], button')).filter((c) => c !== el && !c.contains(el) && c.getAttribute('aria-hidden') !== 'true');
      if (others.length) break;
      if (box.contains(top)) return 'cover:' + selectorFor(top);
    }
    const what = top ? `${top.tagName.toLowerCase()}${top.id ? '#' + top.id : ''} ${String(top.className || '').slice(0, 40)}` : 'nothing';
    return 'covered by ' + what;
  }
  el.click();
  return 'clicked';
}"""

PAGE_HAS_TEXT = r"""(needle) => (document.body.innerText || '').toLowerCase().includes(String(needle).toLowerCase())"""


async def _json(driver: Driver, js: str, arg: Any) -> dict:
    raw = await driver.evaluate(js, arg)
    return json.loads(raw) if isinstance(raw, str) else raw


async def read_field(driver: Driver, selector: str) -> dict:
    return await _json(driver, READ_FIELD, selector)


async def list_options(driver: Driver, selector: str, *, attempts: int = 12, pause_ms: int = 150) -> dict:
    """The open listbox's options. Polls: menus render after the click, and async
    option sources (school search) fill in after the request returns."""
    last = {"listbox": False, "options": []}
    for _ in range(attempts):
        last = await _json(driver, LIST_OPTIONS, selector)
        if last["listbox"] and last["options"]:
            return last
        await driver.wait(pause_ms)
    return last


# ---------------------------------------------------------------------------
# helpers


def search_terms(key: str, answer: dict, store: AnswerStore | None) -> list[str]:
    """What to type into a searchable dropdown. Never a regex or a boolean literal —
    typing "false" or "^male\\b" filters the real option out (Autofill's bug)."""
    kind, value = answer["kind"], answer["value"]
    if kind == "bool":
        return ["Yes" if value else "No"]
    if kind == "option":
        out = []
        if store and store.text_for(key):
            out.append(store.text_for(key))
        for pat in value:
            words = re.sub(r"\\b|\^|\$|\\s|\\", " ", pat)
            if not re.search(r"[\[\](){}|*+?.]", words):
                out.append(re.sub(r"\s+", " ", words).strip())
        return [t for t in dict.fromkeys(out) if t]
    from det_autofill.options import candidate_strings

    return candidate_strings(key, value)


def _compatible(display: str, chosen: str) -> bool:
    """Does what the control shows correspond to the option clicked? Controls often
    show part of it ("+1" for "United States +1") or a different case."""
    d, forms = nv(display), text_forms(chosen)
    return bool(d) and any(d == f or d in f or f in d for f in forms)


def _pick(key: str, answer: dict, options: list[dict]):
    choices = [Choice(o.get("value") or "", o["text"]) for o in options]
    if answer["kind"] == "option":
        return match_option(key, None, choices, answer["value"])
    return match_option(key, answer["value"], choices)


def _text_variants(key: str, value: str, maxlength: int = 0) -> list[str]:
    out = [value]
    if key == "phone":
        digits = re.sub(r"\D", "", value)
        national = digits[1:] if len(digits) == 11 and digits.startswith("1") else digits
        dashed = f"{national[:3]}-{national[3:6]}-{national[6:]}" if len(national) == 10 else ""
        out += [dashed, digits, national]
    out = [v for v in dict.fromkeys(out) if v]
    if maxlength:
        fitting = [v for v in out if len(v) <= maxlength]
        out = fitting or out
    return out


# ---------------------------------------------------------------------------
# strategies


async def fill_text(driver: Driver, selector: str, key: str, value: str, maxlength: int = 0) -> FillResult:
    for variant in _text_variants(key, str(value), maxlength):
        await driver.type(selector, variant)
        state = await read_field(driver, selector)
        got = state.get("value", "")
        got_digits, want_digits = re.sub(r"\D", "", got), re.sub(r"\D", "", variant)
        # intl-tel-input (Teamtailor, Workable) adds the dial code to what was typed
        if key == "phone" and want_digits and (got_digits == want_digits or (len(want_digits) >= 10 and got_digits.endswith(want_digits))):
            # Eightfold rejects "(408) 555-…" ("Only use digits, dashes or parentheses")
            # once the box loses focus: leave it, and try the next format if flagged
            await driver.press("Tab")
            await driver.wait(250)
            if (await read_field(driver, selector)).get("invalid"):
                continue
            return FillResult(True, "typed", variant)
        if got == variant:
            return FillResult(True, "typed", variant)
    return FillResult(False, "value-did-not-stick")


async def fill_select(driver: Driver, selector: str, key: str, answer: dict, options: list[dict]) -> FillResult:
    pick = _pick(key, answer, options)
    if pick.index is None:
        return FillResult(False, pick.how)
    chosen = options[pick.index]
    await driver.select_option(selector, chosen.get("value") or chosen["text"])
    got = (await read_field(driver, selector)).get("value", "")
    ok = nv(got) == nv(chosen["text"])
    return FillResult(ok, pick.how if ok else "selection-did-not-stick", chosen["text"])


# The label to click instead of a radio/checkbox drawn at zero size or transparent;
# '' when the input itself is clickable.
CHOICE_LABEL = "(selector) => {" + _SELECTOR_FOR + r"""
  const el = document.querySelector(selector);
  if (!el) return '';
  const r = el.getBoundingClientRect();
  const st = getComputedStyle(el);
  // opacity 0 over a real box is still clickable (Eightfold lays it over the label)
  if (r.width >= 2 && r.height >= 2 && st.visibility !== 'hidden') return '';
  const lbl = el.closest('label') || (el.labels && el.labels[0]);
  return lbl ? selectorFor(lbl) : '';
}"""


async def dismiss_overlays(driver: Driver) -> bool:
    """Close a cookie/consent banner if one is showing. Marks the button, then clicks it
    by that mark so the click goes through the driver like any other action."""
    found = await driver.evaluate(OVERLAY_JS)
    if found is True or found == "true":
        try:
            await driver.click('[data-det-overlay="1"]')
            await driver.wait(300)
            return True
        except Exception:
            return False
    return False


async def _click_choice(driver: Driver, selector: str) -> None:
    """Click a radio/checkbox, through its label when the input is not drawn
    (BambooHR hides the native input behind a styled span; clicking it waited out
    Playwright's visibility timeout three times over)."""
    label = await driver.evaluate(CHOICE_LABEL, selector)
    await driver.click(label or selector)


async def fill_radio(driver: Driver, selectors: list[str], key: str, answer: dict, options: list[dict]) -> FillResult:
    pick = _pick(key, answer, options)
    if pick.index is None:
        return FillResult(False, pick.how)
    target = selectors[pick.index]
    if not (await read_field(driver, target)).get("checked"):
        await _click_choice(driver, target)
    ok = bool((await read_field(driver, target)).get("checked"))
    return FillResult(ok, pick.how if ok else "radio-did-not-check", options[pick.index]["text"])


async def fill_option_checkbox(driver: Driver, selector: str, option_label: str, prefs: list[str]) -> FillResult:
    """One checkbox of a multi-choice question ("select all that apply" race boxes).
    Ticked only when its own label matches the user's answer; every other box is left
    unticked. Treating the preference list as a plain True ticked "Hispanic, Latino,
    or Spanish origin" for an Asian applicant on a live Lever form."""
    label = nv(option_label)
    want = bool(label) and any(re.search(p, f, re.I) for p in prefs for f in text_forms(option_label))
    if not want:
        state = await read_field(driver, selector)
        return FillResult(not state.get("checked"), "left-unchecked" if not state.get("checked") else "checked-by-someone-else")
    return await fill_checkbox(driver, selector, True)


async def fill_checkbox(driver: Driver, selector: str, want: bool) -> FillResult:
    if bool((await read_field(driver, selector)).get("checked")) != want:
        await _click_choice(driver, selector)
    ok = bool((await read_field(driver, selector)).get("checked")) == want
    return FillResult(ok, "checkbox" if ok else "checkbox-did-not-toggle", str(want))


async def _close(driver: Driver, selector: str, typed: bool = False) -> None:
    """Leave a failed dropdown as found: menu shut and no search text behind —
    leftover text reads as an answer and the next plan would skip the field."""
    if typed:
        await driver.type(selector, "")
    if (await read_field(driver, selector)).get("expanded"):
        await driver.press("Escape")


async def fill_combobox(driver: Driver, selector: str, key: str, answer: dict,
                        store: AnswerStore | None = None, *, search: bool = False) -> FillResult:
    """Open, pick (scan, then search), follow nested levels, verify the commit."""
    if search:
        return await _fill_search(driver, selector, key, answer, store)
    await driver.click(selector)
    found = await list_options(driver, selector)
    if key == "eeo_race" and store and found["options"] and hispanic_only(found["options"]):
        # Eightfold's "Ethnicity" offers only Hispanic / Not Hispanic: that is the
        # Hispanic question, and the race answer ("Asian") matches none of it
        alt = store.get("eeo_hispanic")
        if alt:
            key, answer = "eeo_hispanic", {"kind": alt.kind, "value": alt.value}
    typed = False
    for _hop in range(MAX_HOPS):
        opts = found["options"]
        pick = _pick(key, answer, opts) if opts else None
        if (pick is None or pick.index is None) and (not opts or len(opts) >= LONG_LIST or not found["listbox"]):
            # nothing usable rendered, or a virtualised slice: filter by typing
            for term in search_terms(key, answer, store):
                await driver.type(selector, term, slowly=True)
                typed = True
                found = await list_options(driver, selector)
                pick = _pick(key, answer, found["options"]) if found["options"] else None
                if pick and pick.index is not None:
                    break
        if pick is None or pick.index is None:
            await _close(driver, selector, typed)
            if pick is not None:
                return FillResult(False, pick.how)
            return FillResult(False, "no-search-match" if found["listbox"] or typed else "listbox-never-appeared")
        option = found["options"][pick.index]
        await driver.click(option["selector"])
        await driver.wait(150)
        state = await read_field(driver, selector)
        if not state.get("expanded"):
            shown = state.get("display") or state.get("value") or " ".join(state.get("chips") or [])
            chosen_text = option["text"]
            if _compatible(shown, chosen_text) or any(_compatible(c, chosen_text) for c in state.get("chips") or []):
                return FillResult(True, pick.how, chosen_text)
            # closed without showing the pick: a Workday prompt that only counts
            # ("1 item selected") keeps the chosen label in its chip list
            return FillResult(False, f"closed-without-{chosen_text!r}")
        nested = await list_options(driver, selector, attempts=4)
        if nested["options"] and [o["text"] for o in nested["options"]] != [o["text"] for o in found["options"]]:
            found = nested  # a category opened its next level
            continue
        await _close(driver, selector, typed)
        return FillResult(False, "pick-did-not-commit")
    await _close(driver, selector, typed)
    return FillResult(False, "too-many-nested-levels")


async def _fill_search(driver: Driver, selector: str, key: str, answer: dict, store: AnswerStore | None) -> FillResult:
    """Workday multiselect: typed text does not commit; Enter lists matches and a
    click adds a chip."""
    for term in search_terms(key, answer, store):
        await driver.type(selector, term)
        await driver.press("Enter")
        found = await list_options(driver, selector)
        if not found["options"]:
            continue
        pick = _pick(key, answer, found["options"])
        if pick.index is None:
            continue
        option = found["options"][pick.index]
        await driver.click(option["selector"])
        await driver.wait(150)
        chips = (await read_field(driver, selector)).get("chips") or []
        if any(_compatible(c, option["text"]) for c in chips):
            return FillResult(True, pick.how, option["text"])
        return FillResult(False, "chip-did-not-appear")
    return FillResult(False, "no-search-match")


async def fill_file(driver: Driver, selector: str, path: str) -> FillResult:
    if not path:
        return FillResult(False, "no-file")
    name = path.rsplit("/", 1)[-1]
    # Eightfold keeps an earlier upload and renders it a moment after the form, when
    # its label no longer opens a chooser
    if await driver.evaluate(PAGE_HAS_TEXT, name):
        return FillResult(True, "already-uploaded", name)
    await driver.upload(selector, [path])
    # Greenhouse clears the input and shows the name only once its own upload
    # finishes, so poll instead of reading once
    for _ in range(20):
        state = await read_field(driver, selector)
        if name in (state.get("files") or []) or await driver.evaluate(PAGE_HAS_TEXT, name):
            return FillResult(True, "uploaded", name)
        await driver.wait(250)
    return FillResult(False, "file-not-registered", name)


def _in_search_box(descriptor: dict | None) -> bool:
    return bool(descriptor and descriptor.get("automation_id") == "searchBox")


async def fill_row(driver: Driver, row, store: AnswerStore | None = None, descriptor: dict | None = None) -> FillResult:
    """Dispatch one planned row (classify.Row with action == 'fill')."""
    answer, key = row.answer, row.key
    sel = row.selectors[0]
    try:
        if answer["kind"] == "file":
            return await fill_file(driver, sel, answer["value"])
        if row.widget == "select":
            return await fill_select(driver, sel, key, answer, row.options)
        if row.widget == "radio":
            return await fill_radio(driver, row.selectors, key, answer, row.options)
        if row.widget == "checkbox":
            if answer["kind"] == "option":
                return await fill_option_checkbox(driver, sel, (descriptor or {}).get("option_label", ""), answer["value"])
            return await fill_checkbox(driver, sel, bool(answer["value"]))
        if row.widget == "combobox" or _in_search_box(descriptor):
            return await fill_combobox(driver, sel, key, answer, store, search=_in_search_box(descriptor))
        if answer["kind"] in ("text", "date"):
            return await fill_text(driver, sel, key, str(answer["value"]), (descriptor or {}).get("maxlength", 0))
        if answer["kind"] == "bool":
            return await fill_text(driver, sel, key, "Yes" if answer["value"] else "No")
        return FillResult(False, f"no-strategy:{row.widget}/{answer['kind']}")
    except CaptchaCover:
        raise
    except Exception as exc:  # one broken widget must not stop the page
        return FillResult(False, f"error:{type(exc).__name__}:{str(exc)[:120]}")


# ---------------------------------------------------------------------------
# drivers


class PlaywrightDriver:
    """Async Playwright page."""

    def __init__(self, page):
        self.page = page

    async def click(self, selector):
        await self.page.locator(selector).click(timeout=5000)

    async def type(self, selector, text, *, slowly=False):
        loc = self.page.locator(selector)
        if slowly:
            await loc.fill("", timeout=5000)
            await loc.press_sequentially(text, delay=30)
        else:
            await loc.fill(text, timeout=5000)

    async def press(self, key):
        await self.page.keyboard.press(key)

    async def select_option(self, selector, value):
        await self.page.locator(selector).select_option(value, timeout=5000)

    async def evaluate(self, js, arg=None):
        return await self.page.evaluate(js, arg)

    async def upload(self, selector, paths):
        await self.page.locator(selector).set_input_files(paths, timeout=5000)

    async def wait(self, ms):
        await asyncio.sleep(ms / 1000)

    async def tabs(self):
        return len(self.page.context.pages)

    async def select_last_tab(self):
        self.page = self.page.context.pages[-1]
        await self.page.bring_to_front()


# The pruning proxy's read-before-write gate reads "#e<digits>" in an action's
# arguments as a snapshot ref the agent has not fetched and refuses the action.
# Ashby ids are UUIDs ("#e061b99b-3f7c-..."), so those selectors were silently
# dropped. The attribute form selects the same element without the "#e" prefix.
_REF_LOOKALIKE = re.compile(r"#(e\d[\w\\-]*)")
# a refusal by the proxy's gate, or any Playwright error ("element ... intercepts
# pointer events" when a cookie banner covers the control) — both used to be ignored
_BLOCKED = re.compile(r"^\W*Blocked:|was NOT performed|^### Error|^Error:|intercepts pointer events|Timeout \d+ms exceeded", re.I | re.M)


def _unref(selector: str) -> str:
    return _REF_LOOKALIKE.sub(lambda m: '[id="' + m.group(1).replace("\\", "") + '"]', selector)


class McpDriver:
    """Playwright MCP (det_autofill.mcpio.Browser) with CSS selectors as targets."""

    def __init__(self, browser):
        self.b = browser

    async def _act(self, name, **args):
        out = await self.b.call(name, **args)
        if _BLOCKED.search(out or ""):
            raise RuntimeError(f"{name} refused by the MCP server: {out[:160]}")
        return out

    async def click(self, selector):
        selector = _unref(selector)
        for attempt in range(3):
            out = await self.b.call("browser_click", element=selector, target=selector)
            if not _BLOCKED.search(out or ""):
                return
            if "intercepts pointer events" in out:
                # Ask the page what is on top. Lever opens hCaptcha's challenge as a
                # full-viewport iframe mid-fill: that is a handoff, not a broken widget.
                # When the page's own hit test lands on the control the cover is already
                # gone, and a DOM click is what the user's click would be.
                hit = await self.evaluate(HIT_CLICK, {"selector": selector})
                if isinstance(hit, str) and hit.startswith("captcha"):
                    raise CaptchaCover(hit)
                if hit == "clicked":
                    return
                if isinstance(hit, str) and hit.startswith("cover:") and hit[6:] != selector:
                    await self.click(hit[6:])
                    return
                raise RuntimeError(f"browser_click intercepted, page hit test says {hit!r}: {out[:160]}")
            # a control can be covered for a few seconds while the page works
            # (Lever parsing an uploaded resume); a timeout is worth waiting out
            if "Timeout" not in out or attempt == 2:
                raise RuntimeError(f"browser_click refused by the MCP server: {out[:160]}")
            await self.wait(1500)

    async def type(self, selector, text, *, slowly=False):
        selector = _unref(selector)
        if slowly:  # pressSequentially appends, so empty the box first
            await self._act("browser_type", element=selector, target=selector, text="")
        await self._act("browser_type", element=selector, target=selector, text=text, slowly=slowly)

    async def press(self, key):
        await self._act("browser_press_key", key=key)

    async def select_option(self, selector, value):
        selector = _unref(selector)
        await self._act("browser_select_option", element=selector, target=selector, values=[value])

    async def evaluate(self, js, arg=None):
        fn = js if arg is None else f"() => ({js})({json.dumps(arg)})"
        out = await self.b.call("browser_evaluate", function=fn)
        if re.match(r"\s*### Error", out or ""):
            # e.g. "Execution context was destroyed" mid-navigation; returning the
            # error text as the value made callers parse it as page data
            raise RuntimeError(f"browser_evaluate failed: {out[:200]}")
        m = re.search(r"### Result\s*\n(.*?)(?:\n###|\Z)", out, re.S)
        raw = (m.group(1) if m else out).strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    async def upload(self, selector, paths):
        # setInputFiles works on the hidden <input type=file> real forms use.
        # Paths are inside the browser container (--allow-unrestricted-file-access).
        code = f"async (page) => {{ await page.locator({json.dumps(selector)}).setInputFiles({json.dumps(paths)}); }}"
        out = await self.b.call("browser_run_code_unsafe", code=code)
        if "not available" not in out and "not found" not in out.lower():
            return
        # The pruning proxy (the agent's own browser session) hides run_code: open the
        # file chooser with the field's visible Attach/Upload button — a hidden input
        # cannot be clicked — and hand the file to browser_file_upload.
        tried = []
        for _ in range(2):
            trigger = await self.evaluate(FILE_TRIGGER, selector)
            if not trigger or trigger in tried:
                break
            tried.append(trigger)
            await self.click(trigger)
            try:
                await self._act("browser_file_upload", paths=paths)
                return
            except RuntimeError:
                # Eightfold asks for a "Data Privacy Agreement" before its chooser
                # opens: agree, then the chooser is there
                if await dismiss_overlays(self):
                    try:
                        await self._act("browser_file_upload", paths=paths)
                        return
                    except RuntimeError:
                        tried.remove(trigger)  # the click went to the dialog: once more
                # JazzHR's "Attach resume" link only reveals the real input; no file
                # chooser opened, so look for the trigger again on the changed page
                await self.wait(300)
        raise RuntimeError(f"no upload button opened a file chooser for {selector} (tried {tried})")

    async def wait(self, ms):
        await asyncio.sleep(ms / 1000)

    async def tabs(self):
        out = await self.b.call("browser_tabs", action="list")
        return len(re.findall(r"^\s*- \d+:", out or "", re.M))

    async def select_last_tab(self):
        await self._act("browser_tabs", action="select", index=await self.tabs() - 1)

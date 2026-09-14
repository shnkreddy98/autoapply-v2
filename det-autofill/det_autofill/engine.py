"""Page loop: fill a whole application, page by page, deterministically.

    extract -> plan -> fill (files first) -> re-plan for fields that appeared
            -> resolver for required fields nothing answered (one batch per page)
            -> Next  (verify the page changed; on validation errors, one repair pass)
            -> ... -> Submit only with autosubmit, confirmed by the page

Stops, never guesses, when: a captcha or one-time code appears (handoff), a required
field stays unanswered, Next does not move the page twice in a row, or no Next /
Submit control exists. Nothing here talks to a database or an LLM directly — the
`resolver` callback is where saved answers and model calls plug in.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from det_autofill.answers import AnswerStore
from det_autofill.classify import Row, plan
from det_autofill.fill import CaptchaCover, Driver, FillResult, fill_row

EXTRACT_JS = (Path(__file__).parent / "extract.js").read_text()

MAX_PAGES = 12
MAX_FILL_PASSES = 3  # fields revealed by earlier answers ("If yes, explain")
MAX_REPAIRS = 1  # extra attempts after a Next that the page rejected

# Skip reasons a resolver may still answer. Everything else (honeypot, disabled,
# poisoned context, already filled, ...) is a decision, not a gap. Duplicate-key and
# control-mismatch rows are misclassifications of a field that still needs an answer:
# a required one skipped for those reasons silently left the form incomplete.
RESOLVABLE = {"no-answer", "unclassified", "no-text-answer-for-free-text-box", "below-confidence-floor",
              "leave-blank"}
MISCLASSIFIED_PREFIXES = ("duplicate-key", "control-mismatch")
FILLABLE_WIDGETS = {"text", "textarea", "select", "radio", "checkbox", "combobox", "date"}

NAV_JS = r"""() => {
  const text = (n) => (n.innerText || n.value || n.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim();
  const visible = (n) => { const r = n.getBoundingClientRect(); const s = getComputedStyle(n); return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const esc = (s) => CSS.escape(s);
  const unique = (sel) => { try { return document.querySelectorAll(sel).length === 1; } catch (e) { return false; } };
  function selectorFor(el) {
    if (el.id && unique('#' + esc(el.id))) return '#' + esc(el.id);
    const aid = el.getAttribute('data-automation-id');
    if (aid && unique('[data-automation-id="' + esc(aid) + '"]')) return '[data-automation-id="' + esc(aid) + '"]';
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
  // plain links are included for "Apply for this job" (Lever): _nav only lets links
  // count as Apply, never as Next or Submit
  const nodes = Array.from(document.querySelectorAll('button, input[type=submit], input[type=button], a[role=button], [role=button], a[href]'))
    .filter((n) => visible(n) && !n.disabled && n.getAttribute('aria-disabled') !== 'true');
  return JSON.stringify(nodes.map((n) => ({ text: text(n).slice(0, 80), selector: selectorFor(n),
    link: n.tagName === 'A' && n.getAttribute('role') !== 'button',
    type: (n.getAttribute('type') || '').toLowerCase(), automation_id: n.getAttribute('data-automation-id') || '' })));
}"""

STATE_JS = r"""() => {
  const visible = (n) => { const r = n.getBoundingClientRect(); const s = getComputedStyle(n); return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const text = (n) => (n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim();
  const fields = Array.from(document.querySelectorAll('input, select, textarea, [role=combobox]')).filter(visible)
    .map((n) => n.id || n.name || n.getAttribute('data-automation-id') || n.tagName);
  const headings = Array.from(document.querySelectorAll('h1, h2, h3, [role=heading]')).filter(visible).map(text).slice(0, 8);
  const alerts = Array.from(document.querySelectorAll('[role=alert], [aria-live=assertive], .error, [class*="error" i], [data-automation-id*="error" i]'))
    .filter(visible).map(text).filter((t) => t && t.length < 300).slice(0, 12);
  const invalid = Array.from(document.querySelectorAll('[aria-invalid="true"]')).filter(visible)
    .map((n) => '#' + CSS.escape(n.id || '')).filter((s) => s !== '#');
  const body = (document.body.innerText || '').toLowerCase();
  return JSON.stringify({ url: location.href, fields, headings, alerts: Array.from(new Set(alerts)), invalid,
    confirmation: /thank you for (applying|your application)|application (has been |was )?(received|submitted)|we('ve| have) received your application|successfully submitted/.test(body) });
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
  const boxes = marked.concat(floating).filter((n) => visible(n) && !n.closest('form'));
  for (const box of boxes) {
    const btn = Array.from(box.querySelectorAll('button, [role=button], a')).find((b) => visible(b) && words.test((b.innerText || b.getAttribute('aria-label') || '').trim()));
    if (btn) { btn.setAttribute('data-det-overlay', '1'); return true; }
  }
  return false;
}"""

SUBMIT_RE = re.compile(r"^(submit( (my )?application)?|send( application)?|apply( now)?|finish|complete( application)?)$", re.I)
NEXT_RE = re.compile(r"^(next( step| page)?|continue|save (and|&) continue|save and next|proceed|review( application)?)\b", re.I)
APPLY_RE = re.compile(r"^(apply( now| for this job| for this position)?|i'?m interested|start application)$", re.I)


class Resolver(Protocol):
    async def resolve(self, rows: list[Row], context: dict) -> dict[int, dict]:
        """row.index -> answer {"kind", "value"} for rows it can answer."""

    async def learned(self, row: Row, answer: dict, result: FillResult, context: dict) -> None:
        """Called after each resolver answer was filled (ok or not)."""


@dataclass
class PageReport:
    number: int
    url: str
    filled: list[dict] = field(default_factory=list)  # label, key, how, ok, source
    unresolved: list[str] = field(default_factory=list)  # required labels nothing answered
    errors: list[str] = field(default_factory=list)
    action: str | None = None  # next | submit | apply | None


@dataclass
class RunResult:
    status: str  # submitted | ready-to-submit | handoff | stuck | unconfirmed
    reason: str = ""
    pages: list[PageReport] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


async def _state(driver: Driver) -> dict:
    raw = await driver.evaluate(STATE_JS)
    return json.loads(raw) if isinstance(raw, str) else raw


def _signature(state: dict) -> str:
    return hashlib.sha1(json.dumps([state["url"], state["fields"], state["headings"]]).encode()).hexdigest()


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


async def settle(driver: Driver, *, quiet_polls: int = 2, attempts: int = 16, pause_ms: int = 400) -> None:
    """Wait until the page stops changing (two identical signatures in a row)."""
    last, quiet = None, 0
    for _ in range(attempts):
        sig = _signature(await _state(driver)) + json.dumps((await _state(driver))["alerts"])
        quiet = quiet + 1 if sig == last else 0
        if quiet >= quiet_polls:
            return
        last = sig
        await driver.wait(pause_ms)


async def extract(driver: Driver) -> tuple[str, list[dict]]:
    raw = await driver.evaluate(EXTRACT_JS)
    data = json.loads(raw) if isinstance(raw, str) else raw
    return data["url"], data["fields"]


async def _buttons(driver: Driver) -> list[dict]:
    raw = await driver.evaluate(NAV_JS)
    return json.loads(raw) if isinstance(raw, str) else raw


def _nav(buttons: list[dict], pattern: re.Pattern) -> dict | None:
    hits = [b for b in buttons if pattern.search(b["text"]) and (pattern is APPLY_RE or not b.get("link"))]
    if pattern is SUBMIT_RE:
        # Greenhouse pages carry a top "Apply" button (scrolls to the form) and the
        # real "Submit application" at the bottom: the most specific wording wins
        hits.sort(key=lambda b: (not re.search(r"submit", b["text"], re.I), not re.search(r"application", b["text"], re.I)))
    return hits[0] if hits else None


async def _wait_for_change(driver: Driver, before: str, *, attempts: int = 20, pause_ms: int = 300) -> dict:
    state = await _state(driver)
    for _ in range(attempts):
        if _signature(state) != before or state["alerts"] or state["confirmation"]:
            return state
        await driver.wait(pause_ms)
        state = await _state(driver)
    return state


def _needs_resolver(row: Row, failed: set[str]) -> bool:
    if row.widget not in FILLABLE_WIDGETS:
        return False
    if row.selectors[0] in failed:
        return True
    reason = row.reason or ""
    return row.action == "skip" and row.required and (reason in RESOLVABLE or reason.startswith(MISCLASSIFIED_PREFIXES))


async def fill_page(driver: Driver, store: AnswerStore, resolver: Resolver | None, report: PageReport,
                    context: dict, *, overwrite: bool = False, invalid: set[str] | None = None) -> tuple[str | None, list[Row]]:
    """Fill everything answerable on the current page. Returns (handoff reason or None,
    required rows still unanswered). `invalid` are selectors the page flagged after a
    rejected Next: they count as required even without a required marker."""
    try:
        return await _fill_page(driver, store, resolver, report, context, overwrite=overwrite, invalid=invalid)
    except CaptchaCover as exc:
        return f"captcha over the form ({exc})", []


async def _fill_page(driver, store, resolver, report, context, *, overwrite, invalid):
    invalid = invalid or set()
    attempted: set[str] = set()
    filled_ok: set[str] = set()  # a later re-render (Lever parses the resume) can wipe these
    retried: set[str] = set()
    # keyed by selector, not row index: react-select adds hidden inputs after each pick,
    # which shifts every later index and lost track of a required field that failed
    failed: set[str] = set()
    rows: list[Row] = []
    for _pass in range(MAX_FILL_PASSES):
        url, fields = await extract(driver)
        p = plan(fields, store, url, overwrite=overwrite)
        rows = p.rows
        for r in rows:
            if invalid.intersection(r.selectors):
                r.required = True
        by_index = {f["i"]: f for f in fields}
        handoff = [r for r in rows if r.action == "handoff"]
        if handoff:
            return f"{handoff[0].reason}: {handoff[0].label[:60]}", []
        todo = [r for r in rows if r.action == "fill" and (
            r.selectors[0] not in attempted
            or (r.selectors[0] in filled_ok and r.selectors[0] not in retried))]
        todo.sort(key=lambda r: r.widget != "file")  # uploads can autofill the rest
        if not todo:
            break
        for row in todo:
            sel = row.selectors[0]
            if sel in attempted:
                retried.add(sel)  # filled before, empty now: the page reset it once
            attempted.add(sel)
            result = await fill_row(driver, row, store, by_index.get(row.index))
            report.filled.append({"label": row.label[:80], "key": row.key, "ok": result.ok, "how": result.how, "source": "profile"})
            if result.ok:
                filled_ok.add(sel)
                failed.discard(sel)
                if row.widget == "file":
                    await settle(driver)  # resume parsing re-renders some forms
            else:
                failed.add(sel)

    # a required upload that did not register cannot be answered by anyone else
    broken_files = [r for r in rows if r.widget == "file" and r.selectors[0] in failed and r.required]
    gaps = [r for r in rows if _needs_resolver(r, failed)]
    if gaps and resolver:
        url, fields = await extract(driver)
        by_index = {f["i"]: f for f in fields}
        answers = await resolver.resolve(gaps, context)
        for row in gaps:
            answer = answers.get(row.index)
            if not answer:
                continue
            row.answer, row.action = answer, "fill"
            result = await fill_row(driver, row, store, by_index.get(row.index))
            report.filled.append({"label": row.label[:80], "key": row.key, "ok": result.ok, "how": result.how,
                                  "source": answer.get("source", "resolver")})
            await resolver.learned(row, answer, result, context)
            if result.ok:
                failed.discard(row.selectors[0])
                gaps = [g for g in gaps if g.selectors[0] != row.selectors[0]]
    unanswered = broken_files + [r for r in gaps if r.required]
    return None, unanswered


async def run(driver: Driver, store: AnswerStore, *, resolver: Resolver | None = None, autosubmit: bool = False,
              context: dict | None = None, max_pages: int = MAX_PAGES, overwrite: bool = False) -> RunResult:
    """Drive the application from the page the browser is on."""
    context = dict(context or {})
    result = RunResult(status="stuck")
    repairs = 0
    invalid: set[str] = set()
    for number in range(1, max_pages + 1):
        state = await _state(driver)
        report = PageReport(number=number, url=state["url"])
        result.pages.append(report)
        await dismiss_overlays(driver)

        # a job-board landing page: the form appears after "Apply"
        buttons = await _buttons(driver)
        _, fields = await extract(driver)
        if not [f for f in fields if f.get("visible") and f.get("widget") in FILLABLE_WIDGETS | {"file"}]:
            apply_btn = _nav(buttons, APPLY_RE)
            if apply_btn:
                report.action = "apply"
                before = _signature(state)
                await driver.click(apply_btn["selector"])
                await _wait_for_change(driver, before)
                continue

        handoff, unanswered = await fill_page(driver, store, resolver, report, context,
                                              overwrite=overwrite, invalid=invalid)
        if handoff:
            result.status, result.reason = "handoff", handoff
            return result
        if unanswered:
            report.unresolved = [r.label[:80] for r in unanswered]
            result.status = "stuck"
            result.reason = "required field(s) unanswered: " + "; ".join(report.unresolved[:5])
            if context.get("resolver_error"):
                result.reason += f" (model call failed: {context['resolver_error']})"
            return result

        buttons = await _buttons(driver)
        nxt, submit = _nav(buttons, NEXT_RE), _nav(buttons, SUBMIT_RE)
        state = await _state(driver)
        before = _signature(state)
        if nxt:
            report.action = "next"
            await driver.click(nxt["selector"])
            after = await _wait_for_change(driver, before)
            if _signature(after) == before or after["alerts"]:
                report.errors = after["alerts"] or ["page did not change after Next"]
                if repairs >= MAX_REPAIRS:
                    result.status, result.reason = "stuck", "page rejected Next: " + "; ".join(report.errors[:3])
                    return result
                repairs += 1
                # re-plan with the errors visible; invalid fields become resolver work
                context["errors"] = report.errors
                invalid = set(after["invalid"])
                continue
            repairs, invalid = 0, set()
            context.pop("errors", None)
            continue
        if submit:
            if not autosubmit:
                result.status, result.reason = "ready-to-submit", f"stopped before {submit['text']!r}"
                return result
            report.action = "submit"
            await driver.click(submit["selector"])
            after = await _wait_for_change(driver, before, attempts=30)
            if after["confirmation"]:
                result.status, result.reason = "submitted", "confirmation page seen"
            elif after["alerts"]:
                result.status, result.reason = "stuck", "submit rejected: " + "; ".join(after["alerts"][:3])
            else:
                result.status, result.reason = "unconfirmed", "no confirmation text after submit"
            return result
        result.status, result.reason = "stuck", "no Next or Submit control on the page"
        return result
    result.status, result.reason = "stuck", f"more than {max_pages} pages"
    return result

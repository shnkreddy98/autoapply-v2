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
from det_autofill.fill import OVERLAY_JS, CaptchaCover, Driver, FillResult, dismiss_overlays, fill_row  # noqa: F401

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
  // [onclick]: Paycor's "Apply for this Position" is a bare <div onclick>
  // custom-element buttons: UKG's <ukg-button>Apply now</ukg-button>
  const custom = Array.from(document.getElementsByTagName('*')).filter((n) => /-BUTTON$/.test(n.tagName));
  const nodes = Array.from(document.querySelectorAll('button, input[type=submit], input[type=button], a[role=button], [role=button], a[href], [onclick]')).concat(custom)
    .filter((n) => visible(n) && !n.disabled && n.getAttribute('aria-disabled') !== 'true');
  return JSON.stringify(nodes.map((n) => ({ text: text(n).slice(0, 80), selector: selectorFor(n),
    // href="#" / javascript: is a script-driven button styled as a link (JazzHR's
    // <a href="#" class="btn">Submit Application</a>), not navigation
    link: n.tagName === 'A' && n.getAttribute('role') !== 'button' && !/^(#|javascript:)/i.test(n.getAttribute('href') || ''),
    type: (n.getAttribute('type') || '').toLowerCase(), automation_id: n.getAttribute('data-automation-id') || '' })));
}"""

STATE_JS = r"""() => {
  const visible = (n) => { const r = n.getBoundingClientRect(); const s = getComputedStyle(n); return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const text = (n) => (n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim();
  const fields = Array.from(document.querySelectorAll('input, select, textarea, [role=combobox]')).filter(visible)
    .map((n) => n.id || n.name || n.getAttribute('data-automation-id') || n.tagName);
  const headings = Array.from(document.querySelectorAll('h1, h2, h3, [role=heading]')).filter(visible).map(text).slice(0, 8);
  // Workday's Apply opens a chooser dialog and nothing else changes
  const dialogs = Array.from(document.querySelectorAll('[role=dialog], [aria-modal="true"], dialog[open]')).filter(visible).map((n) => text(n).slice(0, 120));
  // a class that styles an error state ("peer-invalid:text-error", Tailwind variants on
  // Teamtailor's consent labels) is not an error message, nor is a label around a control
  const errorClass = (n) => String(n.className || '').split(/\s+/).some((c) => /error/i.test(c) && !/[:\[]/.test(c));
  const alerts = Array.from(document.querySelectorAll('[role=alert], [aria-live=assertive], .error, [class*="error" i], [data-automation-id*="error" i]'))
    .filter((n) => visible(n) && n.tagName !== 'LABEL' && !n.querySelector('input, select, textarea')
      && (n.matches('[role=alert], [aria-live=assertive], [data-automation-id*="error" i]') || errorClass(n)))
    .map(text).filter((t) => t && t.length < 300).slice(0, 12);
  const invalid = Array.from(document.querySelectorAll('[aria-invalid="true"]')).filter(visible)
    .map((n) => '#' + CSS.escape(n.id || '')).filter((s) => s !== '#');
  const body = (document.body.innerText || '').toLowerCase();
  return JSON.stringify({ url: location.href, fields, headings, dialogs, alerts: Array.from(new Set(alerts)), invalid,
    confirmation: /thank you for (applying|your application)|application (has been |was )?(received|submitted)|we('ve| have) received your application|successfully submitted/.test(body) });
}"""

# the URL of a large visible iframe on the same site: the page's real content
FRAME_JS = r"""() => {
  const site = location.hostname.split('.').slice(-2).join('.');
  const f = Array.from(document.querySelectorAll('iframe[src]')).find((n) => {
    const r = n.getBoundingClientRect();
    try { return r.width > 300 && r.height > 300 && new URL(n.src).hostname.endsWith(site); } catch (e) { return false; }
  });
  return f ? f.src : '';
}"""

# a sign-in or account-creation step: a visible password box, or an identity
# provider / account URL (Google's email-first sign-in has no password box yet)
ACCOUNT_PAGE_JS = r"""() => {
  const shown = (n) => { const r = n.getBoundingClientRect(); return r.width > 1 && r.height > 1 && getComputedStyle(n).visibility !== 'hidden'; };
  if (Array.from(document.querySelectorAll('input[type=password]')).some(shown)) return true;
  return /accounts\.google\.|login\.microsoftonline\.|linkedin\.com\/(uas\/)?login|passport\.|\/(sign-?in|log-?in|create-?account|register)(\b|\/|\?|$)|applylogin/i.test(location.href);
}"""

SUBMIT_RE = re.compile(r"^(submit( (my )?application)?|send( application)?|apply( now)?|finish|complete( application)?)$", re.I)
# not "Next slide": SuccessFactors pages carry a job carousel beside POSTULER/Apply
NEXT_RE = re.compile(r"^(next(?! (slide|image|photo|item|job|jobs|article|story|post))( step| page)?|continue|save (and|&) continue|save and next|proceed|review( application)?)\b", re.I)
# "apply manually": Workday's Apply opens a chooser (autofill with resume / apply manually)
# Breezy: "Apply To Position"; Paycor: "Apply for this Position"
# Dayforce: "Apply without an Account" beside Sign In; UKG Ready: "Type it in myself"
# beside "Use my resume"
APPLY_RE = re.compile(r"^(apply( now| manually| online| here| (to|for) (this )?(job|position|role|opening)| without an account| as (a )?guest)?|continue as (a )?guest|type it in myself|submit (your |a )?(resume|cv)|enter (it|my (info|information|details)) manually|i'?m interested|start (your |an |the )?application|begin application)$", re.I)


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
    return hashlib.sha1(json.dumps([state["url"], state["fields"], state["headings"], state.get("dialogs")]).encode()).hexdigest()


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
    for attempt in range(3):
        try:
            raw = await driver.evaluate(EXTRACT_JS)
            data = json.loads(raw) if isinstance(raw, str) else raw
            return data["url"], data["fields"]
        except (RuntimeError, ValueError, TypeError, KeyError):
            # a page still navigating (a redirect after Apply) has no document to read
            if attempt == 2:
                raise
            await driver.wait(1500)


async def _buttons(driver: Driver) -> list[dict]:
    raw = await driver.evaluate(NAV_JS)
    return json.loads(raw) if isinstance(raw, str) else raw


def _nav(buttons: list[dict], pattern: re.Pattern) -> dict | None:
    hits = [b for b in buttons if pattern.search(b["text"]) and (pattern is APPLY_RE or not b.get("link"))]
    if pattern is SUBMIT_RE:
        # Greenhouse pages carry a top "Apply" button (scrolls to the form) and the
        # real "Submit application" at the bottom: the most specific wording wins
        hits.sort(key=lambda b: (not re.search(r"submit", b["text"], re.I), not re.search(r"application", b["text"], re.I)))
    if pattern is APPLY_RE:
        # Workday's chooser: "Apply Manually" beside "Autofill with Resume"; the
        # posting's own "Apply" stays on the page behind it
        hits.sort(key=lambda b: not re.search(r"manually|without an account|guest|myself", b["text"], re.I))
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


async def _nav_click(driver: Driver, selector: str) -> str | None:
    """Click Apply/Next/Submit; the error text when it cannot be clicked. A consent
    overlay can open after the page loaded (ADP's OneTrust preference centre), so one
    dismissal and retry before giving up."""
    try:
        await driver.click(selector)
        return None
    except CaptchaCover as exc:
        return f"captcha over the form ({exc})"
    except Exception as exc:
        if not await dismiss_overlays(driver):
            return f"{type(exc).__name__}: {str(exc)[:160]}"
    try:
        await driver.click(selector)
        return None
    except Exception as exc:
        return f"{type(exc).__name__}: {str(exc)[:160]}"


def _looks_like_form(fields: list[dict], store: AnswerStore, url: str) -> bool:
    """An application form, not a posting page with a job-search box. GM's Workday
    posting has visible search and filter inputs, so "any visible field" skipped the
    Apply click and the posting's own Apply button was then taken for the final submit."""
    visible = {f["selector"] for f in fields if f.get("visible") and f.get("widget") in FILLABLE_WIDGETS | {"file"}}
    if not visible:
        return False
    if any(f.get("widget") == "file" for f in fields if f["selector"] in visible):
        return True
    rows = [r for r in plan(fields, store, url).rows if visible.intersection(r.selectors)]
    return any(r.required for r in rows) or sum(1 for r in rows if r.key) >= 3


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
    # covered when tried: Teamtailor keeps the form behind a disabled overlay until its
    # certification question is answered, and uploads go first — so try again later
    covered: set[str] = set()
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
            or (r.selectors[0] in filled_ok and r.selectors[0] not in retried)
            or (r.selectors[0] in covered and r.selectors[0] not in retried))]
        todo.sort(key=lambda r: r.widget != "file")  # uploads can autofill the rest
        if not todo:
            break
        for row in todo:
            sel = row.selectors[0]
            if sel in attempted:
                retried.add(sel)  # filled before and emptied, or covered: one more try
            attempted.add(sel)
            result = await fill_row(driver, row, store, by_index.get(row.index))
            report.filled.append({"label": row.label[:80], "key": row.key, "ok": result.ok, "how": result.how, "source": "profile"})
            if row.widget == "file":
                await settle(driver)  # resume parsing re-renders some forms
            if result.ok:
                filled_ok.add(sel)
                failed.discard(sel)
            else:
                failed.add(sel)
                if "intercepted" in result.how:
                    covered.add(sel)
            if row.widget == "file" or "intercepted" in result.how:
                # an upload can open an agreement dialog over the form (Eightfold's
                # "Data Privacy Agreement", before the file even registers)
                await dismiss_overlays(driver)

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
              context: dict | None = None, max_pages: int = MAX_PAGES, overwrite: bool = False,
              stop_at_account_pages: bool = False) -> RunResult:
    """Drive the application from the page the browser is on.

    stop_at_account_pages: hand off on a sign-in / create-account page instead of
    typing into it. The live ATS check sets it: it typed the applicant's email into
    Google's and Amazon's sign-in flows and pressed Next."""
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
        url, fields = await extract(driver)
        if not _looks_like_form(fields, store, url):
            apply_btn = _nav(buttons, APPLY_RE)
            if apply_btn:
                report.action = "apply"
                before = _signature(state)
                tabs = await driver.tabs() if hasattr(driver, "tabs") else 0
                if err := await _nav_click(driver, apply_btn["selector"]):
                    result.status = "handoff" if err.startswith("captcha") else "stuck"
                    result.reason = f"could not click {apply_btn['text']!r}: {err}"
                    return result
                after = await _wait_for_change(driver, before)
                if _signature(after) == before and await dismiss_overlays(driver):
                    continue  # Apply opened a consent gate (ADP); the form is behind it
                if _signature(after) == before and tabs:
                    await driver.wait(1500)  # a popup window lands after the click returns
                if _signature(after) == before and tabs and await driver.tabs() > tabs:
                    # Hirebridge's Apply is javascript:popUp(...): the form is in a new tab
                    await driver.select_last_tab()
                    await settle(driver)
                    continue
                if _signature(after) == before:
                    result.status, result.reason = "stuck", f"{apply_btn['text']!r} did not open an application form"
                    return result
                continue
            # iCIMS renders the posting and its form inside a same-site iframe that
            # the page probes cannot reach: open the frame's own URL instead
            frame = await driver.evaluate(FRAME_JS)
            if frame and hasattr(driver, "navigate") and frame not in context.setdefault("_frames", []):
                context["_frames"].append(frame)
                report.action = "open-frame"
                await driver.navigate(frame)
                await settle(driver)
                continue

        if stop_at_account_pages and await driver.evaluate(ACCOUNT_PAGE_JS):
            result.status, result.reason = "handoff", "account page: sign in or create an account"
            return result
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
            if err := await _nav_click(driver, nxt["selector"]):
                result.status = "handoff" if err.startswith("captcha") else "stuck"
                result.reason = f"could not click {nxt['text']!r}: {err}"
                return result
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
        if submit and APPLY_RE.search(submit["text"]) and not any(f["ok"] for f in report.filled):
            # nothing was filled and the only way on is "Apply": still a posting page;
            # clicking could just as well send a prefilled form, so stop and say so
            result.status, result.reason = "stuck", f"only {submit['text']!r} and nothing filled: not an application form"
            return result
        if submit:
            if not autosubmit:
                result.status, result.reason = "ready-to-submit", f"stopped before {submit['text']!r}"
                return result
            report.action = "submit"
            if err := await _nav_click(driver, submit["selector"]):
                result.status = "handoff" if err.startswith("captcha") else "stuck"
                result.reason = f"could not click {submit['text']!r}: {err}"
                return result
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

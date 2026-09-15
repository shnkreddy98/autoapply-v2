"""Sign in to an applicant portal with the user's stored credentials.

The engine reaches account pages on most enterprise ATS (Workday, iCIMS, Taleo, UKG,
SuccessFactors, Oracle's emailed code): a password box, an email-first step, or a
one-time code sent to the applicant's inbox. This module handles those pages and
nothing else, one step at a time, reading the page again after every click.

It never creates an account, never retries a rejected password (a second wrong
attempt is how portals lock accounts) and never guesses a code: the code comes from
`otp()` — the applicant's inbox — or the run hands off.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

from det_autofill.fill import FRAMES_JS, Driver, dismiss_overlays, read_field

MAX_STEPS = 6

# What is on screen, in the page and readable frames: the boxes that matter for a
# sign-in and the buttons that could move it forward.
LOGIN_JS = "() => {" + FRAMES_JS + r"""
  const visible = (n) => { const r = n.getBoundingClientRect(); const s = getComputedStyle(n); return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const esc = (s) => CSS.escape(s);
  const sel = (el) => {
    const doc = el.ownerDocument;
    const one = (s) => { try { return doc.querySelectorAll(s).length === 1; } catch (e) { return false; } };
    const prefix = framePrefix(doc);
    if (el.id && one('#' + esc(el.id))) return prefix + '#' + esc(el.id);
    const name = el.getAttribute('name');
    if (name && one(`${el.tagName.toLowerCase()}[name="${esc(name)}"]`)) return prefix + `${el.tagName.toLowerCase()}[name="${esc(name)}"]`;
    el.setAttribute('data-det-login', String(Math.random()).slice(2));
    return prefix + `[data-det-login="${el.getAttribute('data-det-login')}"]`;
  };
  const hint = (el) => [el.name, el.id, el.getAttribute('autocomplete'), el.getAttribute('placeholder'),
    el.getAttribute('aria-label'), el.labels && el.labels[0] && el.labels[0].innerText].join(' ').toLowerCase();
  const inputs = deepAll('input').filter((n) => visible(n) && !n.disabled && !/^(hidden|submit|button|checkbox|radio|file)$/i.test(n.type));
  const passwords = inputs.filter((n) => n.type === 'password');
  const codes = inputs.filter((n) => n.type !== 'password' && (/\b(otp|one.?time|verification|passcode|security code|code)\b/.test(hint(n))
    || (n.maxLength === 1 && /numeric|tel|text/.test(n.inputMode || n.type))));
  const emails = inputs.filter((n) => n.type !== 'password' && !codes.includes(n)
    && (n.type === 'email' || /e-?mail|user ?name|login|user ?id/.test(hint(n))));
  const buttons = deepAll('button, input[type=submit], input[type=button], [role=button], a')
    .filter((n) => visible(n) && !n.disabled)
    .map((n) => ({ text: (n.innerText || n.value || n.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim().slice(0, 60), selector: sel(n) }))
    .filter((b) => b.text);
  const text = frameDocs().map(({ doc }) => (doc.body && doc.body.innerText) || '').join('\n').slice(0, 4000);
  // a challenge the portal raised (iCIMS shows hCaptcha after the email step)
  const captcha = deepAll('iframe').some((f) => { const r = f.getBoundingClientRect();
    return r.width > 100 && r.height > 100 && getComputedStyle(f).visibility !== 'hidden' && /captcha|challenge/i.test(`${f.title} ${f.src}`); });
  return JSON.stringify({ url: location.href, emails: emails.map(sel), passwords: passwords.map(sel),
    codes: codes.map(sel), buttons, text, captcha });
}"""

SIGN_IN_RE = re.compile(r"^(sign ?in|log ?in|login|continue|next|verify( code)?|submit( code)?)$", re.I)
# a link from a create-account form to the sign-in form (Workday shows Create Account first)
SWITCH_RE = re.compile(r"^(sign ?in|log ?in|already have an account\??.*|i already have an account|existing (user|candidate).*)$", re.I)
CREATE_RE = re.compile(r"create (an |your )?account|register|sign ?up|verify (new )?password|confirm password|re-?enter password", re.I)
REJECTED_RE = re.compile(
    # not a prompt: Hirebridge shows "Incorrect Email? click here" on every sign-in page
    r"(incorrect|invalid|wrong)\s+(e-?mail|user ?name|password|credentials|login|code)(?!\s*\?)|"
    r"(password|credentials)\s+(is|are)\s+(incorrect|invalid)|does(n't| not) match|account (is )?locked|too many (failed )?attempts|"
    r"(don't|do not|didn't|did not) recogni[sz]e (those|your|these) (credentials|details)|credentials (are )?not recogni[sz]ed",
    re.I)


@dataclass
class Credentials:
    email: str
    password: str


async def _page(driver: Driver) -> dict:
    raw = await driver.evaluate(LOGIN_JS)
    return json.loads(raw) if isinstance(raw, str) else raw


async def _type(driver: Driver, selector: str, text: str) -> bool:
    try:
        await driver.type(selector, text)
    except Exception:
        # MCP error text echoes the Playwright call, typed value included: keep the
        # password out of reports and logs
        return False
    return len((await read_field(driver, selector)).get("value", "")) == len(text)


def _button(page: dict, pattern: re.Pattern) -> dict | None:
    hits = [b for b in page["buttons"] if pattern.search(b["text"]) and not CREATE_RE.search(b["text"])]
    # "Sign In" over a generic "Continue"/"Next"
    hits.sort(key=lambda b: not re.search(r"sign ?in|log ?in", b["text"], re.I))
    return hits[0] if hits else None


async def _click(driver: Driver, selector: str, wait_ms: int) -> None:
    """Click and wait; follow a tab the click opened (UKG Ready opens its sign-in form
    in a new window)."""
    tabs = await driver.tabs() if hasattr(driver, "tabs") else 0
    await driver.click(selector)
    await driver.wait(wait_ms)
    if tabs and await driver.tabs() > tabs:
        await driver.select_last_tab()
        await driver.wait(1500)


def _signature(page: dict) -> str:
    return json.dumps([page["url"], page["emails"], page["passwords"], page["codes"]])


async def sign_in(driver: Driver, creds: Credentials | None, otp: Callable[[], Awaitable[str | None]] | None,
                  context: dict) -> str | None:
    """Work through sign-in steps on the current page. None when the portal let us in
    (no password, email-first or code step left); otherwise why the run must hand off.

    `context["login_attempted"]` records the hosts already tried this run: one password
    per portal per run."""
    tried: set[str] = context.setdefault("login_attempted", set())
    switched = False
    last = ""
    for _step in range(MAX_STEPS):
        await dismiss_overlays(driver)
        page = await _page(driver)
        if page.get("captcha"):
            return "captcha at sign-in: solve it in the browser (noVNC :6080), then continue"
        if REJECTED_RE.search(page["text"]):
            return "sign-in rejected: " + REJECTED_RE.search(page["text"]).group(0)
        if not (page["emails"] or page["passwords"] or page["codes"]):
            # a notice with a Log in button and no boxes yet: open the sign-in form once
            notice = re.search(r"already have an account|(log|sign) in to continue|already (registered|associated)", page["text"], re.I)
            switch = None if switched or not notice else next((b for b in page["buttons"] if SWITCH_RE.search(b["text"])), None)
            if not switch:
                return None
            switched = True
            last = _signature(page)
            await _click(driver, switch["selector"], 2500)
            continue
        if _signature(page) == last and not switched:
            return "sign-in step did not advance"
        last = _signature(page)
        host = re.sub(r"^https?://([^/]+).*", r"\1", page["url"])

        if page["codes"]:
            if not otp:
                return "one-time code needed: connect the applicant's email (autoapply gmail connect)"
            code = await otp()
            if not code:
                return "one-time code did not arrive in the inbox"
            boxes = page["codes"]
            if len(boxes) > 1 and len(boxes) == len(code):
                for box, digit in zip(boxes, code):  # one box per digit (Oracle)
                    await driver.type(box, digit)
            else:
                await _type(driver, boxes[0], code)
            button = _button(page, SIGN_IN_RE)
            if button:
                await _click(driver, button["selector"], 2500)
            else:
                await driver.press("Enter")
                await driver.wait(2500)
            continue

        if not creds or not creds.password:
            return "account page and no stored portal password"
        if len(page["passwords"]) > 1:
            # a create-account form (password + verify password, Workday's default):
            # switch to sign-in once, never fill it in
            switch = None if switched else next((b for b in page["buttons"] if SWITCH_RE.search(b["text"])), None)
            if not switch:
                return "no account on this portal (the check never creates one)"
            switched = True
            await _click(driver, switch["selector"], 2000)
            continue

        if page["passwords"]:
            if host in tried:
                return "sign-in already tried on this portal this run"
            tried.add(host)
            for box in page["emails"][:1]:
                if not (await read_field(driver, box)).get("value"):
                    await _type(driver, box, creds.email)
            if not await _type(driver, page["passwords"][0], creds.password):
                return "password box did not take the password"
        elif page["emails"]:
            if not await _type(driver, page["emails"][0], creds.email):
                return "email box did not take the address"
        button = _button(page, SIGN_IN_RE)
        if not button:
            return "no sign-in button"
        await _click(driver, button["selector"], 3000)
    return "sign-in took too many steps"

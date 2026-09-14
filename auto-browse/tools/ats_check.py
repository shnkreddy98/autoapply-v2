"""Live ATS regression check: one real posting per ATS, filled by the det engine up to
(never past) the submit button, then audited for empty required fields and errors.

    uv run python -m tools.ats_check                      # every target
    uv run python -m tools.ats_check --only greenhouse,workday
    uv run python -m tools.ats_check --list               # targets + last results

Targets live in kb/ats-check/targets.json (gitignored: they come from the user's own
application history). Each target lists candidate postings, newest first; the first
one still open is used. Results go to kb/ats-check/report-<date>.md and back into
targets.json (url, last_status, last_checked).

Per target:
  PASS     reached the submit button, no empty required field, no error on the page
  FAIL     a fill failed, a required field stayed empty, or the page shows errors —
           split into fill bugs (a widget did not take a value) and profile gaps
           (nobody could answer: unbacked qualification questions, missing facts)
  BLOCKED  captcha / one-time code, or a sign-in / create-account page
  GONE     every candidate posting is closed — find a fresh one on that ATS (see
           kb/ats-check/README.md) and add it to the target's candidates

Safety: autosubmit is off, and the driver refuses any click on a control whose text
reads like a final submit, so a mislabelled button cannot send an application.
Answers come from the user's real profile (default role unless --label); model calls
use auto-browse's OPENROUTER_* settings. Nothing is written to saved_answers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parents[1]
load_dotenv(HERE / ".env")

from common import db, repo  # noqa: E402
from common import storage as storagemod  # noqa: E402
from det_autofill import engine  # noqa: E402
from det_autofill.answers import AnswerStore  # noqa: E402
from det_autofill.classify import _has_value  # noqa: E402
from det_autofill.fill import McpDriver  # noqa: E402
from det_autofill.llm import LLMResolver  # noqa: E402
from det_autofill.mcpio import Browser  # noqa: E402
from det_autofill.profile import from_export  # noqa: E402

from agent import det_apply  # noqa: E402
from agent.loop import DEFAULT_MODEL, RESUME_CONTAINER_ROOT, SCRATCH_ROOT  # noqa: E402
from agent.profile import profile_json  # noqa: E402

KB = HERE.parent / "kb" / "ats-check"
TARGETS = KB / "targets.json"
PER_TARGET_TIMEOUT_S = 900

GONE_RE = re.compile(
    r"no longer (available|accepting|open|posted)|job (not found|has expired|is closed|is no longer)"
    r"|position (has been|is) (filled|closed)|posting (has )?(expired|closed|been removed)"
    r"|page (not found|does ?n.t exist)|this (job|requisition|position) (is|has been) (closed|filled|removed)"
    r"|job you (are|were) looking for|no longer (be )?found|sorry, this job|opening (is )?closed|\b404\b",
    re.I,
)
LOGIN_RE = re.compile(r"\b(sign ?in|log ?in|create (an |your )?account|forgot (your )?password|verify your email)\b", re.I)
# Final-submit wording the check driver refuses to click, whatever the engine thinks.
SUBMIT_GUARD = re.compile(r"\b(submit|send (my |your )?application|finish application|complete application)\b", re.I)
FILLABLE = {"text", "textarea", "select", "radio", "checkbox", "combobox", "date", "file"}
# fill results that are correct refusals (the page offers nothing that matches the
# profile), not widget bugs
REFUSALS = re.compile(r"^(no-matching-option|no-search-match|ambiguous|no-options|no-value)")

PAGE_TEXT_JS = "() => (document.body && document.body.innerText || '').slice(0, 8000)"
CONTROL_TEXT_JS = r"""(selector) => { const el = document.querySelector(selector);
  return el ? (el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/\s+/g, ' ').trim() : ''; }"""


class NoSubmitDriver(McpDriver):
    """McpDriver that will not click anything reading like a final submit."""

    async def click(self, selector):
        text = await self.evaluate(CONTROL_TEXT_JS, selector)
        if isinstance(text, str) and len(text) < 60 and SUBMIT_GUARD.search(text):
            raise RuntimeError(f"refused to click {text!r}: the ATS check never submits")
        await super().click(selector)


class ReadOnlySaved:
    """saved_answers lookups without writes or use counts: a check must not change data."""

    def __init__(self, conn, user_id):
        self.conn, self.user_id = conn, user_id

    def get(self, row, *, per_job):
        if per_job:
            return None
        found = repo.get_saved_answer(self.conn, self.user_id, row.label)
        return {"answer": found["answer"], "source": found["source"]} if found else None

    def put(self, *a, **kw):
        return None


def load_targets() -> dict:
    return json.loads(TARGETS.read_text())


async def open_live_posting(browser, driver, candidates, max_candidates) -> tuple[str | None, list[str]]:
    tried = []
    for url in candidates[:max_candidates]:
        try:
            await browser.navigate(url)
        except Exception as exc:
            tried.append(f"{url} -> navigate error {type(exc).__name__}")
            continue
        await driver.wait(2500)
        text = await driver.evaluate(PAGE_TEXT_JS)
        text = text if isinstance(text, str) else ""
        if len(text.strip()) < 80 or GONE_RE.search(text[:3000]):
            tried.append(f"{url} -> gone")
            continue
        return url, tried
    return None, tried


async def audit_page(driver) -> dict:
    """What is still wrong on the page the engine stopped on."""
    _, fields = await engine.extract(driver)
    state = await engine._state(driver)
    groups: dict[str, list[dict]] = {}
    for f in fields:
        if f.get("group_key"):
            groups.setdefault(f["group_key"], []).append(f)
    empty, seen = [], set()
    for f in fields:
        if not f.get("required") or f.get("widget") not in FILLABLE or f.get("disabled"):
            continue
        if not f.get("visible") and f.get("widget") != "file":
            continue
        key = f.get("group_key") or f["selector"]
        if key in seen:
            continue
        seen.add(key)
        members = groups.get(f.get("group_key"), [f]) if f.get("group_key") else [f]
        if not _has_value(f, members):
            empty.append((f.get("group_label") or f.get("label") or f.get("placeholder") or f["selector"])[:100])
    has_password = any(f.get("type") == "password" and f.get("visible") for f in fields)
    return {"empty_required": empty, "errors": state["alerts"], "invalid": state["invalid"],
            "password_page": has_password, "url": state["url"]}


def verdict(result: engine.RunResult, audit: dict, page_text: str) -> str:
    if result.status == "handoff":
        return "BLOCKED"
    if audit["password_page"] or (result.status == "stuck" and LOGIN_RE.search(page_text[:2000])
                                  and not audit["empty_required"]):
        return "BLOCKED"
    if result.status == "ready-to-submit" and not audit["empty_required"] and not audit["errors"] and not audit["invalid"]:
        return "PASS"
    return "FAIL"


async def check_target(target, *, browser, conn, storage, user_id, doc, store, complete, max_candidates) -> dict:
    driver = NoSubmitDriver(browser)
    url, tried = await open_live_posting(browser, driver, target["candidates"], max_candidates)
    out = {"platform": target["platform"], "url": url, "tried": tried, "checked": datetime.now(timezone.utc).isoformat()}
    if not url:
        return out | {"status": "GONE"}
    page_text = await driver.evaluate(PAGE_TEXT_JS)
    page_text = page_text if isinstance(page_text, str) else ""

    async def per_job(question: str, words: int) -> str | None:
        if not complete:
            return None
        reply = await complete([
            {"role": "system", "content": "Write the candidate's answer to a job application question in first "
             f"person, under {words} words, no em dashes, grounded only in the profile and job posting given."},
            {"role": "user", "content": f"Profile:\n{json.dumps(doc, default=str)[:8000]}\n\nJob posting:\n{page_text[:5000]}"
             f"\n\nQuestion: {question}"},
        ])
        return (reply or "").strip() or None

    resolver = LLMResolver(complete, doc, page_text, saved=ReadOnlySaved(conn, user_id), per_job=per_job)
    context: dict = {}
    try:
        result = await asyncio.wait_for(
            engine.run(driver, store, resolver=resolver, autosubmit=False, context=context),
            timeout=PER_TARGET_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        result = engine.RunResult(status="stuck", reason=f"timed out after {PER_TARGET_TIMEOUT_S}s")
    except Exception as exc:
        result = engine.RunResult(status="stuck", reason=f"error: {type(exc).__name__}: {str(exc)[:200]}")
    audit = await audit_page(driver)
    end_text = await driver.evaluate(PAGE_TEXT_JS)
    status = verdict(result, audit, end_text if isinstance(end_text, str) else "")
    failed = [f for p in result.pages for f in p.filled if not f["ok"]]
    fill_failures = list(dict.fromkeys(f"{f['label'][:70]} ({f['how']})" for f in failed if not REFUSALS.match(f["how"])))
    refused = list(dict.fromkeys(f"{f['label'][:70]} ({f['how']})" for f in failed if REFUSALS.match(f["how"])))
    unanswered = sorted({u for p in result.pages for u in p.unresolved})
    return out | {
        "status": status, "engine_status": result.status, "reason": result.reason,
        "pages": len(result.pages), "filled": sum(1 for p in result.pages for f in p.filled if f["ok"]),
        "fill_failures": fill_failures, "refused": refused, "unanswered": unanswered,
        "empty_required": audit["empty_required"], "errors": audit["errors"] + audit["invalid"],
        "ended_on": audit["url"], "model_error": context.get("resolver_error"),
    }


def write_report(results: list[dict], label: str | None) -> Path:
    """Merge into today's results (a later run of a platform replaces its earlier row,
    so `--only x` reruns do not erase the rest of the day's sweep), then render."""
    day = date.today().isoformat()
    path = KB / f"report-{day}.md"
    previous = KB / f"results-{day}.json"
    if previous.exists():
        fresh = {r["platform"] for r in results}
        results = [r for r in json.loads(previous.read_text()) if r["platform"] not in fresh] + results
    counts = {s: sum(1 for r in results if r["status"] == s) for s in ("PASS", "FAIL", "BLOCKED", "GONE")}
    lines = [f"# ATS check {day}", "", f"role: {label or '(default)'}  |  " + "  ".join(f"{k} {v}" for k, v in counts.items()), "",
             "| platform | status | pages | filled | detail |", "|---|---|---|---|---|"]
    for r in results:
        detail = r.get("reason", "")
        if r["status"] == "GONE":
            detail = f"{len(r['tried'])} candidate(s) closed — needs a fresh posting"
        lines.append(f"| {r['platform']} | {r['status']} | {r.get('pages', '-')} | {r.get('filled', '-')} | {detail[:120]} |")
    for r in results:
        if r["status"] in ("FAIL", "BLOCKED"):
            lines += ["", f"## {r['platform']} — {r['status']}", f"url: {r['url']}", f"ended on: {r.get('ended_on')}",
                      f"engine: {r.get('engine_status')} — {r.get('reason')}"]
            for title, key in (("fill failures (bugs)", "fill_failures"), ("refused: no option matches the profile", "refused"),
                               ("unanswered by resolver (profile gaps)", "unanswered"),
                               ("empty required at the end", "empty_required"), ("errors on page", "errors")):
                if r.get(key):
                    lines.append(f"- {title}:")
                    lines += [f"  - {x}" for x in r[key]]
            if r.get("model_error"):
                lines.append(f"- model error: {r['model_error']}")
    path.write_text("\n".join(lines) + "\n")
    (KB / f"results-{day}.json").write_text(json.dumps(results, indent=1))
    return path


async def main_async(args) -> int:
    data = load_targets()
    targets = data["targets"]
    if args.only:
        wanted = set(args.only.split(","))
        targets = [t for t in targets if t["platform"] in wanted]
    if args.list:
        for t in data["targets"]:
            print(f"{t['platform']:<30} {str(t.get('last_status')):<8} {str(t.get('last_checked'))[:10]:<10} {t.get('url') or t['candidates'][0]}")
        return 0

    storage = storagemod.from_env()
    run_dir = f"ats-check-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}"
    with db.connect() as conn:
        user_id = str(repo.active_user_ids(conn)[0]) if not args.user else args.user
        bundle = repo.fetch_profile(conn, user_id, label=args.label)
        doc = profile_json(bundle)
        pdf = repo.get_base_asset(conn, user_id, variant="pdf", label=args.label)
        if pdf is None:
            raise SystemExit(f"no base PDF for role {bundle.label!r}")
        host_resume = Path(SCRATCH_ROOT) / run_dir / "resume.pdf"
        host_resume.parent.mkdir(parents=True, exist_ok=True)
        storage.get_file(pdf["s3_key"], str(host_resume))
        store = AnswerStore(from_export(doc, resume_path=f"{RESUME_CONTAINER_ROOT}/{run_dir}/resume.pdf",
                                        login_password=(bundle.login or {}).get("login_password")))
        complete = None if args.no_model else det_apply.openrouter_complete(args.model or DEFAULT_MODEL)
        print(f"ATS check: {len(targets)} target(s), role {bundle.label!r}, mcp {args.mcp}")

        results = []
        browser = await Browser(args.mcp).connect()
        try:
            for i, target in enumerate(targets, 1):
                print(f"[{i}/{len(targets)}] {target['platform']} ...", flush=True)
                try:
                    r = await check_target(target, browser=browser, conn=conn, storage=storage, user_id=user_id,
                                           doc=doc, store=store, complete=complete, max_candidates=args.max_candidates)
                except Exception as exc:  # one broken target never stops the sweep
                    r = {"platform": target["platform"], "url": None, "tried": [], "status": "FAIL",
                         "reason": f"check crashed: {type(exc).__name__}: {str(exc)[:200]}"}
                results.append(r)
                print(f"    {r['status']}: {r.get('reason', '')[:140]}", flush=True)
                for t in data["targets"]:
                    if t["platform"] == target["platform"]:
                        t.update({"url": r.get("url") or t.get("url"), "last_status": r["status"],
                                  "last_checked": r.get("checked")})
                TARGETS.write_text(json.dumps(data, indent=1))
        finally:
            await browser.close()
    report = write_report(results, bundle.label)
    print(f"report: {report}")
    return 0 if all(r["status"] == "PASS" for r in results) else 1


def main() -> None:
    p = argparse.ArgumentParser(description="Live ATS regression check (never submits)")
    p.add_argument("--only", help="comma-separated platforms from targets.json")
    p.add_argument("--list", action="store_true", help="print targets and their last results")
    p.add_argument("--label", help="role whose resume/profile to use (default: the user's default role)")
    p.add_argument("--user", help="user id (default: the only active user)")
    p.add_argument("--mcp", default=det_apply.PLAYWRIGHT_MCP_URL, help="Playwright MCP URL (default: the proxy)")
    p.add_argument("--model", help="model for the resolver (default: OPENROUTER_MODEL)")
    p.add_argument("--no-model", action="store_true", help="deterministic answers only; gaps are reported")
    p.add_argument("--max-candidates", type=int, default=6, help="closed postings to skip per target before GONE")
    sys.exit(asyncio.run(main_async(p.parse_args())))


if __name__ == "__main__":
    main()

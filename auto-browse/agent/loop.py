import asyncio
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)

from agent.observability import (
    LOGGER_NAME,
    Trace,
    append_usage,
    new_run_id,
    setup_logging,
    write_form_record,
)

load_dotenv()

log = logging.getLogger(LOGGER_NAME)

SYSTEM_PROMPT = """You are an autonomous job-application agent driving a real \
browser. You are given a JOB URL. Apply for that job on the applicant's behalf \
using the APPLICANT PROFILE provided below.

Procedure:
1. Navigate to the job URL. Find the application form — follow "Apply" / "Apply \
   now" links if the form is not on the first page.
2. If the portal requires signing in or creating an account before applying, \
   use the Login Credentials in the APPLICANT PROFILE: first try to SIGN IN \
   with that email and password; if sign-in fails or no account exists, CREATE \
   an account with the same email and password, complete any email/verification \
   step you can, then continue applying.
3. ALWAYS prefer an "autofill" / "Autofill with Resume" / "Apply with resume" / \
   parse-resume option when the portal offers one — choose it over manual entry, \
   upload the resume to autofill, then review and correct the prefilled fields \
   against the APPLICANT PROFILE. Only fill manually if no autofill option exists.
4. Fill every REQUIRED field using the APPLICANT PROFILE. Required fields are \
   marked with an asterisk (*) or "Required". Do NOT waste steps on optional \
   (non-asterisk) fields — skip them. Match fields to profile values by label \
   (name, email, phone, address, work authorization, salary, start date, etc.).
   - For dropdowns/comboboxes without an exact matching option (e.g. Education \
     "Field of Study"), pick the closest option only if the field is required; \
     if it is optional, leave it blank and move on rather than forcing a wrong \
     value or burning steps searching.
   - For a "How did you hear about us?" field, pick the first of these that the \
     options offer, in order: (1) careers page / company website (or closest, \
     e.g. "Corporate Website"), (2) LinkedIn, (3) Google / search engine, \
     (4) Internet. Use the closest available option to these.
5. Upload the resume with the `file_upload` tool, using the resume path given \
   at the top of the profile. Click the upload control first if needed.
6. Leave voluntary/EEO fields (gender, race, veteran, disability) blank unless \
   they are required to proceed.
7. Tick declaration / acknowledgement checkboxes per the profile (all "Yes").
8. Work through EVERY page/step of the application, clicking "Next"/"Continue" \
   to advance. Each Next validates that page and will NOT proceed while a \
   required field is missing — fix anything it flags. Fill all required fields \
   on every page until you reach the final Review/Submit step (this walks the \
   whole form and surfaces fields that only appear on later pages).
9. LAST STEP — VALIDATE, then hand off. On the Review step, confirm every \
   required field across all sections is filled and there are no validation \
   errors (fix any that remain). Do NOT click the final Submit / Apply / Send \
   button — leave that for the user. Take a screenshot, then return a plain-text \
   summary: each field you filled, anything you could not complete, and a clear \
   statement that the form is validated and ready for the user to review and \
   submit.

Browsing tips:
- `snapshot(goal=...)` returns a small pruned tree to find WHERE things are; \
  `expand_region(ref)` reads the FULL content of a region (labels, values).
- Pass `next_goal` on actions to keep the returned snapshot focused.
- If a snapshot is empty or a ref errors "not found", call `wait_for` then \
  `snapshot` again — never reuse a ref from an older snapshot.
- NEW TABS: if a result's "### Open tabs" list shows a new tab (index > 0) — \
  e.g. the "Apply" button opened the form in a new tab — call `select_tab` with \
  that index to switch to it, then continue there. Do NOT re-click the same \
  button hoping the form appears; that only spawns more duplicate tabs. Use \
  `close_tab` to clean up stray duplicates if they pile up.
- ALREADY APPLIED / CLOSED: if the page says you have already applied for this \
  job, or the posting is closed / no longer accepting applications, STOP \
  immediately and return a one-line summary stating that status. Do NOT search \
  for the role again or try to re-apply.

ATS form tips (Workday / Greenhouse / iCIMS — read carefully, these fail often):
- DROPDOWNS are a button that opens a listbox, NOT a `<select>`. To pick a \
  value: `click` the button, then `click` the option. Do NOT use \
  `select_option` or `fill_form` with type "combobox" on them — that errors with \
  "Element is not a <select>".
- RADIO / CHECKBOX questions (Yes/No, "I decline to say", disability/veteran): \
  `click` the specific option's ref directly (e.g. click the "Yes" radio ref). Do \
  NOT use `fill_form` with type "radio"/"checkbox" — it does not reliably check \
  them (it leaves the answer empty), and the field will still read as unfilled.
- REQUIRED CHECK: every form result ends with a "REQUIRED CHECK" line listing any \
  required (*) fields still EMPTY, with their refs. After filling, READ it: if it \
  lists fields, they did NOT get set — fix each one (click the radio/checkbox ref, \
  pick a dropdown option, type the text) before clicking Save/Submit or finishing. \
  Only finish once it says "all required field(s) filled".
- FILE UPLOAD needs the chooser open first: `click` the "Attach"/"Select \
  files"/"Upload" control, THEN call `file_upload`. Calling `file_upload` with no \
  open chooser fails ("requires modal state").
- After ANY click that changes the page, refs are stale — take a fresh \
  `snapshot` before the next action. Reusing an old ref errors "ref not found".
- ACTION BUTTONS ("Save and Continue" / "Next" / "Submit" / "Review") live in an \
  action bar at the bottom of the FORM — this is NOT the page footer (which has \
  social/legal links). To find one, call `snapshot(goal='save and continue \
  button')`; do NOT `expand_region` the footer / contentinfo region looking for it.
- MULTI-STEP forms: click "Next"/"Continue"/"Review" to advance through EVERY \
  step. You are NOT on the final/Review page until you have actually clicked \
  through to it AND a fresh `snapshot` shows it (e.g. a Review/Submit step with a \
  Submit button). NEVER claim you reached Review or finished a page you have not \
  navigated to and confirmed by snapshot.

Thinking: before each tool call, state your plan in ONE short sentence as your \
message content (e.g. "Fill the email field, then click Next") — then make the \
call. Keep it to one line; do not write long explanations.

Be methodical and persistent. Never click the final application Submit — walk \
every page to validate, then hand off to the user. (Signing in or creating an \
account is allowed and expected; only the final application submit is off-limits.) \
If a field's value is missing from the profile, leave it blank (unless required) \
and note it in your final summary."""

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
PROXY_MCP_URL = os.getenv("PROXY_MCP_URL", "http://localhost:8932/mcp")
DEFAULT_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-3-flash-preview")

HERE = Path(__file__).resolve().parent.parent
PROFILE_PATH = Path(
    os.getenv("PROFILE_PATH", HERE / "data" / "profile" / "profile.txt")
)

# Context management (Tier 1): keep only the last N tool-result snapshots full;
# older ones are stubbed since refs are only valid on the latest tree and the
# profile is the durable source of truth. See README "Context management".
HISTORY_KEEP_SNAPSHOTS = int(os.getenv("HISTORY_KEEP_SNAPSHOTS", "2"))
_STUB = "[older page snapshot dropped to save context — see PROGRESS LEDGER]"

# Completion is judged from evidence, not from the model deciding to stop. If the
# model emits no tool call but evidence is missing, it is hinted and made to
# continue — up to MAX_FINISH_HINTS times, then allowed to finish anyway.
MAX_FINISH_HINTS = int(os.getenv("MAX_FINISH_HINTS", "2"))
# Identity-field label keywords that count as "core fields filled" evidence.
_IDENTITY_KEYS = ("name", "email", "phone", "address", "city", "zip", "postal")
# A tool result containing one of these is a failure even though it came back as
# normal text (so it must not count as evidence).
_TOOL_FAILURE_MARKERS = ("session terminated", "### error")

# Repeated-action guard: if the model calls the same action with the same target
# this many times in a run, it is stuck in a loop (e.g. re-clicking "Apply" that
# opened a new tab, or re-clicking a dropdown that never opens). When the limit
# is hit it gets nudged to change approach and the counter resets. Read-only
# tools (snapshot/expand_region/wait_for/screenshot) are exempt — repeated reads
# are legitimate re-orientation, not a stuck loop. See README "Bugs".
REPEAT_LIMIT = int(os.getenv("REPEAT_LIMIT", "3"))
_GUARDED_TOOLS = frozenset(
    {
        "click",
        "type",
        "select_option",
        "fill_form",
        "file_upload",
        "press_key",
        "hover",
        "navigate",
        "select_tab",
        "close_tab",
    }
)
_REPEAT_GUARD_MSG = (
    f"LOOP DETECTED: you called {{names}} with the SAME target/args {REPEAT_LIMIT} "
    "times with no progress. STOP repeating that call. Take a fresh `snapshot` to "
    "get current refs, then CHANGE approach: pick a DIFFERENT element, switch tabs "
    "with `select_tab` if the action opened a new tab, or move on to the next "
    "required field. Do NOT issue that same call again."
)

# Evidence gate: a run is only "complete" when the resume is uploaded AND the
# agent reached a review/submit step AND the latest page shows no validation
# error. These markers are matched (case-insensitive) against the latest page
# observation. Tightened after a run that uploaded the resume LAST — overwriting
# manually-filled fields — and "finished" without ever validating. See README.
_REVIEW_MARKERS = (
    "submit application",
    "submit your application",
    "review your application",
    "review and submit",
    "ready to submit",
    "ready to review",
    'button "submit',
    "submit and",
)
# Real error-message phrases only. The loose "is required" / "required field"
# variants were removed: they collide with static helper text ("* indicates a
# required field") and with the REQUIRED CHECK footer wording. Required-field
# emptiness is now covered authoritatively by _REQUIRED_EMPTY_MARKER below.
_VALIDATION_ERROR_MARKERS = (
    "this field is required",
    "please complete",
    "please enter",
    "please select",
    "cannot be blank",
    "must be completed",
    "fix the error",
    # ATS portals render field errors as an icon node, not text. The pruner now
    # always surfaces these (proxy/pruner.py _always_keep), so the gate can see them.
    'img "error"',
)
# Substring of the proxy's "REQUIRED CHECK" footer that means a required (*) field
# is still empty (see proxy/required_check.py INCOMPLETE_MARKER).
_REQUIRED_EMPTY_MARKER = "required field(s) still empty"

# Terminal page states the agent must NOT try to apply through — exit gracefully
# instead of looping back to search/re-apply. Maps a status to the page phrases
# that signal it. See README "Bugs"/"Improvements".
_TERMINAL_MARKERS: dict[str, tuple[str, ...]] = {
    "already_applied": (
        "you've already applied",
        "you have already applied",
        "already applied for this",
        "already applied to this",
        "you already applied",
        "already submitted an application",
    ),
    "closed": (
        "no longer accepting applic",
        "this position has been filled",
        "this job is no longer",
        "this posting is closed",
        "applications are closed",
        "this requisition is closed",
    ),
}

# Upload-first enforcement: when the page offers an autofill / upload-resume
# affordance and the resume is NOT uploaded yet, manual value-entry is hard-
# blocked so the agent uploads first and lets the resume autofill the form.
# Uploading LAST (after manual entry) re-renders/overwrites the fields with the
# parsed-resume values — the ultipro bug. Login pages don't show these markers,
# so sign-in fills are not blocked. Toggle with ENFORCE_UPLOAD_FIRST=0.
ENFORCE_UPLOAD_FIRST = os.getenv("ENFORCE_UPLOAD_FIRST", "1").lower() not in (
    "0",
    "false",
    "no",
)
_AUTOFILL_MARKERS = (
    "autofill with resume",
    "apply with resume",
    "upload resume",
    "upload your resume",
    "parse resume",
    "import from resume",
    "autofill",
)
_VALUE_FILL_TOOLS = frozenset({"type", "fill_form", "select_option"})
_UPLOAD_FIRST_MSG = (
    "Blocked: this page offers an autofill / upload-resume option and the resume "
    "is not uploaded yet. Upload the resume FIRST so it autofills these fields — "
    "click the 'Autofill with Resume' / 'Upload Resume' control, then call "
    "`file_upload` with the resume path from the profile, THEN review and correct "
    "the prefilled values. Do not type fields manually before uploading."
)

# LLM call resilience.
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "6"))
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "120"))
LLM_BACKOFF_BASE = float(os.getenv("LLM_BACKOFF_BASE", "2"))  # seconds
LLM_BACKOFF_CAP = float(os.getenv("LLM_BACKOFF_CAP", "60"))  # seconds
_RETRYABLE = (APITimeoutError, APIConnectionError, RateLimitError, InternalServerError)
# Markers for a rate-limit that OpenRouter surfaces as a non-429 status (e.g. a
# 400 "Provider returned error" wrapping an upstream 429 / quota exhaustion).
_RATE_LIMIT_MARKERS = (
    "rate-limit",
    "rate limit",
    "ratelimit",
    "temporarily rate-limited",
    "provider returned error",
    "resource_exhausted",
    "quota",
    "429",
)


def _is_rate_limited(exc: APIStatusError) -> bool:
    """True if an API status error is (or wraps) an upstream rate-limit / quota
    error, even when the HTTP status is not 429."""
    if getattr(exc, "status_code", None) == 429:
        return True
    text = str(exc).lower()
    return any(m in text for m in _RATE_LIMIT_MARKERS)


def _retry_after(exc: Any) -> float | None:
    """Honour a Retry-After header (seconds) if the provider sent one."""
    resp = getattr(exc, "response", None)
    val = resp.headers.get("retry-after") if resp is not None else None
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _backoff_seconds(attempt: int, retry_after: float | None) -> float:
    """Exponential backoff with full jitter, capped; respects Retry-After."""
    base = min(LLM_BACKOFF_BASE * (2**attempt), LLM_BACKOFF_CAP)
    wait = random.uniform(0, base)  # full jitter
    if retry_after is not None:
        wait = max(wait, retry_after)
    return min(wait, LLM_BACKOFF_CAP)


def load_profile() -> str:
    """Read the exported applicant profile that the agent fills forms from."""
    if not PROFILE_PATH.is_file():
        raise SystemExit(
            f"Profile not found at {PROFILE_PATH}. Run `uv run python -m agent.profile` first."
        )
    return PROFILE_PATH.read_text(encoding="utf-8")


def _new_usage() -> dict[str, Any]:
    return {
        "requests": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cost": 0.0,
    }


def _accumulate_usage(totals: dict[str, Any], usage: Any) -> None:
    """Add one response's token usage + cost (OpenRouter extension) into totals."""
    if usage is None:
        return
    totals["requests"] += 1
    totals["prompt_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
    totals["completion_tokens"] += getattr(usage, "completion_tokens", 0) or 0
    totals["total_tokens"] += getattr(usage, "total_tokens", 0) or 0
    cost = getattr(usage, "cost", None)
    if cost is None and getattr(usage, "model_extra", None):
        cost = usage.model_extra.get("cost")
    totals["cost"] += cost or 0.0


def _record_usage(
    totals: dict[str, Any],
    run_id: str,
    model: str,
    job_url: str,
    iterations: int,
    success: bool,
    error: str | None,
    duration_s: float,
    outcome: str = "blocked",
) -> dict[str, Any]:
    """Build + append-and-print one run's usage record (data/usage.jsonl)."""
    record = {
        "run_id": run_id,
        "time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model,
        "job_url": job_url,
        "requests": totals["requests"],
        "prompt_tokens": totals["prompt_tokens"],
        "completion_tokens": totals["completion_tokens"],
        "total_tokens": totals["total_tokens"],
        "cost_usd": round(totals["cost"], 6),
        "iterations": iterations,
        "success": success,
        "outcome": outcome,
        "error": error,
        "duration_s": round(duration_s, 2),
    }
    append_usage(record)
    log.info(
        "run usage: requests=%d total_tokens=%s cost=$%.6f iters=%d success=%s "
        "duration=%.1fs",
        record["requests"],
        f"{record['total_tokens']:,}",
        record["cost_usd"],
        iterations,
        success,
        duration_s,
    )
    return record


def _filled_fields(tool_name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize a value-bearing tool call into form-field entries (for the
    per-run form record). Returns [] for non-form tools."""
    if tool_name == "type":
        return [
            {
                "tool": "type",
                "label": args.get("element"),
                "ref": args.get("target"),
                "value": args.get("text"),
            }
        ]
    if tool_name == "select_option":
        vals = args.get("values") or []
        return [
            {
                "tool": "select_option",
                "label": args.get("element"),
                "ref": args.get("target"),
                "value": ", ".join(map(str, vals)),
            }
        ]
    if tool_name == "file_upload":
        paths = args.get("paths") or []
        return [
            {
                "tool": "file_upload",
                "label": "resume/file",
                "value": ", ".join(map(str, paths)),
            }
        ]
    if tool_name == "fill_form":
        out = []
        for f in args.get("fields") or []:
            out.append(
                {
                    "tool": "fill_form",
                    "label": f.get("element") or f.get("name"),
                    "ref": f.get("target"),
                    "type": f.get("type"),
                    "value": f.get("value"),
                }
            )
        return out
    return []


def _tool_failed(text: str) -> bool:
    """True if a tool result is a failure delivered as normal text (e.g. the
    upstream session died, or an MCP '### Error' block)."""
    low = text.lower()
    return any(m in low for m in _TOOL_FAILURE_MARKERS)


def _action_signature(tool_name: str, args: dict[str, Any]) -> str | None:
    """Stable signature of a state-changing action for the repeated-action guard.

    Returns None for read-only/idle tools (those are exempt). Keyed on the tool
    plus its salient target so the same ref clicked again maps to the same
    signature; after a fresh snapshot refs change, so a genuinely new action gets
    a new signature and the counter naturally resets.
    """
    if tool_name not in _GUARDED_TOOLS:
        return None
    if tool_name == "fill_form":
        ids = ",".join(
            sorted(
                str(f.get("target") or f.get("name") or "")
                for f in (args.get("fields") or [])
            )
        )
        return f"fill_form:{ids}"
    for key in ("target", "url", "key", "paths"):
        val = args.get(key)
        if val:
            if isinstance(val, list):
                val = ",".join(map(str, val))
            return f"{tool_name}:{val}"
    return tool_name


def _terminal_status(text: str) -> str | None:
    """Return a terminal status ('already_applied' / 'closed') if the page text
    signals the application cannot/should not proceed; otherwise None."""
    low = text.lower()
    for status, markers in _TERMINAL_MARKERS.items():
        if any(m in low for m in markers):
            return status
    return None


def _resume_uploaded(filled_fields: list[dict[str, Any]] | None) -> bool:
    """True once a successful file_upload has been recorded."""
    return bool(filled_fields) and any(
        f.get("tool") == "file_upload" for f in filled_fields
    )


def _autofill_available(observation: str) -> bool:
    """True if the latest page shows an autofill / upload-resume affordance."""
    low = observation.lower()
    return any(m in low for m in _AUTOFILL_MARKERS)


def _outcome(success: bool, terminal_status: str | None) -> str:
    """Single structured run outcome for the usage/form record.

    applied_ready  — form validated and ready for the user to submit
    already_applied / closed — terminal page state, graceful no-op
    blocked        — ran out of iterations / stopped without evidence / errored
    """
    if terminal_status in ("already_applied", "closed"):
        return terminal_status
    return "applied_ready" if success else "blocked"


def _completion_evidence(
    filled_fields: list[dict[str, Any]], last_observation: str = ""
) -> tuple[bool, list[str]]:
    """Decide whether the application is actually complete.

    Requirements (all must hold):
    - resume/CV uploaded (a successful file_upload) — the hard requirement;
    - reached a review/submit step (the latest page shows a Submit/Review
      control or wording), so the form was actually walked to the end;
    - no validation error visible on the latest page.

    Supporting only (not required — portals often autofill these from the
    resume): core identity fields. `last_observation` is the latest page text;
    when empty the review/validation checks are skipped (legacy callers).
    Returns (complete, missing-reasons).
    """
    tools = {f.get("tool") for f in filled_fields}
    labels = " ".join((f.get("label") or "").lower() for f in filled_fields)
    resume_uploaded = "file_upload" in tools
    identity_filled = any(k in labels for k in _IDENTITY_KEYS)

    obs = last_observation.lower()
    has_obs = bool(obs.strip())
    reached_review = (not has_obs) or any(m in obs for m in _REVIEW_MARKERS)
    no_validation_error = not any(m in obs for m in _VALIDATION_ERROR_MARKERS)
    # The proxy appends a deterministic "REQUIRED CHECK" footer to form results;
    # this marker means a required (*) field is still empty (e.g. an unchecked
    # radio a fill_form silently failed to set).
    requireds_complete = _REQUIRED_EMPTY_MARKER not in obs

    missing: list[str] = []
    if not resume_uploaded:
        missing.append("resume/CV not uploaded yet — use the file_upload tool")
    if not identity_filled:
        missing.append(
            "core fields (name/email/phone/address) not filled (ok only if the "
            "form autofilled them)"
        )
    if has_obs and not reached_review:
        missing.append(
            "not at the review/submit step yet — click Next/Continue through "
            "every page until the final Review/Submit step appears"
        )
    if not no_validation_error:
        missing.append(
            "the page still shows validation errors — fix the flagged required "
            "fields before finishing"
        )
    if not requireds_complete:
        missing.append(
            "required (*) fields are still empty per the REQUIRED CHECK — fill "
            "every listed field (click radio/checkbox refs directly) before finishing"
        )

    complete = (
        resume_uploaded
        and reached_review
        and no_validation_error
        and requireds_complete
    )
    return complete, missing


def debug(label: str, value: Any = None) -> None:
    """Operational app-log line (routed to the agent logger + data/logs/app.log)."""
    if value is None:
        log.info("%s", label)
        return
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    log.info("%s: %s", label, _brief(value, 300))


def mcp_tool_to_openai(tool: Any) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.inputSchema or {"type": "object", "properties": {}},
        },
    }


def parse_tool_arguments(arguments: Any) -> dict[str, Any]:
    if arguments is None or arguments == "":
        return {}
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        parsed = json.loads(arguments)
        if not isinstance(parsed, dict):
            raise ValueError("Tool arguments must decode to a JSON object.")
        return parsed
    raise TypeError(f"Unsupported tool argument type: {type(arguments).__name__}")


def result_text(result: Any) -> str:
    parts = [c.text for c in result.content if isinstance(c, TextContent)]
    return "\n".join(parts) if parts else str(result.content)


def create_openrouter_client() -> AsyncOpenAI:
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set. Add it to .env.")

    headers: dict[str, str] = {}
    if referer := os.getenv("OPENROUTER_HTTP_REFERER"):
        headers["HTTP-Referer"] = referer
    if app_name := os.getenv("OPENROUTER_APP_NAME"):
        headers["X-OpenRouter-Title"] = app_name

    options: dict[str, Any] = {
        "api_key": api_key,
        "base_url": OPENROUTER_BASE_URL,
        "timeout": LLM_TIMEOUT,
    }
    if headers:
        options["default_headers"] = headers
    return AsyncOpenAI(**options)


def _brief(value: Any, n: int = 40) -> str:
    """Single-line, length-capped string for ledger entries."""
    s = " ".join(str(value).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _ledger_line(n: int, tool_name: str, args: dict[str, Any], outcome: str) -> str:
    """One compact line summarising an action for the progress ledger."""
    salient = []
    for key in ("url", "target", "key"):
        if args.get(key):
            salient.append(f"{key}={_brief(args[key])}")
    if args.get("paths"):
        salient.append(f"paths={_brief(args['paths'])}")
    if args.get("text"):
        salient.append(f'text="{_brief(args["text"])}"')
    if isinstance(args.get("fields"), list):
        salient.append(f"fields={len(args['fields'])}")
    if args.get("next_goal"):
        salient.append(f"next_goal={_brief(args['next_goal'], 50)}")
    arg_str = ", ".join(salient)

    first = next((ln for ln in outcome.splitlines() if ln.strip()), "")
    return f"{n}. {tool_name}({arg_str}) -> {_brief(first, 120)}"


def _trim_history(messages: list[dict[str, Any]], keep: int) -> None:
    """Bound context: stub all but the last `keep` tool-result snapshots.
    Message order and tool_call/tool pairing are preserved (OpenAI requires it).
    """
    tool_idxs = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for i in tool_idxs[:-keep] if keep else tool_idxs:
        content = messages[i].get("content", "")
        if content != _STUB and len(content) > 300:
            messages[i]["content"] = _STUB


async def _complete_with_retry(
    client: AsyncOpenAI,
    request: dict[str, Any],
    max_retries: int = LLM_MAX_RETRIES,
    trace: Trace | None = None,
    iteration: int | None = None,
) -> Any:
    """Call chat.completions with exponential backoff for transient API errors,
    rate limits (incl. upstream 429s OpenRouter surfaces as another status), and
    error/empty bodies where `choices` is None."""
    last_err: str = "unknown error"
    for attempt in range(max_retries):
        retry_after: float | None = None
        try:
            response = await client.chat.completions.create(**request)
        except _RETRYABLE as exc:  # timeouts, conn, 429 RateLimitError, 5xx
            last_err = f"{type(exc).__name__}: {exc}"
            retry_after = _retry_after(exc)
        except APIStatusError as exc:
            # Only retry status errors that are (or wrap) a rate limit; re-raise
            # genuine client errors (auth, bad request, etc.).
            if not _is_rate_limited(exc):
                raise
            last_err = f"rate-limited ({type(exc).__name__}): {exc}"
            retry_after = _retry_after(exc)
        else:
            if getattr(response, "choices", None):
                return response
            err = (getattr(response, "model_extra", None) or {}).get("error")
            last_err = f"empty response (no choices); error={err}"

        wait = _backoff_seconds(attempt, retry_after)
        log.warning(
            "llm retry attempt=%d error=%s wait_s=%.1f", attempt + 1, last_err, wait
        )
        if trace is not None:
            trace.event(
                "retry",
                iteration=iteration,
                attempt=attempt + 1,
                error=last_err,
                wait_s=round(wait, 1),
            )
        await asyncio.sleep(wait)

    raise RuntimeError(f"LLM call failed after {max_retries} attempts: {last_err}")


async def agent_loop(
    model: str,
    original_task: str,
    messages: list[dict[str, Any]],
    session: ClientSession,
    client: AsyncOpenAI,
    usage_totals: dict[str, Any],
    trace: Trace | None = None,
    filled_fields: list[dict[str, Any]] | None = None,
    run_state: dict[str, Any] | None = None,
    max_iterations: int = 250,
) -> str:
    tools_result = await session.list_tools()
    openrouter_tools = [mcp_tool_to_openai(tool) for tool in tools_result.tools]

    if trace is not None:
        trace.event(
            "run_start",
            model=model,
            task=original_task,
            tools=[t.name for t in tools_result.tools],
            system_prompt=messages[0]["content"] if messages else "",
            profile_chars=len(messages[1]["content"]) if len(messages) > 1 else 0,
        )

    # Running progress ledger — a single system message refreshed each turn so the
    # model retains what it has done without carrying every old page snapshot.
    ledger: list[str] = []
    ledger_msg = {
        "role": "system",
        "content": "PROGRESS LEDGER (actions so far):\n(none yet)",
    }
    messages.insert(2, ledger_msg)
    action_n = 0
    finish_attempts = 0
    repeat_counts: dict[str, int] = {}
    last_observation = ""  # latest successful page text — feeds the evidence gate
    if run_state is not None:
        run_state.setdefault("terminal_status", None)
        run_state.setdefault("last_observation", "")

    for iteration in range(1, max_iterations + 1):
        ledger_msg["content"] = "PROGRESS LEDGER (actions so far):\n" + (
            "\n".join(ledger) if ledger else "(none yet)"
        )

        request: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "tools": openrouter_tools,
            "tool_choice": "auto",
            # ask OpenRouter to include token usage + cost in the response
            "extra_body": {"usage": {"include": True}},
        }

        response = await _complete_with_retry(
            client, request, trace=trace, iteration=iteration
        )
        message = response.choices[0].message
        usage = getattr(response, "usage", None)
        _accumulate_usage(usage_totals, usage)

        debug("iteration", iteration)
        # Lightweight "thinking": the model's one-line plan rides in plain content
        # (no API reasoning / thought signatures — keeps it portable + cheap).
        debug("thought", message.content or "(none)")

        tool_calls = [
            {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
            for tc in (message.tool_calls or [])
        ]
        if trace is not None:
            trace.event(
                "llm_turn",
                iteration=iteration,
                assistant_content=message.content or "",
                tool_calls=tool_calls,
                usage={
                    "prompt": getattr(usage, "prompt_tokens", None),
                    "completion": getattr(usage, "completion_tokens", None),
                    "total": getattr(usage, "total_tokens", None),
                    "cost": getattr(usage, "cost", None),
                },
                finished=not message.tool_calls,
            )

        if not message.tool_calls:
            # The model wants to stop. Only accept that as "done" if evidence
            # supports it; otherwise hint and make it keep going.
            if filled_fields is None:
                return message.content or ""
            complete, missing = _completion_evidence(filled_fields, last_observation)
            if complete or finish_attempts >= MAX_FINISH_HINTS:
                if not complete:
                    log.warning(
                        "finishing without full evidence after %d hint(s): %s",
                        finish_attempts,
                        "; ".join(missing),
                    )
                if run_state is not None:
                    run_state["success"] = complete
                    run_state["missing"] = missing
                return message.content or ""

            finish_attempts += 1
            log.warning(
                "model tried to finish without evidence (hint %d/%d): %s",
                finish_attempts,
                MAX_FINISH_HINTS,
                "; ".join(missing),
            )
            if trace is not None:
                trace.event(
                    "finish_blocked",
                    iteration=iteration,
                    attempt=finish_attempts,
                    missing=missing,
                )
            # keep history valid: record the model's (content-only) turn, then nudge
            messages.append({"role": "assistant", "content": message.content or ""})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "You are NOT finished — do not write a summary or claim completion. "
                        "Evidence check failed: " + "; ".join(missing) + ". "
                        "Take the next concrete action now: if the resume is not uploaded, "
                        "click the resume Attach/Upload control, then call file_upload with "
                        "the resume path from the profile; then fill any remaining required "
                        "(*) fields. Continue until the resume is uploaded and required "
                        "fields are filled, then reach the final review/submit step."
                    ),
                }
            )
            continue

        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [
                tc.model_dump(exclude_none=True) for tc in message.tool_calls
            ],
        }
        messages.append(assistant_message)

        looped_tools: list[str] = []
        for tool_call in message.tool_calls:
            tool_name = tool_call.function.name
            arguments: dict[str, Any] = {}
            ok = True
            blocked = False
            try:
                arguments = parse_tool_arguments(tool_call.function.arguments)
                debug("tool_call", {"name": tool_name, "arguments": arguments})
                if (
                    ENFORCE_UPLOAD_FIRST
                    and tool_name in _VALUE_FILL_TOOLS
                    and filled_fields is not None
                    and not _resume_uploaded(filled_fields)
                    and _autofill_available(last_observation)
                ):
                    # Upload-first: refuse manual entry while an autofill/upload-
                    # resume option is available and the resume isn't uploaded.
                    blocked = True
                    tool_content = _UPLOAD_FIRST_MSG
                    log.warning(
                        "upload-first: blocked %s before resume upload", tool_name
                    )
                    if trace is not None:
                        trace.event(
                            "upload_first_block", iteration=iteration, tool=tool_name
                        )
                else:
                    result = await session.call_tool(tool_name, arguments)
                    tool_content = result_text(result)
                    debug(
                        "tool_call_result", {"name": tool_name, "result": tool_content}
                    )
                    if _tool_failed(tool_content):
                        ok = False  # failure delivered as normal text (e.g. session died)
                        log.warning(
                            "tool %s reported failure: %s",
                            tool_name,
                            _brief(tool_content, 120),
                        )
            except Exception as exc:
                ok = False
                tool_content = (
                    f"Tool call {tool_name!r} failed: {type(exc).__name__}: {exc}"
                )
                log.error("tool %s failed: %s", tool_name, tool_content)

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": tool_content,
                }
            )
            action_n += 1
            ledger.append(_ledger_line(action_n, tool_name, arguments, tool_content))
            if trace is not None:
                trace.event(
                    "tool_result",
                    iteration=iteration,
                    tool_call_id=tool_call.id,
                    name=tool_name,
                    arguments=arguments,
                    result=tool_content,
                    ok=ok,
                )
            if ok and not blocked and filled_fields is not None:
                for entry in _filled_fields(tool_name, arguments):
                    filled_fields.append({"iteration": iteration, **entry})

            # Track the latest page text (feeds the evidence gate) and bail out
            # gracefully on a terminal page state instead of looping to re-apply.
            if ok and not blocked and tool_content:
                last_observation = tool_content
                if run_state is not None:
                    run_state["last_observation"] = tool_content
                status = _terminal_status(tool_content)
                if status is not None:
                    log.warning("terminal page state '%s' — exiting gracefully", status)
                    if run_state is not None:
                        run_state["terminal_status"] = status
                    if trace is not None:
                        trace.event(
                            "terminal_state", iteration=iteration, status=status
                        )
                    note = {
                        "already_applied": "You have already applied for this job.",
                        "closed": "This posting is no longer accepting applications.",
                    }.get(status, status)
                    return (
                        f"Stopped: {note} No application was submitted; nothing "
                        "further to do for this posting."
                    )

            # Repeated-action guard: same action+target called REPEAT_LIMIT times.
            sig = _action_signature(tool_name, arguments)
            if sig is not None:
                repeat_counts[sig] = repeat_counts.get(sig, 0) + 1
                if repeat_counts[sig] >= REPEAT_LIMIT:
                    looped_tools.append(tool_name)
                    repeat_counts[sig] = 0  # reset; re-fire only if it loops again
                    log.warning(
                        "repeat guard: %s called %d× with same target (%s)",
                        tool_name,
                        REPEAT_LIMIT,
                        sig,
                    )
                    if trace is not None:
                        trace.event(
                            "repeat_guard",
                            iteration=iteration,
                            signature=sig,
                            count=REPEAT_LIMIT,
                        )

        if looped_tools:
            names = ", ".join(sorted(set(looped_tools)))
            messages.append(
                {"role": "user", "content": _REPEAT_GUARD_MSG.format(names=names)}
            )

        # Bound context: stub old page snapshots.
        _trim_history(messages, HISTORY_KEEP_SNAPSHOTS)

    raise RuntimeError(f"Agent did not finish after {max_iterations} iterations.")


async def main(job_url: str, model: str) -> None:
    run_id = new_run_id()
    setup_logging(run_id)
    trace = Trace(run_id)
    log.info("run start: model=%s job_url=%s", model, job_url)

    profile = load_profile()
    task = (
        f"Apply for this job: {job_url}\n\n"
        "Fill the application using the applicant profile, upload the resume, "
        "and STOP before the final submit so the user can review."
    )

    client = create_openrouter_client()
    usage = _new_usage()
    filled_fields: list[dict[str, Any]] = []
    start = time.monotonic()
    success = False
    error: str | None = None
    terminal_status: str | None = None
    result = ""
    run_state: dict[str, Any] = {}
    try:
        async with streamable_http_client(PROXY_MCP_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                messages: list[dict[str, Any]] = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "system", "content": f"APPLICANT PROFILE:\n\n{profile}"},
                    {"role": "user", "content": task},
                ]
                result = await agent_loop(
                    model,
                    task,
                    messages,
                    session,
                    client,
                    usage,
                    trace=trace,
                    filled_fields=filled_fields,
                    run_state=run_state,
                )
                terminal_status = run_state.get("terminal_status")
                if terminal_status:
                    # Graceful no-op (already applied / closed): not an error, but
                    # not a completed application either.
                    success = False
                    log.info("terminal state: %s", terminal_status)
                else:
                    # success reflects EVIDENCE (resume uploaded + reached review
                    # + no validation error), not just that the model stopped.
                    success, missing = _completion_evidence(
                        filled_fields, run_state.get("last_observation", "")
                    )
                    if not success:
                        log.warning(
                            "run ended without completion evidence: %s",
                            "; ".join(missing),
                        )
                log.info("final: %s", result)
                print(f"final: {result}")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        log.exception("run failed")
        raise
    finally:
        duration = time.monotonic() - start
        outcome = _outcome(success, terminal_status)
        _record_usage(
            usage,
            run_id,
            model,
            job_url,
            usage["requests"],
            success,
            error,
            duration,
            outcome,
        )
        trace.event(
            "run_end",
            iterations=usage["requests"],
            success=success,
            outcome=outcome,
            final_text=result,
            error=error,
            usage_totals=usage,
            duration_s=round(duration, 2),
        )
        form_path = write_form_record(
            run_id,
            {
                "run_id": run_id,
                "time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "model": model,
                "job_url": job_url,
                "success": success,
                "outcome": outcome,
                "error": error,
                "fields": filled_fields,
                "final_summary": result,
            },
        )
        log.info("trace=%s form=%s", trace.path, form_path)
        await client.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(
            'Usage: uv run python -m agent.loop "<url_index>" starting with 1'
        )

    with open("urls.txt", "r") as f:
        urls = f.readlines()
    url_index = int(sys.argv[1]) - 1

    # job_url=sys.argv[1]
    job_url = urls[url_index]

    asyncio.run(main(job_url=job_url, model=DEFAULT_MODEL))

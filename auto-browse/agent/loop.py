"""Minimal browser agent loop.

Calls an OpenRouter chat model in a tool-use loop over the text-extracting Playwright
proxy MCP: the model sees each page's visible text from a tool result (and calls
`fetch_aria` to recover element refs when it needs to act), driving the browser until
it stops (no tool call) or the iteration cap is hit.
Every turn is written to a JSONL trace for the trace viewer. Deliberately bare —
no pruning, no evidence gates, no guards. A clean base to build on.
"""

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

from agent.apply import resolve_resume
from agent.profile import build_profile
from common import db, repo
from common import storage as storagemod
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent
from openai import (
    APIConnectionError,
    APIError,
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
)

load_dotenv()

log = logging.getLogger(LOGGER_NAME)

# Human-in-the-loop handoff scenarios: when the page shows one of these, the agent
# must stop and hand control to the human operator instead of attempting it itself.
# Add new triggers here — the list is injected into the system prompt below, so new
# scenarios need no other code changes.
HIL_SCENARIOS: list[str] = [
    "A verification / one-time passcode (OTP) is requested (SMS or authenticator app).",
    "You are asked to confirm via a link or code sent to the applicant's email.",
    "A bot-detection / CAPTCHA / 'are you human' challenge or puzzle appears.",
]

# Name of the virtual tool the agent calls to hand off to the human. Handled in the
# agent loop (not forwarded to the browser MCP) — see _human_handoff.
HUMAN_TOOL_NAME = "request_human_help"


def _handoff_block(scenarios: list[str]) -> str:
    bullets = "\n".join(f"  - {s}" for s in scenarios)
    return (
        "HUMAN HANDOFF — some steps must be done by the human, not you. If the page "
        "shows ANY of the following, do NOT attempt it yourself: call "
        f"`{HUMAN_TOOL_NAME}` with a short `reason` and clear `instructions` telling "
        "the human what to do on screen, then resume once it returns the human's "
        "response (take a fresh `browser_snapshot` first, since the page changed):\n"
        f"{bullets}\n"
        "EXCEPTION for a CAPTCHA / bot-detection challenge: before handing off, FIRST "
        "look around for an accessibility option or an alternative challenge you CAN "
        "solve yourself — e.g. an 'audio', 'accessibility', or 'try a different "
        "challenge'/refresh control, or a simple text/logic question. Switch to and "
        "solve that if one exists. Only call "
        f"`{HUMAN_TOOL_NAME}` if no solvable alternative is available (e.g. it requires "
        "selecting images or solving a visual puzzle)."
    )


SYSTEM_PROMPT = (
    """You are a browser agent applying for a job on the applicant's behalf, using the APPLICANT PROFILE below and the browser tools provided.

WORKFLOW: you are given a job URL. `browser_navigate` to it, then read the page. The action tools (`browser_navigate`, `browser_click`, `browser_type`, `browser_fill_form`) return the page's VISIBLE TEXT — not refs. To act on an element you need its `ref`: `fetch_aria` searches the page's ARIA for one or more text keys and returns each match's `[ref=...]`. So the loop is: read the visible text, `fetch_aria` the label(s) you want, then click/type using that ref as `target`. After every action the page text updates so you can continue.

GOAL: fill every field across every step and reach the final review/submit step. Upload the resume along the way. Do NOT click the final Submit.

Eg — fill the name:
user: "url: https://job-boards.greenhouse.io/imaginepediatrics/jobs/4709050005"
agent: browser_navigate(url="https://job-boards.greenhouse.io/imaginepediatrics/jobs/4709050005")
result: "Some Role, Some Company ... Apply  Name*: type here  Autofill..."
agent: fetch_aria(keys=["Name"])
result:
"Name": |2-
  - generic [ref=e159]: Name*
  - textbox "Name*" [ref=e161] [cursor=pointer]:
    - /placeholder: Type here...
agent: browser_type(target="e161", text="John Doe")
result: "Some Role, Some Company ... Name*: John Doe  Autofill..."

`fetch_aria` takes a `keys` array — pass several field labels at once to get all their refs in one call (cheaper than one call per field). The result is a YAML map: one entry per key in the order you asked, the value a literal block (`|2-`) of the matched ARIA lines, or `(no match)`. The `context` arg sets how many lines are kept AFTER each match — raise it to capture a full option list. Eg: fetch_aria(keys=["First Name","Last Name","Email"], context=4).

Repeat for every field and every step. Use `browser_fill_form` to fill several fields in one call. The other thing you'll often do is sign in or create an account on the portal — that is also just a fill + button click.

Dropdowns are the tricky part:
- Some comboboxes already list all their options in the page text — raise `fetch_aria`'s `context` to capture the whole list (country code, country, state, ...).
- Others must be expanded first: click the combobox or its caret/chevron expand button; the options then render (often at the BOTTOM of the page text). `fetch_aria` the option you want, then click its ref.
- Options can be NESTED: clicking one (e.g. a category) may just reveal more sub-options instead of choosing a value. A click is NOT a selection until the field actually shows a chosen value — the option reads `checked` / `selected`, or the field shows `1 item selected` / the value and is no longer `Expanded`. While it still shows `not checked`, `Select One`, or a freshly revealed list, keep `fetch_aria`-ing and clicking the next option down until the field is filled. Never move on assuming the first click stuck.

Eg — a dropdown that must be expanded:
agent: fetch_aria(keys=["State"])
result:
"State": |2-
  - generic [ref=e298]: State*
  - button "State Select One Required" [ref=e302]: Select One
agent: browser_click(target="e302")
result: "... (options render at the bottom) Select One  Alabama  Alaska  Arizona  Arkansas  California ..."
agent: fetch_aria(keys=["California"])
result:
"California": |2-
  - option "California" [ref=e436]:
    - generic [ref=e437]: California
  - option "Colorado" [ref=e438]: ...
agent: browser_click(target="e436")

MUST DOs:
- Pass a ref as the `target` argument EXACTLY as shown, with no decoration. For the line `- button "Apply Now" [ref=e76] [cursor=pointer]`, click with: browser_click({"element": "Apply Now button", "target": "e76"}). Never wrap the ref in a selector (NOT `button[ref=e76]`, NOT `#e76`) and never invent a CSS/text selector — only the bare ref token (`e76`) matches.
- Navigate to the job URL, follow Apply links, fill the application from the profile, and upload the resume (path is at the top of the profile).
- Prefer autofill from the uploaded RESUME when the page offers it. Sign-in / account-based autofill is allowed where the portal requires it (per-ATS rules may forbid it).
- If the portal requires an account: ALWAYS try to SIGN IN FIRST, using the Login Email and Login Password from the profile (use that same given password for every site). Only if sign-in fails because no account exists (e.g. "account not found", "no user", "incorrect email") should you then CREATE an account, reusing the same email and given password. Never create an account before trying to sign in.
- Work through every page/step. Do NOT click the final Submit — stop before it and return a short plain-text summary of what you filled and anything you couldn't.
- State your next step in one short sentence before each tool call."""
    + "\n\n"
    + _handoff_block(HIL_SCENARIOS)
)

# Virtual tool exposed to the model alongside the browser MCP tools. Calling it
# pauses the run and hands control to the human (see _human_handoff).
HUMAN_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": HUMAN_TOOL_NAME,
        "description": (
            "Hand control to the human operator for a step you must NOT do yourself "
            "(verification code, email confirmation, CAPTCHA/bot check). The run "
            "pauses; the human acts on the browser screen and types a response (e.g. "
            "'continue' or a verification code), which is returned to you so you can "
            "resume from the current page."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "Which handoff scenario triggered this. "
                        'Example: reason="verification code required".'
                    ),
                },
                "instructions": {
                    "type": "string",
                    "description": (
                        "What the human should do on screen before the run continues. "
                        'Example: instructions="Enter the 6-digit code from the '
                        'applicant\'s email, then type continue".'
                    ),
                },
            },
            "required": ["reason", "instructions"],
        },
    },
}

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
PROXY_MCP_URL = os.getenv("PROXY_MCP_URL", "http://localhost:8932/mcp")
DEFAULT_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-3-flash-preview")

HERE = Path(__file__).resolve().parent.parent
ATS_INSTRUCTIONS_PATH = Path(
    os.getenv("ATS_INSTRUCTIONS_PATH", HERE / "input" / "ats_instructions.md")
)

MAX_ITERATIONS = int(os.getenv("MAX_ITERATIONS", "250"))

# Hard cost ceiling per application run. Once accrued LLM spend reaches this, the
# loop aborts before issuing another (billable) call — a wedged/looping run can't
# quietly rack up dollars. Cost is only known after each call returns, so the guard
# trips right after the call that crosses the line.
MAX_COST_USD = float(os.getenv("MAX_COST_USD", "1.0"))

# Hard cap on tokens the model may emit per call. Bounds the blast radius of a
# wedged generation (a degenerate run once spat 203 KB of a single repeated token).
# 4096 leaves headroom for large `browser_fill_form` arg lists so real tool calls
# are not truncated mid-emission.
MAX_COMPLETION_TOKENS = int(os.getenv("MAX_COMPLETION_TOKENS", "4096"))

# A no-tool-call turn that is truncated (finish_reason=length) or degenerate is not
# a real "done" — likely the accumulated history wedged the model. Rebuild a fresh
# minimal context (system prompt + profile + current page) and retry, up to N times
# before failing the run.
MAX_CONTEXT_RESETS = int(os.getenv("MAX_CONTEXT_RESETS", "2"))

# LLM call resilience.
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "6"))
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "120"))
LLM_BACKOFF_BASE = float(os.getenv("LLM_BACKOFF_BASE", "2"))  # seconds
LLM_BACKOFF_CAP = float(os.getenv("LLM_BACKOFF_CAP", "60"))  # seconds
_RETRYABLE = (APITimeoutError, APIConnectionError, RateLimitError, InternalServerError)
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
    if getattr(exc, "status_code", None) == 429:
        return True
    return any(m in str(exc).lower() for m in _RATE_LIMIT_MARKERS)


def _retry_after(exc: Any) -> float | None:
    resp = getattr(exc, "response", None)
    val = resp.headers.get("retry-after") if resp is not None else None
    try:
        return float(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _backoff_seconds(attempt: int, retry_after: float | None) -> float:
    base = min(LLM_BACKOFF_BASE * (2**attempt), LLM_BACKOFF_CAP)
    wait = random.uniform(0, base)  # full jitter
    if retry_after is not None:
        wait = max(wait, retry_after)
    return min(wait, LLM_BACKOFF_CAP)


def load_ats_instructions() -> str | None:
    """ATS/domain-specific rules. Optional — return None if the file is absent."""
    if not ATS_INSTRUCTIONS_PATH.is_file():
        return None
    text = ATS_INSTRUCTIONS_PATH.read_text(encoding="utf-8").strip()
    return text or None


def _new_usage() -> dict[str, Any]:
    return {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0.0}


def _accumulate_usage(totals: dict[str, Any], usage: Any) -> None:
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


def _looks_degenerate(text: str) -> bool:
    """A completion is degenerate if it's long but built from almost no distinct
    characters — a repeated-token loop (e.g. `aminase` × 29000, 203 KB, ~7 uniques).
    A normal short final message can't trip either threshold."""
    return len(text) > 8000 and len(set(text)) < 40


def _record_usage(
    totals: dict[str, Any],
    run_id: str,
    model: str,
    job_url: str,
    iterations: int,
    error: str | None,
    duration_s: float,
) -> None:
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
        "error": error,
        "duration_s": round(duration_s, 2),
    }
    append_usage(record)
    log.info(
        "run usage: requests=%d total_tokens=%s cost=$%.6f iters=%d duration=%.1fs",
        record["requests"],
        f"{record['total_tokens']:,}",
        record["cost_usd"],
        iterations,
        duration_s,
    )


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


def _brief(value: Any, n: int = 300) -> str:
    s = " ".join(str(value).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def debug(label: str, value: Any = None) -> None:
    if value is None:
        log.info("%s", label)
        return
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    log.info("%s: %s", label, _brief(value))


async def _complete_with_retry(
    client: AsyncOpenAI,
    request: dict[str, Any],
    max_retries: int = LLM_MAX_RETRIES,
    trace: Trace | None = None,
    iteration: int | None = None,
) -> Any:
    """chat.completions with exponential backoff for transient errors, rate limits
    (incl. upstream 429s OpenRouter surfaces as another status), and empty bodies."""
    last_err = "unknown error"
    for attempt in range(max_retries):
        retry_after: float | None = None
        try:
            response = await client.chat.completions.create(**request)
        except _RETRYABLE as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            retry_after = _retry_after(exc)
        except APIStatusError as exc:
            if not _is_rate_limited(exc):
                raise
            last_err = f"rate-limited ({type(exc).__name__}): {exc}"
            retry_after = _retry_after(exc)
        except (json.JSONDecodeError, APIError) as exc:
            # OpenRouter sometimes returns a non-JSON / malformed body (HTML gateway
            # error, empty or truncated response) — the SDK's response.json() then
            # raises JSONDecodeError. Treat it as transient and retry, don't crash.
            last_err = f"bad response body: {type(exc).__name__}: {exc}"
        else:
            if getattr(response, "choices", None):
                return response
            err = (getattr(response, "model_extra", None) or {}).get("error")
            last_err = f"empty response (no choices); error={err}"

        wait = _backoff_seconds(attempt, retry_after)
        log.warning("llm retry attempt=%d error=%s wait_s=%.1f", attempt + 1, last_err, wait)
        if trace is not None:
            trace.event(
                "retry", iteration=iteration, attempt=attempt + 1, error=last_err, wait_s=round(wait, 1)
            )
        await asyncio.sleep(wait)

    raise RuntimeError(f"LLM call failed after {max_retries} attempts: {last_err}")


async def _human_handoff(
    arguments: dict[str, Any], trace: Trace | None, iteration: int
) -> str:
    """Pause the run and hand control to the human operator.

    The human can act two ways, in either combination: (1) change the page directly
    in the browser over VNC, and/or (2) type a meaningful response here (a
    verification code, an instruction, or just 'continue'). Their typed response is
    injected back into the conversation as this tool's result; whatever they changed
    on screen is picked up by the next `browser_snapshot`.
    """
    reason = str(arguments.get("reason") or "human action required")
    instructions = str(
        arguments.get("instructions") or "Complete the step on screen, then type 'continue'."
    )
    bar = "=" * 64
    banner = (
        f"\n{bar}\n"
        f"  HUMAN HANDOFF NEEDED — {reason}\n"
        f"  On the browser screen (VNC): {instructions}\n"
        "  When done, type a response (a code, an instruction, or 'continue')\n"
        "  and press Enter to resume.\n"
        f"{bar}\n> "
    )
    if trace is not None:
        trace.event(
            "human_handoff_request",
            iteration=iteration,
            reason=reason,
            instructions=instructions,
        )
    try:
        response = (await asyncio.to_thread(input, banner)).strip()
    except EOFError:  # non-interactive run — nothing to wait on
        response = ""
    log.info("human handoff (%s) -> %r", reason, response)
    if trace is not None:
        trace.event("human_handoff_response", iteration=iteration, response=response)
    if not response:
        response = "continue"
    return (
        f"HUMAN RESPONSE: {response}\n"
        "The human has finished the requested step (and may have changed the page "
        "directly). Take a fresh `browser_snapshot` to see the current page, then "
        "continue the application."
    )


async def _current_page_text(session: ClientSession) -> str:
    """Fresh visible-text snapshot of the page, used to reseed a reset context.
    Degrades to a note rather than crashing recovery if the snapshot fails."""
    try:
        return result_text(await session.call_tool("browser_snapshot", {}))
    except Exception as exc:
        return f"(snapshot unavailable: {type(exc).__name__})"


async def agent_loop(
    model: str,
    original_task: str,
    messages: list[dict[str, Any]],
    session: ClientSession,
    client: AsyncOpenAI,
    usage_totals: dict[str, Any],
    trace: Trace | None = None,
    max_iterations: int = MAX_ITERATIONS,
) -> str:
    tools_result = await session.list_tools()
    # Browser MCP tools + the virtual human-handoff tool (handled in-loop).
    openrouter_tools = [mcp_tool_to_openai(tool) for tool in tools_result.tools]
    openrouter_tools.append(HUMAN_TOOL)

    if trace is not None:
        trace.event(
            "run_start",
            model=model,
            task=original_task,
            tools=[t.name for t in tools_result.tools] + [HUMAN_TOOL_NAME],
            system_prompt=messages[0]["content"] if messages else "",
            profile_chars=len(messages[1]["content"]) if len(messages) > 1 else 0,
        )

    # Seed context (system prompt(s) + profile + ATS + user task) as it stands before
    # any assistant/tool turns — restored verbatim on a degenerate-turn context reset.
    # Shallow copy is safe: the message dicts are never mutated in place.
    base_messages = list(messages)
    context_resets = 0

    for iteration in range(1, max_iterations + 1):
        request: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "tools": openrouter_tools,
            "tool_choice": "auto",
            "max_tokens": MAX_COMPLETION_TOKENS,
            "extra_body": {"usage": {"include": True}},
        }

        response = await _complete_with_retry(client, request, trace=trace, iteration=iteration)
        message = response.choices[0].message
        usage = getattr(response, "usage", None)
        _accumulate_usage(usage_totals, usage)

        if usage_totals["cost"] >= MAX_COST_USD:
            raise RuntimeError(
                f"cost guard: run spent ${usage_totals['cost']:.4f} "
                f">= ${MAX_COST_USD:.2f} cap — aborting before next call"
            )

        debug("iteration", iteration)
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
            finish = response.choices[0].finish_reason
            content = message.content or ""
            if finish != "length" and not _looks_degenerate(content):
                return content  # genuine, clean completion — the agent is done
            # Truncated or degenerate: the accumulated history likely wedged the
            # model. Reset to a fresh minimal context and retry before giving up.
            if context_resets < MAX_CONTEXT_RESETS:
                context_resets += 1
                page = await _current_page_text(session)
                messages[:] = base_messages + [
                    {
                        "role": "user",
                        "content": (
                            "[Recovering from a stalled turn — context was reset.]\n"
                            f"CURRENT PAGE:\n{page}\n\n"
                            "Continue the application from here."
                        ),
                    }
                ]
                log.warning(
                    "degenerate turn (finish=%s len=%d) — context reset %d/%d",
                    finish, len(content), context_resets, MAX_CONTEXT_RESETS,
                )
                if trace is not None:
                    trace.event(
                        "context_reset",
                        iteration=iteration,
                        finish_reason=finish,
                        content_len=len(content),
                        attempt=context_resets,
                    )
                continue
            raise RuntimeError(
                f"degenerate/truncated completion after {MAX_CONTEXT_RESETS} context "
                f"resets (finish={finish}, len={len(content)}, uniq={len(set(content))})"
            )

        messages.append(
            {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [tc.model_dump(exclude_none=True) for tc in message.tool_calls],
            }
        )

        for tool_call in message.tool_calls:
            tool_name = tool_call.function.name
            arguments: dict[str, Any] = {}
            ok = True
            try:
                arguments = parse_tool_arguments(tool_call.function.arguments)
                debug("tool_call", {"name": tool_name, "arguments": arguments})
                if tool_name == HUMAN_TOOL_NAME:
                    # Virtual tool: pause for the human instead of calling the MCP.
                    tool_content = await _human_handoff(arguments, trace, iteration)
                else:
                    result = await session.call_tool(tool_name, arguments)
                    tool_content = result_text(result)
                    debug("tool_call_result", {"name": tool_name, "result": tool_content})
            except Exception as exc:
                ok = False
                tool_content = f"Tool call {tool_name!r} failed: {type(exc).__name__}: {exc}"
                log.error("tool %s failed: %s", tool_name, tool_content)

            messages.append(
                {"role": "tool", "tool_call_id": tool_call.id, "content": tool_content}
            )
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

    raise RuntimeError(f"Agent did not finish after {max_iterations} iterations.")


# Resume is staged here on the host, then uploaded by the browser. This dir is bind-
# mounted into the playwright container as RESUME_CONTAINER_ROOT (compose.yaml:
# ./output/resume -> /resume), so the defaults must stay in sync with that mount.
SCRATCH_ROOT = os.getenv("SCRATCH_ROOT", str(HERE / "output" / "resume"))
# path root the resume is reachable at INSIDE the playwright container (mount of SCRATCH_ROOT)
RESUME_CONTAINER_ROOT = os.getenv("RESUME_CONTAINER_ROOT", "/resume")


async def main(user_id: str, job_url: str, model: str) -> None:
    run_id = new_run_id()
    setup_logging(run_id)
    trace = Trace(run_id)
    log.info("run start: model=%s user=%s job_url=%s", model, user_id, job_url)

    storage = storagemod.from_env()
    host_resume = f"{SCRATCH_ROOT}/{run_id}/resume.pdf"
    container_resume = f"{RESUME_CONTAINER_ROOT}/{run_id}/resume.pdf"
    with db.connect() as conn:
        job_id = repo.get_or_create_job(conn, job_url, source="manual")
        _, source = await resolve_resume(conn, storage, user_id, job_id, host_resume)
        log.info("resume resolved (%s) -> %s", source, host_resume)
        profile = build_profile(conn, user_id, container_resume)
    ats_instructions = load_ats_instructions()
    task = (
        f"Apply for this job: {job_url}\n\n"
        "Fill the application using the applicant profile, upload the resume, "
        "and STOP before the final submit so the user can review."
    )

    client = create_openrouter_client()
    usage = _new_usage()
    start = time.monotonic()
    error: str | None = None
    result = ""
    try:
        async with streamable_http_client(PROXY_MCP_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                messages: list[dict[str, Any]] = [
                    {"role": "system", "content": f"{SYSTEM_PROMPT}\n\nToday's date is {today}."},
                    {"role": "system", "content": f"APPLICANT PROFILE:\n\n{profile}"},
                ]
                if ats_instructions:
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                "ATS / DOMAIN-SPECIFIC INSTRUCTIONS — match the current "
                                "page's host to a section and follow its rules; these "
                                f"override generic guidance:\n\n{ats_instructions}"
                            ),
                        }
                    )
                messages.append({"role": "user", "content": task})
                result = await agent_loop(
                    model, task, messages, session, client, usage, trace=trace
                )
                log.info("final: %s", result)
                print(f"final: {result}")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        log.exception("run failed")
        raise
    finally:
        duration = time.monotonic() - start
        _record_usage(usage, run_id, model, job_url, usage["requests"], error, duration)
        trace.event(
            "run_end",
            iterations=usage["requests"],
            outcome="error" if error else "finished",
            success=error is None,
            final_text=result,
            error=error,
            usage_totals=usage,
            duration_s=round(duration, 2),
        )
        log.info("trace=%s", trace.path)
        await client.close()


if __name__ == "__main__":
    # Usage: uv run python -m agent.loop <user_id> <idx>
    # <idx> is a 0-based index into urls.txt (one URL per line; blanks ignored).
    # idx 0 == the first line, so editor line N == idx N-1.
    if len(sys.argv) < 3:
        raise SystemExit("Usage: uv run python -m agent.loop <user_id> <idx>")
    user_id = sys.argv[1]
    idx = int(sys.argv[2])
    # Read relative to this file, not the CWD, so it works from anywhere.
    urls = [ln.strip() for ln in (HERE / "input" / "urls.txt").read_text().splitlines() if ln.strip()]
    if not 0 <= idx < len(urls):
        raise SystemExit(f"idx {idx} out of range (0..{len(urls) - 1}); urls.txt has {len(urls)} URLs")
    job_url = urls[idx]
    print(f"[{idx}] {job_url}")
    asyncio.run(main(user_id=user_id, job_url=job_url, model=DEFAULT_MODEL))

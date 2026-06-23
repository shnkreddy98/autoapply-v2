import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent
from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)

load_dotenv()

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
8. STOP BEFORE SUBMITTING. Do NOT click the final Submit / Apply / Send button. \
   Once every field is filled and the resume uploaded, take a screenshot, then \
   return a plain-text summary listing each field you filled and anything you \
   could not complete, and tell the user to review in the browser and submit \
   manually. (Signing in or creating an account is allowed and expected; only \
   the final application submit is off-limits.)

Browsing tips:
- `snapshot(goal=...)` returns a small pruned tree to find WHERE things are; \
  `expand_region(ref)` reads the FULL content of a region (labels, values).
- Pass `next_goal` on actions to keep the returned snapshot focused.
- If a snapshot is empty or a ref errors "not found", call `wait_for` then \
  `snapshot` again — never reuse a ref from an older snapshot.

Thinking: before each tool call, state your plan in ONE short sentence as your \
message content (e.g. "Fill the email field, then click Next") — then make the \
call. Keep it to one line; do not write long explanations.

Be methodical and persistent. Never submit. If a field's value is missing from \
the profile, leave it blank and note it in your final summary."""

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
PROXY_MCP_URL = os.getenv("PROXY_MCP_URL", "http://localhost:8932/mcp")
DEFAULT_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-3-flash-preview")

HERE = Path(__file__).resolve().parent.parent
PROFILE_PATH = Path(
    os.getenv("PROFILE_PATH", HERE / "data" / "profile" / "profile.txt")
)
USAGE_PATH = Path(os.getenv("USAGE_PATH", HERE / "data" / "usage.txt"))

# Context management (Tier 1): keep only the last N tool-result snapshots full;
# older ones are stubbed since refs are only valid on the latest tree and the
# profile is the durable source of truth. See README "Context management".
HISTORY_KEEP_SNAPSHOTS = int(os.getenv("HISTORY_KEEP_SNAPSHOTS", "2"))
_STUB = "[older page snapshot dropped to save context — see PROGRESS LEDGER]"

# LLM call resilience.
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "4"))
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "120"))
_RETRYABLE = (APITimeoutError, APIConnectionError, RateLimitError, InternalServerError)


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


def write_usage(totals: dict[str, Any], model: str, job_url: str) -> None:
    """Overwrite the usage report for the most recent run."""
    report = (
        "auto-browse run usage\n"
        f"Time (UTC):        {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}\n"
        f"Model:             {model}\n"
        f"Job URL:           {job_url}\n"
        f"LLM requests:      {totals['requests']}\n"
        f"Prompt tokens:     {totals['prompt_tokens']:,}\n"
        f"Completion tokens: {totals['completion_tokens']:,}\n"
        f"Total tokens:      {totals['total_tokens']:,}\n"
        f"Cost (USD):        ${totals['cost']:.6f}\n"
    )
    USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    USAGE_PATH.write_text(report, encoding="utf-8")
    print("\n" + report + f"(saved to {USAGE_PATH})")


def debug(label: str, value: Any = None) -> None:
    """Print a DEBUG-prefixed line; JSON-format dicts/lists."""
    if value is None:
        print(f"DEBUG {label}")
        return
    if isinstance(value, (dict, list)):
        value = json.dumps(value, indent=2, ensure_ascii=False, default=str)
    print(f"DEBUG {label}: {value}")


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
    client: AsyncOpenAI, request: dict[str, Any], max_retries: int = LLM_MAX_RETRIES
) -> Any:
    """Call chat.completions with retry/backoff for transient API errors and for
    error/empty bodies where `choices` is None (an OpenRouter provider error)."""
    last_err: str = "unknown error"
    for attempt in range(max_retries):
        try:
            response = await client.chat.completions.create(**request)
        except _RETRYABLE as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        else:
            if getattr(response, "choices", None):
                return response
            err = (getattr(response, "model_extra", None) or {}).get("error")
            last_err = f"empty response (no choices); error={err}"

        wait = min(2**attempt, 30)
        debug("llm_retry", {"attempt": attempt + 1, "error": last_err, "wait_s": wait})
        await asyncio.sleep(wait)

    raise RuntimeError(f"LLM call failed after {max_retries} attempts: {last_err}")


async def agent_loop(
    model: str,
    original_task: str,
    messages: list[dict[str, Any]],
    session: ClientSession,
    client: AsyncOpenAI,
    usage_totals: dict[str, Any],
    max_iterations: int = 250,
) -> str:
    tools_result = await session.list_tools()
    openrouter_tools = [mcp_tool_to_openai(tool) for tool in tools_result.tools]

    # Running progress ledger — a single system message refreshed each turn so the
    # model retains what it has done without carrying every old page snapshot.
    ledger: list[str] = []
    ledger_msg = {
        "role": "system",
        "content": "PROGRESS LEDGER (actions so far):\n(none yet)",
    }
    messages.insert(2, ledger_msg)
    action_n = 0

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

        response = await _complete_with_retry(client, request)
        message = response.choices[0].message
        _accumulate_usage(usage_totals, getattr(response, "usage", None))

        debug("iteration", iteration)
        # Lightweight "thinking": the model's one-line plan rides in plain content
        # (no API reasoning / thought signatures — keeps it portable + cheap).
        debug("thought", message.content or "(none)")

        if not message.tool_calls:
            return message.content or ""

        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [
                tc.model_dump(exclude_none=True) for tc in message.tool_calls
            ],
        }
        messages.append(assistant_message)

        for tool_call in message.tool_calls:
            tool_name = tool_call.function.name
            arguments: dict[str, Any] = {}
            try:
                arguments = parse_tool_arguments(tool_call.function.arguments)
                debug("tool_call", {"name": tool_name, "arguments": arguments})
                result = await session.call_tool(tool_name, arguments)
                tool_content = result_text(result)
                debug("tool_call_result", {"name": tool_name, "result": tool_content})
            except Exception as exc:
                tool_content = (
                    f"Tool call {tool_name!r} failed: {type(exc).__name__}: {exc}"
                )
                debug("tool_call_error", {"name": tool_name, "error": tool_content})

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": tool_content,
                }
            )
            action_n += 1
            ledger.append(_ledger_line(action_n, tool_name, arguments, tool_content))

        # Bound context: stub old page snapshots.
        _trim_history(messages, HISTORY_KEEP_SNAPSHOTS)

    raise RuntimeError(f"Agent did not finish after {max_iterations} iterations.")


async def main(job_url: str, model: str) -> None:
    profile = load_profile()
    task = (
        f"Apply for this job: {job_url}\n\n"
        "Fill the application using the applicant profile, upload the resume, "
        "and STOP before the final submit so the user can review."
    )

    client = create_openrouter_client()
    usage = _new_usage()
    try:
        async with streamable_http_client(PROXY_MCP_URL) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                messages: list[dict[str, Any]] = [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "system", "content": f"APPLICANT PROFILE:\n\n{profile}"},
                    {"role": "user", "content": task},
                ]
                result = await agent_loop(model, task, messages, session, client, usage)
                print(f"final: {result}")
    finally:
        write_usage(usage, model, job_url)
        await client.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit('Usage: uv run python -m agent.loop "<job_url>"')
    asyncio.run(main(job_url=sys.argv[1], model=DEFAULT_MODEL))

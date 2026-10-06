"""The two apply engines, pointed at a mock ATS instead of the user's jobs.

Both get the same synthetic profile, the same resume and the same model setting as a
real run would, minus the database: no saved_answers (a bench run must not learn from
or write to the user's answers), no Gmail (codes are a planned challenge, see
bench/README.md), no tailoring.

det    det_autofill.engine on a local headless Chromium (PlaywrightDriver). The model,
       if any, only answers what the profile cannot: an LLM (LLMResolver, one call per
       page) or Jev (JevResolver: picks options and profile values, writes nothing;
       written answers go to an optional writer LLM).
agent  agent.loop.agent_loop, the LLM browser agent, through the pruning proxy over a
       local Playwright MCP that bench/__main__.py starts (or --mcp URL).
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
from pathlib import Path
from typing import Awaitable, Callable

from det_autofill import engine
from det_autofill.answers import AnswerStore
from det_autofill.fill import PlaywrightDriver
from det_autofill.llm import LLMResolver
from det_autofill.profile import from_export

Complete = Callable[[list[dict]], Awaitable[str]]

FLUSH_JS = "async () => { if (window.__bench) { await window.__bench.flush(); return true; } return false; }"


def chromium_executable() -> str | None:
    """Playwright's own build when downloaded, else a cached headless shell (the
    det-autofill tests do the same). BENCH_CHROMIUM overrides."""
    if os.environ.get("BENCH_CHROMIUM"):
        return os.environ["BENCH_CHROMIUM"]
    found = sorted(glob.glob(os.path.expanduser(
        "~/.cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell")))
    return found[-1] if found else None


def openrouter_complete(model: str) -> Complete:
    """async (messages) -> text over OpenRouter; the det engine's resolver seam, and
    the one a non-OpenRouter backend (Jev, a local model) plugs into instead."""
    from agent import det_apply

    return det_apply.openrouter_complete(model)


class CountingJev:
    """CountingComplete for Jev's system_one."""

    def __init__(self, inner):
        self.inner, self.calls, self.errors = inner, 0, []

    async def __call__(self, state, questions):
        self.calls += 1
        try:
            return await self.inner(state, questions)
        except Exception as exc:
            self.errors.append(f"{type(exc).__name__}: {str(exc)[:200]}")
            raise


class CountingComplete:
    """Wraps a model callable to count calls and keep failures. The engine keeps its
    own copy of the run context, so its resolver_error never reaches the caller; a
    run whose model calls all 404'd scored exactly like --no-model and looked real."""

    def __init__(self, inner: Complete):
        self.inner, self.calls, self.errors = inner, 0, []

    async def __call__(self, messages: list[dict]) -> str:
        self.calls += 1
        try:
            return await self.inner(messages)
        except Exception as exc:
            self.errors.append(f"{type(exc).__name__}: {str(exc)[:200]}")
            raise


class ResumeOnlyStore(AnswerStore):
    """The answer store for --model-only: the resume and nothing else. An empty profile
    is not enough, because AnswerStore has defaults of its own (decline-first EEO
    picks, "Company Website" for how-you-heard) that filled 20 fields before the model
    was asked, so the first Jev-only run measured det's defaults rather than Jev."""

    def get(self, key, label=""):
        return super().get(key, label) if key == "resume_upload" else None

    def text_for(self, key):
        return None


def per_job_writer(complete: Complete | None, profile: dict, jd_text: str):
    """Answers "why this company" questions. In production that is ai-assist's `ask`
    (DB-bound, per job); the bench asks the same model directly, as the ATS check does."""
    if complete is None:
        return None

    async def per_job(question: str, words: int) -> str | None:
        reply = await complete([
            {"role": "system", "content": "Write the candidate's answer to a job application question in first "
             f"person, under {words} words, no em dashes, grounded only in the profile and job posting given."},
            {"role": "user", "content": f"Profile:\n{json.dumps(profile, default=str)[:8000]}\n\nJob posting:\n{jd_text[:5000]}"
             f"\n\nQuestion: {question}"},
        ])
        return (reply or "").strip() or None

    return per_job


async def run_det(url: str, *, profile: dict, resume_path: str, jd_text: str, complete: Complete | None,
                  jev=None, model_only: bool = False, autosubmit: bool = True, headed: bool = False,
                  timeout_s: float = 600) -> dict:
    """`complete` is an LLM; with `jev` (a system_one) Jev resolves and `complete`, if
    given, only writes the answers Jev cannot (why us, text areas). `model_only`: det's
    profile rules are switched off (the answer store holds only the resume) and every
    field, optional ones too, goes to the model; det just clicks and types."""
    from playwright.async_api import async_playwright

    from det_autofill.jev import JevResolver

    store = ResumeOnlyStore({"resume_path": resume_path}) if model_only else AnswerStore(from_export(profile, resume_path=resume_path))
    llm = LLMResolver(complete, profile, jd_text, saved=None,
                      per_job=per_job_writer(complete, profile, jd_text)) if complete else None
    resolver = JevResolver(jev, profile, writer=llm) if jev else llm
    context: dict = {"url": url, "resolve_optional": model_only}
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=not headed)
        except Exception:
            browser = await pw.chromium.launch(headless=not headed, executable_path=chromium_executable())
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            await page.goto(url)
            driver = PlaywrightDriver(page)
            try:
                result = await asyncio.wait_for(
                    engine.run(driver, store, resolver=resolver, autosubmit=autosubmit, context=context),
                    timeout=timeout_s)
            except asyncio.TimeoutError:
                result = engine.RunResult(status="stuck", reason=f"timed out after {timeout_s:.0f}s")
            except Exception as exc:  # an engine crash is a result, not a bench crash
                result = engine.RunResult(status="stuck", reason=f"error: {type(exc).__name__}: {str(exc)[:300]}")
            try:
                await driver.page.evaluate(FLUSH_JS)
            except Exception:
                pass  # page gone or navigated: the runtime's own ticks already reported
        finally:
            await browser.close()
    return {
        "engine": "det", "status": result.status, "reason": result.reason, "pages": len(result.pages),
        "filled_log": [f for p in result.pages for f in p.filled],
        "unresolved": sorted({u for p in result.pages for u in p.unresolved}),
        "model_calls": getattr(complete, "calls", 0) + getattr(jev, "calls", 0),
        "model_errors": list(getattr(complete, "errors", [])) + list(getattr(jev, "errors", [])),
        "jev_picks": getattr(resolver, "log", None),
    }


def _profile_text(profile: dict, resume_path: str) -> str:
    from agent.profile import render_profile
    from common.repo import ProfileData

    data = ProfileData(
        user=profile["contact"], profile=profile["facts"], label=profile.get("role"),
        summary=profile.get("summary"), work_experiences=profile.get("work_experiences") or [],
        education=profile.get("education") or [], skill_groups=profile.get("skill_groups") or [],
        references=profile.get("references") or [], login=None,
    )
    return render_profile(data, resume_path)


async def run_agent(url: str, *, profile: dict, resume_path: str, model: str, mcp_url: str,
                    autosubmit: bool = True, max_iterations: int = 120, timeout_s: float = 1200) -> dict:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from agent import loop

    handoffs: list[dict] = []

    async def no_human(arguments, trace, iteration, **_):
        # a bench run has nobody at the keyboard: record the ask and let the agent finish
        handoffs.append({"iteration": iteration, **{k: str(v) for k, v in arguments.items()}})
        return ("HUMAN RESPONSE: nobody is available during this run. Do not wait for help; "
                "finish what you can and report what is left.")

    real_handoff, loop._human_handoff = loop._human_handoff, no_human
    client = loop.create_openrouter_client()
    usage = loop._new_usage()
    final, error = "", None
    try:
        async with streamable_http_client(mcp_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                page = loop.result_text(await session.call_tool(loop.NAVIGATE_TOOL_NAME, {"url": url}))
                task = f"{loop.apply_task(url, autosubmit)}\n\nCURRENT PAGE:\n{loop._wrap_untrusted(page)}"
                messages = loop.initial_messages(_profile_text(profile, resume_path), task, autosubmit)
                try:
                    final = await asyncio.wait_for(
                        loop.agent_loop(model, task, messages, session, client, usage,
                                        max_iterations=max_iterations, initial_page_text=page),
                        timeout=timeout_s)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {str(exc)[:300]}"
                try:
                    await session.call_tool("browser_evaluate", {"function": FLUSH_JS})
                except Exception:
                    pass
    finally:
        loop._human_handoff = real_handoff
        await client.close()
    return {
        "engine": "agent", "status": "error" if error else "finished", "reason": error or final[-500:],
        # the agent is the model: a failed call ends the run
        "model_errors": [error] if error and ("Error code" in error or "APIError" in error or "Connection" in error) else [],
        "handoffs": handoffs, "model_calls": usage.get("requests", 0), "cost_usd": round(usage.get("cost", 0.0), 4),
    }


def resume_pdf(path: Path) -> Path:
    """A one-page PDF standing in for the resume: the forms only check that a PDF was
    attached, and a generated file keeps real resumes out of the repo."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "Morgan Castillo - Senior Data Engineer - morgan.castillo@example.com"
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))
    return path

"""`autoapply apply --engine det`: the deterministic filler with this user's data.

What gets plugged into det_autofill.engine:
  * the profile of the role the job was tailored from (facts + that role's parsed
    resume), as the answer store — portal password included for login pages, but
    never shown to the model
  * the resume staged for the browser container (agent.apply.resolve_resume)
  * saved_answers: reusable answers replayed, per-job ones scoped to this job
  * per-job questions ("why this company") answered by ai-assist through `ask`
  * one OpenRouter call per page for whatever is still unanswered

The browser is the container's Playwright MCP, reached through the pruning proxy
(:8932), driven by CSS selectors.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Callable

from common import keys, repo
from det_autofill import engine
from det_autofill.answers import AnswerStore
from det_autofill.fill import McpDriver
from det_autofill.llm import LLMResolver
from det_autofill.mcpio import Browser
from det_autofill.profile import from_export

from agent.apply import resolve_resume
from agent.profile import profile_json

log = logging.getLogger(__name__)

# The pruning proxy, not raw :8931: the proxy holds the container browser's only MCP
# session (one session per browser profile), so a direct :8931 client is refused with
# "Browser is already in use" while the stack runs. Through the proxy the filler shares
# the agent's browser; uploads fall back to the chooser (run_code is hidden there).
PLAYWRIGHT_MCP_URL = os.getenv("DET_PLAYWRIGHT_MCP_URL", "http://localhost:8932/mcp")
REPORT_DIR = Path(__file__).resolve().parent.parent / "output" / "det-runs"


class DbSavedAnswers:
    """det_autofill.llm.SavedAnswers over the saved_answers table."""

    def __init__(self, conn, user_id: str, job_id: str):
        self.conn, self.user_id, self.job_id = conn, user_id, job_id

    def get(self, row, *, per_job: bool) -> dict | None:
        found = repo.get_saved_answer(
            self.conn, self.user_id, row.label, job_id=self.job_id if per_job else None
        )
        if not found:
            return None
        repo.mark_answer_used(self.conn, found["id"])
        return {"answer": found["answer"], "source": found["source"]}

    def put(self, row, answer: str, *, per_job: bool, source: str) -> None:
        options = [o.get("text") for o in row.options if o.get("text")] or None
        repo.save_answer(
            self.conn, self.user_id, row.label, answer, source=source, options=options,
            job_id=self.job_id if per_job else None,
        )


def openrouter_complete(model: str) -> Callable:
    """async (messages) -> reply text, on the same OpenRouter settings as the agent."""
    from openai import AsyncOpenAI

    from agent.loop import OPENROUTER_BASE_URL

    client = AsyncOpenAI(base_url=OPENROUTER_BASE_URL, api_key=os.getenv("OPENROUTER_API_KEY", ""))

    async def complete(messages: list[dict]) -> str:
        resp = await client.chat.completions.create(model=model, messages=messages, temperature=0)
        return (resp.choices[0].message.content or "") if resp.choices else ""

    return complete


def jd_text(storage, job_id: str) -> str:
    try:
        return storage.get_bytes(keys.jd_key(job_id)).decode("utf-8")
    except Exception:  # no captured JD: the model just gets less context
        return ""


async def run_det(
    conn,
    storage,
    user_id: str,
    job_id: str,
    url: str,
    *,
    run_id: str,
    model: str,
    answer_question: Callable[[str, str, str, int], dict],
    autosubmit: bool = False,
    navigate: bool = True,
    mcp_url: str = PLAYWRIGHT_MCP_URL,
    complete: Callable | None = None,
) -> engine.RunResult:
    """Fill one application. `answer_question(user_id, job_id, question, words)` is
    `cli.main.run_answer` (ai-assist); `navigate=False` continues the open page."""
    from agent.loop import RESUME_CONTAINER_ROOT, SCRATCH_ROOT

    label = repo.base_label_for_job(conn, user_id, job_id)
    bundle = repo.fetch_profile(conn, user_id, label=label)
    doc = profile_json(bundle)
    host_resume = f"{SCRATCH_ROOT}/{run_id}/resume.pdf"
    container_resume = f"{RESUME_CONTAINER_ROOT}/{run_id}/resume.pdf"
    _, source = await resolve_resume(conn, storage, user_id, job_id, host_resume)
    log.info("det run %s: role=%s resume=%s", run_id, bundle.label, source)
    store = AnswerStore(from_export(
        doc, resume_path=container_resume, login_password=(bundle.login or {}).get("login_password"),
    ))

    async def per_job(question: str, words: int) -> str | None:
        reply = await asyncio.to_thread(answer_question, user_id, job_id, question, words)
        return reply.get("answer")

    resolver = LLMResolver(
        complete or openrouter_complete(model), doc, jd_text(storage, job_id),
        saved=DbSavedAnswers(conn, user_id, job_id), per_job=per_job,
    )
    browser = await Browser(mcp_url).connect()
    try:
        if navigate:
            await browser.navigate(url)
        result = await engine.run(
            McpDriver(browser), store, resolver=resolver, autosubmit=autosubmit,
            context={"job_id": str(job_id), "url": url},
        )
    finally:
        await browser.close()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / f"{run_id}.json").write_text(json.dumps(result.to_dict(), indent=2, default=str))
    return result

"""`autoapply apply --engine det`: the deterministic filler with this user's data.

What gets plugged into det_autofill.engine:
  * the profile of the role the job was tailored from (facts + that role's parsed
    resume), as the answer store — portal password included for login pages, but
    never shown to the model
  * the resume staged for the browser container (agent.apply.resolve_resume)
  * saved_answers: reusable answers replayed, per-job ones scoped to this job
  * per-job questions ("why this company") answered by ai-assist through `ask`
  * one OpenRouter call per page for whatever is still unanswered — or, with a Jev
    model (`--model jev-latest`), one TypeSafe Jev request that picks options and
    profile values; Jev writes nothing, so free-text answers come only from `ask`

The browser is the container's Playwright MCP, reached through the pruning proxy
(:8932), driven by CSS selectors.

`--engine det+agent`: when det stops short (`agent_should_take_over`), the LLM agent
continues on the same open page with `agent_handover` — what det filled, what is
left and why it stopped — so it completes only the rest.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Awaitable, Callable

from common import keys, repo
from det_autofill import engine, login, taxonomy
from det_autofill.answers import AnswerStore
from det_autofill.fill import McpDriver
from det_autofill.jev import JevResolver
from det_autofill.llm import LLMResolver
from det_autofill.mcpio import Browser
from det_autofill.profile import from_export
from det_autofill.aria import FormField

from agent import email_otp, notify
from agent.apply import resolve_resume
from agent.jev_client import is_jev, make_system_one
from agent.profile import fetch_job_profile, profile_json

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


def push_handoff(result: engine.RunResult, url: str, job_id) -> None:
    """det stops at a captcha / code it cannot get from the inbox; the page stays open
    for `autoapply continue JOB --engine det` once the person is done."""
    notify.push(
        f"Apply paused: {result.reason[:80]}",
        f"{url}\nFinish it on screen, then: autoapply continue {job_id} --engine det",
        click=notify.vnc_url(),
    )


# Stops the agent must not pick up: a captcha or an emailed code is the person's, a
# rejected password is never retried, and det already used this run's sign-in try.
# Rivian's "no sign-in button" and "no account on this portal" are fine: the agent
# signs in or creates the account (det never creates one).
_PERSON_ONLY_RE = re.compile(
    r"captcha|human check|one-time code|code did not arrive|sign-in rejected|already tried|needs-human", re.I
)


def _left_for_the_applicant(label: str) -> bool:
    rule = taxonomy.match(FormField(ref="", role="", label=label))
    return bool(rule and rule.key == "ai_disclosure")


def agent_should_take_over(result: engine.RunResult) -> bool:
    """det+agent: hand the open page to the agent when det stopped short of Submit
    for a reason the agent can act on. Not when the only gaps are questions det leaves
    to the applicant (AI use: the agent leaves them too, so it could only stop again)."""
    if result.status not in ("stuck", "handoff") or _PERSON_ONLY_RE.search(result.reason or ""):
        return False
    gaps = result.pages[-1].unresolved if result.pages else []
    return not (gaps and all(_left_for_the_applicant(g) for g in gaps))


def agent_handover(result: engine.RunResult, limit: int = 40) -> str:
    """The brief the agent continues from: what det filled on the page it is on, what
    is left and why det stopped. The agent re-reading every field from scratch
    refilled them and spent most of its calls on work det had done."""
    page = result.pages[-1] if result.pages else engine.PageReport(number=0, url="")
    ok = list(dict.fromkeys(f["label"] for f in page.filled if f["ok"]))
    failed = [f["label"] for f in page.filled if not f["ok"] and f["label"] not in ok]
    left = list(dict.fromkeys(page.unresolved + failed))
    lines = [
        "HANDOVER FROM THE DETERMINISTIC FILLER: a rule-based filler already filled this "
        f"page from the applicant profile and stopped ({result.status}: {result.reason[:300]}).",
        "Already filled — keep these; touch one only if the page marks it invalid: "
        + ("; ".join(ok[:limit]) or "(nothing on this page)"),
        "Left for you: " + ("; ".join(left[:limit]) or "(nothing listed: find what blocks the page, e.g. an "
                            "unsaved section, a sign-in step, a control it could not operate)"),
        "Complete only what is left, then continue through the remaining steps. Answer from the "
        "APPLICANT PROFILE and the resume only: a field whose answer they do not state stays "
        "empty and is named in your summary — never guess one to get past validation. Questions "
        "about AI use in this application are the applicant's: leave them. Captchas are a "
        "human handoff, never solved by you.",
    ]
    return "\n".join(lines)


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


def portal_credentials(bundle) -> login.Credentials | None:
    """The applicant's portal sign-in (`autoapply profile init`), or None without a
    password. Never logged: it is typed into the portal and nowhere else."""
    cred = bundle.login or {}
    if not cred.get("login_password"):
        return None
    email = cred.get("login_email") or (bundle.user or {}).get("email")
    return login.Credentials(email, cred["login_password"]) if email else None


def inbox_otp(conn, user_id: str, url: str) -> Callable[[], Awaitable[str | None]] | None:
    """An `otp()` for the engine that reads the verification code from the applicant's
    Gmail (`autoapply gmail connect`), or None when no inbox is connected. Only mail
    that arrived after this run started counts, and each message is used once."""
    token = repo.get_gmail_token(conn, user_id)
    if not token:
        return None

    company = email_otp.host_words(url)
    since, seen = int(time.time()) - 60, set()

    async def otp() -> str | None:
        found = await email_otp.fetch_email(token["refresh_token"], company, None, since, seen)
        m = re.search(r"VERIFICATION CODE: (\w+)", found)
        return m.group(1) if m else None

    return otp


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
    push: bool = True,
) -> engine.RunResult:
    """Fill one application. `answer_question(user_id, job_id, question, words)` is
    `cli.main.run_answer` (ai-assist); `navigate=False` continues the open page.
    `push=False` leaves the handoff alert to the caller (det+agent decides first
    whether the agent takes over)."""
    from agent.loop import RESUME_CONTAINER_ROOT, SCRATCH_ROOT

    bundle = fetch_job_profile(conn, user_id, job_id)
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

    saved = DbSavedAnswers(conn, user_id, job_id)
    if is_jev(model):
        # Jev picks; the LLM resolver stays only for per-job answers through `ask`
        # (complete=None: no OpenRouter call)
        resolver = JevResolver(make_system_one(model), doc, saved=saved,
                               writer=LLMResolver(None, doc, saved=saved, per_job=per_job))
    else:
        resolver = LLMResolver(
            complete or openrouter_complete(model), doc, jd_text(storage, job_id),
            saved=saved, per_job=per_job,
        )
    browser = await Browser(mcp_url).connect()
    try:
        if navigate:
            await browser.navigate(url)
        result = await engine.run(
            McpDriver(browser), store, resolver=resolver, autosubmit=autosubmit,
            context={"job_id": str(job_id), "url": url},
            credentials=portal_credentials(bundle), otp=inbox_otp(conn, user_id, url),
        )
    finally:
        await browser.close()
    if push and result.status == "handoff":
        await asyncio.to_thread(push_handoff, result, url, job_id)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / f"{run_id}.json").write_text(json.dumps(result.to_dict(), indent=2, default=str))
    return result

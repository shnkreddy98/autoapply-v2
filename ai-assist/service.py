"""Internal HTTP surface for ai-assist.

Exists because ai-assist has no build-system and `env.py` loads `dev.env`
relative to cwd, so it cannot be imported by `api/`. Running it as its own
uvicorn process with cwd=ai-assist keeps those constraints intact and gives
both `api/` and the auto-browse form-filler one place to call.

    uv run uvicorn service:app --host 127.0.0.1 --port 8100

INTERNAL ONLY. It takes `user_id` from the request body and trusts it, so the
caller is the one doing authentication. Bind it to loopback (or a private
network) and set AI_ASSIST_TOKEN. Ownership of the job is still re-checked here,
but nothing stops a caller that can reach the port from naming any user_id.

The answering logic itself lives in `qa.py`, shared with `autoapply ask`.
"""

from __future__ import annotations

import functools
import logging
import os
import secrets
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

import qa
from common import db
from common import storage as storagemod

log = logging.getLogger(__name__)

MAX_WORDS_LIMIT = 1000
TOKEN = os.getenv("AI_ASSIST_TOKEN", "")
if not TOKEN:
    log.warning(
        "AI_ASSIST_TOKEN is unset: /answer is unauthenticated and will act on any "
        "user_id it is handed. Only acceptable bound to 127.0.0.1."
    )


@functools.lru_cache(maxsize=1)
def _storage():
    return storagemod.from_env()


def require_token(authorization: str = Header(default="")) -> None:
    if not TOKEN:
        return
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(value, TOKEN):
        raise HTTPException(401, "bad or missing bearer token")


class AnswerIn(BaseModel):
    user_id: UUID
    job_id: UUID
    question: str = Field(min_length=1, max_length=2000)
    max_words: int = Field(default=qa.MAX_WORDS_DEFAULT, ge=10, le=MAX_WORDS_LIMIT)
    # which base resume to answer from; omitted -> the base this job was tailored
    # from, so a role-specific resume's experience isn't dropped (qa.choose_base_asset)
    label: str | None = Field(default=None, max_length=64)


class AnswerOut(BaseModel):
    answer: str
    word_count: int
    max_words: int
    base_label: str


app = FastAPI(title="ai-assist", description="Internal resume/LLM helpers")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/answer", response_model=AnswerOut, dependencies=[Depends(require_token)])
async def answer(body: AnswerIn) -> AnswerOut:
    """Answer one free-text application question from the job's JD + the user's resume."""
    from textfmt import word_count

    user_id, job_id = str(body.user_id), str(body.job_id)
    try:
        # connection released before the LLM call — it can run for minutes
        with db.get_conn() as conn:
            resume, jd, base_label = qa.load_context(
                conn, _storage(), user_id, job_id, body.label
            )
    except qa.NotOwner:
        raise HTTPException(404, "no such job for this user")
    except qa.MissingResume:
        raise HTTPException(404, "no base resume on file for this user")
    except qa.MissingJD as exc:
        raise HTTPException(409, f"no captured JD for this job yet: {exc}")

    text = await qa.run_agent(resume, jd, body.question, body.max_words)
    log.info(
        "answered job=%s user=%s words=%s/%s base=%s",
        job_id, user_id, word_count(text), body.max_words, base_label,
    )
    return AnswerOut(
        answer=text,
        word_count=word_count(text),
        max_words=body.max_words,
        base_label=base_label,
    )

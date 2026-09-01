"""Answering free-text application questions for a user×job.

Shared by the two front doors: `service.py` (HTTP, for api/ and the form-filler)
and `answer_cli.py` (stdin/stdout, for `autoapply ask`). Neither imports the
other, so the context-loading rules live here once.

Raises plain domain errors; each front door maps them to its own vocabulary.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from common import keys, repo

MAX_WORDS_DEFAULT = 100


class NotOwner(Exception):
    """The user does not own this job."""


class MissingResume(Exception):
    """No base resume on file for the user."""


class MissingJD(Exception):
    """The job's JD has not been captured yet."""


def check_owner(conn, user_id: str, job_id: str) -> None:
    # job_owner_ids yields UUID objects; compare as text
    if str(user_id) not in {str(u) for u in repo.job_owner_ids(conn, job_id)}:
        raise NotOwner(f"user {user_id} does not own job {job_id}")


def resume_text(conn, storage, user_id: str) -> str:
    """Plain text of the user's base resume .docx.

    Deliberately the base and not the tailored PDF: the tailored artifact is a
    PDF (needs parsing back out) and differs from the base by two replacements,
    so it adds no facts an answer could be grounded in.

    Extraction goes through `utils.docx_text`, the same walk the tailoring path
    uses. Reading `doc.paragraphs` instead drops every table, which in this resume
    template is all of the education and every employer/title/date.
    """
    from utils import docx_text

    asset = repo.get_base_asset(conn, user_id)
    if not asset:
        raise MissingResume(f"no base resume on file for user {user_id}")
    work = Path(tempfile.mkdtemp(prefix="answer-"))
    try:
        path = work / "base.docx"
        storage.get_file(asset["s3_key"], str(path))
        return docx_text(str(path))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def jd_text(storage, job_id: str) -> str:
    try:
        return storage.get_bytes(keys.jd_key(job_id)).decode("utf-8")
    except Exception as exc:
        raise MissingJD(f"no captured JD for job {job_id}: {exc}") from exc


async def run_agent(resume: str, jd: str, question: str, max_words: int) -> str:
    """Indirection so tests can swap the LLM out. Imported lazily: `agents` pulls in
    `env`, which refuses to load without an API key."""
    from agents import ApplicationQuestionAgent

    return await ApplicationQuestionAgent().answer(
        resume, jd, question, max_words=max_words
    )


def load_context(conn, storage, user_id: str, job_id: str) -> tuple[str, str]:
    """(resume_text, jd_text) for the pair, after an ownership check. Kept separate
    from `run_agent` so callers can release the connection before the LLM call,
    which can run for minutes."""
    check_owner(conn, user_id, job_id)
    return resume_text(conn, storage, user_id), jd_text(storage, job_id)


async def answer(
    conn,
    storage,
    user_id: str,
    job_id: str,
    question: str,
    max_words: int = MAX_WORDS_DEFAULT,
) -> str:
    """Convenience for short-lived callers (the CLI) that hold their own connection."""
    resume, jd = load_context(conn, storage, user_id, job_id)
    return await run_agent(resume, jd, question, max_words)

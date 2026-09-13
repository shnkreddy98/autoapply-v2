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

# Label standing for "the user's default role" (users.default_resume_label). Callers
# pass it to force the default when the job's own tailoring used another role. No
# real role may use this name (cli.onboard.check_label refuses it).
DEFAULT_LABEL = "default"


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


def choose_base_asset(
    conn, user_id: str, job_id: str, label: str | None = None
) -> tuple[dict, str]:
    """The base .docx to ground answers in, and the label it came from.

    `label=None` follows the job: whichever base its tailoring was made from
    (`autoapply tailor --label aiml` -> the aiml base). Without this an answer for
    an AI/ML posting is written from the default resume and denies experience that
    is only listed on the role base. `label=DEFAULT_LABEL` forces the user's default
    role; any other value forces that role's base.

    A named label with no base uploaded falls back to the default rather than
    failing — a grounded-but-generic answer beats no answer, and the returned label
    tells the caller which one was actually used.

    Pinned to the docx variant because this parses it as a docx; the pdf base is
    the upload artifact, not a text source.
    """
    if label is None:
        label = repo.base_label_for_job(conn, user_id, job_id)
    if label == DEFAULT_LABEL:
        label = None
    asset = repo.get_base_asset(conn, user_id, variant="docx", label=label)
    if asset is None and label is not None:
        asset = repo.get_base_asset(conn, user_id, variant="docx")
    if asset is None:
        raise MissingResume(f"no base resume on file for user {user_id}")
    return asset, asset["label"]


def resume_text(storage, asset: dict) -> str:
    """Plain text of a base resume .docx asset.

    Deliberately the base and not the tailored PDF: the tailored artifact is a
    PDF (needs parsing back out) and differs from the base by two replacements,
    so it adds no facts an answer could be grounded in.

    Extraction goes through `utils.docx_text`, the same walk the tailoring path
    uses. Reading `doc.paragraphs` instead drops every table, which in this resume
    template is all of the education and every employer/title/date.
    """
    from utils import docx_text

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


def load_context(
    conn, storage, user_id: str, job_id: str, label: str | None = None
) -> tuple[str, str, str]:
    """(resume_text, jd_text, base_label) for the pair, after an ownership check.

    Kept separate from `run_agent` so callers can release the connection before the
    LLM call, which can run for minutes. `label` picks the base resume; see
    `choose_base_asset`. The returned label is the base actually used, so the caller
    can report which resume the answer was grounded in."""
    check_owner(conn, user_id, job_id)
    asset, used = choose_base_asset(conn, user_id, job_id, label)
    return resume_text(storage, asset), jd_text(storage, job_id), used


async def answer(
    conn,
    storage,
    user_id: str,
    job_id: str,
    question: str,
    max_words: int = MAX_WORDS_DEFAULT,
    label: str | None = None,
) -> str:
    """Convenience for short-lived callers (the CLI) that hold their own connection."""
    resume, jd, _ = load_context(conn, storage, user_id, job_id, label)
    return await run_agent(resume, jd, question, max_words)

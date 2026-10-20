"""Tailor worker: consume the `tailored_resumes` queue and produce tailored resumes.

For each claimed row it downloads the user's base `.docx` + the job's captured JD
from object storage, runs the ResumeTailorAgent, converts to PDF,
uploads the tailored PDF, and flips the row to `ready` with the before/after scores.

The tailoring step is injected (`tailor`) so the orchestration is unit-testable
without an LLM; `real_tailor` wires the existing agent + docx->pdf conversion.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

import psycopg

from common import keys, queue, repo
from common import storage as storagemod

log = logging.getLogger(__name__)


@dataclass
class TailorOutcome:
    pdf_path: str
    resume_score: int
    new_resume_score: int
    job_match_summary: str
    cloud: str | None = None
    company_name: str | None = None  # read out of the JD; backfills jobs.company
    role: str | None = None  # the JD's job title; backfills jobs.title


# async tailor(base_docx_path, jd_text) -> TailorOutcome   (mutates the docx in place)
Tailor = Callable[[str, str], Awaitable[TailorOutcome]]


async def process_once(
    conn: psycopg.Connection,
    storage: storagemod.Storage,
    tailor: Tailor,
    scratch_root: str,
    worker_id: str = "tailor",
) -> str | None:
    """Claim one pending tailoring job and complete it. Returns the row id handled
    (success or failed), or None if the queue was empty."""
    row = queue.claim_pending(conn, "tailored_resumes", worker_id=worker_id)
    if row is None:
        return None
    tid, user_id, job_id = row["id"], row["user_id"], row["job_id"]
    work = Path(scratch_root) / str(tid)
    try:
        work.mkdir(parents=True, exist_ok=True)
        base = repo.get_resume_asset(conn, row["base_asset_id"]) if row["base_asset_id"] else None
        if not base:
            raise ValueError("no base resume asset for user")
        base_path = work / "base.docx"
        storage.get_file(base["s3_key"], str(base_path))
        jd_text = storage.get_bytes(keys.jd_key(job_id)).decode("utf-8")

        outcome = await tailor(str(base_path), jd_text)
        repo.set_job_company(conn, job_id, outcome.company_name)
        repo.set_job_title(conn, job_id, outcome.role)

        tkey = keys.tailored_resume_key(user_id, job_id, "pdf")
        storage.put_file(tkey, outcome.pdf_path)
        asset_id = repo.insert_resume_asset(
            conn, user_id, "tailored", tkey, job_id=job_id, content_type="application/pdf"
        )
        queue.mark(
            conn, "tailored_resumes", tid, "ready",
            tailored_asset_id=asset_id,
            resume_score=outcome.resume_score,
            new_resume_score=outcome.new_resume_score,
            job_match_summary=outcome.job_match_summary,
            cloud=outcome.cloud,
        )
        log.info("tailored job=%s user=%s score %s->%s", job_id, user_id,
                 outcome.resume_score, outcome.new_resume_score)
    except Exception as exc:  # one bad job must not kill the worker
        log.warning("tailoring failed id=%s: %s", tid, exc)
        queue.mark(conn, "tailored_resumes", tid, "failed", error=str(exc)[:500])
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return tid


async def run(
    storage: storagemod.Storage,
    tailor: Tailor,
    conn: psycopg.Connection,
    scratch_root: str,
    poll_interval: float = 5.0,
    once: bool = False,
) -> None:
    while True:
        tid = await process_once(conn, storage, tailor, scratch_root)
        if tid is None:
            if once:
                return
            await asyncio.sleep(poll_interval)


async def real_tailor(base_docx_path: str, jd_text: str) -> TailorOutcome:
    """Production tailor: run ResumeTailorAgent (mutates the docx) + convert to PDF."""
    from agents import ResumeTailorAgent
    from tools import DocumentTools
    from word import convert_docx_to_pdf

    doc_tools = DocumentTools(base_docx_path)
    result = await ResumeTailorAgent(doc_tools).tailor_resume(jd_text)
    pdf_path = await convert_docx_to_pdf(base_docx_path)
    if not pdf_path:
        raise RuntimeError("docx->pdf conversion failed")
    return TailorOutcome(
        pdf_path=pdf_path,
        resume_score=round(result.resume_score),
        new_resume_score=round(result.new_resume_score),
        job_match_summary=result.job_match_summary,
        cloud=result.cloud,
        company_name=result.company_name,
        role=result.role,
    )


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    from common import db

    storage = storagemod.from_env()
    scratch = os.getenv("SCRATCH_ROOT", "/tmp/tailor-scratch")
    with db.connect() as conn:
        await run(storage, real_tailor, conn, scratch, once=bool(os.getenv("TAILOR_ONCE")))


if __name__ == "__main__":
    asyncio.run(_main())

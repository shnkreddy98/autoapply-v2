"""M5: tailor worker orchestration. Integration vs live PG+MinIO with a fake tailor
(no LLM). Real-LLM path is exercised separately behind RUN_LLM_E2E."""

import os
from pathlib import Path

import pytest

from common import keys, repo
from worker import TailorOutcome, process_once


def _seed(db, s3):
    """One user with a base.docx in MinIO, a job with a captured JD, and a pending
    tailoring row. Returns (tid, user_id, job_id)."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, full_name) VALUES (%s, %s) RETURNING user_id",
            ("t@example.com", "T"),
        )
        user_id = cur.fetchone()["user_id"]
    db.commit()
    base_key = f"users/{user_id}/resumes/base/default.docx"
    s3.put_bytes(base_key, b"PK\x03\x04 fake docx bytes")
    base_id = repo.insert_resume_asset(db, user_id, "base", base_key, variant="default")

    job_id = repo.get_or_create_job(db, "https://example.com/jobs/1", "Eng")
    s3.put_bytes(keys.jd_key(job_id), b"# Job\nNeed dbt + Airflow.", "text/markdown")
    tid = repo.enqueue_tailoring(db, user_id, job_id, base_id)
    return tid, user_id, job_id


@pytest.mark.asyncio
async def test_tailor_success(db, s3, tmp_path):
    tid, user_id, job_id = _seed(db, s3)
    seen = {}

    async def fake_tailor(docx_path, jd_text):
        seen["docx_exists"] = Path(docx_path).is_file()
        seen["jd"] = jd_text
        pdf = tmp_path / "out.pdf"
        pdf.write_bytes(b"%PDF-1.4 tailored")
        return TailorOutcome(str(pdf), resume_score=65, new_resume_score=82,
                             job_match_summary="surfaced dbt", cloud="aws")

    handled = await process_once(db, s3, fake_tailor, str(tmp_path / "scratch"))
    assert handled == tid
    assert seen["docx_exists"] is True            # base.docx was downloaded for the tailor
    assert "dbt" in seen["jd"]                     # JD passed through

    row = repo.lookup_tailored(db, user_id, job_id)
    assert row["status"] == "ready"
    assert row["resume_score"] == 65 and row["new_resume_score"] == 82
    assert row["cloud"] == "aws"
    assert row["tailored_asset_id"] is not None

    tkey = keys.tailored_resume_key(user_id, job_id, "pdf")
    assert s3.get_bytes(tkey) == b"%PDF-1.4 tailored"
    # scratch cleaned up
    assert not (tmp_path / "scratch" / str(tid)).exists()


@pytest.mark.asyncio
async def test_tailor_no_base_asset_marks_failed(db, s3, tmp_path):
    with db.cursor() as cur:
        cur.execute("INSERT INTO users (email) VALUES (%s) RETURNING user_id", ("n@e.co",))
        user_id = cur.fetchone()["user_id"]
    db.commit()
    job_id = repo.get_or_create_job(db, "https://example.com/jobs/2")
    tid = repo.enqueue_tailoring(db, user_id, job_id, None)  # no base asset

    async def fake_tailor(docx_path, jd_text):
        raise AssertionError("should not be called")

    await process_once(db, s3, fake_tailor, str(tmp_path / "scratch"))
    row = repo.lookup_tailored(db, user_id, job_id)
    assert row["status"] == "failed"
    assert "base resume" in row["error"]


@pytest.mark.asyncio
async def test_tailor_empty_queue(db, s3, tmp_path):
    async def fake_tailor(docx_path, jd_text):
        return TailorOutcome("x", 1, 2, "s")

    assert await process_once(db, s3, fake_tailor, str(tmp_path)) is None


@pytest.mark.skipif(not os.getenv("RUN_LLM_E2E"), reason="real LLM + LibreOffice; opt-in")
@pytest.mark.asyncio
async def test_real_tailor_e2e(tmp_path):
    """End-to-end real tailoring: needs OPENROUTER_API_KEY + LibreOffice + a base
    .docx at AI_ASSIST_BASE_DOCX. Run with RUN_LLM_E2E=1."""
    import shutil

    from worker import real_tailor

    src = os.environ["AI_ASSIST_BASE_DOCX"]
    docx = tmp_path / "base.docx"
    shutil.copyfile(src, docx)
    outcome = await real_tailor(str(docx), "We need a data engineer with dbt and Airflow.")
    assert Path(outcome.pdf_path).is_file()
    assert 0 <= outcome.resume_score <= 100
    assert 0 <= outcome.new_resume_score <= 100

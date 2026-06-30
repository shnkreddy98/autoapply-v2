"""M7 end-to-end: search -> JD-capture -> tailor -> apply, through the real
Postgres queue + MinIO, driving each stage's real orchestration function.

The tailor stage lives in ai-assist; its `process_once` only needs common+storage
at import time (docx is lazy), so we add ai-assist to the path and call it directly.
A fake navigator and fake tailor stand in for the browser and the LLM.
"""

import sys
from pathlib import Path

import pytest

from agent import jd_capture
from agent.apply import resolve_resume
from common import keys, repo

# make ai-assist's worker importable (cross-service, via the shared queue contract)
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "ai-assist"))


@pytest.mark.asyncio
async def test_full_pipeline(db, s3, tmp_path):
    import worker  # ai-assist/worker.py

    # ---- onboarding: a user with a base resume in MinIO ----
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, full_name) VALUES (%s, %s) RETURNING user_id",
            ("e2e@example.com", "E2E User"),
        )
        user_id = cur.fetchone()["user_id"]
    db.commit()
    base_key = f"users/{user_id}/resumes/base/default.docx"
    s3.put_bytes(base_key, b"PK\x03\x04 base docx")
    repo.insert_resume_asset(db, user_id, "base", base_key, variant="default")

    # ---- stage 1: search discovers a job ----
    job_id = repo.get_or_create_job(db, "https://boards.greenhouse.io/acme/jobs/42", "DE", "search")

    # ---- stage 2: JD-capture (stub navigator) ----
    async def navigate(url):
        return "# Data Engineer\nWe need dbt, Airflow, and Spark."

    await jd_capture.capture_once(db, navigate, s3)
    with db.cursor() as cur:
        cur.execute("SELECT status FROM jobs WHERE job_id = %s", (job_id,))
        assert cur.fetchone()["status"] == "jd_ready"
    assert s3.exists(keys.jd_key(job_id))
    t = repo.lookup_tailored(db, user_id, job_id)
    assert t["status"] == "pending"  # auto-enqueued for the active user

    # ---- stage 3: tailor worker (fake tailor, real orchestration) ----
    async def fake_tailor(docx_path, jd_text):
        assert Path(docx_path).is_file() and "dbt" in jd_text
        pdf = tmp_path / "tailored.pdf"
        pdf.write_bytes(b"%PDF tailored-for-acme")
        return worker.TailorOutcome(str(pdf), 64, 83, "surfaced dbt/airflow", "aws")

    await worker.process_once(db, s3, fake_tailor, str(tmp_path / "scratch"))
    t = repo.lookup_tailored(db, user_id, job_id)
    assert t["status"] == "ready" and t["new_resume_score"] == 83

    # ---- stage 4: apply consumer resolves the precomputed tailored resume ----
    dest = str(tmp_path / "upload.pdf")
    path, source = await resolve_resume(db, s3, user_id, job_id, dest)
    assert source == "tailored"
    assert open(path, "rb").read() == b"%PDF tailored-for-acme"

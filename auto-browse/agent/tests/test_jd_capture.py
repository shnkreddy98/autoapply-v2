"""M4: JD-capture worker orchestration (unit fakes + integration vs live PG+MinIO)."""

import pytest

from agent import jd_capture
from common import keys, repo

# A JD that clears jd_capture's quality gate (>= MIN_JD_CHARS with signal words).
_VALID_JD = (
    "# Senior Data Engineer\n\n"
    "About the role: we are looking for a data engineer to design and own our "
    "data platform end to end.\n\n"
    "Responsibilities:\n"
    "- Build and maintain scalable ETL/ELT pipelines with dbt, Airflow, and Spark.\n"
    "- Model data in the warehouse for analytics and machine learning.\n"
    "- Partner with analysts and stakeholders to ship reliable datasets.\n\n"
    "Requirements:\n"
    "- 5+ years of experience in data engineering or a closely related field.\n"
    "- Strong SQL and Python skills; hands-on experience with dbt and cloud warehouses.\n"
    "- Familiarity with orchestration, CI/CD, and infrastructure-as-code.\n\n"
    "What you'll get: a collaborative team, competitive pay, and room to grow.\n"
)


class FakeStorage:
    def __init__(self):
        self.objs = {}

    def put_bytes(self, key, data, content_type=None):
        self.objs[key] = data

    def get_bytes(self, key):
        return self.objs[key]

    def put_file(self, key, path):
        with open(path, "rb") as f:
            self.objs[key] = f.read()

    def get_file(self, key, dest):
        with open(dest, "wb") as f:
            f.write(self.objs[key])

    def exists(self, key):
        return key in self.objs


def _seed_job_and_user(db, url="https://example.com/jobs/1", email="u@example.com"):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, full_name) VALUES (%s, %s) RETURNING user_id",
            (email, "U"),
        )
        user_id = cur.fetchone()["user_id"]
    db.commit()
    job_id = repo.add_job_for_user(db, user_id, url, "Eng")
    repo.insert_resume_asset(db, user_id, "base", f"users/{user_id}/resumes/base/default.docx",
                             variant="docx")
    return job_id, user_id


@pytest.mark.asyncio
async def test_capture_success_enqueues_tailoring(db):
    job_id, user_id = _seed_job_and_user(db)
    store = FakeStorage()

    async def navigate(url):
        return _VALID_JD

    handled = await jd_capture.capture_once(db, navigate, store)
    assert handled == job_id

    with db.cursor() as cur:
        cur.execute("SELECT status, jd_s3_key FROM jobs WHERE job_id = %s", (job_id,))
        row = cur.fetchone()
    assert row["status"] == "jd_ready"
    assert row["jd_s3_key"] == keys.jd_key(job_id)
    assert store.objs[keys.jd_key(job_id)] == jd_capture._clean_jd(_VALID_JD).encode("utf-8")

    # tailoring enqueued for the owner, linked to their base asset
    t = repo.lookup_tailored(db, user_id, job_id)
    assert t["status"] == "pending"
    assert t["base_asset_id"] is not None


@pytest.mark.asyncio
async def test_capture_enqueues_owners_only(db):
    """An active non-owner must NOT get a tailoring row for someone else's job."""
    job_id, owner = _seed_job_and_user(db)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, full_name) VALUES ('bystander@example.com', 'B') "
            "RETURNING user_id"
        )
        bystander = cur.fetchone()["user_id"]
    db.commit()
    store = FakeStorage()

    async def navigate(url):
        return _VALID_JD

    await jd_capture.capture_once(db, navigate, store)
    assert repo.lookup_tailored(db, owner, job_id) is not None
    assert repo.lookup_tailored(db, bystander, job_id) is None


@pytest.mark.asyncio
async def test_capture_empty_text_marks_failed(db):
    job_id, _ = _seed_job_and_user(db)
    store = FakeStorage()

    async def navigate(url):
        return "   "

    await jd_capture.capture_once(db, navigate, store)
    with db.cursor() as cur:
        cur.execute("SELECT status, error FROM jobs WHERE job_id = %s", (job_id,))
        row = cur.fetchone()
    assert row["status"] == "failed"
    assert "empty JD" in row["error"]
    assert store.objs == {}


@pytest.mark.asyncio
async def test_capture_navigation_raises_marks_failed(db):
    job_id, _ = _seed_job_and_user(db)
    store = FakeStorage()

    async def navigate(url):
        raise RuntimeError("nav boom")

    await jd_capture.capture_once(db, navigate, store)
    with db.cursor() as cur:
        cur.execute("SELECT status, error FROM jobs WHERE job_id = %s", (job_id,))
        row = cur.fetchone()
    assert row["status"] == "failed"
    assert "nav boom" in row["error"]


@pytest.mark.asyncio
async def test_capture_empty_queue_returns_none(db):
    store = FakeStorage()

    async def navigate(url):
        return "x"

    assert await jd_capture.capture_once(db, navigate, store) is None


@pytest.mark.asyncio
async def test_run_once_drains_backlog(db):
    repo.get_or_create_job(db, "https://example.com/jobs/a")
    repo.get_or_create_job(db, "https://example.com/jobs/b")
    store = FakeStorage()

    async def navigate(url):
        return _VALID_JD

    await jd_capture.run(navigate, store, db, once=True)
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS c FROM jobs WHERE status = 'jd_ready'")
        assert cur.fetchone()["c"] == 2

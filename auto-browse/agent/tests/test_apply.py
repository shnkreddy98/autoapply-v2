"""M6: resolve_resume branches — ready / miss->ready / miss->timeout->base / none.

Uses live Postgres + a fake storage so timing is deterministic (injected sleep)."""

import pytest

from agent.apply import resolve_resume
from common import keys, repo


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


def _user(db, email="a@e.co"):
    with db.cursor() as cur:
        cur.execute("INSERT INTO users (email) VALUES (%s) RETURNING user_id", (email,))
        uid = cur.fetchone()["user_id"]
    db.commit()
    return uid


def _base(db, store, uid):
    # Base resume is stored as two variants: 'docx' (tailoring source) and 'pdf'
    # (the ready-to-upload fallback resolve_resume returns on a tailored miss).
    docx_key = f"users/{uid}/resumes/base/default.docx"
    pdf_key = f"users/{uid}/resumes/base/default.pdf"
    store.objs[docx_key] = b"BASEDOCX"
    store.objs[pdf_key] = b"BASE"
    repo.insert_resume_asset(db, uid, "base", docx_key, variant="docx")
    return repo.insert_resume_asset(
        db, uid, "base", pdf_key, variant="pdf", content_type="application/pdf"
    )


def _ready_tailored(db, store, uid, job_id):
    key = keys.tailored_resume_key(uid, job_id, "pdf")
    store.objs[key] = b"TAILORED"
    asset = repo.insert_resume_asset(
        db, uid, "tailored", key, job_id=job_id, content_type="application/pdf"
    )
    tid = repo.enqueue_tailoring(db, uid, job_id, None)
    from common import queue

    queue.mark(db, "tailored_resumes", tid, "ready", tailored_asset_id=asset)


async def _noop_sleep(_):
    return None


@pytest.mark.asyncio
async def test_ready_tailored_used(db, tmp_path):
    store = FakeStorage()
    uid = _user(db)
    _base(db, store, uid)
    job_id = repo.get_or_create_job(db, "https://example.com/jobs/1")
    _ready_tailored(db, store, uid, job_id)

    dest = str(tmp_path / "r.pdf")
    path, source = await resolve_resume(db, store, uid, job_id, dest, sleep=_noop_sleep)
    assert source == "tailored"
    assert open(path, "rb").read() == b"TAILORED"


@pytest.mark.asyncio
async def test_miss_then_ready_within_timeout(db, tmp_path):
    store = FakeStorage()
    uid = _user(db)
    _base(db, store, uid)
    job_id = repo.get_or_create_job(db, "https://example.com/jobs/2")

    # sleep callback flips the pending row to ready on first poll (simulates worker)
    async def flip_sleep(_):
        _ready_tailored(db, store, uid, job_id)

    dest = str(tmp_path / "r.pdf")
    path, source = await resolve_resume(
        db, store, uid, job_id, dest, wait_timeout=4, poll_interval=2, sleep=flip_sleep
    )
    assert source == "tailored"
    assert open(path, "rb").read() == b"TAILORED"


@pytest.mark.asyncio
async def test_miss_timeout_falls_back_to_base(db, tmp_path):
    store = FakeStorage()
    uid = _user(db)
    _base(db, store, uid)
    job_id = repo.get_or_create_job(db, "https://example.com/jobs/3")

    dest = str(tmp_path / "r.pdf")
    path, source = await resolve_resume(
        db, store, uid, job_id, dest, wait_timeout=2, poll_interval=2, sleep=_noop_sleep
    )
    assert source == "base"
    assert open(path, "rb").read() == b"BASE"
    # on-demand tailoring was enqueued for the miss
    assert repo.lookup_tailored(db, uid, job_id) is not None


@pytest.mark.asyncio
async def test_no_resume_raises(db, tmp_path):
    store = FakeStorage()
    uid = _user(db)  # no base, no tailored
    job_id = repo.get_or_create_job(db, "https://example.com/jobs/4")
    with pytest.raises(RuntimeError, match="no base PDF resume"):
        await resolve_resume(
            db,
            store,
            uid,
            job_id,
            str(tmp_path / "r.pdf"),
            wait_timeout=2,
            poll_interval=2,
            sleep=_noop_sleep,
        )

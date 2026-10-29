"""export_applications copies a job's cover letter next to its resume, and only for
jobs `autoapply cover` ran on. LocalFSStorage stands in for MinIO."""

from common import keys, repo
from common.storage import LocalFSStorage
from tools import export_applications as ex


def _ready_tailoring(db, store, uid, base, url, company):
    job_id = repo.add_job_for_user(db, uid, url)
    key = keys.tailored_resume_key(str(uid), str(job_id))
    store.put_bytes(key, b"%PDF resume")
    tailored = repo.insert_resume_asset(db, uid, "tailored", key, variant="pdf", job_id=job_id)
    with db.cursor() as cur:
        cur.execute("UPDATE jobs SET company = %s WHERE job_id = %s", (company, job_id))
        cur.execute(
            "INSERT INTO tailored_resumes (user_id, job_id, base_asset_id, tailored_asset_id, "
            "status, resume_score, new_resume_score) VALUES (%s, %s, %s, %s, 'ready', 60, 80)",
            (uid, job_id, base, tailored),
        )
    db.commit()
    return str(job_id)


def test_cover_letter_lands_next_to_the_resume(db, tmp_path):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, full_name, default_resume_label) "
            "VALUES ('x@test.dev', 'Jane Doe', 'swe') RETURNING user_id"
        )
        uid = str(cur.fetchone()["user_id"])
    db.commit()
    store = LocalFSStorage(str(tmp_path / "store"))
    base = repo.insert_resume_asset(db, uid, "base", "b.docx", variant="docx", label="swe")
    with_cover = _ready_tailoring(db, store, uid, base, "https://ex.dev/j/a", "Initech")
    _ready_tailoring(db, store, uid, base, "https://ex.dev/j/b", "Globex")
    store.put_bytes(keys.cover_letter_key(uid, with_cover, "pdf"), b"%PDF cover")

    root = tmp_path / "out"
    exported = ex.export(db, store, uid, root, "jane_doe.pdf", day=None, label="swe")

    assert sorted(rc for rc, _ in exported) == ["swe/globex", "swe/initech"]
    assert (root / "swe/initech/jane_doe_cover_letter.pdf").read_bytes() == b"%PDF cover"
    assert (root / "swe/initech/jane_doe.pdf").read_bytes() == b"%PDF resume"
    assert not (root / "swe/globex/jane_doe_cover_letter.pdf").exists()
    assert "cover     jane_doe_cover_letter.pdf" in (root / "swe/initech/job.txt").read_text()
    assert "cover     -" in (root / "swe/globex/job.txt").read_text()
    index = (root / "swe/index.md").read_text()
    assert "`initech/jane_doe.pdf` + `jane_doe_cover_letter.pdf`" in index

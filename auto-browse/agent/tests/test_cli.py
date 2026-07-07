"""Unit tests for the autoapply CLI core functions (cli/main.py). DB-backed tests
use the throwaway jobs_test DB via the shared `db` fixture; skip when infra is down."""

import subprocess
import uuid

import pytest

from cli import main as cli
from common import repo


def _mk_user(db, email="cli@test.dev", active=True) -> str:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, full_name, is_active) VALUES (%s, 'CLI Test', %s) "
            "RETURNING user_id",
            (email, active),
        )
        uid = cur.fetchone()["user_id"]
    db.commit()
    return uid


# ---------------------------------------------------------------------------
# resolve_user


def test_resolve_user_single_active(db):
    uid = _mk_user(db)
    assert cli.resolve_user(db, None) == uid


def test_resolve_user_none_active(db):
    with pytest.raises(SystemExit, match="no active users"):
        cli.resolve_user(db, None)


def test_resolve_user_multiple_active(db):
    _mk_user(db, "a@test.dev")
    _mk_user(db, "b@test.dev")
    with pytest.raises(SystemExit, match="multiple active users"):
        cli.resolve_user(db, None)


def test_resolve_user_by_email(db):
    uid = _mk_user(db)
    _mk_user(db, "other@test.dev")
    assert cli.resolve_user(db, "cli@test.dev") == uid


def test_resolve_user_by_uuid_passthrough(db):
    uid = str(uuid.uuid4())
    assert cli.resolve_user(db, uid) == uid


def test_resolve_user_unknown_email(db):
    with pytest.raises(SystemExit, match="no user with email"):
        cli.resolve_user(db, "ghost@test.dev")


# ---------------------------------------------------------------------------
# resolve_job


def test_resolve_job_by_uuid(db):
    job_id = repo.get_or_create_job(db, "https://ex.dev/j/1", source="manual")
    assert cli.resolve_job(db, job_id)["job_id"] == job_id


def test_resolve_job_by_prefix(db):
    job_id = repo.get_or_create_job(db, "https://ex.dev/j/1", source="manual")
    assert cli.resolve_job(db, str(job_id)[:8])["job_id"] == job_id


def test_resolve_job_by_url_variants(db):
    job_id = repo.get_or_create_job(db, "https://ex.dev/j/1", source="manual")
    # url_normalize drops www + trailing slash, forces https
    assert cli.resolve_job(db, "http://www.ex.dev/j/1/")["job_id"] == job_id


def test_resolve_job_unknown(db):
    with pytest.raises(SystemExit, match="no job"):
        cli.resolve_job(db, "deadbeef")


def test_resolve_job_ambiguous_prefix(db):
    # Insert jobs until two share a first hex char (bounded: 17 draws must collide).
    ids = [
        str(repo.get_or_create_job(db, f"https://ex.dev/j/{i}", source="manual"))
        for i in range(17)
    ]
    first_chars = [i[0] for i in ids]
    dup = next(c for c in first_chars if first_chars.count(c) > 1)
    with pytest.raises(SystemExit, match="ambiguous"):
        cli.resolve_job(db, dup)


# ---------------------------------------------------------------------------
# jobs add


def test_add_jobs_fresh_and_duplicate(db):
    r1 = cli.add_jobs(db, ["https://ex.dev/j/1"])[0]
    assert r1["duplicate"] is False
    with db.cursor() as cur:
        cur.execute("SELECT source, status FROM jobs WHERE job_id = %s", (r1["job_id"],))
        row = cur.fetchone()
    assert row["source"] == "manual"
    assert row["status"] == "jd_pending"

    r2 = cli.add_jobs(db, ["http://www.ex.dev/j/1/"])[0]  # normalized dup
    assert r2["duplicate"] is True
    assert r2["job_id"] == r1["job_id"]


def test_parse_url_lines():
    text = "https://a.dev/1\n\n# comment\n  https://b.dev/2  \n"
    assert cli.parse_url_lines(text) == ["https://a.dev/1", "https://b.dev/2"]


# ---------------------------------------------------------------------------
# tailor: backfill + summary


def _mk_ready_job(db, url="https://ex.dev/j/1") -> str:
    job_id = repo.get_or_create_job(db, url, source="manual")
    with db.cursor() as cur:
        cur.execute("UPDATE jobs SET status = 'jd_ready' WHERE job_id = %s", (job_id,))
    db.commit()
    return job_id


def test_backfill_tailoring(db):
    uid = _mk_user(db)
    job_id = _mk_ready_job(db)
    base_id = repo.insert_resume_asset(db, uid, "base", "k/base.docx", variant="docx")

    assert cli.backfill_tailoring(db, uid) == 1
    row = repo.lookup_tailored(db, uid, job_id)
    assert row["status"] == "pending"
    assert row["base_asset_id"] == base_id

    assert cli.backfill_tailoring(db, uid) == 0  # idempotent


def test_backfill_skips_non_ready_jobs(db):
    uid = _mk_user(db)
    repo.get_or_create_job(db, "https://ex.dev/j/1", source="manual")  # jd_pending
    assert cli.backfill_tailoring(db, uid) == 0


def test_tailoring_summary(db):
    uid = _mk_user(db)
    other = _mk_user(db, "other@test.dev")
    j1 = _mk_ready_job(db, "https://ex.dev/j/1")
    j2 = _mk_ready_job(db, "https://ex.dev/j/2")
    t1 = repo.enqueue_tailoring(db, uid, j1)
    repo.enqueue_tailoring(db, uid, j2)
    repo.enqueue_tailoring(db, other, j1)  # must not appear in uid's summary
    with db.cursor() as cur:
        cur.execute(
            "UPDATE tailored_resumes SET status='ready', resume_score=60, "
            "new_resume_score=85 WHERE id = %s",
            (t1,),
        )
    db.commit()

    s = cli.tailoring_summary(db, uid)
    assert s["tailor_counts"] == {"ready": 1, "pending": 1}
    assert len(s["rows"]) == 2
    ready = next(r for r in s["rows"] if r["status"] == "ready")
    assert (ready["resume_score"], ready["new_resume_score"]) == (60, 85)


# ---------------------------------------------------------------------------
# applications helpers (common/repo.py)


def test_application_lifecycle(db):
    uid = _mk_user(db)
    job_id = repo.get_or_create_job(db, "https://ex.dev/j/1", source="manual")
    app_id = repo.start_application(db, uid, job_id, run_id="run-1")
    with db.cursor() as cur:
        cur.execute("SELECT * FROM applications WHERE id = %s", (app_id,))
        row = cur.fetchone()
    assert row["status"] == "running"
    assert row["run_id"] == "run-1"
    assert row["started_at"] is not None
    assert row["finished_at"] is None

    repo.finish_application(db, app_id, "failed", outcome="boom")
    with db.cursor() as cur:
        cur.execute("SELECT * FROM applications WHERE id = %s", (app_id,))
        row = cur.fetchone()
    assert row["status"] == "failed"
    assert row["outcome"] == "boom"
    assert row["finished_at"] is not None


# ---------------------------------------------------------------------------
# tailor worker subprocess wiring


def test_run_tailor_worker_wiring(monkeypatch):
    calls = {}

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        calls["kw"] = kw
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cli.run_tailor_worker() == 0
    assert calls["cmd"] == ["uv", "run", "python", "worker.py"]
    assert str(calls["kw"]["cwd"]).endswith("ai-assist")
    assert calls["kw"]["env"]["TAILOR_ONCE"] == "1"


# ---------------------------------------------------------------------------
# parser smoke tests (no DB)


@pytest.mark.parametrize(
    "argv",
    [
        ["jobs", "add", "https://ex.dev/j/1"],
        ["jobs", "add", "--file", "urls.txt"],
        ["jobs", "list"],
        ["jobs", "list", "--status", "jd_ready"],
        ["tailor"],
        ["tailor", "--user", "u@x.dev", "--retry-failed"],
        ["status"],
        ["status", "abcd1234"],
        ["resume", "show", "abcd1234", "--out", "/tmp/r.pdf", "--open"],
        ["apply", "abcd1234", "https://ex.dev/j/2", "--model", "m"],
    ],
)
def test_parser_accepts(argv):
    args = cli.build_parser().parse_args(argv)
    assert callable(args.func)


def test_parser_rejects_bad_status():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["jobs", "list", "--status", "bogus"])

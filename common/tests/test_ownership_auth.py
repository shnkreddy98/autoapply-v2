"""Ownership (user_jobs) + auth-bootstrap helpers (sessions, google upsert, audit).
Integration against the throwaway jobs_test DB via the shared `db` fixture."""

import uuid

from common import queue, repo


def _mk_user(db, email="own@test.dev") -> str:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, full_name) VALUES (%s, 'Owner') RETURNING user_id",
            (email,),
        )
        uid = cur.fetchone()["user_id"]
    db.commit()
    return uid


# ---------------------------------------------------------------------------
# add_job_for_user / job_owner_ids / list_jobs_for_user


def test_add_job_for_user_deterministic_id_and_link(db):
    uid = _mk_user(db)
    url = "https://example.com/jobs/1"
    job_id = repo.add_job_for_user(db, uid, url, "Eng")
    assert str(job_id) == queue.job_id_for_url(url)
    assert repo.job_owner_ids(db, job_id) == [uid]


def test_add_job_for_user_shared_dedup(db):
    a = _mk_user(db, "a@test.dev")
    b = _mk_user(db, "b@test.dev")
    ja = repo.add_job_for_user(db, a, "https://example.com/jobs/1")
    jb = repo.add_job_for_user(db, b, "http://www.example.com/jobs/1/")  # normalized dup
    assert ja == jb
    assert set(repo.job_owner_ids(db, ja)) == {a, b}
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS c FROM jobs")
        assert cur.fetchone()["c"] == 1


def test_add_job_for_user_relinks_idempotently(db):
    uid = _mk_user(db)
    j1 = repo.add_job_for_user(db, uid, "https://example.com/jobs/1")
    j2 = repo.add_job_for_user(db, uid, "https://example.com/jobs/1")
    assert j1 == j2
    assert repo.job_owner_ids(db, j1) == [uid]


def test_add_job_for_user_reuses_legacy_random_id(db):
    """Rows created before deterministic ids keep their stored id."""
    uid = _mk_user(db)
    legacy_id = str(uuid.uuid4())
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO jobs (job_id, url, url_normalized, source) "
            "VALUES (%s, %s, %s, 'manual')",
            (legacy_id, "https://example.com/jobs/old", "https://example.com/jobs/old"),
        )
    db.commit()
    job_id = repo.add_job_for_user(db, uid, "https://example.com/jobs/old")
    assert str(job_id) == legacy_id
    assert repo.job_owner_ids(db, job_id) == [uid]


def test_list_jobs_for_user_scoped_and_filtered(db):
    a = _mk_user(db, "a@test.dev")
    b = _mk_user(db, "b@test.dev")
    ja = repo.add_job_for_user(db, a, "https://example.com/jobs/a")
    repo.add_job_for_user(db, b, "https://example.com/jobs/b")
    rows = repo.list_jobs_for_user(db, a)
    assert [r["job_id"] for r in rows] == [ja]
    assert repo.list_jobs_for_user(db, a, status="jd_ready") == []
    assert len(repo.list_jobs_for_user(db, a, status="jd_pending")) == 1


# ---------------------------------------------------------------------------
# sessions


def test_session_lifecycle(db):
    uid = _mk_user(db)
    token = repo.create_session(db, uid)
    sess = repo.get_session(db, token)
    assert sess["user_id"] == uid
    assert sess["email"] == "own@test.dev"
    assert "session_id" in sess

    assert repo.get_session(db, "not-a-real-token") is None

    repo.revoke_session(db, token)
    assert repo.get_session(db, token) is None


def test_session_expiry(db):
    uid = _mk_user(db)
    token = repo.create_session(db, uid)
    with db.cursor() as cur:
        cur.execute("UPDATE sessions SET expires_at = now() - interval '1 minute'")
    db.commit()
    assert repo.get_session(db, token) is None


def test_session_stores_hash_not_token(db):
    uid = _mk_user(db)
    token = repo.create_session(db, uid)
    with db.cursor() as cur:
        cur.execute("SELECT token_hash FROM sessions")
        stored = cur.fetchone()["token_hash"]
    assert stored != token
    assert token not in stored


# ---------------------------------------------------------------------------
# upsert_google_user


def test_upsert_google_user_creates_and_is_idempotent(db):
    u1 = repo.upsert_google_user(db, "sub-123", "new@test.dev", "New User")
    assert u1["role"] == "user"
    u2 = repo.upsert_google_user(db, "sub-123", "new@test.dev")
    assert u2["user_id"] == u1["user_id"]
    with db.cursor() as cur:
        cur.execute("SELECT count(*) AS c FROM users")
        assert cur.fetchone()["c"] == 1


def test_upsert_google_user_attaches_to_seeded_email(db):
    seeded = _mk_user(db, "seeded@test.dev")
    u = repo.upsert_google_user(db, "sub-456", "seeded@test.dev", "Ignored")
    assert u["user_id"] == seeded
    assert u["google_sub"] == "sub-456"
    assert u["full_name"] == "Owner"  # existing name wins


def test_upsert_google_user_follows_email_change(db):
    u1 = repo.upsert_google_user(db, "sub-789", "old@test.dev")
    u2 = repo.upsert_google_user(db, "sub-789", "renamed@test.dev")
    assert u2["user_id"] == u1["user_id"]
    assert u2["email"] == "renamed@test.dev"


# ---------------------------------------------------------------------------
# audit


def test_audit_appends_row(db):
    uid = _mk_user(db)
    repo.audit(db, uid, "jobs.add", "job", "abc", {"url": "https://x.dev/1"})
    with db.cursor() as cur:
        cur.execute("SELECT * FROM audit_log")
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["action"] == "jobs.add"
    assert rows[0]["detail"] == {"url": "https://x.dev/1"}

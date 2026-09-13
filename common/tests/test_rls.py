"""RLS proof: connect as the RLS-bound `autoapply_api` role and show that one
user's requests can never see or write another user's rows — even with raw SQL
(i.e. an API bug or injection can't cross tenants). Policies live in db/init.sql."""

import os

import psycopg
import pytest
from psycopg.rows import dict_row

from common import repo


def _api_connect():
    return psycopg.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "15432"),
        dbname=os.environ["DB_NAME"],
        user="autoapply_api",
        password=os.getenv("AUTOAPPLY_API_PASSWORD", "autoapply_api_dev"),
        row_factory=dict_row,
    )


@pytest.fixture
def api(db):  # depends on db so the schema exists and tables are truncated
    try:
        conn = _api_connect()
    except Exception as exc:
        pytest.skip(f"autoapply_api role unavailable: {exc}")
    yield conn
    conn.close()


@pytest.fixture
def two_users(db):
    """Two users with profiles + gmail tokens + one owned job each (as admin)."""
    out = {}
    for tag in ("a", "b"):
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO users (email, full_name) VALUES (%s, %s) RETURNING user_id",
                (f"{tag}@rls.dev", tag.upper()),
            )
            uid = cur.fetchone()["user_id"]
            cur.execute(
                "INSERT INTO application_profiles (user_id, city) VALUES (%s, %s)",
                (uid, f"{tag}ville"),
            )
            cur.execute(
                "INSERT INTO gmail_tokens (user_id, refresh_token) VALUES (%s, %s)",
                (uid, f"secret-{tag}"),
            )
        db.commit()
        job_id = repo.add_job_for_user(db, uid, f"https://rls.dev/jobs/{tag}")
        out[tag] = {"user_id": uid, "job_id": job_id}
    return out


def _as(conn, user_id, sql, params=()):
    """Run one statement inside a transaction with app.user_id set (SET LOCAL)."""
    with conn.transaction():
        conn.execute("SELECT set_config('app.user_id', %s, true)", (str(user_id),))
        return conn.execute(sql, params).fetchall()


def test_profiles_isolated(api, two_users):
    a = two_users["a"]["user_id"]
    rows = _as(api, a, "SELECT * FROM application_profiles")
    assert [r["city"] for r in rows] == ["aville"]


def test_gmail_tokens_isolated(api, two_users):
    a = two_users["a"]["user_id"]
    rows = _as(api, a, "SELECT * FROM gmail_tokens")
    assert [r["refresh_token"] for r in rows] == ["secret-a"]


def test_users_sees_only_self(api, two_users):
    a = two_users["a"]["user_id"]
    rows = _as(api, a, "SELECT * FROM users")
    assert [r["user_id"] for r in rows] == [a]


def test_jobs_visible_only_when_linked(api, two_users):
    a, b = two_users["a"], two_users["b"]
    rows = _as(api, a["user_id"], "SELECT job_id FROM jobs")
    assert [r["job_id"] for r in rows] == [a["job_id"]]

    # linking B's job makes it visible (add-then-link, deterministic ids)
    with api.transaction():
        api.execute(
            "SELECT set_config('app.user_id', %s, true)", (str(a["user_id"]),)
        )
        api.execute(
            "INSERT INTO user_jobs (user_id, job_id) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (a["user_id"], b["job_id"]),
        )
    rows = _as(api, a["user_id"], "SELECT job_id FROM jobs ORDER BY created_at")
    assert {r["job_id"] for r in rows} == {a["job_id"], b["job_id"]}


def test_insert_for_other_user_rejected(api, two_users):
    a, b = two_users["a"]["user_id"], two_users["b"]["user_id"]
    with pytest.raises(psycopg.errors.Error, match="row-level security"):
        _as(
            api, a,
            "INSERT INTO user_jobs (user_id, job_id) VALUES (%s, %s)",
            (b, two_users["b"]["job_id"]),
        )
    with pytest.raises(psycopg.errors.Error, match="row-level security"):
        _as(
            api, a,
            "INSERT INTO application_profiles (user_id, city) VALUES (%s, 'x')",
            (b,),
        )


def test_no_user_set_sees_nothing(api, two_users):
    with api.transaction():
        rows = api.execute("SELECT * FROM application_profiles").fetchall()
    assert rows == []


def test_sessions_and_audit_deny_all_for_api_role(api, two_users, db):
    a = two_users["a"]["user_id"]
    repo.create_session(db, a)  # admin plane can
    assert _as(api, a, "SELECT * FROM sessions") == []
    with pytest.raises(psycopg.errors.Error, match="row-level security"):
        _as(api, a, "INSERT INTO audit_log (user_id, action) VALUES (%s, 'x')", (a,))


def test_api_role_cannot_update_jobs(api, two_users):
    a = two_users["a"]
    # no UPDATE policy on jobs -> zero rows affected, silently, even for owned rows
    with api.transaction():
        api.execute("SELECT set_config('app.user_id', %s, true)", (str(a["user_id"]),))
        cur = api.execute(
            "UPDATE jobs SET status = 'failed' WHERE job_id = %s", (a["job_id"],)
        )
        assert cur.rowcount == 0

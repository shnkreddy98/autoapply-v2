"""Integration fixtures. Need live Postgres + MinIO from the root compose:

    docker compose up -d            # in repo root

Tests using `db` / `s3` are skipped automatically if the infra is unreachable.
"""

import os

import pytest

# default env so common.db / common.storage point at the root compose services
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_PORT", "15432")
os.environ.setdefault("S3_ENDPOINT", "http://localhost:9100")
os.environ.setdefault("S3_BUCKET", "autoapply")
# Force tests onto a throwaway DB — the `db` fixture TRUNCATEs every table, so it must
# never point at the real jobs-db. Set unconditionally (not setdefault) for safety.
os.environ["DB_NAME"] = os.getenv("TEST_DB_NAME", "jobs_test")
# Valid Fernet key (base64 of 32 bytes) for at-rest encryption in tests.
os.environ.setdefault("AUTOAPPLY_ENC_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")

_TABLES = [
    "audit_log", "sessions", "saved_answers", "applications", "tailored_resumes", "resume_assets",
    "user_jobs", "jobs", "gmail_tokens", "professional_references",
    "application_profiles", "portal_credentials",
    "users",
]


@pytest.fixture
def db():
    from common import db as dbmod, testdb

    try:
        testdb.ensure_test_db()
        conn = dbmod.connect()
    except dbmod.NonTestDatabase:
        raise  # misconfigured DB target must fail loudly, never skip
    except Exception as exc:  # infra down -> skip, don't fail
        pytest.skip(f"postgres unavailable: {exc}")
    testdb.assert_test_db(conn)  # hard stop if this is not a *_test database
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE")
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def s3():
    from common import storage

    try:
        store = storage.from_env()  # S3Storage by default
        store.client.list_buckets()
    except Exception as exc:
        pytest.skip(f"minio unavailable: {exc}")
    return store


@pytest.fixture
def seed_user(db):
    """Insert one user whose default role is 'general', with that base resume docx;
    return (user_id, base_asset_id)."""
    from common import repo

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, full_name, default_resume_label) "
            "VALUES (%s, %s, 'general') RETURNING user_id",
            ("tester@example.com", "Test User"),
        )
        user_id = cur.fetchone()["user_id"]
    db.commit()
    asset_id = repo.insert_resume_asset(
        db, user_id, "base", f"users/{user_id}/resumes/base/general/docx.docx",
        variant="docx", label="general",
    )
    return user_id, asset_id

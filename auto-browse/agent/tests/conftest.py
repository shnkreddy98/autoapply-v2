"""Shared fixtures. `db` needs live Postgres (root compose); skips if unreachable."""

import os

import pytest

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
    "audit_log", "sessions", "applications", "tailored_resumes", "resume_assets",
    "user_jobs", "jobs", "gmail_tokens", "professional_references", "skill_groups",
    "education", "work_experiences", "application_profiles", "portal_credentials",
    "users",
]


@pytest.fixture
def db():
    from common import db as dbmod, testdb

    try:
        testdb.ensure_test_db()
        conn = dbmod.connect()
    except Exception as exc:
        pytest.skip(f"postgres unavailable: {exc}")
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE")
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def s3():
    from common import storage

    try:
        store = storage.from_env()
        store.client.list_buckets()
    except Exception as exc:
        pytest.skip(f"minio unavailable: {exc}")
    return store

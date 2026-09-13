"""Integration fixtures. Need live Postgres from the root compose:

    docker compose up -d            # in repo root

Tests using `db` are skipped automatically if Postgres is unreachable.
"""

import os

import pytest

os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_PORT", "15432")
# Force tests onto a throwaway DB — the `db` fixture TRUNCATEs every table, so it must
# never point at the real jobs-db. Set unconditionally (not setdefault) for safety.
os.environ["DB_NAME"] = os.getenv("TEST_DB_NAME", "jobs_test")

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
    except Exception as exc:
        pytest.skip(f"postgres unavailable: {exc}")
    testdb.assert_test_db(conn)  # hard stop if this is not a *_test database
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE")
    conn.commit()
    yield conn
    conn.close()

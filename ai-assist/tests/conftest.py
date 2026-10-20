import os
from pathlib import Path

import pytest

os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_PORT", "15432")
os.environ.setdefault("S3_ENDPOINT", "http://localhost:9100")
os.environ.setdefault("S3_BUCKET", "autoapply")
# Force tests onto a throwaway DB — the `db` fixture TRUNCATEs every table, so it must
# never point at the real jobs-db. Set unconditionally (not setdefault) for safety.
os.environ["DB_NAME"] = os.getenv("TEST_DB_NAME", "jobs_test")
# env.py refuses to import without a key, and every test importing agents.py goes
# through it. CI has no dev.env, so from 2026-07-24 the ai-assist step died at
# collection and the steps after it never ran. No test calls an LLM except the opt-in
# RUN_LLM_E2E one, which needs dev.env's real key — so the stand-in is only set when
# dev.env is absent (load_dotenv never overrides a variable already set). An unset
# Actions secret arrives as "", not missing, so empty counts as absent too.
if not (Path(__file__).resolve().parent.parent / "dev.env").exists():
    if not os.environ.get("OPENROUTER_API_KEY"):
        os.environ["OPENROUTER_API_KEY"] = "placeholder-tests-make-no-llm-calls"

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
    if conn.info.dbname == "jobs-db":  # belt and braces: never TRUNCATE the real DB
        raise RuntimeError(f"refusing to run destructive fixture against {conn.info.dbname}")
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
        store = storage.from_env()
        store.client.list_buckets()
    except Exception as exc:
        pytest.skip(f"minio unavailable: {exc}")
    return store

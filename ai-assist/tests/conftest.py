import os

import pytest

os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_PORT", "15432")
os.environ.setdefault("S3_ENDPOINT", "http://localhost:9100")
os.environ.setdefault("S3_BUCKET", "autoapply")

_TABLES = [
    "applications", "tailored_resumes", "resume_assets", "jobs",
    "professional_references", "skill_groups", "education", "work_experiences",
    "application_profiles", "portal_credentials", "users",
]


@pytest.fixture
def db():
    from common import db as dbmod

    try:
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

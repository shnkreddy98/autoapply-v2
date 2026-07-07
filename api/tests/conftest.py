"""API test fixtures. Need live Postgres from the root compose; tests skip when
it's unreachable. Google OAuth is stubbed — no network, no real Google."""

import os

import pytest

os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_PORT", "15432")
# Force tests onto a throwaway DB — the `db` fixture TRUNCATEs every table, so it must
# never point at the real jobs-db. Set unconditionally (not setdefault) for safety.
os.environ["DB_NAME"] = os.getenv("TEST_DB_NAME", "jobs_test")
# Valid Fernet key (base64 of 32 bytes) for at-rest encryption in tests.
os.environ.setdefault("AUTOAPPLY_ENC_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
# Secure cookies don't survive the http://testserver transport.
os.environ["API_COOKIE_SECURE"] = "0"
# Presigned resume links come from the local FS backend — no MinIO needed.
os.environ["STORAGE_BACKEND"] = "local"

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
def client_factory(db, monkeypatch):
    """Make signed-in TestClients (one cookie jar per user). Google's code
    exchange is stubbed to return the requested identity."""
    from fastapi.testclient import TestClient

    from api import auth
    from api.app import create_app

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-secret")
    app = create_app()

    def make(email: str | None = None, sub: str | None = None) -> "TestClient":
        client = TestClient(app)
        if email is None:
            return client  # anonymous

        async def fake_exchange(code, redirect_uri):
            return {"sub": sub or f"sub-{email}", "email": email, "name": "Test"}

        monkeypatch.setattr(auth, "_exchange_code", fake_exchange)
        r = client.get("/auth/google/login", follow_redirects=False)
        assert r.status_code == 302
        state = r.headers["location"].split("state=")[1].split("&")[0]
        r = client.get(
            f"/auth/google/callback?code=stub&state={state}", follow_redirects=False
        )
        assert r.status_code == 302, r.text
        return client

    return make

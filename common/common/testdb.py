"""Test-database bootstrap. Keeps the destructive TRUNCATE in the test fixtures
off the real `jobs-db`: tests run against a throwaway database (default `jobs_test`,
override with TEST_DB_NAME), created on first use with the canonical schema.

Call `ensure_test_db()` from a conftest AFTER pointing DB_NAME at the test DB.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg

# repo root: common/common/testdb.py -> common/common -> common -> <root>
_INIT_SQL = Path(__file__).resolve().parents[2] / "db" / "init.sql"


# Databases that hold real data. Truncating one of these is unrecoverable without a
# dump, so the guard below is a hard stop, not a warning.
_PROTECTED_DBS = {"jobs-db", "jobs", "postgres"}


def assert_test_db(conn) -> None:
    """Abort unless `conn` is on a throwaway test database.

    Call this immediately before any destructive fixture statement. It asks the
    *server* for the current database rather than trusting DB_NAME, so an inherited
    env var, a DB_DSN override or a fixture that connects on its own can't quietly
    aim a TRUNCATE at live data. Anything not named `*_test` is rejected."""
    current = conn.execute("SELECT current_database() AS name").fetchone()["name"]
    if current in _PROTECTED_DBS or not current.endswith("_test"):
        raise RuntimeError(
            f"refusing to run a destructive test fixture against {current!r} — "
            "test databases must be named *_test (set TEST_DB_NAME, or fix DB_NAME/DB_DSN)"
        )


def _admin_dsn() -> str:
    """DSN to the maintenance `postgres` DB — used only to CREATE the test DB."""
    return (
        f"host={os.getenv('DB_HOST', 'localhost')} "
        f"port={os.getenv('DB_PORT', '15432')} "
        f"dbname=postgres "
        f"user={os.getenv('DB_USER', 'admin')} "
        f"password={os.getenv('DB_PASSWORD', 'password')}"
    )


# Newest table in init.sql — bump when the schema grows so stale test DBs rebuild.
_SCHEMA_SENTINEL = "public.saved_answers"


def ensure_test_db() -> None:
    """Create the test DB (if missing) and load the schema (if not yet loaded).

    Idempotent: safe to call before every test. `init.sql` uses bare CREATE TABLE
    (not IF NOT EXISTS), so it only ever runs against an empty schema: a fresh DB,
    or — when the sentinel table is missing (schema drift) — after dropping and
    recreating `public`. The test DB is throwaway by contract, so that's safe."""
    name = os.environ["DB_NAME"]  # conftest has already pointed this at the test DB
    with psycopg.connect(_admin_dsn(), autocommit=True) as admin:
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).fetchone()
        if not exists:
            admin.execute(f'CREATE DATABASE "{name}"')

    from common import db as dbmod

    with dbmod.connect() as conn:
        current = conn.execute(
            "SELECT to_regclass(%s)", (_SCHEMA_SENTINEL,)
        ).fetchone()["to_regclass"]
        if not current:
            conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
            conn.execute(_INIT_SQL.read_text())
            conn.commit()

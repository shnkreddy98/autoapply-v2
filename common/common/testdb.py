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


def _admin_dsn() -> str:
    """DSN to the maintenance `postgres` DB — used only to CREATE the test DB."""
    return (
        f"host={os.getenv('DB_HOST', 'localhost')} "
        f"port={os.getenv('DB_PORT', '15432')} "
        f"dbname=postgres "
        f"user={os.getenv('DB_USER', 'admin')} "
        f"password={os.getenv('DB_PASSWORD', 'password')}"
    )


def ensure_test_db() -> None:
    """Create the test DB (if missing) and load the schema (if not yet loaded).

    Idempotent: safe to call before every test. `init.sql` is applied only when the
    `users` table is absent, since it uses bare CREATE TABLE (not IF NOT EXISTS)."""
    name = os.environ["DB_NAME"]  # conftest has already pointed this at the test DB
    with psycopg.connect(_admin_dsn(), autocommit=True) as admin:
        exists = admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).fetchone()
        if not exists:
            admin.execute(f'CREATE DATABASE "{name}"')

    from common import db as dbmod

    with dbmod.connect() as conn:
        loaded = conn.execute("SELECT to_regclass('public.users')").fetchone()["to_regclass"]
        if not loaded:
            conn.execute(_INIT_SQL.read_text())
            conn.commit()

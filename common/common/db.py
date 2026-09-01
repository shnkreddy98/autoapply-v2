"""Postgres connection helpers. DSN from env (DB_* names shared across services)."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class NonTestDatabase(RuntimeError):
    """Raised when a test run tries to open a connection to a real database.

    Its own type so conftests can re-raise it instead of folding it into their
    "infra unavailable -> skip" branch, where a misconfigured run would look green."""


def _guard_test_run(name: str) -> None:
    """Under pytest, refuse to hand out a connection to a non-test database.

    Test fixtures TRUNCATE every table, so a package whose conftest forgets to point
    DB_NAME at a throwaway DB destroys live data. That has happened twice (the second
    time because a newly added package copied a conftest that predated the fix), so
    the check lives here — on the one path every package's tests must go through —
    rather than in each conftest, where it can be forgotten again.

    Escape hatch for a deliberate read-only test against real data:
    ALLOW_NON_TEST_DB=1."""
    if "PYTEST_CURRENT_TEST" not in os.environ and "PYTEST_VERSION" not in os.environ:
        return
    if os.getenv("ALLOW_NON_TEST_DB") == "1" or name.endswith("_test"):
        return
    raise NonTestDatabase(
        f"refusing to connect to {name!r} from a test run — test databases must be "
        "named *_test. Point DB_NAME/TEST_DB_NAME at a throwaway DB (see "
        "common.testdb), or set ALLOW_NON_TEST_DB=1 if this test really is read-only."
    )


def dsn() -> str:
    name = os.getenv("DB_NAME", "jobs-db")
    _guard_test_run(name)
    return (
        f"host={os.getenv('DB_HOST', 'localhost')} "
        f"port={os.getenv('DB_PORT', '15432')} "
        f"dbname={name} "
        f"user={os.getenv('DB_USER', 'admin')} "
        f"password={os.getenv('DB_PASSWORD', 'password')}"
    )


_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    """Lazily-opened shared pool. Long-lived workers reuse one pool."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            dsn(),
            min_size=1,
            max_size=int(os.getenv("DB_POOL_MAX", "10")),
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _pool


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    """A pooled connection (dict rows). Caller controls the transaction."""
    with pool().connection() as conn:
        yield conn


def connect() -> psycopg.Connection:
    """A standalone connection (dict rows). For short scripts / tests that don't
    want the pool. Caller must close it."""
    return psycopg.connect(dsn(), row_factory=dict_row)

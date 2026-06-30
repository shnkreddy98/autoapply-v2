"""Postgres connection helpers. DSN from env (DB_* names shared across services)."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def dsn() -> str:
    return (
        f"host={os.getenv('DB_HOST', 'localhost')} "
        f"port={os.getenv('DB_PORT', '15432')} "
        f"dbname={os.getenv('DB_NAME', 'jobs-db')} "
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

"""Two connection planes for the API.

* `admin_conn()` — the container superuser (same creds as workers). Used ONLY for
  auth bootstrap (session lookup happens before we know the user) and admin
  endpoints. Keep its query surface minimal.
* `user_conn(user_id)` — the RLS-bound `autoapply_api` role with
  `SET LOCAL app.user_id` inside a transaction. All user-facing data access goes
  through this: even raw SQL cannot cross tenants (policies in db/init.sql).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from common import db as commondb


def _api_dsn() -> str:
    return (
        f"host={os.getenv('DB_HOST', 'localhost')} "
        f"port={os.getenv('DB_PORT', '15432')} "
        f"dbname={os.getenv('DB_NAME', 'jobs-db')} "
        f"user=autoapply_api "
        f"password={os.getenv('AUTOAPPLY_API_PASSWORD', 'autoapply_api_dev')}"
    )


_user_pool: ConnectionPool | None = None


def user_pool() -> ConnectionPool:
    global _user_pool
    if _user_pool is None:
        _user_pool = ConnectionPool(
            _api_dsn(),
            min_size=1,
            max_size=int(os.getenv("API_DB_POOL_MAX", "10")),
            kwargs={"row_factory": dict_row},
            open=True,
        )
    return _user_pool


@contextmanager
def user_conn(user_id: str) -> Iterator[psycopg.Connection]:
    """RLS-scoped connection: one transaction with app.user_id pinned. Commits on
    clean exit, rolls back on exception."""
    with user_pool().connection() as conn:
        with conn.transaction():
            conn.execute(
                "SELECT set_config('app.user_id', %s, true)", (str(user_id),)
            )
            yield conn


@contextmanager
def admin_conn() -> Iterator[psycopg.Connection]:
    """Superuser connection (bypasses RLS) — auth bootstrap + /admin only."""
    with commondb.get_conn() as conn:
        yield conn

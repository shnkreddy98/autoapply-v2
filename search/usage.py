"""Monthly Brave-query budget, tracked in Postgres.

One row per calendar month; `try_consume` atomically increments the counter and
refuses once the cap is reached, so concurrent runners can't overspend. Each
billed API request (every pagination page) must consume exactly one unit.
"""

from __future__ import annotations

import psycopg

DDL = """
CREATE TABLE IF NOT EXISTS search_api_usage (
    month   date PRIMARY KEY,
    queries int  NOT NULL DEFAULT 0
)
"""


def ensure_table(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()


def try_consume(conn: psycopg.Connection, cap: int) -> int | None:
    """Reserve one query against this month's budget.

    Returns the new used-count, or None if the cap is already reached
    (nothing is consumed in that case)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO search_api_usage AS u (month, queries)
            VALUES (date_trunc('month', now())::date, 1)
            ON CONFLICT (month) DO UPDATE SET queries = u.queries + 1
            WHERE u.queries < %s
            RETURNING queries
            """,
            (cap,),
        )
        row = cur.fetchone()
    conn.commit()
    return row["queries"] if row else None


def used_this_month(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT queries FROM search_api_usage WHERE month = date_trunc('month', now())::date"
        )
        row = cur.fetchone()
    return row["queries"] if row else 0

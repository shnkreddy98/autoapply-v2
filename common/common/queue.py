"""Postgres job-queue helpers — the transport that decouples the pipeline stages.

`claim_pending` uses FOR UPDATE SKIP LOCKED so many workers can claim disjoint
rows concurrently. Each queue table has a `pending -> in-progress -> terminal`
status flow; claiming flips the row to its in-progress status inside one short
transaction (no network I/O held), then the worker does slow work and calls
`mark` to set the terminal status.
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import psycopg

# Per-queue config: (primary-key column, pending status, in-progress status).
# Restricting to known tables also keeps the f-string SQL injection-safe.
_QUEUES: dict[str, tuple[str, str, str]] = {
    "jobs": ("job_id", "jd_pending", "jd_capturing"),
    "tailored_resumes": ("id", "pending", "tailoring"),
}


def claim_pending(conn: psycopg.Connection, table: str, worker_id: str | None = None) -> dict | None:
    """Atomically claim the oldest pending row, flipping it to in-progress.
    Returns the claimed row (as it was, pre-flip) or None if the queue is empty."""
    pk, pending, in_progress = _QUEUES[table]
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM {table} WHERE status = %s "  # noqa: S608 - table from allowlist
            f"ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1",
            (pending,),
        )
        row = cur.fetchone()
        if row is None:
            conn.commit()
            return None
        # only tailored_resumes carries claimed_by/claimed_at; jobs does not
        extra = {"claimed_by": worker_id, "claimed_at": _NOW} if table == "tailored_resumes" else {}
        _update_status(cur, table, pk, row[pk], in_progress, extra)
        conn.commit()
        return row


_NOW = object()  # sentinel -> SQL now()


def _update_status(cur, table: str, pk: str, row_id, status: str, extra: dict) -> None:
    sets = ["status = %s", "updated_at = now()"]
    vals: list[Any] = [status]
    for col, val in extra.items():
        if val is _NOW:
            sets.append(f"{col} = now()")
        else:
            sets.append(f"{col} = %s")
            vals.append(val)
    vals.append(row_id)
    cur.execute(
        f"UPDATE {table} SET {', '.join(sets)} WHERE {pk} = %s",  # noqa: S608 - allowlisted
        vals,
    )


def mark(conn: psycopg.Connection, table: str, row_id: str, status: str, **fields: Any) -> None:
    """Set a row's terminal status (+ optional columns) and commit."""
    pk = _QUEUES[table][0]
    sets = ["status = %s", "updated_at = now()"]
    vals: list[Any] = [status]
    for col, val in fields.items():
        sets.append(f"{col} = %s")
        vals.append(val)
    vals.append(row_id)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {table} SET {', '.join(sets)} WHERE {pk} = %s",  # noqa: S608 - allowlisted
            vals,
        )
    conn.commit()


def url_normalize(url: str) -> str:
    """Canonicalize a job URL for dedupe: force https, drop www / query / fragment,
    lowercase host, strip trailing slash."""
    raw = url.strip()
    parts = urlsplit(raw if "://" in raw else "https://" + raw)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, "", ""))


def job_id_for_url(url: str) -> str:
    """Deterministic UUID5 from the normalized URL (stable id without a DB round-trip)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, url_normalize(url)))

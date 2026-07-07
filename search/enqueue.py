"""Persist discovered jobs into the Postgres queue (status `jd_pending`).

Dedupe is handled by `repo.get_or_create_job` on the normalized URL, so repeated
search runs over the same postings are idempotent. The JD-capture worker picks
these up next.
"""

from __future__ import annotations

from typing import Iterable

import psycopg

from common import db, repo


def enqueue_jobs(
    results: Iterable[dict],
    source: str = "search",
    conn: psycopg.Connection | None = None,
    user_id: str | None = None,
) -> list[str]:
    """Insert each result's URL as a job (deduped). Returns the job_ids.

    Pass user_id to link ownership (user_jobs) — without an owner the JD-capture
    worker will capture the JD but enqueue tailoring for nobody."""
    own = conn is None
    conn = conn or db.connect()
    job_ids: list[str] = []
    try:
        for r in results:
            url = (r.get("url") or "").strip()
            if not url:
                continue
            if user_id:
                job_ids.append(repo.add_job_for_user(conn, user_id, url, r.get("title"), source))
            else:
                job_ids.append(repo.get_or_create_job(conn, url, r.get("title"), source))
    finally:
        if own:
            conn.close()
    return job_ids

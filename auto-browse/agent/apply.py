"""Apply-time resume resolution: pick the resume to upload for a (user, job).

Prefers a precomputed tailored resume. On a miss it enqueues on-demand tailoring
and waits a bounded time; if it isn't ready in time, falls back to the user's base
resume so the application never blocks. Returns the local path the file was staged
to (for the browser's file upload) plus which source was used.
"""

from __future__ import annotations

import asyncio
import logging
import math
from pathlib import Path
from typing import Awaitable, Callable

import psycopg

from common import keys, repo
from common import storage as storagemod

log = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]


def _download(storage: storagemod.Storage, s3_key: str, dest: str) -> str:
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    storage.get_file(s3_key, dest)
    return dest


def _ready_tailored_key(conn: psycopg.Connection, row: dict | None) -> str | None:
    if not row or row.get("status") != "ready" or not row.get("tailored_asset_id"):
        return None
    asset = repo.get_resume_asset(conn, row["tailored_asset_id"])
    return asset["s3_key"] if asset else None


async def resolve_resume(
    conn: psycopg.Connection,
    storage: storagemod.Storage,
    user_id: str,
    job_id: str,
    dest: str,
    *,
    wait_timeout: float = 30.0,
    poll_interval: float = 2.0,
    sleep: Sleep = asyncio.sleep,
) -> tuple[str, str]:
    """Stage the resume to `dest`; return (path, source) where source is
    'tailored' or 'base'. Raises if the user has no resume at all."""
    tailored = repo.lookup_tailored(conn, user_id, job_id)

    key = _ready_tailored_key(conn, tailored)
    if key:
        return _download(storage, key, dest), "tailored"

    base = repo.get_base_asset(conn, user_id)
    if tailored is None:  # never queued for this user×job -> trigger on-demand
        repo.enqueue_tailoring(conn, user_id, job_id, base["id"] if base else None)

    polls = max(1, math.ceil(wait_timeout / poll_interval))
    for _ in range(polls):
        await sleep(poll_interval)
        tailored = repo.lookup_tailored(conn, user_id, job_id)
        key = _ready_tailored_key(conn, tailored)
        if key:
            return _download(storage, key, dest), "tailored"
        if tailored and tailored["status"] == "failed":
            break

    if base is None:
        base = repo.get_base_asset(conn, user_id)
    if base is None:
        raise RuntimeError(f"no resume available for user {user_id}")
    log.info("tailored resume not ready for job=%s; using base resume", job_id)
    return _download(storage, base["s3_key"], dest), "base"

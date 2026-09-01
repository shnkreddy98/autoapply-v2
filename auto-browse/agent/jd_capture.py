"""JD-capture worker: turn `jd_pending` jobs into captured job descriptions.

For each claimed job it navigates the URL (via the text-extracting proxy MCP),
stores the visible-text JD to object storage, flips the job to `jd_ready`, and
enqueues a tailoring job per active user — so resumes are tailored *ahead* of apply.

The navigation + storage are injected, so the orchestration (`capture_once`) is
unit-testable with fakes. The real navigator (`proxy_navigator`) needs the
playwright + proxy containers and is exercised manually / behind an env flag.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from datetime import datetime, timezone
from typing import Awaitable, Callable

import psycopg

from common import keys, queue, repo
from common import storage as storagemod

log = logging.getLogger(__name__)

# async navigate(url) -> visible page text
Navigator = Callable[[str], Awaitable[str]]

PROXY_MCP_URL = os.getenv("PROXY_MCP_URL", "http://localhost:8932/mcp")
# Hard cap on a single page navigation. A slow/wedged site (Workday SPAs, bot walls)
# must not stall the whole queue — on timeout the job is marked failed and we move on.
NAV_TIMEOUT_SECONDS = float(os.getenv("NAV_TIMEOUT_SECONDS", "60"))

# Reject a capture that's clearly not a job description, so we never store a loading
# shell / pure form page and then tailor against junk (which silently scores 0).
MIN_JD_CHARS = int(os.getenv("MIN_JD_CHARS", "500"))
# A real JD almost always uses some of these; a loading screen / nav chrome uses none.
_JD_SIGNAL_WORDS = (
    "responsibilit",
    "requirement",
    "qualif",
    "experience",
    "skills",
    "you'll",
    "you will",
    "what you",
    "the role",
    "we are looking",
    "about the",
)
# Substrings that mark an un-rendered SPA shell.
_LOADING_MARKERS = ("loading... please wait", "please wait", "just a moment")


def _clean_jd(text: str) -> str:
    """Drop the proxy's leading stale-ref interaction hint (prepended on every
    navigate) — it's guidance for an interacting agent, not JD content."""
    s = text.lstrip()
    if s.startswith("⚠"):
        _, sep, rest = s.partition("\n\n")
        if sep:
            s = rest
    return s.strip()


def _reject_reason(text: str) -> str | None:
    """Return why `text` is not a usable JD, or None if it passes the quality gate."""
    low = text.lower()
    if any(m in low for m in _LOADING_MARKERS) and len(text) < 1500:
        return "page still loading (SPA shell)"
    if len(text) < MIN_JD_CHARS:
        return f"too short ({len(text)} < {MIN_JD_CHARS} chars)"
    if not any(w in low for w in _JD_SIGNAL_WORDS):
        return "no job-description content detected"
    return None


def _enqueue_for_owners(
    conn: psycopg.Connection,
    job_id: str,
    base_labels: dict[str, str] | None = None,
) -> int:
    """Queue a tailoring job (user×job) for every owner of the job (user_jobs),
    attaching their current base resume asset if they have one. Owners-only: a
    job one user added must never fan out to other users' queues.

    `base_labels` maps user_id -> role label for the users who asked for one (the
    caller of `autoapply tailor --label`); every other owner keeps their default
    base. Without it, capture would enqueue against the default base and the row
    would already exist by the time the CLI's label-aware backfill ran, silently
    ignoring --label. A label with no uploaded base falls back to the default
    rather than enqueuing with no source at all."""
    labels = base_labels or {}
    n = 0
    for uid in repo.job_owner_ids(conn, job_id):
        label = labels.get(str(uid))
        base = repo.get_base_asset(
            conn, uid, variant="docx", label=label
        )  # tailoring edits the docx
        if base is None and label is not None:
            base = repo.get_base_asset(conn, uid, variant="docx")
        repo.enqueue_tailoring(conn, uid, job_id, base["id"] if base else None)
        n += 1
    return n


async def capture_once(
    conn: psycopg.Connection,
    navigate: Navigator,
    storage: storagemod.Storage,
    worker_id: str = "jd-capture",
    base_labels: dict[str, str] | None = None,
) -> str | None:
    """Claim one pending job, capture its JD, enqueue tailoring. Returns the job_id
    handled (success or failed), or None if the queue was empty.

    `base_labels` (user_id -> role label) picks which base resume the enqueued
    tailoring starts from; see `_enqueue_for_owners`."""
    job = queue.claim_pending(conn, "jobs", worker_id=worker_id)
    if job is None:
        return None
    job_id = job["job_id"]
    try:
        try:
            raw = await asyncio.wait_for(navigate(job["url"]), NAV_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            raise TimeoutError(f"navigation exceeded {NAV_TIMEOUT_SECONDS:.0f}s")
        jd_text = _clean_jd(raw or "")
        if not jd_text:
            raise ValueError("empty JD text from navigation")
        reason = _reject_reason(jd_text)
        if reason:
            raise ValueError(f"not a usable JD: {reason}")
        key = keys.jd_key(job_id)
        storage.put_bytes(key, jd_text.encode("utf-8"), "text/markdown")
        queue.mark(
            conn,
            "jobs",
            job_id,
            "jd_ready",
            jd_s3_key=key,
            jd_captured_at=datetime.now(timezone.utc),
        )
        count = _enqueue_for_owners(conn, job_id, base_labels)
        log.info("captured JD job=%s bytes=%d enqueued=%d", job_id, len(jd_text), count)
    except Exception as exc:  # one bad job must not kill the worker
        log.warning("JD capture failed job=%s: %s", job_id, exc)
        queue.mark(conn, "jobs", job_id, "failed", error=str(exc)[:500])
    return job_id


async def run(
    navigate: Navigator,
    storage: storagemod.Storage,
    conn: psycopg.Connection,
    poll_interval: float = 5.0,
    once: bool = False,
) -> None:
    """Drain the queue, then poll. `once=True` processes the whole backlog and stops."""
    while True:
        job_id = await capture_once(conn, navigate, storage)
        if job_id is None:
            if once:
                return
            await asyncio.sleep(poll_interval)


@contextlib.asynccontextmanager
async def proxy_navigator():
    """Real navigator over the proxy MCP. Needs playwright + proxy running.

    async with proxy_navigator() as navigate:
        text = await navigate(url)
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.types import TextContent

    async with streamable_http_client(PROXY_MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def navigate(url: str) -> str:
                result = await session.call_tool("browser_navigate", {"url": url})
                parts = [c.text for c in result.content if isinstance(c, TextContent)]
                return "\n".join(parts)

            yield navigate


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    from common import db

    storage = storagemod.from_env()
    with db.connect() as conn:
        async with proxy_navigator() as navigate:
            await run(navigate, storage, conn, once=bool(os.getenv("JD_CAPTURE_ONCE")))


if __name__ == "__main__":
    asyncio.run(_main())

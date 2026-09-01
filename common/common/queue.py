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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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


# Query params that are tracking/presentation noise, never posting identity. Anything
# NOT listed here is kept: on many ATS the job id lives in the query (`?gh_jid=`,
# `?ShowJob=`, `?opportunityId=`), so dropping the whole query merged distinct
# postings into one row and silently lost the second. A missed tracker at worst
# creates a duplicate row, which `autoapply tailor` now reports — the safer failure.
_TRACKING_PARAMS = {
    "domain", "iis", "iisn", "lang", "microsite", "mobile", "mode", "height", "width",
    "ref", "referer", "referrer", "src", "source", "trk", "trackingid",
    "gh_src", "campaign", "medium", "utm",
}
_TRACKING_PREFIXES = ("utm_",)


def _keep_param(key: str) -> bool:
    k = key.lower()
    return k not in _TRACKING_PARAMS and not k.startswith(_TRACKING_PREFIXES)


# Trailing path segments that mean "the application form for the posting above", not
# a posting of their own. Ashby (`/<org>/<uuid>/application`), Lever/Workday/Workable
# (`.../apply`) and friends all serve a bare form there — no description, so JD capture
# gets an empty page and the job fails. Dropping the segment yields the JD page, which
# is both what the browser should fetch and the right dedupe key: the form URL and the
# posting URL are the same job.
_APPLY_SEGMENTS = {"application", "applications", "apply", "apply-now", "application_form"}


def _has_identity_query(query: str) -> bool:
    """True when the query carries a non-tracking param — i.e. the posting id."""
    return any(_keep_param(k) for k, _ in parse_qsl(query, keep_blank_values=False))


def _strip_apply_path(path: str, query: str = "") -> str:
    """Drop trailing apply-form segments from a URL path (`/a/b/application` -> `/a/b`).

    Loops so stacked suffixes (`/apply/application`) collapse too. Two guards stop it
    eating real postings: an identity-bearing query means the path is not the id
    (`/apply?gh_jid=51946` IS the posting on Greenhouse-embedded career sites), and
    the last remaining segment is never removed, so a board whose postings live under
    a bare `/apply` can't collapse to the site root."""
    if _has_identity_query(query):
        return path
    segments = [s for s in path.split("/") if s]
    while len(segments) > 1 and segments[-1].lower() in _APPLY_SEGMENTS:
        segments.pop()
    return "/" + "/".join(segments) if segments else "/"


def canonical_url(url: str) -> str:
    """The URL to actually store and fetch: the input with its apply-form suffix
    removed and a scheme added if missing. Unlike `url_normalize` this preserves host
    case, www and the query string — it is a real fetchable URL, not a dedupe key."""
    raw = url.strip()
    parts = urlsplit(raw if "://" in raw else "https://" + raw)
    return urlunsplit(parts._replace(path=_strip_apply_path(parts.path, parts.query)))


def url_normalize(url: str) -> str:
    """Canonicalize a job URL for dedupe: force https, drop www, lowercase host,
    strip trailing slash, drop the apply-form suffix and tracking params (keeping
    identity-bearing ones), and keep an id-like fragment.

    The fragment is kept only when it contains a digit: SPA job boards route the
    posting id through it (`/open-positions#/6676421003`), while plain anchors
    (`#apply`, `#content`) are page furniture and would otherwise split one posting
    into several rows."""
    raw = url.strip()
    parts = urlsplit(raw if "://" in raw else "https://" + raw)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = _strip_apply_path(parts.path.rstrip("/") or "/", parts.query)
    # sorted so param order in the source URL can't produce two keys for one posting
    kept = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if _keep_param(k)
    )
    query = urlencode(kept)
    fragment = parts.fragment if any(c.isdigit() for c in parts.fragment) else ""
    return urlunsplit(("https", host, path, query, fragment))


def job_id_for_url(url: str) -> str:
    """Deterministic UUID5 from the normalized URL (stable id without a DB round-trip)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, url_normalize(url)))

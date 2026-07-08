"""Typed reads/writes over the schema. Thin SQL helpers shared by all services;
no business logic (rendering, tailoring, navigation) lives here."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from common.queue import job_id_for_url, url_normalize


@dataclass
class ProfileData:
    """Everything needed to render an applicant's form-fill + resume context."""

    user: dict[str, Any]
    profile: dict[str, Any]
    work_experiences: list[dict[str, Any]] = field(default_factory=list)
    education: list[dict[str, Any]] = field(default_factory=list)
    skill_groups: list[dict[str, Any]] = field(default_factory=list)
    references: list[dict[str, Any]] = field(default_factory=list)
    login: dict[str, Any] | None = None


def fetch_profile(conn: psycopg.Connection, user_id: str) -> ProfileData:
    """Load a user's full profile bundle from Postgres."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
        user = cur.fetchone()
        if not user:
            raise LookupError(f"no user {user_id}")
        cur.execute("SELECT * FROM application_profiles WHERE user_id = %s", (user_id,))
        profile = cur.fetchone() or {}
        cur.execute(
            "SELECT * FROM work_experiences WHERE user_id = %s ORDER BY display_order, from_date DESC NULLS LAST",
            (user_id,),
        )
        work = cur.fetchall()
        cur.execute(
            "SELECT * FROM education WHERE user_id = %s ORDER BY display_order, from_date DESC NULLS LAST",
            (user_id,),
        )
        edu = cur.fetchall()
        cur.execute(
            "SELECT * FROM skill_groups WHERE user_id = %s ORDER BY display_order", (user_id,)
        )
        skills = cur.fetchall()
        cur.execute(
            "SELECT * FROM professional_references WHERE user_id = %s ORDER BY display_order",
            (user_id,),
        )
        refs = cur.fetchall()
        cur.execute("SELECT * FROM portal_credentials WHERE user_id = %s", (user_id,))
        login = cur.fetchone()
    return ProfileData(user, profile, work, edu, skills, refs, login)


def active_user_ids(conn: psycopg.Connection) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT user_id FROM users WHERE is_active ORDER BY created_at")
        return [r["user_id"] for r in cur.fetchall()]


def get_or_create_job(
    conn: psycopg.Connection,
    url: str,
    title: str | None = None,
    source: str = "search",
) -> str:
    """Insert a job (dedup on normalized URL) and return its job_id. New rows get
    the deterministic uuid5-of-URL id so RLS-bound callers can compute it without
    reading rows they don't own (see jobs policies in db/init.sql)."""
    norm = url_normalize(url)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO jobs (job_id, url, url_normalized, title, source) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (url_normalized) DO UPDATE SET updated_at = now() "
            "RETURNING job_id",
            (job_id_for_url(url), url, norm, title, source),
        )
        job_id = cur.fetchone()["job_id"]
    conn.commit()
    return job_id


def add_job_for_user(
    conn: psycopg.Connection,
    user_id: str,
    url: str,
    title: str | None = None,
    source: str = "manual",
    *,
    commit: bool = True,
) -> str:
    """Insert-or-noop the global job row, then link ownership in user_jobs.

    Works on both access planes: admin connections see the existing row and reuse
    its id; an RLS-bound connection that cannot see another owner's row falls back
    to the deterministic uuid5 id (identical for all rows created by this code)."""
    norm = url_normalize(url)
    job_id = job_id_for_url(url)
    # Plain INSERT + savepoint, NOT ON CONFLICT: under RLS, ON CONFLICT adds the
    # SELECT policy's USING clause as a check on the new row (anti-leak guard),
    # which always fails here — the ownership link doesn't exist yet.
    try:
        with conn.transaction():
            conn.execute(
                "INSERT INTO jobs (job_id, url, url_normalized, title, source) "
                "VALUES (%s, %s, %s, %s, %s)",
                (job_id, url, norm, title, source),
            )
    except psycopg.errors.UniqueViolation:
        pass  # someone owns it already — link below
    with conn.cursor() as cur:
        # prefer the stored id — pre-deterministic rows may have a random uuid
        cur.execute("SELECT job_id FROM jobs WHERE url_normalized = %s", (norm,))
        row = cur.fetchone()
        if row:
            job_id = row["job_id"]
        cur.execute(
            "INSERT INTO user_jobs (user_id, job_id, source) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id, job_id) DO NOTHING",
            (user_id, job_id, source),
        )
    if commit:  # False when the caller manages the transaction (API user_conn)
        conn.commit()
    return job_id


def job_owner_ids(conn: psycopg.Connection, job_id: str) -> list[str]:
    """Users who added this job (tailoring fan-out targets)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT user_id FROM user_jobs WHERE job_id = %s ORDER BY added_at",
            (job_id,),
        )
        return [r["user_id"] for r in cur.fetchall()]


def list_jobs_for_user(
    conn: psycopg.Connection,
    user_id: str,
    status: str | None = None,
    since: datetime | None = None,
) -> list[dict]:
    """Jobs owned by a user (via user_jobs), newest first, optionally by status
    and/or added on/after `since` (user_jobs.added_at — when *this user* added
    the job; jobs are globally deduped so jobs.created_at may be older).
    Each row carries the user's latest apply attempt as apply_status/apply_outcome."""
    q = (
        "SELECT j.*, ap.status AS apply_status, ap.outcome AS apply_outcome "
        "FROM jobs j JOIN user_jobs uj USING (job_id) "
        "LEFT JOIN LATERAL ("
        "  SELECT a.status, a.outcome FROM applications a "
        "  WHERE a.job_id = j.job_id AND a.user_id = uj.user_id "
        "  ORDER BY a.started_at DESC LIMIT 1"
        ") ap ON true "
        "WHERE uj.user_id = %s"
    )
    params: list[Any] = [user_id]
    if status:
        q += " AND j.status = %s"
        params.append(status)
    if since is not None:
        q += " AND uj.added_at >= %s"
        params.append(since)
    q += " ORDER BY j.created_at DESC"
    with conn.cursor() as cur:
        cur.execute(q, params)
        return cur.fetchall()


def list_applications(
    conn: psycopg.Connection, user_id: str, job_id: str | None = None
) -> list[dict]:
    """A user's apply attempts (optionally one job's), newest first, with job url/title."""
    q = (
        "SELECT a.*, j.url, j.title FROM applications a JOIN jobs j USING (job_id) "
        "WHERE a.user_id = %s"
    )
    params: list[Any] = [user_id]
    if job_id:
        q += " AND a.job_id = %s"
        params.append(job_id)
    q += " ORDER BY a.started_at DESC"
    with conn.cursor() as cur:
        cur.execute(q, params)
        return cur.fetchall()


def get_base_asset(
    conn: psycopg.Connection, user_id: str, variant: str | None = None
) -> dict | None:
    """Newest ready base resume asset for a user (optionally a specific variant)."""
    with conn.cursor() as cur:
        if variant:
            cur.execute(
                "SELECT * FROM resume_assets WHERE user_id = %s AND kind = 'base' "
                "AND variant = %s AND status = 'ready' ORDER BY created_at DESC LIMIT 1",
                (user_id, variant),
            )
        else:
            cur.execute(
                "SELECT * FROM resume_assets WHERE user_id = %s AND kind = 'base' "
                "AND status = 'ready' ORDER BY created_at DESC LIMIT 1",
                (user_id,),
            )
        return cur.fetchone()


def get_resume_asset(conn: psycopg.Connection, asset_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM resume_assets WHERE id = %s", (asset_id,))
        return cur.fetchone()


def insert_resume_asset(
    conn: psycopg.Connection,
    user_id: str,
    kind: str,
    s3_key: str,
    *,
    variant: str | None = None,
    job_id: str | None = None,
    content_type: str | None = None,
    status: str = "ready",
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO resume_assets (user_id, kind, variant, job_id, s3_key, content_type, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (user_id, kind, variant, job_id, s3_key, content_type, status),
        )
        asset_id = cur.fetchone()["id"]
    conn.commit()
    return asset_id


def enqueue_tailoring(
    conn: psycopg.Connection,
    user_id: str,
    job_id: str,
    base_asset_id: str | None = None,
) -> str:
    """Enqueue a tailoring job (one per user×job). Returns the row id (existing on conflict)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tailored_resumes (user_id, job_id, base_asset_id) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id, job_id) DO UPDATE SET updated_at = now() RETURNING id",
            (user_id, job_id, base_asset_id),
        )
        row_id = cur.fetchone()["id"]
    conn.commit()
    return row_id


def lookup_tailored(conn: psycopg.Connection, user_id: str, job_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM tailored_resumes WHERE user_id = %s AND job_id = %s",
            (user_id, job_id),
        )
        return cur.fetchone()


def save_gmail_token(
    conn: psycopg.Connection,
    user_id: str,
    gmail_address: str | None,
    refresh_token: str,
    scopes: list[str] | None = None,
) -> None:
    """Upsert a user's Gmail OAuth grant. The refresh token is encrypted at rest
    (Fernet, key from AUTOAPPLY_ENC_KEY) — a DB dump alone cannot read inboxes."""
    from common import crypto

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO gmail_tokens (user_id, gmail_address, refresh_token, scopes) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET gmail_address = EXCLUDED.gmail_address, "
            "refresh_token = EXCLUDED.refresh_token, scopes = EXCLUDED.scopes, "
            "updated_at = now()",
            (user_id, gmail_address, crypto.encrypt(refresh_token), scopes or []),
        )
    conn.commit()


def get_gmail_token(conn: psycopg.Connection, user_id: str) -> dict | None:
    """Fetch a user's Gmail grant with the refresh token decrypted for use."""
    from common import crypto

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM gmail_tokens WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
    if row:
        row["refresh_token"] = crypto.decrypt(row["refresh_token"])
    return row


# --------------------------- auth (API bootstrap) ---------------------------
# These run on the API's admin-role pool: session lookup happens before we know
# the user, so RLS can't scope it. sessions/audit_log are deny-all for the
# RLS-bound role (see db/init.sql).

SESSION_TTL = timedelta(days=30)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn: psycopg.Connection, user_id: str) -> str:
    """Mint a session; returns the raw token for the cookie. Only sha256(token)
    is stored, so the DB alone cannot impersonate anyone."""
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + SESSION_TTL
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sessions (user_id, token_hash, expires_at) VALUES (%s, %s, %s)",
            (user_id, _token_hash(token), expires),
        )
    conn.commit()
    return token


def get_session(conn: psycopg.Connection, token: str) -> dict | None:
    """The session's user row (plus session id) if the token is live, else None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT s.id AS session_id, u.* FROM sessions s "
            "JOIN users u USING (user_id) "
            "WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > now()",
            (_token_hash(token),),
        )
        return cur.fetchone()


def revoke_session(conn: psycopg.Connection, token: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE sessions SET revoked_at = now() "
            "WHERE token_hash = %s AND revoked_at IS NULL",
            (_token_hash(token),),
        )
    conn.commit()


def upsert_google_user(
    conn: psycopg.Connection, google_sub: str, email: str, full_name: str | None = None
) -> dict:
    """Find-or-create a user from verified Google id_token claims. Keyed on the
    stable `sub`; a first login attaches the sub to a pre-seeded row with the
    same email instead of duplicating the account."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE google_sub = %s", (google_sub,))
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO users (email, full_name, google_sub) VALUES (%s, %s, %s) "
                "ON CONFLICT (email) DO UPDATE SET google_sub = EXCLUDED.google_sub, "
                "full_name = COALESCE(users.full_name, EXCLUDED.full_name), "
                "updated_at = now() RETURNING *",
                (email, full_name, google_sub),
            )
            row = cur.fetchone()
        elif row["email"] != email:  # user changed their Google email
            cur.execute(
                "UPDATE users SET email = %s, updated_at = now() "
                "WHERE user_id = %s RETURNING *",
                (email, row["user_id"]),
            )
            row = cur.fetchone()
    conn.commit()
    return row


def audit(
    conn: psycopg.Connection,
    user_id: str | None,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    detail: dict | None = None,
) -> None:
    """Append one audit row (API mutations). Best-effort by design — callers
    decide whether a failure should abort the request."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO audit_log (user_id, action, resource_type, resource_id, detail) "
            "VALUES (%s, %s, %s, %s, %s)",
            (user_id, action, resource_type, resource_id,
             Jsonb(detail) if detail is not None else None),
        )
    conn.commit()


def start_application(
    conn: psycopg.Connection,
    user_id: str,
    job_id: str,
    *,
    run_id: str | None = None,
    tailored_resume_id: str | None = None,
) -> str:
    """Record the start of an apply run. Status convention (column is free text):
    'running' -> 'finished' (agent stopped for human review) | 'failed' (outcome
    holds the truncated error). started_at defaults to now() in the schema."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO applications (user_id, job_id, tailored_resume_id, run_id, status) "
            "VALUES (%s, %s, %s, %s, 'running') RETURNING id",
            (user_id, job_id, tailored_resume_id, run_id),
        )
        app_id = cur.fetchone()["id"]
    conn.commit()
    return app_id


def finish_application(
    conn: psycopg.Connection,
    application_id: str,
    status: str,
    outcome: str | None = None,
) -> None:
    """Set an application's terminal status ('finished' | 'failed') and finished_at."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE applications SET status = %s, outcome = %s, finished_at = now() "
            "WHERE id = %s",
            (status, outcome, application_id),
        )
    conn.commit()

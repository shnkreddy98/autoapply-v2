"""Typed reads/writes over the schema. Thin SQL helpers shared by all services;
no business logic (rendering, tailoring, navigation) lives here."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import psycopg

from common.queue import url_normalize


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
    """Insert a job (dedup on normalized URL) and return its job_id."""
    norm = url_normalize(url)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO jobs (url, url_normalized, title, source) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (url_normalized) DO UPDATE SET updated_at = now() "
            "RETURNING job_id",
            (url, norm, title, source),
        )
        job_id = cur.fetchone()["job_id"]
    conn.commit()
    return job_id


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

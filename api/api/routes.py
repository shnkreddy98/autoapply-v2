"""v1 endpoints. Every user-facing query runs on `db.user_conn(user_id)` — the
RLS-bound role — so isolation holds even if a filter is forgotten. Response
models list fields explicitly: secrets (gmail refresh_token, portal
login_password) have no model field and can never serialize."""

from __future__ import annotations

import functools
from datetime import date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api import db
from api.auth import get_current_user, require_admin
from common import repo
from common import storage as storagemod

router = APIRouter()


@functools.lru_cache(maxsize=1)
def _storage():
    return storagemod.from_env()


# ---------------------------------------------------------------------------
# models — explicit fields only


class Me(BaseModel):
    user_id: UUID
    email: str
    full_name: str | None = None
    role: str


class JobOut(BaseModel):
    job_id: UUID
    url: str
    title: str | None = None
    company: str | None = None
    status: str
    source: str
    created_at: datetime


class JobsIn(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=100)


class TailoringOut(BaseModel):
    status: str
    resume_score: int | None = None
    new_resume_score: int | None = None
    job_match_summary: str | None = None
    error: str | None = None


class ResumeLink(BaseModel):
    url: str
    expires_in: int


class ApplicationOut(BaseModel):
    id: UUID
    job_id: UUID
    run_id: str | None = None
    status: str | None = None
    outcome: str | None = None
    started_at: datetime
    finished_at: datetime | None = None


class GmailStatus(BaseModel):
    connected: bool
    gmail_address: str | None = None
    updated_at: datetime | None = None


# application_profiles columns a user may read/write. user_id/updated_at are
# managed and simply not listed (dropped on output, rejected on input).
class Profile(BaseModel):
    street_address: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    date_of_birth: date | None = None
    work_eligible_us: bool | None = None
    visa_sponsorship: bool | None = None
    security_clearance: bool | None = None
    available_start_date: date | None = None
    desired_salary: str | None = None
    employment_type: str | None = None
    years_of_experience: int | None = None
    willing_relocate: bool | None = None
    willing_travel: bool | None = None
    travel_percentage: str | None = None
    current_employee: bool | None = None
    ever_terminated: bool | None = None
    termination_explanation: str | None = None
    gender: str | None = None
    race_ethnicity: str | None = None
    veteran_status: str | None = None
    disability_status: str | None = None
    cert_accuracy: bool | None = None
    cert_dismissal: bool | None = None
    cert_background_check: bool | None = None
    cert_drug_testing: bool | None = None
    cert_at_will: bool | None = None
    cert_job_description: bool | None = None
    cert_privacy_notice: bool | None = None
    cert_data_processing: bool | None = None
    electronic_signature: str | None = None
    signature_date: date | None = None


class ProfileIn(Profile, extra="forbid"):
    """PUT body: unknown/managed fields are rejected, not silently dropped."""


class AdminUser(BaseModel):
    user_id: UUID
    email: str
    full_name: str | None = None
    role: str
    is_active: bool
    created_at: datetime


class AuditOut(BaseModel):
    id: int
    user_id: UUID | None = None
    action: str
    resource_type: str | None = None
    resource_id: str | None = None
    detail: dict | None = None
    at: datetime


# ---------------------------------------------------------------------------
# me / profile


@router.get("/me", response_model=Me)
def me(user: dict = Depends(get_current_user)):
    return user


@router.get("/profile", response_model=Profile)
def get_profile(user: dict = Depends(get_current_user)):
    with db.user_conn(user["user_id"]) as conn:
        row = conn.execute(
            "SELECT * FROM application_profiles WHERE user_id = %s",
            (user["user_id"],),
        ).fetchone()
    return row or Profile()


@router.put("/profile", response_model=Profile)
def put_profile(profile: ProfileIn, user: dict = Depends(get_current_user)):
    data = profile.model_dump(exclude_unset=True)
    cols = list(data)
    with db.user_conn(user["user_id"]) as conn:
        if data:
            updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols)
            conn.execute(
                f"INSERT INTO application_profiles (user_id, {', '.join(cols)}) "  # noqa: S608 — cols from the Profile model, not the request
                f"VALUES (%s, {', '.join('%s' for _ in cols)}) "
                f"ON CONFLICT (user_id) DO UPDATE SET {updates}, updated_at = now()",
                (user["user_id"], *data.values()),
            )
        row = conn.execute(
            "SELECT * FROM application_profiles WHERE user_id = %s",
            (user["user_id"],),
        ).fetchone()
    return row or Profile()


# ---------------------------------------------------------------------------
# jobs


@router.post("/jobs", response_model=list[JobOut])
def add_jobs(body: JobsIn, user: dict = Depends(get_current_user)):
    with db.user_conn(user["user_id"]) as conn:
        ids = [
            repo.add_job_for_user(conn, user["user_id"], url, commit=False)
            for url in body.urls
        ]
        rows = conn.execute(
            "SELECT j.* FROM jobs j JOIN user_jobs uj USING (job_id) "
            "WHERE uj.user_id = %s AND j.job_id = ANY(%s) ORDER BY j.created_at",
            (user["user_id"], ids),
        ).fetchall()
    return rows


@router.get("/jobs", response_model=list[JobOut])
def list_jobs(status: str | None = None, user: dict = Depends(get_current_user)):
    with db.user_conn(user["user_id"]) as conn:
        return repo.list_jobs_for_user(conn, user["user_id"], status)


def _owned_job(conn, user_id, job_id: UUID) -> dict:
    row = conn.execute(
        "SELECT j.* FROM jobs j JOIN user_jobs uj USING (job_id) "
        "WHERE uj.user_id = %s AND j.job_id = %s",
        (user_id, job_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "no such job")
    return row


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: UUID, user: dict = Depends(get_current_user)):
    with db.user_conn(user["user_id"]) as conn:
        return _owned_job(conn, user["user_id"], job_id)


@router.get("/jobs/{job_id}/tailoring", response_model=TailoringOut)
def get_tailoring(job_id: UUID, user: dict = Depends(get_current_user)):
    with db.user_conn(user["user_id"]) as conn:
        _owned_job(conn, user["user_id"], job_id)
        row = repo.lookup_tailored(conn, user["user_id"], job_id)
    if row is None:
        raise HTTPException(404, "tailoring not enqueued")
    return row


@router.get("/jobs/{job_id}/resume", response_model=ResumeLink)
def get_resume(job_id: UUID, user: dict = Depends(get_current_user)):
    with db.user_conn(user["user_id"]) as conn:
        _owned_job(conn, user["user_id"], job_id)
        t = repo.lookup_tailored(conn, user["user_id"], job_id)
        if not t or t["status"] != "ready" or not t["tailored_asset_id"]:
            raise HTTPException(404, "no tailored resume ready for this job")
        asset = repo.get_resume_asset(conn, t["tailored_asset_id"])
    expires = 300
    return {"url": _storage().presigned_get(asset["s3_key"], expires), "expires_in": expires}


# ---------------------------------------------------------------------------
# applications / gmail


@router.get("/applications", response_model=list[ApplicationOut])
def list_applications(user: dict = Depends(get_current_user)):
    with db.user_conn(user["user_id"]) as conn:
        return conn.execute(
            "SELECT * FROM applications WHERE user_id = %s ORDER BY started_at DESC",
            (user["user_id"],),
        ).fetchall()


@router.get("/gmail/status", response_model=GmailStatus)
def gmail_status(user: dict = Depends(get_current_user)):
    with db.user_conn(user["user_id"]) as conn:
        row = conn.execute(
            "SELECT gmail_address, updated_at FROM gmail_tokens WHERE user_id = %s",
            (user["user_id"],),
        ).fetchone()
    if row is None:
        return {"connected": False}
    return {"connected": True, **row}


# ---------------------------------------------------------------------------
# admin


@router.get("/admin/users", response_model=list[AdminUser])
def admin_users(_: dict = Depends(require_admin)):
    with db.admin_conn() as conn:
        return conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()


@router.get("/admin/audit", response_model=list[AuditOut])
def admin_audit(limit: int = 200, _: dict = Depends(require_admin)):
    with db.admin_conn() as conn:
        return conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT %s", (min(limit, 1000),)
        ).fetchall()

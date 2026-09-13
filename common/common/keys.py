"""Deterministic MinIO/S3 object keys. Everything keyed by user_id UUID, never email.

Single bucket (env S3_BUCKET, default `autoapply`) with these prefixes.
"""

from __future__ import annotations


def base_resume_key(user_id: str, label: str, variant: str, ext: str) -> str:
    """Object key for a base resume: one folder per role, one object per format, so
    `resume add --replace` overwrites in place. Bases uploaded before every base had
    a role keep their old keys — the row's s3_key is the source of truth, never
    this function."""
    return f"users/{user_id}/resumes/base/{label}/{variant}.{ext}"


def tailored_resume_key(user_id: str, job_id: str, ext: str = "pdf") -> str:
    return f"users/{user_id}/resumes/tailored/{job_id}.{ext}"


def jd_key(job_id: str) -> str:
    return f"jobs/{job_id}/jd.md"

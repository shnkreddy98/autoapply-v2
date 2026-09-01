"""Deterministic MinIO/S3 object keys. Everything keyed by user_id UUID, never email.

Single bucket (env S3_BUCKET, default `autoapply`) with these prefixes.
"""

from __future__ import annotations


def base_resume_key(
    user_id: str,
    variant: str | None = None,
    ext: str = "docx",
    label: str | None = None,
) -> str:
    """Object key for a base resume. `variant` is the file format ('pdf'/'docx').
    `label` is an optional role/track ('swe','de',...); when set it becomes a
    subfolder so two labels' same-format bases don't collide. label=None keeps the
    original path, so existing default bases are unaffected."""
    name = variant or "default"
    if label:
        return f"users/{user_id}/resumes/base/{label}/{name}.{ext}"
    return f"users/{user_id}/resumes/base/{name}.{ext}"


def tailored_resume_key(user_id: str, job_id: str, ext: str = "pdf") -> str:
    return f"users/{user_id}/resumes/tailored/{job_id}.{ext}"


def jd_key(job_id: str) -> str:
    return f"jobs/{job_id}/jd.md"

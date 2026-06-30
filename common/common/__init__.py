"""Shared infra for autoapply-v2 services (search, auto-browse, ai-assist).

Holds only cross-cutting plumbing — Postgres pool, MinIO/S3 storage, and the
Postgres job-queue helpers — never service business logic. Services import from
here but never from each other.
"""

from common import db, keys, queue, repo, storage

__all__ = ["db", "storage", "queue", "repo", "keys"]

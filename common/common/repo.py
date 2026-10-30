"""Typed reads/writes over the schema. Thin SQL helpers shared by all services;
no business logic (rendering, tailoring, navigation) lives here."""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from common.queue import canonical_url, job_id_for_url, url_normalize


@dataclass
class ProfileData:
    """Everything needed to fill an application for one role.

    `user`/`profile`/`references`/`login` are the user's own facts and are the same
    for every role. `label` names the role resume the rest came from: summary, work
    history, education and skills are parsed per role (resume_assets.parsed), because
    role resumes legitimately differ and a merged copy has to pick one of them."""

    user: dict[str, Any]
    profile: dict[str, Any]
    label: str | None = None
    summary: str | None = None
    work_experiences: list[dict[str, Any]] = field(default_factory=list)
    education: list[dict[str, Any]] = field(default_factory=list)
    skill_groups: list[dict[str, Any]] = field(default_factory=list)
    references: list[dict[str, Any]] = field(default_factory=list)
    login: dict[str, Any] | None = None


def fetch_profile(
    conn: psycopg.Connection, user_id: str, label: str | None = None
) -> ProfileData:
    """Load a user's facts plus the parsed content of one role resume.

    `label=None` means the user's default role. A role with no parsed resume (not
    uploaded, or uploaded before parsing existed) yields empty resume sections rather
    than an error: the facts are still worth filling in."""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
        user = cur.fetchone()
        if not user:
            raise LookupError(f"no user {user_id}")
        cur.execute("SELECT * FROM application_profiles WHERE user_id = %s", (user_id,))
        profile = cur.fetchone() or {}
        cur.execute(
            "SELECT * FROM professional_references WHERE user_id = %s ORDER BY display_order",
            (user_id,),
        )
        refs = cur.fetchall()
        cur.execute("SELECT * FROM portal_credentials WHERE user_id = %s", (user_id,))
        login = cur.fetchone()
    base = get_base_asset(conn, user_id, variant="docx", label=label)
    parsed = (base or {}).get("parsed") or {}
    return ProfileData(
        user=user,
        profile=profile,
        label=base["label"] if base else label,
        summary=parsed.get("summary"),
        work_experiences=parsed.get("work_experiences") or [],
        education=parsed.get("education") or [],
        skill_groups=parsed.get("skill_groups") or [],
        references=refs,
        login=login,
    )


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
    # store the canonical form: an apply-form URL has no JD on it, so capturing the
    # raw input would fail (see queue.canonical_url)
    fetch_url = canonical_url(url)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO jobs (job_id, url, url_normalized, title, source) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (url_normalized) DO UPDATE SET updated_at = now() "
            "RETURNING job_id",
            (job_id_for_url(url), fetch_url, norm, title, source),
        )
        job_id = cur.fetchone()["job_id"]
    conn.commit()
    return job_id


def _backfill_job_text(
    conn: psycopg.Connection, job_id: str, column: str, value: str | None
) -> None:
    """Fill one blank text column on `jobs`. Existing values win — a second user's
    tailoring must not overwrite what the first one already wrote.

    Workers connect as the table owner, which is why this UPDATE is allowed: `jobs`
    has no UPDATE policy (see db/init.sql), so the RLS-bound API role cannot do it.
    `column` is always a literal from this module, never caller input."""
    if not value or not value.strip():
        return
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE jobs SET {column} = %s, updated_at = now() "
            f"WHERE job_id = %s AND ({column} IS NULL OR {column} = '')",
            (value.strip(), job_id),
        )
    conn.commit()


_SCAN_COLUMNS = ("work_auth", "work_auth_note", "salary", "salary_min", "salary_max", "salary_period")


def set_job_scan(conn: psycopg.Connection, job_id: str, scan: Any) -> None:
    """Store what common.jdscan read out of the job's JD. Overwrites: the values are
    a pure function of the JD text, so a rescan after a recapture or a pattern fix
    must replace the old reading, including clearing a limit that no longer fires."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET "
            + ", ".join(f"{c} = %s" for c in _SCAN_COLUMNS)
            + ", updated_at = now() WHERE job_id = %s",
            (*(getattr(scan, c) for c in _SCAN_COLUMNS), job_id),
        )
    conn.commit()


def needs_sponsorship(conn: psycopg.Connection, user_id: str) -> bool:
    """application_profiles.visa_sponsorship: "need visa sponsorship (now or
    future)?" from `profile init`. True for F-1 OPT / STEM OPT as well as H-1B:
    authorized to work today is not enough when the JD rules out future sponsorship."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT visa_sponsorship FROM application_profiles WHERE user_id = %s",
            (user_id,),
        )
        row = cur.fetchone()
    return bool(row and row["visa_sponsorship"])


def get_job_salary_band(conn: psycopg.Connection, job_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT salary_min, salary_max, salary_period FROM jobs WHERE job_id = %s",
            (job_id,),
        )
        return cur.fetchone()


def set_job_company(conn: psycopg.Connection, job_id: str, company: str | None) -> None:
    """Fill in `jobs.company` if it is still blank. The capture path never learns the
    employer name (it only has the URL); the tailor agent reads it out of the JD, so
    the first tailoring of a job backfills it for everyone."""
    _backfill_job_text(conn, job_id, "company", company)


def set_job_title(conn: psycopg.Connection, job_id: str, title: str | None) -> None:
    """Fill in `jobs.title` if it is still blank. Only the `search` discovery path
    supplies a title up front; jobs added by hand have none, and JD capture cannot
    supply one either (the proxy returns visible text, not the page title). The
    tailor agent already reads the role out of the JD, so the first tailoring
    backfills it for free — same deal as `set_job_company`."""
    _backfill_job_text(conn, job_id, "title", title)


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
    fetch_url = canonical_url(url)  # apply-form suffix stripped — see queue.canonical_url
    # Plain INSERT + savepoint, NOT ON CONFLICT: under RLS, ON CONFLICT adds the
    # SELECT policy's USING clause as a check on the new row (anti-leak guard),
    # which always fails here — the ownership link doesn't exist yet.
    try:
        with conn.transaction():
            conn.execute(
                "INSERT INTO jobs (job_id, url, url_normalized, title, source) "
                "VALUES (%s, %s, %s, %s, %s)",
                (job_id, fetch_url, norm, title, source),
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


def remove_job_for_user(conn: psycopg.Connection, user_id: str, job_id: str) -> str:
    """Drop a user's link to a job and everything that link produced.

    Jobs are globally deduped, so the shared `jobs` row is only deleted once nobody
    owns it — otherwise another user's pipeline would vanish with it. Returns
    'deleted' (job row gone), 'unlinked' (other owners remain) or 'absent' (this
    user never owned it).

    The user's own tailorings/applications are always removed, so re-adding the URL
    starts clean. Their MinIO objects are left behind: the assets are addressed by
    key and simply become unreferenced."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM user_jobs WHERE user_id = %s AND job_id = %s",
            (user_id, job_id),
        )
        if cur.rowcount == 0:
            # not this user's job — leave the shared row and anyone else's rows alone
            conn.rollback()
            return "absent"
        # per-user rows are not covered by the jobs cascade while the job survives
        cur.execute(
            "DELETE FROM applications WHERE user_id = %s AND job_id = %s",
            (user_id, job_id),
        )
        cur.execute(
            "DELETE FROM tailored_resumes WHERE user_id = %s AND job_id = %s",
            (user_id, job_id),
        )
        cur.execute(
            "DELETE FROM resume_assets WHERE user_id = %s AND job_id = %s",
            (user_id, job_id),
        )
        cur.execute(
            "DELETE FROM saved_answers WHERE user_id = %s AND job_id = %s",
            (user_id, job_id),
        )
        cur.execute("SELECT 1 FROM user_jobs WHERE job_id = %s LIMIT 1", (job_id,))
        if cur.fetchone() is None:
            cur.execute("DELETE FROM jobs WHERE job_id = %s", (job_id,))
            outcome = "deleted"
        else:
            outcome = "unlinked"
    conn.commit()
    return outcome


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
    conn: psycopg.Connection,
    user_id: str,
    variant: str | None = None,
    label: str | None = None,
) -> dict | None:
    """Newest ready base resume asset for a user.

    `variant` filters by file format ('pdf'/'docx'). `label` selects a role base;
    `label=None` means the user's default role (users.default_resume_label), never
    "any role" — so the default path cannot pick up whichever role was added last.
    None when the user has no default role or no base for it."""
    if label is None:
        label = default_resume_label(conn, user_id)
        if label is None:
            return None
    clauses = ["user_id = %s", "kind = 'base'", "status = 'ready'", "label = %s"]
    params: list = [user_id, label]
    if variant:
        clauses.append("variant = %s")
        params.append(variant)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT * FROM resume_assets WHERE {' AND '.join(clauses)} "  # noqa: S608 — clauses are literals, values parameterized
            "ORDER BY created_at DESC LIMIT 1",
            params,
        )
        return cur.fetchone()


def list_base_assets(conn: psycopg.Connection, user_id: str) -> list[dict]:
    """All ready base assets for a user, newest first. For `autoapply resume bases`."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM resume_assets WHERE user_id = %s AND kind = 'base' "
            "AND status = 'ready' ORDER BY label, variant",
            (user_id,),
        )
        return cur.fetchall()


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
    label: str | None = None,
    job_id: str | None = None,
    content_type: str | None = None,
    status: str = "ready",
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO resume_assets (user_id, kind, variant, label, job_id, s3_key, content_type, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (user_id, kind, variant, label, job_id, s3_key, content_type, status),
        )
        asset_id = cur.fetchone()["id"]
    conn.commit()
    return asset_id


def default_resume_label(conn: psycopg.Connection, user_id: str) -> str | None:
    """The role used when a job has no tailoring of its own."""
    with conn.cursor() as cur:
        cur.execute("SELECT default_resume_label FROM users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
    return row["default_resume_label"] if row else None


def set_default_resume_label(conn: psycopg.Connection, user_id: str, label: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET default_resume_label = %s, updated_at = now() WHERE user_id = %s",
            (label, user_id),
        )
    conn.commit()


def set_resume_parsed(conn: psycopg.Connection, asset_id: str, parsed: dict) -> None:
    """Store the structured content parsed from a base resume (its role's profile)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE resume_assets SET parsed = %s, parsed_at = now() WHERE id = %s",
            (Jsonb(parsed), asset_id),
        )
    conn.commit()


def refresh_resume_asset(
    conn: psycopg.Connection, asset_id: str, content_type: str | None = None
) -> None:
    """Record that an existing asset's object was overwritten with new bytes.

    Used by `resume add --replace`: the storage key is derived from user+label+
    format, so re-uploading writes the same object and inserting a second row would
    just duplicate the first. Updating in place keeps one row per label and leaves
    every tailored_resumes.base_asset_id reference valid."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE resume_assets SET created_at = now(), status = 'ready', "
            "content_type = coalesce(%s, content_type) WHERE id = %s",
            (content_type, asset_id),
        )
    conn.commit()


def enqueue_tailoring(
    conn: psycopg.Connection,
    user_id: str,
    job_id: str,
    base_asset_id: str | None = None,
) -> str:
    """Enqueue a tailoring job (one per user×job). Returns the row id (existing on conflict).

    On conflict the base_asset_id is refreshed to the one passed in, so re-enqueuing a
    job with a different base (e.g. a role-labelled resume) actually switches the source.
    Status is left untouched here — callers that want a re-tailor reset it explicitly."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tailored_resumes (user_id, job_id, base_asset_id) VALUES (%s, %s, %s) "
            "ON CONFLICT (user_id, job_id) DO UPDATE SET "
            "base_asset_id = EXCLUDED.base_asset_id, updated_at = now() RETURNING id",
            (user_id, job_id, base_asset_id),
        )
        row_id = cur.fetchone()["id"]
    conn.commit()
    return row_id


def base_label_for_job(
    conn: psycopg.Connection, user_id: str, job_id: str
) -> str | None:
    """The label of the base resume this user's tailoring for `job_id` started from.

    Lets later steps (answering application questions) reuse whatever base the job
    was actually tailored with instead of guessing. Returns None when there is no
    tailoring row or it has no base attached — both mean "the default role"."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ra.label FROM tailored_resumes t "
            "JOIN resume_assets ra ON ra.id = t.base_asset_id "
            "WHERE t.user_id = %s AND t.job_id = %s",
            (user_id, job_id),
        )
        row = cur.fetchone()
    return row["label"] if row else None


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


def question_key(question_text: str) -> str:
    """Normalize a form question for `saved_answers` lookups: lowercase, every run of
    punctuation/whitespace collapsed to one space. Forms decorate the same question
    differently ("First Name *", "first name:") and those must share a row.
    Deliberately no fuzzier than that — near-identical questions can need opposite
    answers ("authorized to work" vs "require sponsorship")."""
    return " ".join(re.sub(r"[\W_]+", " ", question_text.lower()).split())


def get_saved_answer(
    conn: psycopg.Connection,
    user_id: str,
    question_text: str,
    *,
    job_id: str | None = None,
) -> dict | None:
    """The stored answer to a question. With `job_id`, only that job's per_job row
    (why-this-company answers are never replayed onto another job); without, only
    the reusable row. Which scope a question belongs to is the caller's decision."""
    with conn.cursor() as cur:
        if job_id:
            cur.execute(
                "SELECT * FROM saved_answers WHERE user_id = %s AND question_key = %s "
                "AND scope = 'per_job' AND job_id = %s",
                (user_id, question_key(question_text), job_id),
            )
        else:
            cur.execute(
                "SELECT * FROM saved_answers WHERE user_id = %s AND question_key = %s "
                "AND scope = 'reusable'",
                (user_id, question_key(question_text)),
            )
        return cur.fetchone()


def save_answer(
    conn: psycopg.Connection,
    user_id: str,
    question_text: str,
    answer: str,
    *,
    source: str,
    options: list[str] | None = None,
    job_id: str | None = None,
) -> str:
    """Upsert an answer; `job_id` makes it per_job, otherwise reusable. A later
    answer to the same question replaces the earlier one — a human correction must
    beat the LLM answer it corrects. Returns the row id."""
    key = question_key(question_text)
    scope = "per_job" if job_id else "reusable"
    conflict = (
        "(user_id, job_id, question_key) WHERE scope = 'per_job'"
        if job_id
        else "(user_id, question_key) WHERE scope = 'reusable'"
    )
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO saved_answers (user_id, question_key, question_text, answer, "
            "options, source, scope, job_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            f"ON CONFLICT {conflict} DO UPDATE SET question_text = EXCLUDED.question_text, "
            "answer = EXCLUDED.answer, options = EXCLUDED.options, source = EXCLUDED.source, "
            "updated_at = now() RETURNING id",
            (user_id, key, question_text, answer, options, source, scope, job_id),
        )
        answer_id = cur.fetchone()["id"]
    conn.commit()
    return answer_id


def mark_answer_used(conn: psycopg.Connection, answer_id: str) -> None:
    """Count a replay. Answers that keep getting used are the ones worth reviewing."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE saved_answers SET times_used = times_used + 1 WHERE id = %s",
            (answer_id,),
        )
    conn.commit()

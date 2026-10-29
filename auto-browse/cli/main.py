"""Unified `autoapply` CLI — one front door over the existing pipeline.

    autoapply jobs add [URL ...] [--file urls.txt]     # -> jobs table (source=manual)
    autoapply jobs list [--status S] [--today]         # company - title, id, status
    autoapply jobs rm JOB [JOB ...] [--user U]         # drop from your pipeline; re-addable
    autoapply jobs skip JOB [JOB ...] [--user U]       # not applying: batch apply passes over it
    autoapply tailor [JOB] [--label L] [--user U] [--retry-failed [--retry-since W]] [--force]
                                                       # jd-capture + drain tailor queue;
                                                       # JOB re-tailors one job; --label picks a role base;
                                                       # already-tailored jobs report as duplicates and are
                                                       # skipped unless --force
    autoapply retry [--all] [--user U]                 # reset today's failed JD fetches + recapture
    autoapply status [JOB] [--today | --date D] [--user U]
                                                       # pipeline state + scores; --date YYYY-MM-DD:
                                                       # jobs added on that local day
    autoapply resume show JOB [--user U] [--out P] [--open]
    autoapply resume add FILE --label L [--default] [--replace] [--user U]
                                                       # upload + parse a role resume (paid parse);
                                                       # FILE: .docx, or .odt/.md converted to .docx;
                                                       # warns when roles disagree on employer dates
    autoapply resume parse [LABEL ...] [--user U]      # re-parse stored role resumes (paid)
    autoapply resume default LABEL [--user U]          # role used when a job has no tailoring
    autoapply resume bases [--user U]                  # list role resumes
    autoapply profile init [--docx F --label L] [--email E] [--yes]
                                                       # your facts (contact, address, EEO, ...)
    autoapply profile show [--label L] [--user U]
    autoapply profile export [--label L] [--out F]     # facts + one role's resume, as JSON
    autoapply ask JOB "question" [--words N] [--label L] [--user U]
                                                       # answer -> stdout, count -> stderr;
                                                       # base resume follows the job's tailoring label
    autoapply cover JOB [--words N] [--label L] [--user U] [--out P] [--open]
                                                       # write + store a cover letter for the job (paid);
                                                       # letter -> stdout; export copies it next to the resume
    autoapply apply [JOB ...] [--all] [--today] [--user U] [--model M] [--autosubmit]
                    [--engine agent|det|det+agent] [--mcp URL]
                                                       # --all: batch every jd_ready job one by one;
                                                       # --today restricts to jobs added today;
                                                       # --engine det: deterministic filler, model
                                                       # only for fields it cannot answer;
                                                       # --engine det+agent: det first, the agent
                                                       # finishes the page det stopped on;
                                                       # --model jev-latest (det only): Jev picks
                                                       # options/profile values, `ask` writes
    autoapply continue JOB [--autosubmit] [--engine agent|det|det+agent]
                                                       # finish an interrupted run in the open browser

JOB accepts a full job_id UUID, an unambiguous id prefix (4+ characters), or the
job URL.

Command wrappers (cmd_*) own I/O: they open connections, print, and return exit
codes. The core functions take an injected connection so tests can drive them
against the throwaway jobs_test DB. Heavy imports (agent.loop, mcp) happen lazily
inside the commands that need them, so `jobs list` stays fast.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path

from dotenv import load_dotenv

from common import db, queue, repo
from common import storage as storagemod

HERE = Path(__file__).resolve().parent.parent  # auto-browse/
AI_ASSIST_DIR = HERE.parent / "ai-assist"

# Anchor to the package, not cwd — the CLI may be installed globally (uv tool)
# and invoked from anywhere. Existing shell env always wins (override=False).
load_dotenv(HERE / ".env")

_JOB_STATUSES = ("jd_pending", "jd_capturing", "jd_ready", "failed")


# ---------------------------------------------------------------------------
# shared helpers


def _connect():
    try:
        return db.connect()
    except Exception as exc:
        raise SystemExit(
            f"postgres unreachable: {exc}\n"
            "hint: start the shared infra from the repo root: docker compose up -d"
        )


def _storage():
    try:
        return storagemod.from_env()
    except Exception as exc:
        raise SystemExit(
            f"object storage unreachable: {exc}\n"
            "hint: start the shared infra from the repo root: docker compose up -d"
        )


def resolve_user(conn, user_flag: str | None) -> str:
    """--user accepts a user_id UUID or an email. Without the flag, exactly one
    active user must exist (the single-user default)."""
    if user_flag:
        try:
            uuid.UUID(user_flag)
            return user_flag
        except ValueError:
            pass
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM users WHERE email = %s", (user_flag,))
            row = cur.fetchone()
        if not row:
            raise SystemExit(f"no user with email {user_flag!r}")
        return row["user_id"]

    ids = repo.active_user_ids(conn)
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise SystemExit(
            "no active users — create yours: autoapply profile init [--docx resume.docx]"
        )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT user_id, email FROM users WHERE is_active ORDER BY created_at"
        )
        listing = "\n".join(f"  {r['user_id']}  {r['email']}" for r in cur.fetchall())
    raise SystemExit(f"multiple active users — pass --user <id|email>:\n{listing}")


def resolve_job(conn, ref: str) -> dict:
    """Resolve a JOB argument (full UUID, unambiguous id prefix, or URL) to a jobs row."""
    ref = str(ref)  # tolerate uuid.UUID from programmatic callers
    with conn.cursor() as cur:
        try:
            uuid.UUID(ref)
        except ValueError:
            pass
        else:
            cur.execute("SELECT * FROM jobs WHERE job_id = %s", (ref,))
            row = cur.fetchone()
            if not row:
                raise SystemExit(f"no job with id {ref}")
            return row

        if "://" in ref or "." in ref:  # URL-ish
            cur.execute(
                "SELECT * FROM jobs WHERE url_normalized = %s",
                (queue.url_normalize(ref),),
            )
            row = cur.fetchone()
            if not row:
                raise SystemExit(
                    f"no job with url {ref!r} (add it: autoapply jobs add {ref})"
                )
            return row

        if re.fullmatch(r"[0-9a-f-]{1,3}", ref.lower()):
            # The 4-char floor is deliberate: shorter prefixes match too much to be
            # a safe target for `jobs rm`. Say that, rather than "no job matching",
            # which reads like the job is missing.
            raise SystemExit(
                f"job prefix {ref!r} is too short — give at least 4 characters"
            )

        if re.fullmatch(r"[0-9a-f-]{4,}", ref.lower()):
            cur.execute(
                "SELECT * FROM jobs WHERE job_id::text LIKE %s", (ref.lower() + "%",)
            )
            rows = cur.fetchall()
            if len(rows) == 1:
                return rows[0]
            if rows:
                listing = "\n".join(f"  {r['job_id']}  {r['url']}" for r in rows)
                raise SystemExit(f"ambiguous job prefix {ref!r}:\n{listing}")
        raise SystemExit(f"no job matching {ref!r}")


def _id8(row_id) -> str:
    return str(row_id)[:8]


# ---------------------------------------------------------------------------
# jobs add / list


def parse_url_lines(text: str) -> list[str]:
    """One URL per line; blanks and #-comments ignored."""
    urls = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


def add_jobs(conn, user_id: str, urls: list[str]) -> list[dict]:
    """Insert jobs (source=manual) owned by user_id. Marks pre-existing URLs as
    duplicates (they may belong to other users too — jobs are globally deduped)."""
    out = []
    for url in urls:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT job_id, status, url FROM jobs WHERE url_normalized = %s",
                (queue.url_normalize(url),),
            )
            existing = cur.fetchone()
        job_id = repo.add_job_for_user(conn, user_id, url, source="manual")
        # Report the row as it stands in the DB, not the input. Dedup is on
        # url_normalized, so a URL pasted with tracking params can match a row that
        # was stored clean — echoing the input would misdescribe what we own.
        stored = existing["url"] if existing else queue.canonical_url(url)
        out.append(
            {
                "url": stored,
                "job_id": job_id,
                "status": existing["status"] if existing else "jd_pending",
                "duplicate": existing is not None,
                # the stored URL can differ from what was pasted — an apply-form
                # suffix trimmed off, or an existing row that was added clean — so
                # say so rather than letting the printed URL differ silently
                "trimmed": stored != url.strip(),
            }
        )
    return out


def cmd_jobs_add(args) -> int:
    urls = list(args.urls)
    if args.file:
        src = Path(args.file)
        if not src.is_file():  # a typo'd path is a user error, not a crash
            raise SystemExit(f"no such file: {src}")
        urls += parse_url_lines(src.read_text())
    if not urls:
        raise SystemExit("no URLs given (pass URLs or --file urls.txt)")
    with _connect() as conn:
        user_id = resolve_user(conn, args.user)
        results = add_jobs(conn, user_id, urls)
    for r in results:
        note = ""
        if r["duplicate"]:
            note = f"  (duplicate — already {r['status']})"
        elif r["trimmed"]:
            note = "  (trimmed to the posting page)"
        print(f"{_id8(r['job_id'])}  {r['status']:<12} {r['url']}{note}")
    return 0


def cmd_jobs_rm(args) -> int:
    """Drop jobs from this user's pipeline so a URL can be re-added and retried.

    Removes the user's tailorings and apply records for the job; the shared jobs row
    itself only goes when no other user owns it."""
    with _connect() as conn:
        user_id = resolve_user(conn, args.user)
        for ref in args.jobs:
            job = resolve_job(conn, ref)
            outcome = repo.remove_job_for_user(conn, user_id, job["job_id"])
            if outcome == "absent":
                print(f"{_id8(job['job_id'])}  not yours — nothing removed  {job['url']}")
            else:
                print(f"{_id8(job['job_id'])}  {outcome:<12} {job['url']}")
    return 0


def cmd_jobs_skip(args) -> int:
    """Mark jobs as deliberately not applied to. Recorded as an apply attempt with
    outcome "skipped": batch apply (--all / --today) passes over it, `apply JOB` by
    name still runs it, and the job and its tailoring stay (unlike `jobs rm`)."""
    with _connect() as conn:
        user_id = resolve_user(conn, args.user)
        for ref in args.jobs:
            job = resolve_job(conn, ref)
            repo.add_job_for_user(conn, user_id, job["url"], source="manual")  # as apply does
            app_id = repo.start_application(conn, user_id, job["job_id"], run_id=None)
            repo.finish_application(conn, app_id, "finished", outcome="skipped")
            print(f"{_id8(job['job_id'])}  skipped      {job['url']}")
    return 0


def _today_start() -> datetime:
    """Local midnight, tz-aware (job timestamps are UTC; 'today' means the user's day)."""
    return (
        datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    )


def _day_start(day: date) -> datetime:
    """Local midnight of `day`, tz-aware. The next day's start is the end bound, not
    +24h: a DST change makes a local day 23 or 25 hours long."""
    return datetime.combine(day, time()).astimezone()


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from None


def _job_label(row: dict) -> str:
    """Company first, then the title. The company is what you scan these listings
    for — you spot the employer, then hand the id to `autoapply ask` — and a title
    on its own does not say which posting a row is. Either may be missing: only
    `search` supplies a title up front and only a tailoring backfills one, so
    manually added jobs show the company alone until then."""
    return " - ".join(x for x in (row["company"], row["title"]) if x)


def _apply_marker(row: dict) -> str:
    """Human word for the latest apply attempt: applied / applied-before / skipped /
    unconfirmed / handed-off / apply-failed."""
    status, outcome = row.get("apply_status"), row.get("apply_outcome")
    if status is None:
        return ""
    if outcome == "submitted":
        return "applied"
    if outcome == "already applied":
        return "applied-before"  # the portal says so; no new application this run
    if outcome == "skipped":
        return "skipped"
    if outcome == "unconfirmed":
        return "unconfirmed"  # autosubmit run gave no clear SUBMITTED verdict
    if status == "failed":
        return "apply-failed"
    if status == "finished":
        return "handed-off"  # agent finished; submission unconfirmed
    return status


def list_jobs(
    conn, user_id: str, status: str | None, since: datetime | None = None
) -> list[dict]:
    return repo.list_jobs_for_user(conn, user_id, status, since=since)


def cmd_jobs_list(args) -> int:
    with _connect() as conn:
        user_id = resolve_user(conn, args.user)
        rows = list_jobs(
            conn, user_id, args.status, _today_start() if args.today else None
        )
    if not rows:
        scope = " added today" if args.today else ""
        print(
            f"no jobs{scope}" + (f" with status {args.status}" if args.status else "")
        )
        return 0
    for r in rows:
        title = _job_label(r)[:40]
        print(
            f"{_id8(r['job_id'])}  {r['status']:<12} {_apply_marker(r):<12} {title:<40} {r['url']}"
        )
    return 0


# ---------------------------------------------------------------------------
# tailor


def resolve_base(conn, user_id: str, label: str | None):
    """The docx base asset to tailor from. `label` picks a role base; None = the
    user's default role. Raises if a named label has no base uploaded."""
    base = repo.get_base_asset(conn, user_id, variant="docx", label=label)
    if base is None and label is not None:
        raise SystemExit(
            f"no resume labelled {label!r} for this user — upload one: "
            f"autoapply resume add <file.docx> --label {label}"
        )
    return base


def repoint_pending(conn, user_id: str, label: str) -> int:
    """Point this user's still-pending tailorings at `label`'s base resume.

    Only rows that are on the *default role's* base (or none at all) are moved:
    those were queued by JD capture or the apply loop, which don't know about
    --label. A row already on some other role's base is left alone, so running
    `--label de` can't hijack a swe batch that hasn't drained yet. Returns rows moved."""
    base = resolve_base(conn, user_id, label)
    if base is None:
        return 0
    default = repo.get_base_asset(conn, user_id, variant="docx")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE tailored_resumes SET base_asset_id = %s, updated_at = now() "
            "WHERE user_id = %s AND status = 'pending' AND base_asset_id IS DISTINCT FROM %s "
            "AND (base_asset_id IS NULL OR base_asset_id = %s)",
            (base["id"], user_id, base["id"], default["id"] if default else None),
        )
        n = cur.rowcount
    conn.commit()
    return n


def backfill_tailoring(conn, user_id: str, label: str | None = None) -> int:
    """Enqueue tailoring for owned jd_ready jobs this user has no row for (idempotent).
    `label` chooses which base resume feeds the tailoring (None = default base)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT j.job_id FROM jobs j "
            "JOIN user_jobs uj ON uj.job_id = j.job_id AND uj.user_id = %s "
            "WHERE j.status = 'jd_ready' AND NOT EXISTS "
            "(SELECT 1 FROM tailored_resumes t WHERE t.job_id = j.job_id AND t.user_id = %s)",
            (user_id, user_id),
        )
        job_ids = [r["job_id"] for r in cur.fetchall()]
    base = resolve_base(conn, user_id, label)  # tailoring edits the docx
    for job_id in job_ids:
        repo.enqueue_tailoring(conn, user_id, job_id, base["id"] if base else None)
    return len(job_ids)


def already_tailored(conn, user_id: str) -> list[dict]:
    """Jobs this user owns that already have a ready tailoring.

    `backfill_tailoring` skips these (they already have a row), so without this they
    are invisible — a URL re-added from a later search silently does nothing. Tailor
    reports them as duplicates instead."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT j.job_id, j.url, j.company, t.updated_at "
            "FROM tailored_resumes t "
            "JOIN jobs j USING (job_id) "
            "JOIN user_jobs uj ON uj.job_id = j.job_id AND uj.user_id = t.user_id "
            "WHERE t.user_id = %s AND t.status = 'ready' AND j.status = 'jd_ready' "
            "ORDER BY t.updated_at DESC",
            (user_id,),
        )
        return cur.fetchall()


def print_duplicates(rows: list[dict], limit: int = 10) -> None:
    """One line per already-tailored job, capped so a big backlog stays readable."""
    if not rows:
        return
    print(f"{len(rows)} duplicate(s) — already tailored, skipping (--force to re-tailor):")
    for row in rows[:limit]:
        name = row["company"] or company_hint(row["url"])
        when = row["updated_at"].astimezone().strftime("%Y-%m-%d")
        print(f"  {name[:28]:<28} tailored {when}  {row['url'][:60]}")
    if len(rows) > limit:
        print(f"  ... +{len(rows) - limit} more")


def company_hint(url: str) -> str:
    """Host as a stand-in name for jobs whose company the worker hasn't filled yet."""
    from urllib.parse import urlparse

    return urlparse(url).netloc or "?"


def requeue_duplicates(conn, user_id: str, rows: list[dict], label: str | None) -> int:
    """--force: send already-tailored jobs back through the worker from `label`'s base.
    Costs one model call per job, which is why it is never the default."""
    base = resolve_base(conn, user_id, label)
    for row in rows:
        repo.enqueue_tailoring(conn, user_id, row["job_id"], base["id"] if base else None)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE tailored_resumes SET status = 'pending', error = NULL "
            "WHERE user_id = %s AND job_id = ANY(%s)",
            (user_id, [r["job_id"] for r in rows]),
        )
        n = cur.rowcount
    conn.commit()
    return n


def reenqueue_one(conn, user_id: str, job_id: str, label: str | None) -> None:
    """Re-tailor a single job from the chosen base: point the row at that base and
    reset it to pending so the worker redoes it. enqueue_tailoring refreshes
    base_asset_id on conflict; we also flip status back to pending."""
    base = resolve_base(conn, user_id, label)
    repo.enqueue_tailoring(conn, user_id, job_id, base["id"] if base else None)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE tailored_resumes SET status = 'pending', error = NULL "
            "WHERE user_id = %s AND job_id = %s",
            (user_id, job_id),
        )
    conn.commit()


def tailoring_summary(conn, user_id: str, since: datetime | None = None,
                      until: datetime | None = None) -> dict:
    """Counts by status plus a per-job join, scoped to one user; `since` / `until`
    limit everything to jobs the user added in [since, until) (user_jobs.added_at)."""
    added = (" AND uj.added_at >= %s" if since is not None else "") + (" AND uj.added_at < %s" if until is not None else "")
    since_params = tuple(t for t in (since, until) if t is not None)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT j.status, count(*) AS n FROM jobs j "
            "JOIN user_jobs uj ON uj.job_id = j.job_id AND uj.user_id = %s "
            f"WHERE true{added} GROUP BY j.status",
            (user_id, *since_params),
        )
        job_counts = {r["status"]: r["n"] for r in cur.fetchall()}
        cur.execute(
            "SELECT t.status, count(*) AS n FROM tailored_resumes t "
            "JOIN user_jobs uj ON uj.job_id = t.job_id AND uj.user_id = t.user_id "
            f"WHERE t.user_id = %s{added} GROUP BY t.status",
            (user_id, *since_params),
        )
        tailor_counts = {r["status"]: r["n"] for r in cur.fetchall()}
        cur.execute(
            "SELECT t.job_id, t.status, t.resume_score, t.new_resume_score, "
            "t.job_match_summary, t.error, j.title, j.company, j.url, "
            "j.status AS job_status, "
            "j.error AS job_error, ap.status AS apply_status, ap.outcome AS apply_outcome "
            "FROM tailored_resumes t JOIN jobs j USING (job_id) "
            "JOIN user_jobs uj ON uj.job_id = t.job_id AND uj.user_id = t.user_id "
            "LEFT JOIN LATERAL ("
            "  SELECT a.status, a.outcome FROM applications a "
            "  WHERE a.job_id = t.job_id AND a.user_id = t.user_id "
            "  ORDER BY a.started_at DESC LIMIT 1"
            ") ap ON true "
            f"WHERE t.user_id = %s{added} ORDER BY t.created_at DESC",
            (user_id, *since_params),
        )
        rows = cur.fetchall()
    return {"job_counts": job_counts, "tailor_counts": tailor_counts, "rows": rows}


def _print_summary(s: dict, scope: str = "") -> None:
    jc, tc = s["job_counts"], s["tailor_counts"]
    if not jc and not tc:
        # Word the empty pipeline exactly as `jobs list` does — the same state
        # described two different ways reads like two different states.
        print(f"\nno jobs{scope}")
        return
    print(f"\njobs: {jc or 'none'}   tailoring: {tc or 'none'}")
    for r in s["rows"]:
        name = (_job_label(r) or r["url"])[:50]
        score = ""
        if r["new_resume_score"] is not None:
            score = f"  score {r['resume_score']} -> {r['new_resume_score']}"
        err = f"  error: {r['error']}" if r["error"] else ""
        mark = _apply_marker(r)
        applied = f"  [{mark}]" if mark else ""
        print(f"{_id8(r['job_id'])}  {r['status']:<9} {name}{score}{applied}{err}")
    stuck = (
        s["job_counts"].get("jd_capturing", 0),
        s["tailor_counts"].get("tailoring", 0),
    )
    if any(stuck):
        print(
            f"warning: stuck in-progress rows (jd_capturing={stuck[0]}, tailoring={stuck[1]}) "
            "— likely a crashed worker; reset their status manually to retry"
        )


def run_tailor_worker() -> int:
    """Drain the tailoring queue via the ai-assist worker (one-shot). Subprocess
    because ai-assist has no build-system; cwd must be ai-assist/ — its env.py
    loads dev.env relative to CWD.

    OPENROUTER_* is stripped: load_dotenv above put *this* package's LLM creds in
    os.environ, and ai-assist's load_dotenv("dev.env") does not override what is
    already set — so an inherited key would silently be sent to ai-assist's own
    LLM_BASE_URL (401 -> every row fails)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENROUTER_")}
    proc = subprocess.run(
        ["uv", "run", "python", "worker.py"],
        cwd=AI_ASSIST_DIR,
        env={**env, "TAILOR_ONCE": "1"},
    )
    return proc.returncode


def run_jd_capture(conn, base_labels: dict[str, str] | None = None) -> int:
    """Capture JDs for every jd_pending row (needs playwright+proxy containers).
    Prints one line per job; returns a shell exit code.

    `base_labels` (user_id -> role label) is handed to the capture so the tailoring
    row it enqueues points at that role's base resume. Capture creates the row, so
    without this the later label-aware backfill finds nothing to do and --label is
    silently ignored."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM jobs WHERE status = 'jd_pending'")
        n_pending = cur.fetchone()["n"]
    if not n_pending:
        return 0
    from agent import jd_capture

    storage = _storage()

    async def _drain_capture() -> None:
        async with jd_capture.proxy_navigator() as navigate:
            while True:
                job_id = await jd_capture.capture_once(
                    conn, navigate, storage, worker_id="cli", base_labels=base_labels
                )
                if job_id is None:
                    return
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT status, error, title, url FROM jobs WHERE job_id = %s",
                        (job_id,),
                    )
                    row = cur.fetchone()
                outcome = (
                    row["status"]
                    if row["status"] != "failed"
                    else f"failed: {row['error']}"
                )
                print(
                    f"capture {_id8(job_id)}  {(row['title'] or row['url'])[:60]}  ->  {outcome}"
                )

    print(f"capturing JDs for {n_pending} pending job(s)...")
    try:
        asyncio.run(_drain_capture())
    except Exception as exc:
        print(
            f"JD capture failed: {exc}\n"
            f"hint: is the proxy MCP up at {jd_capture.PROXY_MCP_URL}? "
            "start it: docker compose up -d (in auto-browse/)",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_tailor(args) -> int:
    with _connect() as conn:
        user_id = resolve_user(conn, args.user)

        label = getattr(args, "label", None)
        if label:
            # Fail before the expensive JD capture, not after it.
            resolve_base(conn, user_id, label)

        if args.retry_failed:
            # Default window is today: an older failure was left unretried on
            # purpose (or is a dud), and every retry is a paid model call.
            since = parse_since(getattr(args, "retry_since", None))
            window = "all time" if since is None else f"since {since:%Y-%m-%d}"
            # JD-capture failures are label-agnostic — a job has one JD — so the
            # label only scopes the tailoring rows. Both are scoped to this user.
            n_jobs = reset_failed_jobs(conn, user_id, since)
            n_rows = reset_failed_tailorings(conn, user_id, since, label)
            scope = f" on the {label} base" if label else ""
            print(
                f"reset {n_jobs} failed job(s), {n_rows} failed tailoring row(s)"
                f"{scope} ({window})"
            )
            if since is not None:
                older = count_older_failures(conn, user_id, since, label)
                if older:
                    print(
                        f"{older} older failure(s){scope} left alone "
                        "— add --retry-since all (or a date) to include them"
                    )

        # Single-job path: re-tailor just this job from the chosen base and drain.
        if getattr(args, "job", None):
            job = resolve_job(conn, args.job)
            if job["status"] != "jd_ready":
                raise SystemExit(
                    f"job {_id8(job['job_id'])} is {job['status']}, not jd_ready — "
                    "capture its JD first: autoapply tailor"
                )
            reenqueue_one(conn, user_id, job["job_id"], label)
            print(
                f"re-tailoring {_id8(job['job_id'])} "
                f"from {label or 'default'} base via ai-assist worker..."
            )
            rc = run_tailor_worker()
            _print_summary(tailoring_summary(conn, user_id))
            return rc or 0

        # Phase 1 — capture JDs for pending jobs (needs playwright+proxy containers).
        # Capture enqueues the tailoring row itself, so it has to be told the label
        # up front — otherwise the row lands on the default base and phase 2 sees
        # nothing to do.
        rc = run_jd_capture(conn, {user_id: label} if label else None)
        if rc:
            return rc

        # Phase 2 — make sure every captured job has a tailoring row for this user.
        # Anything already tailored is a duplicate: report it, and only redo the work
        # when explicitly asked (each redo is a paid model call).
        dupes = already_tailored(conn, user_id)
        if dupes and getattr(args, "force", False):
            n_forced = requeue_duplicates(conn, user_id, dupes, label)
            print(f"--force: re-queued {n_forced} already-tailored job(s)")
        else:
            print_duplicates(dupes)

        n_backfilled = backfill_tailoring(conn, user_id, label)
        if n_backfilled:
            print(
                f"enqueued tailoring for {n_backfilled} job(s)"
                + (f" from {label} base" if label else "")
            )
        # Rows queued earlier by JD capture or the apply loop still sit on the
        # default base; move the un-run ones onto the requested role.
        if label:
            n_moved = repoint_pending(conn, user_id, label)
            if n_moved:
                print(f"switched {n_moved} pending job(s) to the {label} base")

        # Phase 3 — drain the tailoring queue.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM tailored_resumes WHERE status = 'pending'"
            )
            n_queue = cur.fetchone()["n"]
        if n_queue:
            print(f"tailoring {n_queue} resume(s) via ai-assist worker...")
            rc = run_tailor_worker()
            if rc != 0:
                print(
                    f"tailor worker exited {rc}\n"
                    "hints: OPENROUTER_API_KEY set in ai-assist/dev.env "
                    "(this package's key is deliberately not inherited)? "
                    "LibreOffice installed (docx->pdf)?",
                    file=sys.stderr,
                )

        s = tailoring_summary(conn, user_id)
        _print_summary(s)
        bad = s["tailor_counts"].get("failed", 0) + s["tailor_counts"].get("pending", 0)
        return 1 if bad else 0


# ---------------------------------------------------------------------------
# retry


def reset_failed_jobs(conn, user_id: str, since: datetime | None) -> int:
    """Flip this user's failed JD fetches back to jd_pending so the capture
    worker picks them up again. `since` limits to jobs added on/after it."""
    added = " AND uj.added_at >= %s" if since is not None else ""
    params = (user_id, *((since,) if since is not None else ()))
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET status = 'jd_pending', error = NULL, updated_at = now() "
            "WHERE status = 'failed' AND job_id IN "
            f"(SELECT uj.job_id FROM user_jobs uj WHERE uj.user_id = %s{added})",
            params,
        )
        n = cur.rowcount
    conn.commit()
    return n


def count_failed_jobs(conn, user_id: str, since: datetime | None) -> int:
    added = " AND uj.added_at >= %s" if since is not None else ""
    params = (user_id, *((since,) if since is not None else ()))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM jobs j "
            "JOIN user_jobs uj ON uj.job_id = j.job_id AND uj.user_id = %s "
            f"WHERE j.status = 'failed'{added}",
            params,
        )
        return cur.fetchone()["n"]


def parse_since(value: str | None) -> datetime | None:
    """Cut-off for `--retry-failed`, as local-midnight-aligned tz-aware datetime.

    None (the flag not passed) means today, so a retry never silently re-runs
    failures from previous days that were left alone on purpose — each one costs a
    model call. 'all' removes the cut-off; otherwise take a local date
    (YYYY-MM-DD) or a day count ('7d')."""
    if value is None or value.strip().lower() == "today":
        return _today_start()
    v = value.strip().lower()
    if v == "all":
        return None
    if re.fullmatch(r"\d+d", v):
        return _today_start() - timedelta(days=int(v[:-1]))
    try:
        return datetime.combine(date.fromisoformat(v), time.min).astimezone()
    except ValueError:
        raise SystemExit(
            f"--retry-since: expected 'all', 'today', '7d' or YYYY-MM-DD, got {value!r}"
        )


def _failed_tailoring_filter(user_id: str, label: str | None) -> tuple[str, list]:
    """Shared WHERE fragment for this user's failed tailoring rows.

    With `label`, only rows queued from that role's base match. Matching on the
    label rather than one asset id keeps a base that was replaced (`resume add
    --replace`) in scope: its older rows point at the superseded asset. Rows with
    no base at all belong to no role, so a labelled run leaves them alone."""
    clauses = ["t.user_id = %s", "t.status = 'failed'"]
    params: list = [user_id]
    if label is not None:
        clauses.append(
            "t.base_asset_id IN (SELECT id FROM resume_assets WHERE user_id = %s "
            "AND kind = 'base' AND label = %s)"
        )
        params += [user_id, label]
    return " AND ".join(clauses), params


def reset_failed_tailorings(
    conn, user_id: str, since: datetime | None, label: str | None = None
) -> int:
    """Flip this user's failed tailoring rows back to pending so the worker redoes
    them. Scoped by `since` (when the row failed) and, when given, by the base
    resume's label — so `--label swe --retry-failed` cannot drag a failed `de` row
    into the swe batch, where `repoint_pending` would then move it onto the swe
    base."""
    where, params = _failed_tailoring_filter(user_id, label)
    if since is not None:
        where += " AND t.updated_at >= %s"
        params.append(since)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE tailored_resumes t SET status = 'pending', error = NULL, "
            f"updated_at = now() WHERE {where}",  # noqa: S608 — literals + params
            params,
        )
        n = cur.rowcount
    conn.commit()
    return n


def count_older_failures(
    conn, user_id: str, since: datetime, label: str | None = None
) -> int:
    """Failed rows the cut-off excluded. Reported so an untouched backlog stays
    visible instead of looking like it was retried."""
    where, params = _failed_tailoring_filter(user_id, label)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*) AS n FROM tailored_resumes t "  # noqa: S608
            f"WHERE {where} AND t.updated_at < %s",
            [*params, since],
        )
        return cur.fetchone()["n"]


def cmd_retry(args) -> int:
    with _connect() as conn:
        user_id = resolve_user(conn, args.user)
        since = None if args.all else _today_start()
        scope = "" if args.all else " added today"
        n = reset_failed_jobs(conn, user_id, since)
        if not n:
            print(f"no failed jobs{scope}")
            return 0
        print(f"reset {n} failed job(s){scope} to jd_pending")
        rc = run_jd_capture(conn)
        if rc:
            return rc
        still = count_failed_jobs(conn, user_id, since)
        if still:
            print(
                f"{still} job(s){scope} failed again — see errors: "
                "autoapply jobs list --status failed"
            )
        return 1 if still else 0


# ---------------------------------------------------------------------------
# status / resume show


def cmd_status(args) -> int:
    with _connect() as conn:
        user_id = resolve_user(conn, args.user)
        if args.job:
            job = resolve_job(conn, args.job)
            t = repo.lookup_tailored(conn, user_id, job["job_id"])
            print(f"job     {job['job_id']}")
            print(f"url     {job['url']}")
            print(f"title   {job['title'] or '-'}")
            print(f"company {job['company'] or '-'}")
            print(
                f"status  {job['status']}"
                + (f"  error: {job['error']}" if job["error"] else "")
            )
            if t:
                print(
                    f"tailoring  {t['status']}"
                    + (f"  error: {t['error']}" if t["error"] else "")
                )
                if t["new_resume_score"] is not None:
                    print(f"score      {t['resume_score']} -> {t['new_resume_score']}")
                if t["job_match_summary"]:
                    print(f"match      {t['job_match_summary']}")
            else:
                print("tailoring: not enqueued (run: autoapply tailor)")
            apps = repo.list_applications(conn, user_id, job["job_id"])
            if apps:
                print("applications:")
                for a in apps:
                    when = a["started_at"].strftime("%Y-%m-%d %H:%M")
                    out = f"  {a['outcome']}" if a["outcome"] else ""
                    print(f"  {when}  {a['status']}{out}  (run {a['run_id'] or '-'})")
            return 0
        if getattr(args, "date", None):
            since, until = _day_start(args.date), _day_start(args.date + timedelta(days=1))
            _print_summary(tailoring_summary(conn, user_id, since, until), f" added on {args.date}")
            return 0
        _print_summary(
            tailoring_summary(conn, user_id, _today_start() if args.today else None),
            " added today" if args.today else "",
        )
        return 0


def show_resume(conn, storage, user_id: str, job_id: str, out: str | None) -> str:
    """Download the tailored PDF for user×job; returns the local path."""
    t = repo.lookup_tailored(conn, user_id, job_id)
    if not t or t["status"] != "ready" or not t["tailored_asset_id"]:
        state = t["status"] if t else "not enqueued"
        err = f" ({t['error']})" if t and t.get("error") else ""
        raise SystemExit(
            f"no tailored resume for job {_id8(job_id)}: {state}{err} — run: autoapply tailor"
        )
    asset = repo.get_resume_asset(conn, t["tailored_asset_id"])
    dest = out or str(HERE / "output" / "resume" / "cli" / f"{job_id}.pdf")
    storage.get_file(asset["s3_key"], dest)
    return dest


def cmd_resume_show(args) -> int:
    with _connect() as conn:
        user_id = resolve_user(conn, args.user)
        job = resolve_job(conn, args.job)
        path = show_resume(conn, _storage(), user_id, job["job_id"], args.out)
    print(path)
    if args.open:
        subprocess.Popen(["xdg-open", path])
    return 0


def cmd_resume_add(args) -> int:
    from cli import onboard

    return onboard.cmd_resume_add(args)


def cmd_resume_parse(args) -> int:
    from cli import onboard

    return onboard.cmd_resume_parse(args)


def cmd_resume_default(args) -> int:
    from cli import onboard

    return onboard.cmd_resume_default(args)


def cmd_resume_bases(args) -> int:
    """List the user's role resumes: which labels exist, which is the default, and
    whether each has been parsed into a profile."""
    with _connect() as conn:
        user_id = resolve_user(conn, args.user)
        bases = repo.list_base_assets(conn, user_id)
        default = repo.default_resume_label(conn, user_id)
    if not bases:
        print("no resumes (autoapply resume add <file.docx> --label <role>)")
        return 0
    for b in bases:
        mark = "*" if b["label"] == default else " "
        when = b["created_at"].strftime("%Y-%m-%d")
        parsed = ""
        if b["variant"] == "docx":
            parsed = "parsed" if b.get("parsed") else "NOT PARSED"
        print(f"{mark} {b['label']:<10} {b['variant'] or '-':<5} {when}  {parsed:<10} {b['s3_key']}")
    return 0


# ---------------------------------------------------------------------------
# ask (free-text application questions)


def run_answer(
    user_id: str,
    job_id: str,
    question: str,
    max_words: int,
    label: str | None = None,
) -> dict:
    """Answer one application question via ai-assist. Subprocess for the same reason
    as run_tailor_worker: ai-assist has no build-system and its env.py reads dev.env
    relative to CWD. OPENROUTER_* is stripped for the same reason too — this package's
    creds/base-url must not leak into ai-assist's own LLM config.

    `label` picks the base resume to answer from; None lets ai-assist follow the
    base this job was tailored from.

    Returns the parsed JSON reply: {"answer", "word_count", "base_label"} or
    {"error"}."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENROUTER_")}
    payload = {
        "user_id": str(user_id),
        "job_id": str(job_id),
        "question": question,
        "max_words": max_words,
        "label": label,
    }
    proc = subprocess.run(
        ["uv", "run", "python", "answer_cli.py"],
        cwd=AI_ASSIST_DIR,
        env=env,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {"error": proc.stderr.strip() or f"ai-assist exited {proc.returncode}"}


def cmd_ask(args) -> int:
    question = args.question
    if question == "-":
        question = sys.stdin.read().strip()
    if not question:
        raise SystemExit("empty question")

    with _connect() as conn:
        user_id = resolve_user(conn, args.user)
        job = resolve_job(conn, args.job)
        if job["status"] != "jd_ready":
            raise SystemExit(
                f"job {_id8(job['job_id'])} is {job['status']}, not jd_ready — "
                "the JD must be captured first: autoapply tailor"
            )

    result = run_answer(
        user_id, job["job_id"], question, args.words, getattr(args, "label", None)
    )
    if "error" in result:
        print(f"ask failed: {result['error']}", file=sys.stderr)
        return 1
    print(result["answer"])
    if not args.quiet:
        # naming the base makes a wrong-resume answer visible instead of silent
        print(
            f"\n[{result['word_count']}/{args.words} words "
            f"| base: {result.get('base_label', '?')}]",
            file=sys.stderr,
        )
    return 0


# ---------------------------------------------------------------------------
# cover (cover letter per job)


def run_cover(user_id: str, job_id: str, max_words: int, label: str | None = None) -> dict:
    """Write and store a cover letter via ai-assist's cover_cli.py; a subprocess with
    OPENROUTER_* stripped for the same reasons as run_answer.

    Returns {"text", "word_count", "base_label", "pdf_key", "txt_key"} or {"error"}."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENROUTER_")}
    payload = {
        "user_id": str(user_id),
        "job_id": str(job_id),
        "max_words": max_words,
        "label": label,
    }
    proc = subprocess.run(
        ["uv", "run", "python", "cover_cli.py"],
        cwd=AI_ASSIST_DIR,
        env=env,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )
    line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {"error": proc.stderr.strip() or f"ai-assist exited {proc.returncode}"}


def cmd_cover(args) -> int:
    with _connect() as conn:
        user_id = resolve_user(conn, args.user)
        job = resolve_job(conn, args.job)
        if job["status"] != "jd_ready":
            raise SystemExit(
                f"job {_id8(job['job_id'])} is {job['status']}, not jd_ready — "
                "the JD must be captured first: autoapply tailor"
            )

    result = run_cover(user_id, job["job_id"], args.words, getattr(args, "label", None))
    if "error" in result:
        print(f"cover failed: {result['error']}", file=sys.stderr)
        return 1
    print(result["text"])
    path = None
    if args.out or args.open:
        path = args.out or str(HERE / "output" / "cover" / "cli" / f"{job['job_id']}.pdf")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        _storage().get_file(result["pdf_key"], path)
    if not args.quiet:
        print(
            f"\n[{result['word_count']}/{args.words} words | base: {result.get('base_label', '?')}"
            f" | stored with job {_id8(job['job_id'])}{f' | {path}' if path else ''}]",
            file=sys.stderr,
        )
    if args.open and path:
        subprocess.Popen(["xdg-open", path])
    return 0


# ---------------------------------------------------------------------------
# gmail (2FA email access for the apply agent)


def cmd_gmail_connect(args) -> int:
    from agent import email_otp

    with _connect() as conn:
        user_id = resolve_user(conn, args.user)
        address, refresh_token, scopes = email_otp.run_oauth_flow(
            open_browser=not args.no_open
        )
        repo.save_gmail_token(conn, user_id, address, refresh_token, scopes)
    print(f"gmail connected for user {user_id}: {address or '(address unknown)'}")
    return 0


def cmd_gmail_status(args) -> int:
    with _connect() as conn:
        user_id = resolve_user(conn, args.user)
        tok = repo.get_gmail_token(conn, user_id)
    if not tok:
        print("gmail: not connected (run: autoapply gmail connect)")
        return 1
    print(
        f"gmail: connected as {tok['gmail_address'] or '(address unknown)'} "
        f"(scopes: {', '.join(tok['scopes'])}; updated {tok['updated_at']})"
    )
    return 0


# ---------------------------------------------------------------------------
# profile (onboarding lives in cli/onboard.py; lazy so `jobs list` stays fast)


def _cmd_profile_init(args) -> int:
    from cli import onboard

    return onboard.cmd_profile_init(args)


def _cmd_profile_show(args) -> int:
    from cli import onboard

    return onboard.cmd_profile_show(args)


def _cmd_profile_export(args) -> int:
    from cli import onboard

    return onboard.cmd_profile_export(args)


# ---------------------------------------------------------------------------
# apply


def _run_apply_agent(
    conn,
    user_id: str,
    job_id,
    url: str,
    model: str,
    autosubmit: bool,
    continue_run: bool = False,
    handover: str | None = None,
) -> bool:
    """One agent run against one job, with the applications-row bookkeeping.
    Returns True on success. `handover` (det+agent): det's brief; the agent continues
    the page det left open."""
    from agent import loop
    from agent.observability import new_run_id

    tailored = repo.lookup_tailored(conn, user_id, job_id)
    run_id = new_run_id()
    app_id = repo.start_application(
        conn,
        user_id,
        job_id,
        run_id=run_id,
        tailored_resume_id=tailored["id"] if tailored else None,
    )
    print(f"{'continuing' if continue_run or handover else 'applying'} {_id8(job_id)}  {url}"
          + ("  (agent, from det's handover)" if handover else ""))
    if autosubmit:
        print("  autosubmit on — agent clicks the final Submit itself")
        print("  watch at http://localhost:6080/vnc.html")
    else:
        print("  watch + confirm final submit at http://localhost:6080/vnc.html")
    try:
        final_text = asyncio.run(
            loop.main(
                user_id,
                url,
                model,
                run_id=run_id,
                autosubmit=autosubmit,
                continue_run=continue_run,
                handover=handover,
            )
        )
    except Exception as exc:
        repo.finish_application(conn, app_id, "failed", outcome=str(exc)[:500])
        print(f"apply failed for {_id8(job_id)}: {exc}", file=sys.stderr)
        return False
    outcome = _autosubmit_outcome(final_text) if autosubmit else _confirm_submitted()
    if autosubmit:
        print(f"  agent verdict: {outcome}")
    repo.finish_application(conn, app_id, "finished", outcome=outcome)
    return True


def _run_apply_det(
    conn,
    user_id: str,
    job_id,
    url: str,
    model: str,
    autosubmit: bool,
    mcp_url: str | None = None,
    continue_run: bool = False,
    agent_fallback: bool = False,
) -> bool:
    """One deterministic-filler run, with the same applications-row bookkeeping as the
    agent. Returns True unless the run failed or got stuck. `agent_fallback`
    (det+agent): when det stops short, the agent finishes the open page from det's
    handover, as its own applications row."""
    from agent import det_apply
    from agent.observability import new_run_id

    tailored = repo.lookup_tailored(conn, user_id, job_id)
    run_id = new_run_id()
    app_id = repo.start_application(
        conn, user_id, job_id, run_id=run_id,
        tailored_resume_id=tailored["id"] if tailored else None,
    )
    print(f"{'continuing' if continue_run else 'applying'} {_id8(job_id)}  {url}  (engine det)")
    print("  watch at http://localhost:6080/vnc.html")
    try:
        result = asyncio.run(
            det_apply.run_det(
                conn, _storage(), user_id, job_id, url,
                run_id=run_id, model=model, answer_question=run_answer,
                autosubmit=autosubmit, navigate=not continue_run,
                mcp_url=mcp_url or det_apply.PLAYWRIGHT_MCP_URL, push=not agent_fallback,
            )
        )
    except Exception as exc:
        repo.finish_application(conn, app_id, "failed", outcome=str(exc)[:500])
        print(f"apply failed for {_id8(job_id)}: {exc}", file=sys.stderr)
        return False
    for page in result.pages:
        ok = sum(1 for f in page.filled if f["ok"])
        bad = [f["label"] for f in page.filled if not f["ok"]]
        print(f"  page {page.number}: filled {ok}" + (f", failed {len(bad)}: {'; '.join(bad[:3])}" if bad else "")
              + (f", {page.action}" if page.action else ""))
    print(f"  {result.status}: {result.reason}  (report output/det-runs/{run_id}.json)")
    if agent_fallback and det_apply.agent_should_take_over(result):
        repo.finish_application(conn, app_id, "failed", outcome=f"det stopped, agent took over: {result.reason}"[:500])
        print("  det stopped short — the agent takes over the open page")
        return _run_apply_agent(conn, user_id, job_id, url, model, autosubmit,
                                continue_run=True, handover=det_apply.agent_handover(result))
    if agent_fallback and result.status == "handoff":
        det_apply.push_handoff(result, url, job_id)  # the agent does not take this one
    if result.status in ("submitted", "unconfirmed"):
        repo.finish_application(conn, app_id, "finished", outcome=result.status)
        return True
    if result.status == "stuck":
        repo.finish_application(conn, app_id, "failed", outcome=result.reason[:500])
        return False
    # ready-to-submit, or handed off to you (captcha / code): finish in the VNC view
    print("  finish in the browser, then answer below")
    repo.finish_application(conn, app_id, "finished", outcome=_confirm_submitted())
    return True


# The autosubmit prompt makes the agent end its final reply with `SUBMITTED: yes`
# (confirmation page seen) or `SUBMITTED: no`. Take the LAST match — earlier text
# may discuss the format itself.
_SUBMIT_VERDICT_RE = re.compile(r"SUBMITTED:\s*(yes|no|already)\b", re.IGNORECASE)


def _autosubmit_outcome(final_text: str | None) -> str:
    """DB outcome from the agent's final message. No parsable verdict means the
    run ended without a clear confirmation — record it as such, never as
    submitted (the marker stays out of `applied` and batch mode will retry)."""
    hits = _SUBMIT_VERDICT_RE.findall(final_text or "")
    if not hits:
        return "unconfirmed"
    # "already": the portal said this job was applied to before (Coca-Cola and BU on
    # 2026-10-05, after a manual application); the agent used to report that as "yes"
    return {"yes": "submitted", "already": "already applied"}.get(hits[-1].lower(), "not submitted")


def select_ready_jobs(conn, user_id: str, since: datetime | None) -> list[dict]:
    """Batch-apply selection: jd_ready jobs (added since `since` when given),
    minus the ones whose latest attempt ended in a confirmed submit, that the portal
    says were applied to before, or that the user skipped (`jobs skip`)."""
    rows = repo.list_jobs_for_user(conn, user_id, "jd_ready", since=since)
    return [r for r in rows if r.get("apply_outcome") not in ("submitted", "already applied", "skipped")]


def _check_engine_model(engine: str, model: str) -> None:
    from agent.jev_client import is_jev

    if engine != "det" and is_jev(model):
        # Jev answers typed questions and writes nothing; the agent needs tool calls
        raise SystemExit(f"{model} can only drive --engine det (it picks answers; the agent engine needs a chat model)")


def cmd_apply(args) -> int:
    from agent import loop

    batch = args.all or args.today
    if not args.jobs and not batch:
        raise SystemExit("pass job(s), or --all / --today to batch through ready jobs")

    model = args.model or loop.DEFAULT_MODEL
    _check_engine_model(args.engine, model)
    failed = 0
    with _connect() as conn:
        user_id = resolve_user(conn, args.user)
        targets: list[tuple] = []  # (job_id, url)
        for ref in args.jobs:
            if "://" in ref:  # URL: create on the fly, like agent.loop does
                job_id = repo.add_job_for_user(conn, user_id, ref, source="manual")
                targets.append((job_id, ref))
            else:
                job = resolve_job(conn, ref)
                # applying implies ownership — link it (idempotent)
                repo.add_job_for_user(conn, user_id, job["url"], source="manual")
                targets.append((job["job_id"], job["url"]))
        if batch:
            since = _today_start() if args.today else None
            picked = select_ready_jobs(conn, user_id, since)
            seen = {j for j, _ in targets}
            picked = [r for r in picked if r["job_id"] not in seen]
            scope = "added today" if args.today else "all time"
            print(f"batch: {len(picked)} ready job(s) ({scope}) still to apply")
            targets += [(r["job_id"], r["url"]) for r in picked]
        if not targets:
            print("nothing to apply to")
            return 0
        for i, (job_id, url) in enumerate(targets, 1):
            print(f"[{i}/{len(targets)}]", end=" ")
            if args.engine in ("det", "det+agent"):
                ok = _run_apply_det(conn, user_id, job_id, url, model, args.autosubmit, args.mcp,
                                    agent_fallback=args.engine == "det+agent")
            else:
                ok = _run_apply_agent(conn, user_id, job_id, url, model, args.autosubmit)
            if not ok:
                failed += 1
    return 1 if failed else 0


def cmd_continue(args) -> int:
    """Resume a partially filled application still open in the browser: the agent
    reads the current page and completes only what's missing (no navigation)."""
    from agent import loop

    model = args.model or loop.DEFAULT_MODEL
    _check_engine_model(args.engine, model)
    with _connect() as conn:
        user_id = resolve_user(conn, args.user)
        job = resolve_job(conn, args.job)
        job_id, url = job["job_id"], job["url"]
        apps = repo.list_applications(conn, user_id, job_id)
        if not apps:
            raise SystemExit(
                f"no apply attempt recorded for {_id8(job_id)} — start one: "
                f"autoapply apply {args.job}"
            )
        last = apps[0]
        if last["status"] == "running":
            raise SystemExit(
                "an apply run for this job is still marked running — wait for it "
                "or check `autoapply status`"
            )
        if last["outcome"] == "submitted":
            raise SystemExit("already submitted — nothing to continue")
        if args.engine in ("det", "det+agent"):
            ok = _run_apply_det(
                conn, user_id, job_id, url, model, args.autosubmit, args.mcp, continue_run=True,
                agent_fallback=args.engine == "det+agent",
            )
        else:
            ok = _run_apply_agent(
                conn, user_id, job_id, url, model, args.autosubmit, continue_run=True
            )
    return 0 if ok else 1


def _confirm_submitted(ask=input) -> str | None:
    """The agent stops before the final submit; the human clicks it in the VNC
    view. Ask whether they did, so the DB knows more than 'agent finished'.
    Non-interactive (EOF): record nothing, matching the old behavior."""
    try:
        raw = ask("  did you submit the application? [y/N]: ").strip().lower()
    except EOFError:
        return None
    return "submitted" if raw in ("y", "yes") else "not submitted"


# ---------------------------------------------------------------------------
# parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="autoapply", description="Job application pipeline CLI"
    )
    sub = p.add_subparsers(dest="command", required=True)

    jobs = sub.add_parser("jobs", help="manage job postings")
    jobs_sub = jobs.add_subparsers(dest="jobs_command", required=True)
    add = jobs_sub.add_parser(
        "add",
        help="add job URLs to the pipeline",
        description="URLs are stored canonicalized: a trailing apply-form segment "
        "(/application, /apply, ...) is dropped, because that page carries the form "
        "and no job description — capturing it would fail.",
    )
    add.add_argument("urls", nargs="*", help="job posting URLs")
    add.add_argument("--file", help="file with one URL per line (# comments ok)")
    add.add_argument("--user", help="user id or email (default: the only active user)")
    add.set_defaults(func=cmd_jobs_add)
    rm = jobs_sub.add_parser(
        "rm",
        help="remove jobs from your pipeline (so a URL can be re-added)",
        description="Deletes your tailorings, resume rows and apply records for the "
        "job. The shared jobs row is deleted only if no other user owns it. Tailored "
        "files already in object storage are left in place. Targets are handled in "
        "order and a target that cannot be resolved stops the run, so earlier ones "
        "in the same command stay removed.",
    )
    rm.add_argument("jobs", nargs="+", help="job ids, id prefixes, or URLs")
    rm.add_argument("--user", help="user id or email (default: the only active user)")
    rm.set_defaults(func=cmd_jobs_rm)
    skip = jobs_sub.add_parser(
        "skip",
        help="mark jobs as not applying (batch apply passes over them)",
        description="Records an apply attempt with outcome 'skipped'. `apply --all / "
        "--today` passes over the job; `apply JOB` by name still runs it. The job and "
        "its tailoring stay; `jobs rm` removes them instead.",
    )
    skip.add_argument("jobs", nargs="+", help="job ids, id prefixes, or URLs")
    skip.add_argument("--user", help="user id or email (default: the only active user)")
    skip.set_defaults(func=cmd_jobs_skip)
    lst = jobs_sub.add_parser("list", help="list your jobs")
    lst.add_argument("--status", choices=_JOB_STATUSES)
    lst.add_argument("--today", action="store_true", help="only jobs added today")
    lst.add_argument("--user", help="user id or email (default: the only active user)")
    lst.set_defaults(func=cmd_jobs_list)

    tailor = sub.add_parser(
        "tailor",
        help="capture JDs + tailor resumes for all pending jobs",
        description="Captures JDs for jd_pending jobs (needs the playwright+proxy "
        "containers), then drains the tailoring queue via the ai-assist worker "
        "(needs OPENROUTER_API_KEY + LibreOffice). Note: the worker drains ALL "
        "users' rows; --user only scopes the backfill and summary. Pass a JOB to "
        "re-tailor just that job; --label picks which base resume to tailor from "
        "(see: autoapply resume add / resume bases). --retry-failed only reaches "
        "today's failures, and only the ones on --label's base, so an older backlog "
        "is never silently re-run; --retry-since widens the window.",
    )
    tailor.add_argument(
        "job", nargs="?", help="job id, id prefix, or URL — re-tailor only this job"
    )
    tailor.add_argument(
        "--label", help="role base to tailor from (default: the default base)"
    )
    tailor.add_argument(
        "--force",
        action="store_true",
        help="re-tailor jobs reported as duplicates (one paid model call each)",
    )
    tailor.add_argument(
        "--user", help="user id or email (default: the only active user)"
    )
    tailor.add_argument(
        "--retry-failed",
        action="store_true",
        help="reset today's failed jobs + tailoring rows and retry them "
        "(scoped to --label when given; widen with --retry-since)",
    )
    tailor.add_argument(
        "--retry-since",
        metavar="WHEN",
        help="how far back --retry-failed reaches: 'today' (default), 'all', "
        "a day count like '7d', or a date (YYYY-MM-DD)",
    )
    tailor.set_defaults(func=cmd_tailor)

    retry = sub.add_parser(
        "retry",
        help="reset today's failed JD fetches and recapture them",
        description="Flips your failed jobs (added today, or all with --all) back "
        "to jd_pending and re-runs JD capture (needs the playwright+proxy "
        "containers). Failed TAILORING rows are covered by tailor --retry-failed.",
    )
    retry.add_argument(
        "--all", action="store_true", help="retry all failed jobs, not just today's"
    )
    retry.add_argument("--user", help="user id or email (default: the only active user)")
    retry.set_defaults(func=cmd_retry)

    status = sub.add_parser("status", help="pipeline status + scores")
    status.add_argument(
        "job", nargs="?", help="job id, id prefix, or URL (full detail)"
    )
    when = status.add_mutually_exclusive_group()
    when.add_argument("--today", action="store_true", help="only jobs added today")
    when.add_argument("--date", type=_parse_day, metavar="YYYY-MM-DD",
                      help="only jobs added on this local day (e.g. yesterday's batch)")
    status.add_argument("--user", help="user id or email")
    status.set_defaults(func=cmd_status)

    resume = sub.add_parser("resume", help="tailored resume helpers")
    resume_sub = resume.add_subparsers(dest="resume_command", required=True)
    show = resume_sub.add_parser("show", help="download the tailored resume PDF")
    show.add_argument("job", help="job id, id prefix, or URL")
    show.add_argument("--user", help="user id or email")
    show.add_argument(
        "--out", help="destination path (default output/resume/cli/<job_id>.pdf)"
    )
    show.add_argument("--open", action="store_true", help="open with xdg-open")
    show.set_defaults(func=cmd_resume_show)
    radd = resume_sub.add_parser(
        "add",
        help="add a resume for a role (.docx, .odt or .md), parsed into that role's profile",
        description="Uploads the .docx under --label with a PDF rendering (LibreOffice) "
        "for uploads, and parses it with the LLM (needs OPENROUTER_API_KEY; one paid "
        "call) into the work history, education and skills used for that role. The "
        "first role you add becomes your default. Prints any employer whose dates, or "
        "any current employer, disagrees with your other role resumes. Tailor with "
        "`autoapply tailor --label <label>`. An .odt or .md is converted to .docx first "
        "and only the .docx is kept, since tailoring edits it; a PDF is refused.",
    )
    radd.add_argument("file", help="path to the resume (.docx, .odt or .md)")
    radd.add_argument("--label", required=True, help="role, e.g. general, swe, de, aiml")
    radd.add_argument("--replace", action="store_true", help="overwrite this role's resume")
    radd.add_argument("--default", action="store_true", help="make this the default role")
    radd.add_argument("--model", help="OpenRouter model for parsing (default: OPENROUTER_MODEL)")
    radd.add_argument("--user", help="user id or email")
    radd.set_defaults(func=cmd_resume_add)
    rparse = resume_sub.add_parser(
        "parse",
        help="re-parse stored role resumes (one paid call each)",
        description="Downloads each role's stored .docx, renders its PDF if missing and "
        "re-parses it. For resumes added before parsing existed, or after a parser "
        "change. With no LABEL, every role is parsed.",
    )
    rparse.add_argument("labels", nargs="*", help="roles to parse (default: all)")
    rparse.add_argument("--model", help="OpenRouter model for parsing (default: OPENROUTER_MODEL)")
    rparse.add_argument("--user", help="user id or email")
    rparse.set_defaults(func=cmd_resume_parse)
    rdefault = resume_sub.add_parser(
        "default", help="set the role used when a job has no tailoring of its own"
    )
    rdefault.add_argument("label", help="an existing role")
    rdefault.add_argument("--user", help="user id or email")
    rdefault.set_defaults(func=cmd_resume_default)
    rbases = resume_sub.add_parser("bases", help="list role resumes (* = default)")
    rbases.add_argument("--user", help="user id or email")
    rbases.set_defaults(func=cmd_resume_bases)

    ask = sub.add_parser(
        "ask",
        help="answer a free-text application question for a job",
        description="Answers in the candidate's voice from the job's captured JD "
        "and the user's base resume, via ai-assist (needs OPENROUTER_API_KEY in "
        "ai-assist/dev.env). Capped at --words, never uses em dashes. The answer "
        "goes to stdout so it can be piped or copied; the word count and the base "
        "resume used go to stderr. By default the base is whichever one the job was "
        "tailored from (autoapply tailor --label ...), so a role resume's experience "
        "is not denied; --label overrides it, and --label default forces your "
        "default role.",
    )
    ask.add_argument("job", help="job id, id prefix, or URL")
    ask.add_argument("question", help="the question, or '-' to read it from stdin")
    ask.add_argument(
        "--words", type=int, default=100, help="word cap (default 100)"
    )
    ask.add_argument(
        "--label",
        help="role resume to answer from (default: the one this job was tailored "
        "from; 'default' forces your default role)",
    )
    ask.add_argument("--user", help="user id or email")
    ask.add_argument(
        "-q", "--quiet", action="store_true", help="suppress the word-count line"
    )
    ask.set_defaults(func=cmd_ask)

    cov = sub.add_parser(
        "cover",
        help="write a cover letter for a job and attach it to the job",
        description="Writes a cover letter from the job's captured JD and your base "
        "resume via ai-assist (needs OPENROUTER_API_KEY in ai-assist/dev.env; one "
        "paid call), renders it to PDF and stores it with the job, replacing any "
        "earlier letter for it. The letter goes to stdout; the word count, the base "
        "resume used and the download path go to stderr. The base follows the job's "
        "tailoring like `ask`. `python -m tools.export_applications` copies the PDF "
        "next to the job's resume.",
    )
    cov.add_argument("job", help="job id, id prefix, or URL")
    cov.add_argument(
        "--words", type=int, default=350, help="word cap for the body (default 350)"
    )
    cov.add_argument(
        "--label",
        help="role resume to write from (default: the one this job was tailored "
        "from; 'default' forces your default role)",
    )
    cov.add_argument("--user", help="user id or email")
    cov.add_argument(
        "--out", help="also download the PDF here (with --open, default output/cover/cli/<job_id>.pdf)"
    )
    cov.add_argument(
        "--open", action="store_true", help="download the PDF and open it"
    )
    cov.add_argument(
        "-q", "--quiet", action="store_true", help="suppress the summary line"
    )
    cov.set_defaults(func=cmd_cover)

    profile = sub.add_parser("profile", help="user onboarding + profile inspection")
    profile_sub = profile.add_subparsers(dest="profile_command", required=True)
    pinit = profile_sub.add_parser(
        "init",
        help="enter your facts (contact, address, work authorization, EEO, ...)",
        description="Asks for the facts every application wants, with autofilled "
        "defaults, and saves them for your user. Role resumes are separate: add each "
        "with `autoapply resume add FILE --label <role>`. Passing --docx here "
        "does both in one go: the resume is parsed (needs OPENROUTER_API_KEY; one paid "
        "call), prefills your contact, and is stored as role --label.",
    )
    pinit.add_argument(
        "--docx", help="a resume to parse and store as your first role (.docx, .odt or .md)"
    )
    pinit.add_argument(
        "--label", default="general", help="role for --docx (default: general)"
    )
    pinit.add_argument("--replace", action="store_true", help="overwrite that role's resume")
    pinit.add_argument("--email", help="your email (default: the one on --docx)")
    pinit.add_argument(
        "--model", help="OpenRouter model for parsing (default: OPENROUTER_MODEL)"
    )
    pinit.add_argument(
        "--yes", action="store_true", help="accept all defaults, no prompts"
    )
    pinit.set_defaults(func=_cmd_profile_init)
    pshow = profile_sub.add_parser("show", help="print your facts and role resumes")
    pshow.add_argument("--label", help="role whose resume content to show (default: default role)")
    pshow.add_argument("--user", help="user id or email")
    pshow.set_defaults(func=_cmd_profile_show)
    pexport = profile_sub.add_parser(
        "export",
        help="print the profile JSON for one role (facts + that role's resume)",
        description="The document a form filler consumes. The portal password is "
        "never included, only whether one is set.",
    )
    pexport.add_argument("--label", help="role (default: your default role)")
    pexport.add_argument("--out", help="write to this file instead of stdout")
    pexport.add_argument("--user", help="user id or email")
    pexport.set_defaults(func=_cmd_profile_export)

    gmail = sub.add_parser("gmail", help="Gmail access for 2FA emails during apply")
    gmail_sub = gmail.add_subparsers(dest="gmail_command", required=True)
    connect = gmail_sub.add_parser(
        "connect",
        help="grant gmail.readonly via Google OAuth (needs GMAIL_CLIENT_ID/SECRET in .env)",
    )
    connect.add_argument("--user", help="user id or email")
    connect.add_argument(
        "--no-open",
        action="store_true",
        help="print the consent URL instead of opening a browser",
    )
    connect.set_defaults(func=cmd_gmail_connect)
    gstatus = gmail_sub.add_parser("status", help="show the connected Gmail account")
    gstatus.add_argument("--user", help="user id or email")
    gstatus.set_defaults(func=cmd_gmail_status)

    apply_p = sub.add_parser("apply", help="run the apply agent for job(s)")
    apply_p.add_argument("jobs", nargs="*", help="job ids, id prefixes, or URLs")
    apply_p.add_argument(
        "--all",
        action="store_true",
        help="batch: apply to every jd_ready job (skips already-submitted), one by one",
    )
    apply_p.add_argument(
        "--today",
        action="store_true",
        help="restrict the batch to jobs added today (implies --all)",
    )
    apply_p.add_argument("--user", help="user id or email")
    apply_p.add_argument(
        "--model", help="OpenRouter model (default: OPENROUTER_MODEL env); with --engine det also "
        "jev / jev-latest: TypeSafe Jev picks options and profile values (TYPESAFE_API_KEY) and writes nothing"
    )
    apply_p.add_argument(
        "--autosubmit",
        action="store_true",
        help="agent clicks the final Submit itself (no review pause, no confirm prompt)",
    )
    apply_p.add_argument(
        "--engine",
        choices=("agent", "det", "det+agent"),
        default="agent",
        help="agent: the LLM browser agent (default). det: the deterministic filler — "
        "profile answers, saved answers and `ask` first, one model call per page only "
        "for fields still unanswered; stops at captchas/codes and before Submit "
        "unless --autosubmit. det+agent: det first; when it stops short (a field or "
        "control it cannot handle, an account step) the agent finishes the same page "
        "from det's handover. Captchas, emailed codes and AI-use questions stay yours",
    )
    apply_p.add_argument(
        "--mcp",
        help="Playwright MCP URL for --engine det (default: DET_PLAYWRIGHT_MCP_URL or "
        "http://localhost:8932/mcp, the proxy that owns the agent browser)",
    )
    apply_p.set_defaults(func=cmd_apply)

    cont = sub.add_parser(
        "continue",
        help="finish a handed-off/failed apply run from the page still open in the browser",
        description="Resumes an interrupted apply run: the agent reads the current "
        "browser page, keeps what's already filled, and completes only the missing "
        "fields (resume upload, cleared dropdowns, ...). Requires a previous apply "
        "attempt for the job and an active page in the browser container.",
    )
    cont.add_argument("job", help="job id, id prefix, or URL")
    cont.add_argument("--user", help="user id or email")
    cont.add_argument(
        "--model", help="OpenRouter model (default: OPENROUTER_MODEL env); with --engine det also "
        "jev / jev-latest: TypeSafe Jev picks options and profile values (TYPESAFE_API_KEY) and writes nothing"
    )
    cont.add_argument(
        "--autosubmit",
        action="store_true",
        help="agent clicks the final Submit itself (no review pause, no confirm prompt)",
    )
    cont.add_argument(
        "--engine",
        choices=("agent", "det", "det+agent"),
        default="agent",
        help="agent: the LLM browser agent (default). det: the deterministic filler — "
        "profile answers, saved answers and `ask` first, one model call per page only "
        "for fields still unanswered; stops at captchas/codes and before Submit "
        "unless --autosubmit. det+agent: det first; when it stops short (a field or "
        "control it cannot handle, an account step) the agent finishes the same page "
        "from det's handover. Captchas, emailed codes and AI-use questions stay yours",
    )
    cont.add_argument(
        "--mcp",
        help="Playwright MCP URL for --engine det (default: DET_PLAYWRIGHT_MCP_URL or "
        "http://localhost:8932/mcp, the proxy that owns the agent browser)",
    )
    cont.set_defaults(func=cmd_continue)

    return p


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()

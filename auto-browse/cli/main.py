"""Unified `autoapply` CLI — one front door over the existing pipeline.

    autoapply jobs add [URL ...] [--file urls.txt]     # -> jobs table (source=manual)
    autoapply jobs list [--status S]
    autoapply tailor [--user U] [--retry-failed]       # jd-capture + drain tailor queue
    autoapply status [JOB] [--user U]                  # pipeline state + scores
    autoapply resume show JOB [--user U] [--out P] [--open]
    autoapply apply JOB [JOB ...] [--user U] [--model M]

JOB accepts a full job_id UUID, an unambiguous id prefix, or the job URL.

Command wrappers (cmd_*) own I/O: they open connections, print, and return exit
codes. The core functions take an injected connection so tests can drive them
against the throwaway jobs_test DB. Heavy imports (agent.loop, mcp) happen lazily
inside the commands that need them, so `jobs list` stays fast.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

from common import db, keys, queue, repo
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
        raise SystemExit("no active users; seed one: uv run python -m tools.seed_user --email ... --pdf ...")
    with conn.cursor() as cur:
        cur.execute("SELECT user_id, email FROM users WHERE is_active ORDER BY created_at")
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
                "SELECT * FROM jobs WHERE url_normalized = %s", (queue.url_normalize(ref),)
            )
            row = cur.fetchone()
            if not row:
                raise SystemExit(f"no job with url {ref!r} (add it: autoapply jobs add {ref})")
            return row

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
                "SELECT job_id, status FROM jobs WHERE url_normalized = %s",
                (queue.url_normalize(url),),
            )
            existing = cur.fetchone()
        job_id = repo.add_job_for_user(conn, user_id, url, source="manual")
        out.append(
            {
                "url": url,
                "job_id": job_id,
                "status": existing["status"] if existing else "jd_pending",
                "duplicate": existing is not None,
            }
        )
    return out


def cmd_jobs_add(args) -> int:
    urls = list(args.urls)
    if args.file:
        urls += parse_url_lines(Path(args.file).read_text())
    if not urls:
        raise SystemExit("no URLs given (pass URLs or --file urls.txt)")
    with _connect() as conn:
        user_id = resolve_user(conn, args.user)
        results = add_jobs(conn, user_id, urls)
    for r in results:
        dup = f"  (duplicate — already {r['status']})" if r["duplicate"] else ""
        print(f"{_id8(r['job_id'])}  {r['status']:<12} {r['url']}{dup}")
    return 0


def list_jobs(conn, user_id: str, status: str | None) -> list[dict]:
    return repo.list_jobs_for_user(conn, user_id, status)


def cmd_jobs_list(args) -> int:
    with _connect() as conn:
        user_id = resolve_user(conn, args.user)
        rows = list_jobs(conn, user_id, args.status)
    if not rows:
        print("no jobs" + (f" with status {args.status}" if args.status else ""))
        return 0
    for r in rows:
        title = (r["title"] or "")[:40]
        print(f"{_id8(r['job_id'])}  {r['status']:<12} {title:<40} {r['url']}")
    return 0


# ---------------------------------------------------------------------------
# tailor


def backfill_tailoring(conn, user_id: str) -> int:
    """Enqueue tailoring for owned jd_ready jobs this user has no row for (idempotent)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT j.job_id FROM jobs j "
            "JOIN user_jobs uj ON uj.job_id = j.job_id AND uj.user_id = %s "
            "WHERE j.status = 'jd_ready' AND NOT EXISTS "
            "(SELECT 1 FROM tailored_resumes t WHERE t.job_id = j.job_id AND t.user_id = %s)",
            (user_id, user_id),
        )
        job_ids = [r["job_id"] for r in cur.fetchall()]
    base = repo.get_base_asset(conn, user_id, variant="docx")  # tailoring edits the docx
    for job_id in job_ids:
        repo.enqueue_tailoring(conn, user_id, job_id, base["id"] if base else None)
    return len(job_ids)


def tailoring_summary(conn, user_id: str) -> dict:
    """Counts by status plus a per-job join, scoped to one user."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT j.status, count(*) AS n FROM jobs j "
            "JOIN user_jobs uj ON uj.job_id = j.job_id AND uj.user_id = %s "
            "GROUP BY j.status",
            (user_id,),
        )
        job_counts = {r["status"]: r["n"] for r in cur.fetchall()}
        cur.execute(
            "SELECT status, count(*) AS n FROM tailored_resumes WHERE user_id = %s GROUP BY status",
            (user_id,),
        )
        tailor_counts = {r["status"]: r["n"] for r in cur.fetchall()}
        cur.execute(
            "SELECT t.job_id, t.status, t.resume_score, t.new_resume_score, "
            "t.job_match_summary, t.error, j.title, j.url, j.status AS job_status, "
            "j.error AS job_error "
            "FROM tailored_resumes t JOIN jobs j USING (job_id) "
            "WHERE t.user_id = %s ORDER BY t.created_at DESC",
            (user_id,),
        )
        rows = cur.fetchall()
    return {"job_counts": job_counts, "tailor_counts": tailor_counts, "rows": rows}


def _print_summary(s: dict) -> None:
    jc, tc = s["job_counts"], s["tailor_counts"]
    print(f"\njobs: {jc or 'none'}   tailoring: {tc or 'none'}")
    for r in s["rows"]:
        name = (r["title"] or r["url"])[:50]
        score = ""
        if r["new_resume_score"] is not None:
            score = f"  score {r['resume_score']} -> {r['new_resume_score']}"
        err = f"  error: {r['error']}" if r["error"] else ""
        print(f"{_id8(r['job_id'])}  {r['status']:<9} {name}{score}{err}")
    stuck = (s["job_counts"].get("jd_capturing", 0), s["tailor_counts"].get("tailoring", 0))
    if any(stuck):
        print(
            f"warning: stuck in-progress rows (jd_capturing={stuck[0]}, tailoring={stuck[1]}) "
            "— likely a crashed worker; reset their status manually to retry"
        )


def run_tailor_worker() -> int:
    """Drain the tailoring queue via the ai-assist worker (one-shot). Subprocess
    because ai-assist has no build-system; cwd must be ai-assist/ — its env.py
    loads dev.env relative to CWD."""
    proc = subprocess.run(
        ["uv", "run", "python", "worker.py"],
        cwd=AI_ASSIST_DIR,
        env={**os.environ, "TAILOR_ONCE": "1"},
    )
    return proc.returncode


def cmd_tailor(args) -> int:
    with _connect() as conn:
        user_id = resolve_user(conn, args.user)

        if args.retry_failed:
            with conn.cursor() as cur:
                cur.execute("UPDATE jobs SET status = 'jd_pending', error = NULL WHERE status = 'failed'")
                n_jobs = cur.rowcount
                cur.execute(
                    "UPDATE tailored_resumes SET status = 'pending', error = NULL "
                    "WHERE status = 'failed' AND user_id = %s",
                    (user_id,),
                )
                n_rows = cur.rowcount
            conn.commit()
            print(f"reset {n_jobs} failed jobs, {n_rows} failed tailoring rows")

        # Phase 1 — capture JDs for pending jobs (needs playwright+proxy containers).
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM jobs WHERE status = 'jd_pending'")
            n_pending = cur.fetchone()["n"]
        if n_pending:
            from agent import jd_capture

            storage = _storage()

            async def _drain_capture() -> None:
                async with jd_capture.proxy_navigator() as navigate:
                    while True:
                        job_id = await jd_capture.capture_once(conn, navigate, storage, worker_id="cli")
                        if job_id is None:
                            return
                        with conn.cursor() as cur:
                            cur.execute(
                                "SELECT status, error, title, url FROM jobs WHERE job_id = %s",
                                (job_id,),
                            )
                            row = cur.fetchone()
                        outcome = row["status"] if row["status"] != "failed" else f"failed: {row['error']}"
                        print(f"capture {_id8(job_id)}  {(row['title'] or row['url'])[:60]}  ->  {outcome}")

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

        # Phase 2 — make sure every captured job has a tailoring row for this user.
        n_backfilled = backfill_tailoring(conn, user_id)
        if n_backfilled:
            print(f"enqueued tailoring for {n_backfilled} job(s)")

        # Phase 3 — drain the tailoring queue.
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM tailored_resumes WHERE status = 'pending'")
            n_queue = cur.fetchone()["n"]
        if n_queue:
            print(f"tailoring {n_queue} resume(s) via ai-assist worker...")
            rc = run_tailor_worker()
            if rc != 0:
                print(
                    f"tailor worker exited {rc}\n"
                    "hints: OPENROUTER_API_KEY set (env or ai-assist/dev.env)? "
                    "LibreOffice installed (docx->pdf)?",
                    file=sys.stderr,
                )

        s = tailoring_summary(conn, user_id)
        _print_summary(s)
        bad = s["tailor_counts"].get("failed", 0) + s["tailor_counts"].get("pending", 0)
        return 1 if bad else 0


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
            print(f"status  {job['status']}" + (f"  error: {job['error']}" if job["error"] else ""))
            if not t:
                print("tailoring: not enqueued (run: autoapply tailor)")
                return 0
            print(f"tailoring  {t['status']}" + (f"  error: {t['error']}" if t["error"] else ""))
            if t["new_resume_score"] is not None:
                print(f"score      {t['resume_score']} -> {t['new_resume_score']}")
            if t["job_match_summary"]:
                print(f"match      {t['job_match_summary']}")
            return 0
        _print_summary(tailoring_summary(conn, user_id))
        return 0


def show_resume(conn, storage, user_id: str, job_id: str, out: str | None) -> str:
    """Download the tailored PDF for user×job; returns the local path."""
    t = repo.lookup_tailored(conn, user_id, job_id)
    if not t or t["status"] != "ready" or not t["tailored_asset_id"]:
        state = t["status"] if t else "not enqueued"
        err = f" ({t['error']})" if t and t.get("error") else ""
        raise SystemExit(f"no tailored resume for job {_id8(job_id)}: {state}{err} — run: autoapply tailor")
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


# ---------------------------------------------------------------------------
# gmail (2FA email access for the apply agent)


def cmd_gmail_connect(args) -> int:
    from agent import email_otp

    with _connect() as conn:
        user_id = resolve_user(conn, args.user)
        address, refresh_token, scopes = email_otp.run_oauth_flow(open_browser=not args.no_open)
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
    print(f"gmail: connected as {tok['gmail_address'] or '(address unknown)'} "
          f"(scopes: {', '.join(tok['scopes'])}; updated {tok['updated_at']})")
    return 0


# ---------------------------------------------------------------------------
# apply


def cmd_apply(args) -> int:
    from agent import loop
    from agent.observability import new_run_id

    model = args.model or loop.DEFAULT_MODEL
    failed = 0
    with _connect() as conn:
        user_id = resolve_user(conn, args.user)
        for ref in args.jobs:
            if "://" in ref:  # URL: create on the fly, like agent.loop does
                job_id = repo.add_job_for_user(conn, user_id, ref, source="manual")
                url = ref
            else:
                job = resolve_job(conn, ref)
                job_id, url = job["job_id"], job["url"]
                # applying implies ownership — link it (idempotent)
                repo.add_job_for_user(conn, user_id, url, source="manual")
            tailored = repo.lookup_tailored(conn, user_id, job_id)
            run_id = new_run_id()
            app_id = repo.start_application(
                conn, user_id, job_id, run_id=run_id,
                tailored_resume_id=tailored["id"] if tailored else None,
            )
            print(f"applying {_id8(job_id)}  {url}")
            print("  watch + confirm final submit at http://localhost:6080/vnc.html")
            try:
                asyncio.run(loop.main(user_id, url, model, run_id=run_id))
            except Exception as exc:
                repo.finish_application(conn, app_id, "failed", outcome=str(exc)[:500])
                print(f"apply failed for {_id8(job_id)}: {exc}", file=sys.stderr)
                failed += 1
            else:
                repo.finish_application(conn, app_id, "finished")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autoapply", description="Job application pipeline CLI")
    sub = p.add_subparsers(dest="command", required=True)

    jobs = sub.add_parser("jobs", help="manage job postings")
    jobs_sub = jobs.add_subparsers(dest="jobs_command", required=True)
    add = jobs_sub.add_parser("add", help="add job URLs to the pipeline")
    add.add_argument("urls", nargs="*", help="job posting URLs")
    add.add_argument("--file", help="file with one URL per line (# comments ok)")
    add.add_argument("--user", help="user id or email (default: the only active user)")
    add.set_defaults(func=cmd_jobs_add)
    lst = jobs_sub.add_parser("list", help="list your jobs")
    lst.add_argument("--status", choices=_JOB_STATUSES)
    lst.add_argument("--user", help="user id or email (default: the only active user)")
    lst.set_defaults(func=cmd_jobs_list)

    tailor = sub.add_parser(
        "tailor",
        help="capture JDs + tailor resumes for all pending jobs",
        description="Captures JDs for jd_pending jobs (needs the playwright+proxy "
        "containers), then drains the tailoring queue via the ai-assist worker "
        "(needs OPENROUTER_API_KEY + LibreOffice). Note: the worker drains ALL "
        "users' rows; --user only scopes the backfill and summary.",
    )
    tailor.add_argument("--user", help="user id or email (default: the only active user)")
    tailor.add_argument("--retry-failed", action="store_true", help="reset failed rows and retry them")
    tailor.set_defaults(func=cmd_tailor)

    status = sub.add_parser("status", help="pipeline status + scores")
    status.add_argument("job", nargs="?", help="job id, id prefix, or URL (full detail)")
    status.add_argument("--user", help="user id or email")
    status.set_defaults(func=cmd_status)

    resume = sub.add_parser("resume", help="tailored resume helpers")
    resume_sub = resume.add_subparsers(dest="resume_command", required=True)
    show = resume_sub.add_parser("show", help="download the tailored resume PDF")
    show.add_argument("job", help="job id, id prefix, or URL")
    show.add_argument("--user", help="user id or email")
    show.add_argument("--out", help="destination path (default output/resume/cli/<job_id>.pdf)")
    show.add_argument("--open", action="store_true", help="open with xdg-open")
    show.set_defaults(func=cmd_resume_show)

    gmail = sub.add_parser("gmail", help="Gmail access for 2FA emails during apply")
    gmail_sub = gmail.add_subparsers(dest="gmail_command", required=True)
    connect = gmail_sub.add_parser(
        "connect",
        help="grant gmail.readonly via Google OAuth (needs GMAIL_CLIENT_ID/SECRET in .env)",
    )
    connect.add_argument("--user", help="user id or email")
    connect.add_argument("--no-open", action="store_true", help="print the consent URL instead of opening a browser")
    connect.set_defaults(func=cmd_gmail_connect)
    gstatus = gmail_sub.add_parser("status", help="show the connected Gmail account")
    gstatus.add_argument("--user", help="user id or email")
    gstatus.set_defaults(func=cmd_gmail_status)

    apply_p = sub.add_parser("apply", help="run the apply agent for job(s)")
    apply_p.add_argument("jobs", nargs="+", help="job ids, id prefixes, or URLs")
    apply_p.add_argument("--user", help="user id or email")
    apply_p.add_argument("--model", help="OpenRouter model (default: OPENROUTER_MODEL env)")
    apply_p.set_defaults(func=cmd_apply)

    return p


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()

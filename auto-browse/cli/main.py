"""Unified `autoapply` CLI — one front door over the existing pipeline.

    autoapply jobs add [URL ...] [--file urls.txt]     # -> jobs table (source=manual)
    autoapply jobs list [--status S]
    autoapply tailor [--user U] [--retry-failed]       # jd-capture + drain tailor queue
    autoapply retry [--all] [--user U]                 # reset today's failed JD fetches + recapture
    autoapply status [JOB] [--user U]                  # pipeline state + scores
    autoapply resume show JOB [--user U] [--out P] [--open]
    autoapply ask JOB "question" [--words N] [--user U]   # answer -> stdout, count -> stderr
    autoapply apply [JOB ...] [--all] [--today] [--user U] [--model M] [--autosubmit]
                                                       # --all: batch every jd_ready job one by one;
                                                       # --today restricts to jobs added today
    autoapply continue JOB [--autosubmit]              # finish an interrupted run in the open browser

JOB accepts a full job_id UUID, an unambiguous id prefix, or the job URL.

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
from datetime import datetime
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
            "no active users; seed one: uv run python -m tools.seed_user --email ... --pdf ..."
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
        stored = queue.canonical_url(url)
        out.append(
            {
                "url": stored,
                "job_id": job_id,
                "status": existing["status"] if existing else "jd_pending",
                "duplicate": existing is not None,
                # apply-form URLs get trimmed to the posting page — say so, otherwise
                # the printed URL silently differs from what was pasted
                "trimmed": stored != url.strip(),
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


def _today_start() -> datetime:
    """Local midnight, tz-aware (job timestamps are UTC; 'today' means the user's day)."""
    return (
        datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    )


def _apply_marker(row: dict) -> str:
    """Human word for the latest apply attempt: applied / unconfirmed /
    handed-off / apply-failed."""
    status, outcome = row.get("apply_status"), row.get("apply_outcome")
    if status is None:
        return ""
    if outcome == "submitted":
        return "applied"
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
        title = (r["title"] or "")[:40]
        print(
            f"{_id8(r['job_id'])}  {r['status']:<12} {_apply_marker(r):<12} {title:<40} {r['url']}"
        )
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
    base = repo.get_base_asset(
        conn, user_id, variant="docx"
    )  # tailoring edits the docx
    for job_id in job_ids:
        repo.enqueue_tailoring(conn, user_id, job_id, base["id"] if base else None)
    return len(job_ids)


def tailoring_summary(conn, user_id: str, since: datetime | None = None) -> dict:
    """Counts by status plus a per-job join, scoped to one user; `since` limits
    everything to jobs the user added on/after it (user_jobs.added_at)."""
    added = " AND uj.added_at >= %s" if since is not None else ""
    since_params = (since,) if since is not None else ()
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
            "t.job_match_summary, t.error, j.title, j.url, j.status AS job_status, "
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


def _print_summary(s: dict) -> None:
    jc, tc = s["job_counts"], s["tailor_counts"]
    print(f"\njobs: {jc or 'none'}   tailoring: {tc or 'none'}")
    for r in s["rows"]:
        name = (r["title"] or r["url"])[:50]
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


def run_jd_capture(conn) -> int:
    """Capture JDs for every jd_pending row (needs playwright+proxy containers).
    Prints one line per job; returns a shell exit code."""
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
                    conn, navigate, storage, worker_id="cli"
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

        if args.retry_failed:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE jobs SET status = 'jd_pending', error = NULL WHERE status = 'failed'"
                )
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
        rc = run_jd_capture(conn)
        if rc:
            return rc

        # Phase 2 — make sure every captured job has a tailoring row for this user.
        n_backfilled = backfill_tailoring(conn, user_id)
        if n_backfilled:
            print(f"enqueued tailoring for {n_backfilled} job(s)")

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
        _print_summary(
            tailoring_summary(conn, user_id, _today_start() if args.today else None)
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


# ---------------------------------------------------------------------------
# ask (free-text application questions)


def run_answer(user_id: str, job_id: str, question: str, max_words: int) -> dict:
    """Answer one application question via ai-assist. Subprocess for the same reason
    as run_tailor_worker: ai-assist has no build-system and its env.py reads dev.env
    relative to CWD. OPENROUTER_* is stripped for the same reason too — this package's
    creds/base-url must not leak into ai-assist's own LLM config.

    Returns the parsed JSON reply: {"answer", "word_count"} or {"error"}."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENROUTER_")}
    payload = {
        "user_id": str(user_id),
        "job_id": str(job_id),
        "question": question,
        "max_words": max_words,
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

    result = run_answer(user_id, job["job_id"], question, args.words)
    if "error" in result:
        print(f"ask failed: {result['error']}", file=sys.stderr)
        return 1
    print(result["answer"])
    if not args.quiet:
        print(
            f"\n[{result['word_count']}/{args.words} words]",
            file=sys.stderr,
        )
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
) -> bool:
    """One agent run against one job, with the applications-row bookkeeping.
    Returns True on success."""
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
    print(f"{'continuing' if continue_run else 'applying'} {_id8(job_id)}  {url}")
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


# The autosubmit prompt makes the agent end its final reply with `SUBMITTED: yes`
# (confirmation page seen) or `SUBMITTED: no`. Take the LAST match — earlier text
# may discuss the format itself.
_SUBMIT_VERDICT_RE = re.compile(r"SUBMITTED:\s*(yes|no)\b", re.IGNORECASE)


def _autosubmit_outcome(final_text: str | None) -> str:
    """DB outcome from the agent's final message. No parsable verdict means the
    run ended without a clear confirmation — record it as such, never as
    submitted (the marker stays out of `applied` and batch mode will retry)."""
    hits = _SUBMIT_VERDICT_RE.findall(final_text or "")
    if not hits:
        return "unconfirmed"
    return "submitted" if hits[-1].lower() == "yes" else "not submitted"


def select_ready_jobs(conn, user_id: str, since: datetime | None) -> list[dict]:
    """Batch-apply selection: jd_ready jobs (added since `since` when given),
    minus the ones whose latest attempt already ended in a confirmed submit."""
    rows = repo.list_jobs_for_user(conn, user_id, "jd_ready", since=since)
    return [r for r in rows if r.get("apply_outcome") != "submitted"]


def cmd_apply(args) -> int:
    from agent import loop

    batch = args.all or args.today
    if not args.jobs and not batch:
        raise SystemExit("pass job(s), or --all / --today to batch through ready jobs")

    model = args.model or loop.DEFAULT_MODEL
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
            if not _run_apply_agent(conn, user_id, job_id, url, model, args.autosubmit):
                failed += 1
    return 1 if failed else 0


def cmd_continue(args) -> int:
    """Resume a partially filled application still open in the browser: the agent
    reads the current page and completes only what's missing (no navigation)."""
    from agent import loop

    model = args.model or loop.DEFAULT_MODEL
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
        "files already in object storage are left in place.",
    )
    rm.add_argument("jobs", nargs="+", help="job ids, id prefixes, or URLs")
    rm.add_argument("--user", help="user id or email (default: the only active user)")
    rm.set_defaults(func=cmd_jobs_rm)
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
        "users' rows; --user only scopes the backfill and summary.",
    )
    tailor.add_argument(
        "--user", help="user id or email (default: the only active user)"
    )
    tailor.add_argument(
        "--retry-failed", action="store_true", help="reset failed rows and retry them"
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
    status.add_argument("--today", action="store_true", help="only jobs added today")
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

    ask = sub.add_parser(
        "ask",
        help="answer a free-text application question for a job",
        description="Answers in the candidate's voice from the job's captured JD "
        "and the user's base resume, via ai-assist (needs OPENROUTER_API_KEY in "
        "ai-assist/dev.env). Capped at --words, never uses em dashes. The answer "
        "goes to stdout so it can be piped or copied; the word count goes to stderr.",
    )
    ask.add_argument("job", help="job id, id prefix, or URL")
    ask.add_argument("question", help="the question, or '-' to read it from stdin")
    ask.add_argument(
        "--words", type=int, default=100, help="word cap (default 100)"
    )
    ask.add_argument("--user", help="user id or email")
    ask.add_argument(
        "-q", "--quiet", action="store_true", help="suppress the word-count line"
    )
    ask.set_defaults(func=cmd_ask)

    profile = sub.add_parser("profile", help="user onboarding + profile inspection")
    profile_sub = profile.add_subparsers(dest="profile_command", required=True)
    pinit = profile_sub.add_parser(
        "init",
        help="interactive onboarding: parse a resume, review extracted fields, save",
        description="Extracts contact/experience/education/skills from the resume "
        "via LLM (needs OPENROUTER_API_KEY), asks for the rest with autofilled "
        "defaults, then writes the profile and uploads the base resume assets.",
    )
    pinit.add_argument(
        "--pdf",
        help="base resume PDF (uploaded for applications; "
        "derived from --docx via LibreOffice when omitted)",
    )
    pinit.add_argument(
        "--docx",
        help="same resume as DOCX (tailoring source; also preferred for parsing)",
    )
    pinit.add_argument("--email", help="override the email extracted from the resume")
    pinit.add_argument(
        "--model", help="OpenRouter model for parsing (default: OPENROUTER_MODEL)"
    )
    pinit.add_argument(
        "--yes", action="store_true", help="accept all defaults, no prompts"
    )
    pinit.set_defaults(func=_cmd_profile_init)
    pshow = profile_sub.add_parser("show", help="print the stored profile")
    pshow.add_argument("--user", help="user id or email")
    pshow.set_defaults(func=_cmd_profile_show)

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
        "--model", help="OpenRouter model (default: OPENROUTER_MODEL env)"
    )
    apply_p.add_argument(
        "--autosubmit",
        action="store_true",
        help="agent clicks the final Submit itself (no review pause, no confirm prompt)",
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
        "--model", help="OpenRouter model (default: OPENROUTER_MODEL env)"
    )
    cont.add_argument(
        "--autosubmit",
        action="store_true",
        help="agent clicks the final Submit itself (no review pause, no confirm prompt)",
    )
    cont.set_defaults(func=cmd_continue)

    return p


def main() -> None:
    args = build_parser().parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()

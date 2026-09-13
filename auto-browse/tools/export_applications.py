"""Export every ready tailored resume to a folder tree you can apply from by hand.

Pulls the PDFs out of MinIO and lays them out one directory per role, then per company:

    applications/2026-07-24/swe/nike/jane_doe.pdf
                                    /job.txt      # url, scores, what was tailored
                               /checkr/jane_doe.pdf
                                      /job.txt
                               /index.md          # every posting in this role
                           /de/databricks/...

The role folder is the label of the base resume the tailoring came from
(`autoapply resume add <docx> --label swe`). Tailorings whose base resume row is gone
(or that predate base tracking) go under `unknown-role/`.
Each role gets its own index.md, so exporting a second label into the same date
folder adds to the tree instead of overwriting the first one's index.

Only resumes tailored on the target day are exported, so each date folder holds that
day's batch and nothing else. The day is your local calendar day, not UTC. Pass --all
to dump every ready resume into one folder instead.

No containers, no agent — just Postgres + MinIO reads. Run from auto-browse/:

    uv run python -m tools.export_applications
    uv run python -m tools.export_applications --filename resume.pdf
    uv run python -m tools.export_applications --root ~/apply --date 2026-07-24
    uv run python -m tools.export_applications --label swe   # just that role
    uv run python -m tools.export_applications --all        # everything, ever

Folder names come from `jobs.company`, which the tailor worker fills from the JD.
Jobs tailored before that existed have it NULL, so the name is derived from the
posting URL instead (ATS-aware); where the ATS hides the employer behind a generic
host (paylocity, brassring, oracle), it falls back to `<host>-<id8>` — rename it,
or re-run `autoapply tailor --retry-failed` to get the real name.
"""

from __future__ import annotations

import argparse
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlparse

from common import db, repo
from common import storage as storagemod

# host -> the path segment that holds the company (0-indexed, after the leading /)
_COMPANY_IN_PATH = {
    "job-boards.greenhouse.io": 0,
    "boards.greenhouse.io": 0,
    "jobs.lever.co": 0,
    "ats.rippling.com": 0,
    "jobs.ashbyhq.com": 0,
}

# Generic ATS hosts that never name the employer — fall back to <host>-<id8>.
_OPAQUE_HOSTS = {
    "recruiting.paylocity.com",
    "sjobs.brassring.com",
    "oraclecloud.com",
    "myworkdaysite.com",
    "taleo.net",
}

_STRIP_PREFIXES = ("careers.", "jobs.", "job.", "search.", "searchjobs.", "www.", "apply.")
_STRIP_SUFFIXES = (".com", ".org", ".net", ".io", ".co", ".inc")

# Folder (and --label value) for tailorings with no base resume row to name a role:
# base_asset_id is NULL (the base was deleted, or the row predates base tracking).
# Every existing base names its role, so this is never a real role's folder.
UNLABELLED = "unknown-role"


def slug(text: str) -> str:
    """Lowercase, filesystem-safe, no runs of separators."""
    out = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return out or "unknown"


def company_from_url(url: str, job_id: str) -> str:
    """Best-effort employer name from a posting URL. Never raises, never empty."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    segments = [s for s in parsed.path.split("/") if s]

    idx = _COMPANY_IN_PATH.get(host)
    if idx is not None and len(segments) > idx:
        return slug(segments[idx])

    # <company>.applytojob.com, <company>.wd3.myworkdayjobs.com, <company>.icims.com
    for ats in (".applytojob.com", ".myworkdayjobs.com", ".icims.com", ".bamboohr.com"):
        if host.endswith(ats):
            return slug(host[: -len(ats)].split(".")[0])

    if any(host.endswith(h) or host == h for h in _OPAQUE_HOSTS):
        label = host.split(".")[-2] if "." in host else host
        return f"{slug(label)}-{job_id[:8]}"

    for prefix in _STRIP_PREFIXES:
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    host = host.split(".")[0] if host.count(".") > 1 else host
    for suffix in _STRIP_SUFFIXES:
        if host.endswith(suffix):
            host = host[: -len(suffix)]
            break
    return slug(host) or f"job-{job_id[:8]}"


def local_day_bounds(day: date) -> tuple[datetime, datetime]:
    """[start, end) of `day` in the machine's local timezone, as aware datetimes.

    `updated_at` is timestamptz stored in UTC, so comparing `updated_at::date` would
    slice on UTC midnight — a resume tailored at 23:12 PDT lands on the *next* UTC day
    and would show up in the wrong folder. Comparing against local-midnight bounds
    keeps the folder matching the day you actually worked."""
    start = datetime.combine(day, time.min).astimezone()
    return start, start + timedelta(days=1)


def ready_rows(
    conn, user_id: str, day: date | None, label: str | None = None
) -> list[dict]:
    """Ready tailorings for this user, newest first, joined to their posting.

    `day` limits to resumes tailored on that local calendar day; None exports all.
    `label` limits to tailorings made from that role's base resume; None exports
    every role. `label=UNLABELLED` selects the ones with no base row at all, which is
    why the join is a LEFT JOIN and the filter tests IS NULL rather than comparing
    the string.

    Each row carries `label`, the role folder it belongs in."""
    clauses = [
        "t.user_id = %s",
        "t.status = 'ready'",
        "t.tailored_asset_id IS NOT NULL",
    ]
    params: list = [user_id]
    if day is not None:
        start, end = local_day_bounds(day)
        clauses.append("t.updated_at >= %s AND t.updated_at < %s")
        params += [start, end]
    if label == UNLABELLED:
        clauses.append("ra.label IS NULL")
    elif label is not None:
        clauses.append("ra.label = %s")
        params.append(label)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT t.job_id, t.tailored_asset_id, t.resume_score, t.new_resume_score, "
            "       t.job_match_summary, j.url, j.title, j.company, ra.label "
            "FROM tailored_resumes t JOIN jobs j USING (job_id) "
            # LEFT so a tailoring whose base row is gone still exports (under unknown-role/)
            "LEFT JOIN resume_assets ra ON ra.id = t.base_asset_id "
            f"WHERE {' AND '.join(clauses)} "  # noqa: S608 — clauses are literals, values parameterized
            "ORDER BY t.updated_at DESC",
            params,
        )
        return cur.fetchall()


def default_filename(conn, user_id: str) -> str:
    """`jane_doe.pdf` from the user's full name — recruiters see the filename."""
    with conn.cursor() as cur:
        cur.execute("SELECT full_name, email FROM users WHERE user_id = %s", (user_id,))
        row = cur.fetchone()
    if not row:
        return "resume.pdf"
    name = row["full_name"] or (row["email"] or "").split("@")[0]
    parts = slug(name).split("-")
    # "Jane Quinn Doe" -> jane_doe
    keep = [parts[0], parts[-1]] if len(parts) > 1 else parts
    return "_".join(keep) + ".pdf"


def export(
    conn,
    storage,
    user_id: str,
    root: Path,
    filename: str,
    day: date | None,
    label: str | None = None,
) -> list[tuple[str, Path]]:
    """Write <root>/<role>/<company>/{filename,job.txt} plus a per-role index.md.

    Returns (role, path) per exported resume. The index is written per role, not at
    the root, so exporting one label never rewrites another label's index — you can
    run this once per role into the same date folder."""
    rows = ready_rows(conn, user_id, day, label)
    if not rows:
        return []
    root.mkdir(parents=True, exist_ok=True)
    exported: list[tuple[str, Path]] = []
    # per role: the company folder names already taken, and the index lines so far
    used: dict[str, set[str]] = {}
    index_lines: dict[str, list[str]] = {}

    for row in rows:
        job_id = str(row["job_id"])
        role = slug(row["label"]) if row["label"] else UNLABELLED
        # jobs.company is filled by the tailor worker (it reads the employer out of
        # the JD); rows tailored before that landed fall back to the URL guess.
        company = slug(row["company"]) if row["company"] else company_from_url(row["url"], job_id)
        taken = used.setdefault(role, set())
        if company in taken:  # two postings at one employer, same role
            company = f"{company}-{job_id[:8]}"
        taken.add(company)

        folder = root / role / company
        folder.mkdir(parents=True, exist_ok=True)
        asset = repo.get_resume_asset(conn, row["tailored_asset_id"])
        dest = folder / filename
        storage.get_file(asset["s3_key"], str(dest))

        (folder / "job.txt").write_text(
            f"company   {company}\n"
            f"role      {role}\n"
            f"title     {row['title'] or '-'}\n"
            f"url       {row['url']}\n"
            f"job_id    {job_id}\n"
            f"score     {row['resume_score']} -> {row['new_resume_score']}\n"
            f"resume    {dest.name}\n\n"
            f"{row['job_match_summary'] or ''}\n"
        )
        index_lines.setdefault(role, []).append(
            f"- **{company}** — [{row['url']}]({row['url']}) — "
            f"score {row['resume_score']}→{row['new_resume_score']} — `{company}/{filename}`"
        )
        exported.append((f"{role}/{company}", dest))

    for role, lines in index_lines.items():
        (root / role / "index.md").write_text(
            f"# Applications — {root.name} — {role}\n\n" + "\n".join(lines) + "\n"
        )
    return exported


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--user", help="user id or email (default: the only active user)")
    ap.add_argument("--root", default="applications", help="export root (default: applications/)")
    ap.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="export resumes tailored on this local day, into that date folder "
        "(default: today)",
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help="export every ready resume regardless of when it was tailored "
        "(still written under the --date folder)",
    )
    ap.add_argument(
        "--label",
        help="only export tailorings made from this role's base resume "
        f"(e.g. swe; {UNLABELLED!r} = tailorings whose base resume is gone). "
        "Default: every role, each in its own folder.",
    )
    ap.add_argument("--filename", help="resume filename (default: from the user's full name)")
    args = ap.parse_args()

    try:
        day = date.fromisoformat(args.date)
    except ValueError:
        raise SystemExit(f"--date must be YYYY-MM-DD, got {args.date!r}")

    with db.connect() as conn:
        user_id = _resolve_user(conn, args.user)
        filename = args.filename or default_filename(conn, user_id)
        root = Path(args.root).expanduser() / args.date
        exported = export(
            conn,
            storagemod.from_env(),
            user_id,
            root,
            filename,
            None if args.all else day,
            args.label,
        )

    if not exported:
        scope = "ready tailored resumes" if args.all else f"resumes tailored on {args.date}"
        if args.label:
            scope += f" from the {args.label!r} base"
        print(f"nothing to export — no {scope} (run: autoapply tailor, or pass --all)")
        return 1
    for role_company, dest in exported:
        print(f"{role_company:<36} {dest}")
    roles = sorted({rc.split("/")[0] for rc, _ in exported})
    print(f"\n{len(exported)} resume(s) -> {root}")
    for role in roles:
        print(f"index: {root / role / 'index.md'}")
    return 0


def _resolve_user(conn, user_flag: str | None) -> str:
    if user_flag and "@" not in user_flag:
        return user_flag
    if user_flag:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM users WHERE email = %s", (user_flag,))
            row = cur.fetchone()
        if not row:
            raise SystemExit(f"no user with email {user_flag!r}")
        return str(row["user_id"])
    ids = repo.active_user_ids(conn)
    if len(ids) != 1:
        raise SystemExit(f"expected exactly one active user, found {len(ids)} — pass --user")
    return str(ids[0])


if __name__ == "__main__":
    raise SystemExit(main())

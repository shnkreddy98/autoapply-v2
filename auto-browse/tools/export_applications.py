"""Export every ready tailored resume to a folder tree you can apply from by hand.

Pulls the PDFs out of MinIO and lays them out one directory per company:

    applications/2026-07-24/nike/jane_doe.pdf
                                /job.txt          # url, scores, what was tailored
                           /checkr/jane_doe.pdf
                                  /job.txt
                           /index.md             # every posting, one line each

No containers, no agent — just Postgres + MinIO reads. Run from auto-browse/:

    uv run python -m tools.export_applications
    uv run python -m tools.export_applications --filename resume.pdf
    uv run python -m tools.export_applications --root ~/apply --date 2026-07-24

Folder names come from `jobs.company`, which the tailor worker fills from the JD.
Jobs tailored before that existed have it NULL, so the name is derived from the
posting URL instead (ATS-aware); where the ATS hides the employer behind a generic
host (paylocity, brassring, oracle), it falls back to `<host>-<id8>` — rename it,
or re-run `autoapply tailor --retry-failed` to get the real name.
"""

from __future__ import annotations

import argparse
import re
from datetime import date
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


def ready_rows(conn, user_id: str) -> list[dict]:
    """Every ready tailoring for this user, newest first, joined to its posting."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT t.job_id, t.tailored_asset_id, t.resume_score, t.new_resume_score, "
            "       t.job_match_summary, j.url, j.title, j.company "
            "FROM tailored_resumes t JOIN jobs j USING (job_id) "
            "WHERE t.user_id = %s AND t.status = 'ready' AND t.tailored_asset_id IS NOT NULL "
            "ORDER BY t.updated_at DESC",
            (user_id,),
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


def export(conn, storage, user_id: str, root: Path, filename: str) -> list[tuple[str, Path]]:
    rows = ready_rows(conn, user_id)
    root.mkdir(parents=True, exist_ok=True)
    exported: list[tuple[str, Path]] = []
    used: set[str] = set()

    for row in rows:
        job_id = str(row["job_id"])
        # jobs.company is filled by the tailor worker (it reads the employer out of
        # the JD); rows tailored before that landed fall back to the URL guess.
        company = slug(row["company"]) if row["company"] else company_from_url(row["url"], job_id)
        if company in used:  # two postings at one employer
            company = f"{company}-{job_id[:8]}"
        used.add(company)

        folder = root / company
        folder.mkdir(parents=True, exist_ok=True)
        asset = repo.get_resume_asset(conn, row["tailored_asset_id"])
        dest = folder / filename
        storage.get_file(asset["s3_key"], str(dest))

        (folder / "job.txt").write_text(
            f"company   {company}\n"
            f"title     {row['title'] or '-'}\n"
            f"url       {row['url']}\n"
            f"job_id    {job_id}\n"
            f"score     {row['resume_score']} -> {row['new_resume_score']}\n"
            f"resume    {dest.name}\n\n"
            f"{row['job_match_summary'] or ''}\n"
        )
        exported.append((company, dest))

    index = "\n".join(
        f"- **{company}** — [{r['url']}]({r['url']}) — "
        f"score {r['resume_score']}→{r['new_resume_score']} — `{company}/{filename}`"
        for (company, _), r in zip(exported, rows)
    )
    (root / "index.md").write_text(f"# Applications — {root.name}\n\n{index}\n")
    return exported


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--user", help="user id or email (default: the only active user)")
    ap.add_argument("--root", default="applications", help="export root (default: applications/)")
    ap.add_argument("--date", default=date.today().isoformat(), help="date folder (default: today)")
    ap.add_argument("--filename", help="resume filename (default: from the user's full name)")
    args = ap.parse_args()

    with db.connect() as conn:
        user_id = _resolve_user(conn, args.user)
        filename = args.filename or default_filename(conn, user_id)
        root = Path(args.root).expanduser() / args.date
        exported = export(conn, storagemod.from_env(), user_id, root, filename)

    if not exported:
        print("nothing to export — no ready tailored resumes (run: autoapply tailor)")
        return 1
    for company, dest in exported:
        print(f"{company:<28} {dest}")
    print(f"\n{len(exported)} resume(s) -> {root}\nindex: {root / 'index.md'}")
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

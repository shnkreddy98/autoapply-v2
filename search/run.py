"""Cron/Airflow entrypoint: discover fresh job postings via Brave Search.

Usage:
    uv run python run.py --role "data engineer" --tier 1
    uv run python run.py --role "data engineer" --tier 2 --max-pages 2

Per site we page through results (each page = one billed query) and stop early
as soon as a page contains a posting we already have (same normalized URL in
`jobs`) — freshness is 24h and results repeat across runs, so a seen URL means
the rest of the pages are almost certainly stale too. A monthly query budget
(SEARCH_MONTHLY_QUERY_CAP, default 4000 ≈ $20) is enforced atomically in
Postgres so overlapping runs can't overspend.

Exit codes: 0 = ok (even if budget ran out mid-run), 2 = budget already
exhausted at start, 3 = config error (missing/bad API key).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import psycopg
from dotenv import load_dotenv

import brave
import usage
from common.termui import print  # noqa: A004 — colored on a terminal; plain under cron
from common import db, queue, repo
from enqueue import enqueue_jobs

DATA_DIR = Path(__file__).parent / "data"
TIER_FILES = {"1": ["sites_tier1.txt"], "2": ["sites_tier2.txt"],
              "all": ["sites_tier1.txt", "sites_tier2.txt"]}
REQUEST_GAP_S = 1.1  # stay polite; free-credit throughput is 1 rps


def load_sites(tier: str) -> list[str]:
    sites: list[str] = []
    for name in TIER_FILES[tier]:
        text = (DATA_DIR / name).read_text()
        sites.extend(line.strip() for line in text.splitlines() if line.strip())
    return sites


def split_seen(conn: psycopg.Connection, results: list[dict]) -> tuple[list[dict], int]:
    """Partition a result page into (new, seen_count) against the jobs table."""
    norms = {queue.url_normalize(r["url"]): r for r in results if (r.get("url") or "").strip()}
    if not norms:
        return [], 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT url_normalized FROM jobs WHERE url_normalized = ANY(%s)",
            (list(norms),),
        )
        seen = {row["url_normalized"] for row in cur.fetchall()}
    new = [r for norm, r in norms.items() if norm not in seen]
    return new, len(seen)


def resolve_owner(conn: psycopg.Connection) -> str | None:
    actives = repo.active_user_ids(conn)
    if len(actives) == 1:
        return actives[0]
    print("warning: not exactly one active user — jobs get no owner, so no tailoring fan-out")
    return None


def run(role: str, location: str, tier: str, max_pages: int, cap: int, freshness: str = "pd") -> int:
    conn = db.connect()
    try:
        usage.ensure_table(conn)
        if usage.used_this_month(conn) >= cap:
            print(f"budget exhausted: {cap} queries already used this month, refusing to search")
            return 2

        owner = resolve_owner(conn)
        sites = load_sites(tier)
        queries_spent = new_jobs = 0
        budget_hit = False

        for site in sites:
            query = brave.build_query(role, location, site)
            for page in range(min(max_pages, brave.MAX_OFFSET + 1)):
                used = usage.try_consume(conn, cap)
                if used is None:
                    budget_hit = True
                    break
                queries_spent += 1
                results = brave.search_page(query, offset=page, freshness=freshness)
                time.sleep(REQUEST_GAP_S)
                if not results:
                    break
                new, seen_count = split_seen(conn, results)
                if new:
                    enqueue_jobs(new, conn=conn, user_id=owner)
                    new_jobs += len(new)
                print(f"{site} p{page}: {len(new)} new, {seen_count} seen")
                if seen_count:  # cutoff: hit already-known postings, deeper pages are stale
                    break
                if len(results) < brave.PAGE_SIZE:  # short page = no more results
                    break
            if budget_hit:
                print(f"budget cap ({cap}) reached mid-run — stopping; done sites keep their results")
                break

        print(
            f"\ndone: tier={tier} sites={len(sites)} queries_spent={queries_spent} "
            f"new_jobs={new_jobs} month_total={usage.used_this_month(conn)}/{cap}"
        )
        return 0
    finally:
        conn.close()


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Brave job-posting discovery (24h window)")
    parser.add_argument("--role", required=True, help='e.g. "data engineer"')
    parser.add_argument("--location", default="United States")
    parser.add_argument("--tier", choices=list(TIER_FILES), default="1",
                        help="1 = major ATS every run, 2 = long-tail once daily")
    # "pd" (past day) matches the daily cron. A quiet day returns nothing at all
    # (2026-09-14: 8/8 sites empty for pd, results for pw), so a manual catch-up run
    # can widen the window; the seen-URL cutoff still stops at known postings.
    parser.add_argument("--freshness", choices=["pd", "pw", "pm"], default="pd",
                        help="Brave freshness window: pd=past day (default), pw=week, pm=month")
    parser.add_argument("--max-pages", type=int, default=3,
                        help="pagination depth per site (each page is one billed query)")
    args = parser.parse_args()

    if not os.getenv("BRAVE_API_KEY"):
        print("BRAVE_API_KEY not set (put it in search/.env)")
        return 3
    cap = int(os.getenv("SEARCH_MONTHLY_QUERY_CAP", "4000"))

    try:
        return run(args.role, args.location, args.tier, args.max_pages, cap, args.freshness)
    except brave.BraveAuthError as exc:
        print(f"config error: {exc}")
        return 3


if __name__ == "__main__":
    sys.exit(main())

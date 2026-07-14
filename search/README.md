# search

Discovers fresh job postings (24h window) via the Brave Search API and enqueues
them as `jd_pending` jobs in Postgres for the JD-capture worker.

## Entry point

```sh
uv run python run.py --role "data engineer" --tier 1            # major ATS
uv run python run.py --role "data engineer" --tier 2            # long-tail sites
uv run python run.py --role "data engineer" --tier all --max-pages 2
```

- `--tier 1` — `data/sites_tier1.txt`, 8 major ATS platforms (greenhouse, lever,
  ashby, workday, smartrecruiters, workable, icims, jobvite). Run several times a day.
- `--tier 2` — `data/sites_tier2.txt`, 32 long-tail / single-company domains. Run once daily.
- `--max-pages` — pagination depth per site (default 3). Each page is one billed query.

Exit codes: `0` ok, `2` monthly budget already exhausted, `3` missing/rejected API key.

`main.py` is the older ad-hoc script; `run.py` supersedes it for scheduled runs.

## Budget

Brave has no free tier anymore: $5/1,000 requests, with $5 free credits/month
(~1,000 requests) and **no spending cap on Brave's side** — the cap lives here.
Every request (each pagination page) increments `search_api_usage` (one row per
month) atomically, and the runner refuses to search past
`SEARCH_MONTHLY_QUERY_CAP` (default **4000** ≈ $20/mo, ≈ $15 after credits).

Waste control: per site, pagination stops as soon as a page contains a posting
already in `jobs` (same normalized URL) — with a 24h freshness window, a seen
result means deeper pages are stale.

At the default cadence below: tier-1 = 8 sites x 4 runs/day, tier-2 = 32 sites
x 1 run/day → 64 base queries/day ≈ 1,920/month, leaving ~2,000 queries of
pagination headroom under the cap.

Brave's free-credit terms require attributing "Brave Search API" on the
project's website/about page.

## Scheduling (cron on EC2)

```cron
# tier 1 every 6h, tier 2 once daily at 03:30
0 */6 * * *  cd /path/to/autoapply-v2/search && uv run python run.py --role "data engineer" --tier 1 >> /var/log/search.log 2>&1
30 3 * * *   cd /path/to/autoapply-v2/search && uv run python run.py --role "data engineer" --tier 2 --max-pages 2 >> /var/log/search.log 2>&1
```

For Airflow, call the same command in a BashOperator; exit code 2/3 fails the task.

## Environment (`.env`)

- `BRAVE_API_KEY` — required.
- `SEARCH_MONTHLY_QUERY_CAP` — optional, default 4000.
- `DB_HOST` / `DB_PORT` etc. — Postgres connection (see `common/`).

## Tests

```sh
DB_PORT=25432 uv run pytest tests/
```

Integration tests need live Postgres with the current `db/init.sql` schema
(they skip when unreachable). The Brave API is always faked — tests never spend
queries.

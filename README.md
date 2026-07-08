# autoapply-v2

An AI-powered job search, apply, and tracking system that does the heavy lifting while you focus on other things.

---

## Features

### Must Have
- **Job Search** — aggregates results from Google and Brave search engines
- **AI Resume Assist** — resume editing and scoring
- **AI Apply Assist** — autonomous browser agent to fill and submit job applications

### Should Have
- Login / Sign up, security, RBAC — Google Sign-In + per-user row-level security via `api/`

### Could Have
- Gmail subscription monitoring
- Company career page scraping
- OSS model support (via OpenRouter)

---

## Modules

| Module | Description |
|---|---|
| `search/` | Search API — fetches job listings from Google, Brave, and Gmail |
| `auto-browse/` | Browser agent — CDP-based agent that autonomously applies to jobs; owns the `autoapply` CLI |
| `ai-assist/` | AI utilities — resume editing, scoring, prompts, and LLM tooling |
| `api/` | HTTP API — Google Sign-In, sessions, per-user data isolation (Postgres RLS) |
| `common/` | Shared library — Postgres pool, MinIO storage, queue + repo helpers |

---

## Quickstart

Each module is a self-contained Python package managed with `uv`.

### 1. Shared infra (Postgres + MinIO)

```bash
docker compose up -d        # repo root; schema loads from db/init.sql on first boot
```

### 2. Onboard yourself (one-time)

```bash
cd auto-browse
uv run autoapply profile init --pdf your_resume.pdf --docx your_resume.docx
```

Parses the resume with an LLM (needs `OPENROUTER_API_KEY`), walks you through
every field interactively — extracted values are the defaults, Enter keeps
them, `-` clears — then asks the application questions a resume can't answer
(work authorization, relocation, EEO, certifications) with autofilled answers.
`--yes` accepts all defaults non-interactively. Re-running refreshes the
profile. Inspect with `autoapply profile show`.

### 3. The `autoapply` CLI

Runs from `auto-browse/` (`uv run autoapply ...`). One front door over the whole
pipeline:

```bash
uv run autoapply jobs add https://boards.greenhouse.io/acme/jobs/42   # or --file urls.txt
uv run autoapply jobs list [--status jd_ready]     # your jobs only
uv run autoapply tailor                            # capture JDs + tailor resumes
                                                   # (needs auto-browse containers up:
                                                   #  cd auto-browse && docker compose up -d;
                                                   #  + OPENROUTER_API_KEY, LibreOffice)
uv run autoapply status [JOB]                      # pipeline state + scores
uv run autoapply resume show JOB [--open]          # download the tailored PDF
uv run autoapply gmail connect                     # optional: 2FA email access for apply
uv run autoapply apply JOB                         # run the apply agent
                                                   # (watch at http://localhost:6080/vnc.html;
                                                   #  it pauses before final submit)
```

`JOB` is a job id, an id prefix, or the posting URL. With more than one active
user, pass `--user <id|email>`; with a single user it's inferred.

### 4. Other entry points

```bash
# Search (enqueues results for the single active user)
cd search && uv run python main.py

# Multi-user HTTP API (Google Sign-In; see api/README.md)
cd api && uv run uvicorn api.app:app
```

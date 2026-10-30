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
uv run autoapply profile init --docx general.docx          # your facts + first role
uv run autoapply resume add de.docx --label de             # one resume per role
uv run autoapply resume add swe.docx --label swe
```

A profile has two halves. **Facts** — contact, address, work authorization,
relocation, EEO, certifications — are yours and the same for every role;
`profile init` asks for them with autofilled defaults (Enter keeps, `-` clears,
`--yes` accepts all). **Role resumes** are added per role: each `.docx` is stored
with a PDF rendering and parsed by an LLM (needs `OPENROUTER_API_KEY`) into that
role's work history, education and skills. An `.odt` or `.md` works too: it is
converted to `.docx` with LibreOffice first (Markdown needs a LibreOffice with
Markdown import, e.g. 26.2), and only the `.docx` is kept. A PDF is refused, since
tailoring edits the document text. `--docx` on `profile init` adds the
first role (`--label general` by default) and prefills your contact from it.

Roles are never merged. Tailoring, `ask` and the apply profile all use the role a
job was tailored from (`autoapply tailor --label de`), falling back to your default
role (the first one added; change it with `autoapply resume default`). When two
role resumes disagree on an employer's dates or on your current employer,
`resume add` says so. Inspect with `autoapply profile show [--label de]`; the JSON
a form filler reads is `autoapply profile export --label de`.

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
uv run autoapply status [JOB]                      # pipeline state + scores + posted salary;
                                                   # [NFY] = JD rules out sponsorship and your
                                                   # profile needs it (skip: jobs skip JOB)
uv run autoapply jobs scan                         # re-read captured JDs for those limits and
                                                   # pay bands (free; capture does it for new JDs)
uv run autoapply resume show JOB [--open]          # download the tailored PDF
uv run autoapply cover JOB [--open]                # write a cover letter for the job (paid);
                                                   # stored with it, exported next to the resume
uv run autoapply gmail connect                     # optional: 2FA email access for apply
uv run autoapply apply JOB [--autosubmit]          # run the apply agent
uv run autoapply apply --all [--today]             # batch: every jd_ready job one by one
                                                   # (--today = only jobs added today;
                                                   #  skips already-submitted)
                                                   # desired salary is fitted into the JD's band
                                                   # (watch at http://localhost:6080/vnc.html;
                                                   #  pauses before final submit unless
                                                   #  --autosubmit, then it clicks Submit)
uv run autoapply continue JOB [--autosubmit]       # finish an interrupted run: keeps what
                                                   # the open browser page already has
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

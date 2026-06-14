# autoapply-v2

An AI-powered job search, apply, and tracking system that does the heavy lifting while you focus on other things.

---

## Features

### Must Have
- **Job Search** — aggregates results from Google and Brave search engines
- **AI Resume Assist** — resume editing and scoring
- **AI Apply Assist** — autonomous browser agent to fill and submit job applications

### Should Have
- Login / Sign up, security, RBAC

### Could Have
- Gmail subscription monitoring
- Company career page scraping
- OSS model support (via OpenRouter)

---

## Modules

| Module | Description |
|---|---|
| `search/` | Search API — fetches job listings from Google, Brave, and Gmail |
| `auto-browse/` | Browser agent — CDP-based agent that autonomously applies to jobs |
| `ai-assist/` | AI utilities — resume editing, scoring, prompts, and LLM tooling |

---

## Quickstart

Each module is a self-contained Python package managed with `uv`.

```bash
# Search
cd search && uv run python main.py

# Auto-browse agent
cd auto-browse && ./start.sh

# AI assist
cd ai-assist && uv run python agent.py
```

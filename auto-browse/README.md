# auto-browse

A cheap OpenRouter LLM drives a real browser through a **text-extracting proxy**
in front of Microsoft's Playwright MCP, to fill and advance job applications.

Small models flail on a raw 1000-line accessibility snapshot and hallucinate `ref`
values. So the proxy never hands the model the tree: for every page it returns only
the **visible text** (accessible names, `text:` nodes, inline values — no roles,
refs, URLs, or structure). When the model needs to act on an element it calls
**`fetch_aria`** to pull the raw ARIA for specific labels and recover their
`[ref=...]`. The model reads text, looks up the refs it needs, then clicks/types.
Dangerous/distracting tools are hidden.

## Pieces

| Piece      | Where            | What                                                          |
|------------|------------------|---------------------------------------------------------------|
| `browser/` | Docker (compose) | Playwright MCP, **headed**, watchable over noVNC. Port 8931.  |
| `proxy/`   | Docker (compose) | Text-extracting MCP the agent connects to. Port 8932.         |
| `agent/`   | Host (`uv run`)  | OpenRouter tool-use loop that drives the proxy's tools.       |

```
agent (host) ──MCP──▶ proxy :8932 ──MCP──▶ playwright :8931 ──▶ Chromium ──▶ noVNC :6080
```

## Run — apply to a job

```bash
cp .env.example .env          # add OPENROUTER_API_KEY (and a model)

# 1. Export the applicant profile from the autoapply Postgres DB and stage the
#    resume PDF (stages the resume PDF under output/resume/<file>.pdf).
uv run python -m agent.profile

# 2. Start the browser stack (playwright + noVNC + proxy; mounts the resume).
docker compose up --build

# watch the browser live: open http://localhost:6080/vnc.html

# 3. Run the agent against a job posting URL.
uv run python -m agent.loop "https://example.com/careers/job/123"
```

The agent loads the profile, fills the application form, uploads the resume, and
**stops before the final submit** — review in the noVNC window and submit yourself.

## How the proxy works

`proxy/server.py` opens **one persistent session** to the upstream Playwright MCP
(so every navigate/click acts on the same tab) and relays calls. The transform:

- **Visible text instead of the ARIA tree.** Playwright MCP writes each snapshot to
  a `.yml` file and returns a `[Snapshot](path)` link. The proxy reads that tree and
  replies with only its visible text (`_visible_text` — the single seam for the
  text↔ARIA mapping). Results that carry no snapshot pass through unchanged.
- **Settle before reading.** After an action the upstream often captures the page
  before a JS/SPA shell has rendered (Oracle Cloud, Workday). The proxy waits
  `SETTLE_SECONDS` (default 2), re-snapshots, and returns the settled tree. Set
  `SETTLE_SECONDS=0` to disable.
- **`fetch_aria` (proxy-only tool).** Search the current page's ARIA for one or more
  text keys; returns a YAML map, one entry per key (in request order), the value a
  literal block (`|2-`) of the matched ARIA lines — including each `[ref=...]` — or
  `(no match)`. `context` sets how many lines are kept *after* each match (default
  3); raise it to capture a full option list. This is how the model recovers refs.
- **`browser_snapshot`** is exposed but, like every snapshot-returning call, its
  result is reduced to the page's visible text (its upstream args are dropped — it
  takes none). Raw ARIA comes only from `fetch_aria`.
- **Tool curation.** Only navigation-relevant tools are exposed
  (`browser_navigate`, `_navigate_back`, `_click`, `_type`, `_fill_form`,
  `_select_option`, `_press_key`, `_file_upload`, `_handle_dialog`,
  `_console_messages`, `_evaluate`, `_hover`, `_tabs`, `_wait_for`,
  `browser_snapshot`) plus `fetch_aria`. Everything else (screenshot, resize,
  network, drag/drop, close, `run_code_unsafe`) is hidden. The `target` arg
  descriptions are rewritten with a worked example so the model passes the bare ref
  token (`e76`), never a CSS/text selector.

Proxy env: `PLAYWRIGHT_MCP_URL`, `PROXY_HOST`, `PROXY_PORT`, `SNAPSHOT_DIR`,
`SETTLE_SECONDS`.

## The agent loop

`agent/loop.py` calls an OpenRouter chat model in a tool-use loop over the proxy.
Each turn: the model sees the latest page text, optionally calls `fetch_aria` to get
refs, then drives the browser until it stops (no tool call) or the iteration cap is
hit. The full system prompt (workflow, examples, MUST DOs) lives in `SYSTEM_PROMPT`
in `agent/loop.py`. The applicant profile and any ATS rules are appended as separate
system messages; today's date is injected at runtime.

- **Deliberately bare.** No history pruning, no evidence gates, no repeat/upload
  guards — a clean base to build on. (Those lived in the previous page-model proxy
  and were dropped in the ground-up rewrite; reintroduce them here as needed.)
- **Human handoff.** `request_human_help` is a virtual tool handled in the loop (not
  forwarded to the proxy): it pauses the run and asks the human to act on the browser
  screen for steps the agent must not do itself — OTP/verification codes, email
  confirmation links, and CAPTCHA / bot-detection challenges — then resumes with the
  human's reply. Triggers live in `HIL_SCENARIOS`.
- **LLM resilience.** The model call is wrapped with exponential backoff + full
  jitter (`LLM_MAX_RETRIES`, `LLM_BACKOFF_BASE`, `LLM_BACKOFF_CAP`, `LLM_TIMEOUT`)
  handling timeouts, connection/5xx errors, empty responses, and rate limits —
  including upstream 429s OpenRouter surfaces under a different status. `Retry-After`
  is honoured; genuine client errors (auth, bad request) are not retried.

Agent env: `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` (default
`google/gemini-3-flash-preview`), `PROXY_MCP_URL`, `PROFILE_PATH`,
`ATS_INSTRUCTIONS_PATH`, `MAX_ITERATIONS`, `LLM_*`, `OPENROUTER_HTTP_REFERER`,
`OPENROUTER_APP_NAME`.

## Profile & resume

`agent/profile.py` pulls the applicant from the autoapply Postgres DB, renders the
form-fill context in-memory, and stages the chosen resume PDF to
`output/resume/<file>.pdf` (mounted into the playwright container at `/resume`, which
the agent uploads during the application).

Env: `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD`,
`APPLICANT_EMAIL`, `APPLICANT_LOGIN_EMAIL`, `APPLICANT_LOGIN_PASSWORD`,
`AUTOAPPLY_DATA`, `RESUME_VARIANT` (`""` base, or `aws`/`gcp`/`azu`), `RESUME_FILE`.

> References are currently hardcoded in `agent/profile.py` (`REFERENCES`). Once a
> references table exists in the DB, source them from there.

## ATS / domain-specific rules

If `input/ats_instructions.md` is present it is appended as a system message and
overrides generic guidance. Rules are grouped by ATS host (Greenhouse, Workday, …) —
e.g. Greenhouse: never use sign-in autofill (no credentials), resume autofill is
fine; Workday: never type into comboboxes, expand and click the option (options load
at the page bottom).

## Observability

Each `uv run python -m agent.loop` gets a `run_id` and writes three streams under
`output/` (gitignored). See `agent/observability.py`.

| Artifact | What |
|----------|------|
| `output/logs/app.log` | Operational log, human-readable, **appended** across runs (also printed to console). Each line tagged with the `run_id`. |
| `output/traces/<run_id>.jsonl` | The LLM-conversation **trace**, one JSON event per line — feed it to a trace/observability app. One file per run. |
| `output/usage.jsonl` | One JSON record **appended per run** (tokens, cost, duration, outcome), so all past runs stay visible. Written in a `finally` block, so crashed/interrupted runs are recorded too. |

Env knobs: `LOG_DIR`, `TRACE_DIR`, `FORMS_DIR`, `USAGE_PATH`.

## Tests

No automated suite yet (the old page-model fixtures were removed in the rewrite).
Planned coverage is sketched in `future_tests.md`.

## Bugs
- `The page changed` nudge isn't enough, agent sometimes clicks on randon refs
- Greenhouse country code flaky, i one shotted this specifically

# auto-browse

A cheap OpenRouter LLM drives a real browser through a **deterministic page-model
proxy** in front of Microsoft's Playwright MCP.

Small models flail on a raw 1000-line accessibility snapshot and hallucinate
`ref` values. Instead of returning the tree, the proxy parses it (no LLM) and
returns a **page model**: the page type, every form field (ref, type, required,
current value, options), the action buttons, and any validation errors — exactly
what's needed to fill and advance a job application, and nothing else. The agent
never searches the page; it reads a typed summary and acts. Dangerous/distracting
tools are hidden.

The field-type and error signatures were derived from a corpus of human-recorded
applications across Workday / Greenhouse / Lever / Oracle / Taleo / UltiPro (the
recorder lives in `tools/recorder/`; the corpus snapshots are the page-model test
fixtures in `proxy/tests/fixtures/`).

## Pieces

| Piece      | Where                | What                                                        |
|------------|----------------------|-------------------------------------------------------------|
| `browser/` | Docker (compose)     | Playwright MCP, **headed**, watchable over noVNC. Port 8931. |
| `proxy/`   | Docker (compose)     | Page-model MCP the agent connects to. Port 8932.            |
| `agent/`   | Host (`uv run`)      | OpenRouter loop that calls the proxy's curated tools.       |

```
agent (host) ──MCP──▶ proxy :8932 ──MCP──▶ playwright :8931 ──▶ Chromium ──▶ noVNC :6080
```

## Run — apply to a job

```bash
cp .env.example .env          # add OPENROUTER_API_KEY (and a model)

# 1. Export the applicant profile from the autoapply Postgres DB and stage the
#    resume PDF (writes data/profile/profile.txt and data/resume/<file>.pdf).
uv run python -m agent.profile

# 2. Start the browser stack.
docker compose up --build     # playwright(+VNC) + proxy; mounts the resume

# watch the browser live: open http://localhost:6080/vnc.html

# 3. Run the agent against a job posting URL.
uv run python -m agent.loop "https://example.com/careers/job/123"
```

The agent loads the profile, fills the application form, uploads the resume, and
**stops before the final submit** — review in the noVNC window and submit
yourself. Profile/resume source and DB connection are env-configurable
(`APPLICANT_EMAIL`, `RESUME_VARIANT`, `AUTOAPPLY_DATA`, `DB_*`); see
`agent/profile.py`.

## Page model

Every tree-returning tool result is replaced by a typed page model
(`proxy/page_model.py`, `extract()` + `render()`):

```
PAGE: form (step 3 of 7: My Experience)
ERRORS: Errors Found | The field Degree is required and must have a value.
FIELDS:
  [e12] "First Name" *req text           = "Jane"
  [e15] "Degree"     *req dropdown(use select_options) = (empty)  ⚠ required, empty
  [e18] "Country"        select(use select_option)     = "United States"
  [e20] "Sponsorship?" *req radio(click option ref) Yes[e21] No[e22] = (unset)
ACTIONS: [e30]"Save and Continue"  [e31]"Back"
```

- **page_type**: `job_posting` / `signin_signup` / `form` / `confirmation` /
  `loading` / `job_board` / `unknown`. An `unknown` page falls back to the raw
  (node-capped) tree.
- **fields** carry a **type** that tells the agent how to fill each one — `text`→
  `type`, `dropdown`→`select_options` (ARIA), `select`→`select_option` (native
  `<select>`), `radio`/`checkbox`→`click` the option ref, `file`→open the chooser
  then `file_upload`. Native `<select>` is distinguished from an ARIA dropdown by
  the presence of `option` children. Required is detected from `*` / `✱` /
  "Required" in the label; `⚠` flags a required field still empty or one with an
  error.
- **errors** are extracted per engine (Workday "Errors Found" + "The field X is
  required…", Oracle `alert "…field is required."`, Taleo `". Required"`, etc.),
  with info/loading alerts filtered out.
- `expand_region(ref)` is the READ path: dump a field/region's full raw content
  (long option lists, descriptions) when the one-line summary isn't enough.

Selection is purely structural — there is no goal/keyword search. Refs are only
valid from the latest page model; older refs are stale after any action.

## Tests

```bash
uv run pytest proxy/tests
```

## Context management

A form-filling browser agent is near-Markovian: the next action depends on the
constant goal + profile, the **current** page, and a short record of what's been
done — not on verbatim old page snapshots. The profile (not the page history) is
the durable source of truth, and refs are only valid on the latest tree, so old
snapshots are dead weight that inflate tokens/cost and tempt stale-ref reuse.

**Tier 1 (implemented, `agent/loop.py`):**
- *Sliding window* — only the last `HISTORY_KEEP_SNAPSHOTS` (default 2) tool
  results are kept full; older ones are replaced with a stub. Message order and
  every `tool_call`→`tool` pairing are preserved (OpenAI requires it).
- *Progress ledger* — a single system message, refreshed each turn, holds one
  compact line per action (`N. tool(args) -> outcome`) so nothing important is
  lost when snapshots are dropped.
- The redundant per-iteration "REMINDER — ORIGINAL TASK" message was removed.

The agent runs **without API reasoning / extended thinking** — model-hosted
"thought signatures" are vendor-specific, brittle across provider failover, and
won't exist on the cheap local LLM this targets. Instead the model states a
one-line plan in its normal message content before each tool call (lightweight,
portable, debuggable).

Net: prompt tokens stay roughly flat per turn regardless of how many form pages
the application has.

**Tier 2 (future, not implemented):** stop appending entirely — rebuild
`messages` each turn from `[system, profile, ledger, latest_observation]`. Truly
stateless except the ledger + current page, so context is bounded by
construction. Because every assistant `tool_calls` message needs matching `tool`
replies, Tier 2 rebuilds the whole window coherently rather than deleting
messages mid-history. This is the principled endpoint; revisit if Tier 1 runs
still drift.

The LLM call is wrapped with exponential backoff + full jitter
(`LLM_MAX_RETRIES`, `LLM_BACKOFF_BASE`, `LLM_BACKOFF_CAP`, `LLM_TIMEOUT`) that
handles timeouts, connection/5xx errors, empty responses (`choices=None`), and
rate limits — including upstream 429s that OpenRouter surfaces under a different
status (e.g. a 400 "Provider returned error" wrapping a 429). A `Retry-After`
header is honoured when present; genuine client errors (auth, bad request) are
not retried.

## Observability

Each `uv run python -m agent.loop` gets a `run_id` and writes three streams under
`data/` (gitignored). See `agent/observability.py`.

| Artifact | What |
|----------|------|
| `data/logs/app.log` | Operational log, human-readable, **appended** across runs (also printed to console). Each line tagged with the `run_id`. |
| `data/traces/<run_id>.jsonl` | The LLM-conversation **trace**, one JSON event per line (`run_start`, `llm_turn`, `tool_result`, `retry`, `run_end`) — feed it to a trace/observability app. One file per run. |
| `data/usage.jsonl` | One JSON record **appended per run** (tokens, cost, iterations, success, duration), so all past runs stay visible. Written in a `finally` block, so crashed/interrupted runs are recorded too. |
| `data/forms/<run_id>.json` | A clean per-run record of **every form field the agent filled** (tool, label, ref, value — from `type`/`fill_form`/`select_option`/`file_upload`) plus the final summary. Go back and see exactly what was entered on a past application. |

Env knobs: `LOG_DIR`, `TRACE_DIR`, `FORMS_DIR`, `USAGE_PATH`, `LOG_LEVEL`.

## Notes

- **Completion is judged from evidence, not the model stopping.** When the model
  emits no tool call, the loop checks evidence before accepting "done": the
  resume was uploaded **and** the latest page reached a review/submit step **and**
  no validation error is showing (name/phone/address are supporting only, may be
  autofilled). If evidence is missing it hints the model and makes it continue
  (up to `MAX_FINISH_HINTS` times); the run's `success` reflects that evidence.
  Tool results that fail as plain text (`Session terminated`, `### Error`) are
  counted as failures, not progress.
- **Repeated-action guard.** If the model calls the same action with the same
  target `REPEAT_LIMIT` (default 3) times, the loop nudges it to change approach
  (re-clicking an "Apply" that opened a new tab, or a dropdown that never opens).
  Read-only tools (`snapshot`/`expand_region`/`wait_for`) are exempt.
- **Upload-first (hard).** When a page offers an autofill / upload-resume option
  and the resume is not uploaded yet, manual value-entry (`type`/`fill_form`/
  `select_option`/`select_options`) is **blocked** — the agent must upload the
  resume first and let it autofill, then review. (Uploading last re-renders the
  form and overwrites manual entries.) The check ignores the page URL and never
  blocks credential (password) fills, so sign-in / create-account pages — including
  Workday's, served under an `…/autofillWithResume` URL — are not blocked. Toggle
  with `ENFORCE_UPLOAD_FIRST=0`.
- **Structured outcome.** Every run records a single `outcome` —
  `applied_ready` (validated, ready for the user to submit), `already_applied`,
  `closed`, or `blocked` (ran out / stopped without evidence / errored) — in
  `usage.jsonl` and the form record (`success` is just `outcome == applied_ready`).
- **Terminal page states.** If a page says the applicant has already applied, or
  the posting is closed / no longer accepting applications, the loop exits
  gracefully (`outcome` = `already_applied`/`closed`) instead of looping back to
  search and re-apply.
- **Tabs.** `select_tab` / `close_tab` let the agent follow an "Apply" that opens
  the form in a new tab (the proxy returns the page model of the tab switched to).
- The proxy exposes only navigation-relevant tools and **hides** `evaluate` and
  `run_code_unsafe` (the biggest wrong-tool / safety risks for small models).
- **Required + errors are first-class.** The page model marks every required
  field still empty with `⚠ required, empty` and lists validation errors under an
  `ERRORS:` header, read deterministically from the tree (text value /
  `[selected]` option / `[checked]` radio/checkbox / per-engine error nodes). This
  catches a fill that silently no-ops (e.g. `fill_form` on a radio). The evidence
  gate refuses `applied_ready` while any required field is empty or any error shows.
- **Composite dropdown picker.** ARIA comboboxes render their options only after
  opening, so picking a value used to cost the agent ~3 turns (`click`-open →
  `snapshot` → `click`-option), and a multiselect was done one value at a time
  (~10 turns). `select_options(target, values=[...])` (`proxy/option_select.py` +
  `_select_options` in `proxy/server.py`) does the whole dance **server-side in one
  agent turn**: it opens the control, deterministically matches each requested
  human-readable label to a rendered option (exact → substring → fuzzy, no LLM),
  clicks it, re-opens/re-filters per value for multiselects and type-aheads, and
  returns the page model plus a `matched / no match` summary (unmatched values
  list the available options so the agent can retry with an exact label — it never
  silently picks the wrong option). The N internal browser calls cost zero LLM
  tokens. It is for ARIA dropdowns/listboxes/multiselects only (field type
  `dropdown`); native `<select>` (type `select`) uses `select_option`, and
  radio/checkbox answers are a direct `click`.
- **Radio/checkbox answers** must be a `click` on the specific option ref —
  `fill_form` with type `radio`/`checkbox` does not reliably check them (it emits
  `setChecked(<value>)`, a no-op), leaving the field empty.
- VNC has no password — local/dev use only.




## Bugs

## Improvements
- IMPORTANT: References are currently hardcoded in `agent/profile.py` (`REFERENCES`). Once a references table is added to the database, sourcing them from there is the first thing to work on.
- If the site says you have already applied for this position, gracefully exit instead of trying to find the same role and trying to apply. example conversation for this url: `https://topgolf.wd501.myworkdayjobs.com/en-US/TopgolfCareers/job/TG---HQ---Dallas-Office/Data-Engineer-II_JR2023-19177?source=LinkedIn`
- Application level: record all conversations and based on the domain/company build automate scripts that can offload from AI agents in form filling based on previous form filling conversation. 




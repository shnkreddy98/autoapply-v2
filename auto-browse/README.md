# auto-browse

A cheap OpenRouter LLM drives a real browser through a **deterministic
accessibility-tree pruning proxy** in front of Microsoft's Playwright MCP.

Small models hallucinate `ref` values when handed a full accessibility snapshot.
The proxy filters every returned tree down to the nodes relevant to the current
step goal (keyword/role matching, no LLM), preserving refs exactly, and hides
dangerous/distracting tools.

## Pieces

| Piece      | Where                | What                                                        |
|------------|----------------------|-------------------------------------------------------------|
| `browser/` | Docker (compose)     | Playwright MCP, **headed**, watchable over noVNC. Port 8931. |
| `proxy/`   | Docker (compose)     | Pruning MCP the agent connects to. Port 8932.               |
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
  `select_option`) is **blocked** — the agent must upload the resume first and let
  it autofill, then review. (Uploading last re-renders the form and overwrites
  manual entries.) Sign-in pages show no such affordance, so login fills aren't
  blocked. Toggle with `ENFORCE_UPLOAD_FIRST=0`.
- **Structured outcome.** Every run records a single `outcome` —
  `applied_ready` (validated, ready for the user to submit), `already_applied`,
  `closed`, or `blocked` (ran out / stopped without evidence / errored) — in
  `usage.jsonl` and the form record (`success` is just `outcome == applied_ready`).
- **Terminal page states.** If a page says the applicant has already applied, or
  the posting is closed / no longer accepting applications, the loop exits
  gracefully (`outcome` = `already_applied`/`closed`) instead of looping back to
  search and re-apply.
- **Tabs.** `select_tab` / `close_tab` let the agent follow an "Apply" that opens
  the form in a new tab (the proxy returns a pruned snapshot of the tab switched
  to).
- The proxy exposes only navigation-relevant tools and **hides** `evaluate` and
  `run_code_unsafe` (the biggest wrong-tool / safety risks for small models).
- **Always-surfaced nodes.** Every pruned tree includes, regardless of the step
  goal: entry actions (sign-in / create-account / apply-with-resume / autofill /
  upload-resume), form-action buttons (Save/Continue/Next/Submit), validation
  errors (incl. `img "Error"` icons), required (`*`) fields, and the option/radio
  children of any kept dropdown/radio group. These never share keywords with the
  goal but are exactly what makes a form advance — pruning used to drop them.
- **Structured overview on page-load.** A navigation/settle with no goal (e.g. the
  first `navigate`) returns a bounded *overview* — landmarks + every field/button/
  link + the always-surfaced sets, across all sections of a multi-step form — not
  the raw full tree. An **explicit** `snapshot()` with no goal still returns the
  full tree (the agent's escape hatch).
- Pass a `goal` (on `snapshot`) or `next_goal` (on action tools) to focus the
  pruned tree on a step. Refs are only valid from the latest tree.
- VNC has no password — local/dev use only.




## Bugs
- If a apply button opens a new window, agent gets stuck keeps on opening the same window, conversation found at: `https://apply.jobappnetwork.com/clients/18760/posting/11054618/en?utm_source=linkedin.com`
- filled the form first, even when there was upload resume button, and upload resume happened last which refilled all the form with some empty fields, and then checked out without validating. check here: `https://recruiting2.ultipro.com/UNI1027UDRT/JobBoard/6ccb8fd4-4950-43e4-9978-4bcc85c6f5e1/OpportunityDetail?opportunityId=6954d5bf-51f1-48c6-8a65-294549d8d73b&source=LinkedIn`

## Improvements
- If the site says you have already applied for this position, gracefully exit instead of trying to find the same role and trying to apply. example conversation for this url: `https://topgolf.wd501.myworkdayjobs.com/en-US/TopgolfCareers/job/TG---HQ---Dallas-Office/Data-Engineer-II_JR2023-19177?source=LinkedIn`
- Application level: record all conversations and based on the domain/company build automate scripts that can offload from AI agents in form filling based on previous form filling conversation. 

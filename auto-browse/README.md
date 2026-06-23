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
  every `tool_call`→`tool` pairing are preserved (OpenAI requires it); reasoning
  blobs are stripped from all but the most recent assistant message.
- *Progress ledger* — a single system message, refreshed each turn, holds one
  compact line per action (`N. tool(args) -> outcome`) so nothing important is
  lost when snapshots are dropped.
- The redundant per-iteration "REMINDER — ORIGINAL TASK" message was removed.

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

## Notes

- The proxy exposes only navigation-relevant tools and **hides** `evaluate` and
  `run_code_unsafe` (the biggest wrong-tool / safety risks for small models).
- Pass a `goal` (on `snapshot`) or `next_goal` (on action tools) to get a pruned
  tree; omit it to get the full tree. Refs are only valid from the latest tree.
- VNC has no password — local/dev use only.

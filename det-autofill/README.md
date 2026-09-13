# det-autofill

Deterministic job-application form filler. ARIA snapshot in → matched answers →
fill actions out. The LLM is demoted to a **one-shot fallback** per page (no
agent loop): it only sees the fields the deterministic pass couldn't resolve.

Standalone experiment — integrates into autoapply (`apply --engine det`) only
once proven live.

## Pipeline (per page)

```
PARSE (aria.py)  →  MATCH (taxonomy.py)  →  ANSWER (answers.py)  →  FILL+VERIFY (widgets.py)
     → CLICK NEXT → errors/unmatched-required? → ONE llm shot (llm_shot.py) → NEXT → next page
```

- `aria.py` — playwright ARIA snapshot text → `FormField{ref, role, label, required, options, context}`.
  Handles: textbox, combobox(+options), radio(+question context), checkbox, spinbutton, etc.
- `taxonomy.py` — data-driven rule table: normalized-label regex → canonical key.
  ~85 rules seeded from mining 129 real traces / 25 ATS platforms.
  Adding a question = adding one row.
- `answers.py` — canonical key → applicant answer. Kinds: text / bool / option
  (preference regexes vs the widget's options) / date / file / skip / handoff.
  P2 source: profile JSON (`example_profile()` shows the shape). Integration:
  autoapply Postgres + new `skill_years` map from the resume parse.
- `harvest.py` — mines auto-browse traces into `tests/fixtures/fragments/`
  (1465 real ARIA fragments, deduped, host-tagged).
- `coverage.py` — THE gate metric. Current (2026-07-08):

  | metric | value |
  |---|---|
  | unique fields (deduped by host+label) | 1288 |
  | required fields | 269 |
  | **required matched+answered** | **84.0%** (gate: 80%) |
  | all fields answered | 79.5% |

  Residual = orphaned radio options in truncated fragments (live snapshots carry
  the parent question) + genuine LLM-tier oddballs ("bitcoin journey", ...).

## Decisions (agreed 2026-07-08)

- Employment/education history: fill the MOST RECENT row only, skip add-row —
  the uploaded resume carries the rest.
- `skill_years` map ("years with Databricks?" answers) produced by the LLM
  resume parse at onboarding, consumed deterministically here.
- captcha / OTP / verification-code fields → handoff (VNC human), never LLM.
- Per-ATS widget quirks live in code (`ats.py`), not prompts: Greenhouse
  type-ahead = type+ArrowDown+Enter; Workday = click-open+click-option,
  cascading, never type; refs stale after every action; file-chooser modal
  makes the page look blank (expected — upload immediately).

## Roadmap

- [x] P1 fixture harvest + ARIA parser
- [x] P2 taxonomy + answers + coverage gate (84% ≥ 80%)
- [x] P3 widgets.py + ats.py — live fill proven vs Greenhouse (doximity,
      2026-07-09: 17/21 fields incl. resume upload, typeahead + click
      comboboxes, EEO; 4 misses = custom questions for the P5 LLM tier).
      Workday live pass still pending (needs account-create flow).
      Live findings baked in: playwright MCP writes big snapshots to a linked
      .yml file (mcpio resolves via the compose bind mount); action results
      often omit the snapshot (mcpio re-snapshots); job boards need an Apply
      click before the form exists (runner.ensure_form).
- [ ] P4 engine.py — page state machine, next-click, error detect (role=alert, page-unchanged)
- [ ] P5 llm_shot.py — one-shot JSON contract `[{ref, action, value}]`, executed by widgets.py; cap 2/page
- [ ] P6 integrate: `autoapply apply --engine det`; runner talks to raw playwright MCP :8931 (full ARIA — pruning proxy is an LLM-era optimization)

## Run

```bash
uv run python -m det_autofill.harvest    # refresh fixtures from ../auto-browse/output/traces
uv run python -m det_autofill.coverage   # match-rate report
uv run pytest -q
```

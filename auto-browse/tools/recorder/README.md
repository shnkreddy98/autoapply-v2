# Application recorder

Captures a corpus of **human-completed** job applications — raw ARIA accessibility
trees + screenshots at every page state — to design the proxy's deterministic
page-model extractor.

You drive a real Chromium; the recorder logs what each page looks like as you apply.

## Setup (one-time)

```bash
uv run playwright install chromium
```

## Run

```bash
# from the auto-browse/ repo root
uv run python -m tools.recorder.record tools/recorder/urls.example.txt
# or a custom list / output dir:
uv run python -m tools.recorder.record my-urls.txt --out tools/recorder/recordings
```

1. A headed Chromium opens one tab per URL (persistent profile — your logins stick).
2. Apply to each job normally, tab by tab. **Close a tab when you finish it.**
3. Recording stops when all tabs are closed, or on `Ctrl-C`.

Aim for **diversity over volume** — ~20 distinct ATS engines tells us more than 50
of the same. Top up toward 50 in the background for a robustness corpus.

## What it records

Per meaningful event (`click` / `change` / `submit` / `blur`) and every navigation:

- raw ARIA tree (`aria_snapshot()` YAML — same format `proxy/tree_parser.parse()`
  consumes, minus `[ref=eN]`)
- a screenshot
- the acted-on element's descriptor (tag, type, role, accessible name, value, required)

## Output

```
recordings/<session-ts>/
  manifest.jsonl          # one line per event; references the files below
  01-<host>/
    snap_0000.txt ...     # raw ARIA trees
    shot_0000.png ...     # screenshots
```

Identical consecutive snapshots are de-duplicated (the manifest line points at the
prior file and sets `"snapshot_unchanged": true`).

## Notes

- Snapshots/screenshots contain **real PII** (your name, email, answers). Everything
  stays local; `recordings/` and the browser profile are gitignored.
- `aria_snapshot()` omits the `[ref=eN]` ids the live proxy uses — fine here, since
  the design only needs structure/roles/types. Actions are correlated to nodes by
  role + accessible name.
- iframe-embedded forms are captured at page level only (v1 limitation).

# Deterministic Accessibility Tree Pruning Tool

## Goal

Cheap/small LLMs (Gemini Flash, Qwen, etc.) driving Playwright MCP fail to find
the right element when handed a full accessibility snapshot, because the tree
is large, mostly irrelevant to the current step, and full of refs that look
similar. This causes hallucinated `ref` values and wasted/incorrect tool calls.

We are adding a **deterministic pruning tool** that sits between Playwright's
`browser_snapshot` (or equivalent) output and the LLM. It is called on every
tool call that would otherwise return a full tree snapshot, and returns a
filtered, much smaller tree containing only the nodes relevant to the current
step's goal — while preserving every `ref` exactly as-is so the LLM can still
act on them.

**No LLM call is used inside the pruning tool itself.** Keyword/role-based
deterministic matching is sufficient for this problem because step-level
instructions ("click the search button", "type into the email field") reuse
the same words that appear in accessibility tree `role`/`name` fields. An LLM
pruning step would reintroduce the exact hallucination risk we're trying to
eliminate, add latency/cost to the highest-frequency operation in the loop,
and — critically — fail silently by pruning away the correct node with no
fallback. Deterministic matching degrades gracefully (under-filtering, not
wrong-filtering) and has clear escalation rules instead.

---

## High-Level Flow

```
LLM tool call (e.g. browser_click, browser_type, browser_snapshot)
        │
        ▼
Playwright MCP executes the action / takes raw snapshot
        │
        ▼
Raw accessibility tree (large, nested, ref-tagged)
        │
        ▼
 ┌─────────────────────────────┐
 │  prune_snapshot(tree, goal)  │   <-- NEW TOOL, deterministic
 └─────────────────────────────┘
        │
        ▼
Pruned tree (small, relevant, refs intact)
        │
        ▼
Returned to LLM as tool result
```

This tool wraps/post-processes the snapshot result. It should be implemented
as a **post-processing step on every tool call that returns a tree**, not just
an optional separate tool the LLM has to remember to call. The LLM should
never see the raw full tree by default.

---

## Inputs

1. **Raw accessibility tree** — the exact structure currently returned by
   Playwright MCP (the format shown in the example: indented tree with
   `role "name" [ref=eN]` lines, `/url:` lines, nested children).
2. **Goal string** — the current step's instruction. This can come from:
   - The most recent user/agent message describing the immediate action
     (e.g. "click the search box and type 'pizza'")
   - A `goal` parameter the orchestrator passes explicitly alongside each
     tool call (preferred — more reliable than re-parsing chat history)

If no explicit goal is available, fall back to "no filtering" (return full
tree) rather than guessing — silent full-tree fallback is safer than wrong
pruning.

---

## Step 1: Goal → Keyword Extraction (Deterministic, No LLM)

Build a small NLP utility (regex + dictionaries, no model) that extracts three
things from the goal string:

### a) Literal values (things to type, not search for)
Extract anything in quotes — single or double — as a **value**, not a
keyword to match against the tree. These should be excluded from the keyword
set used for filtering.

```
"type 'pizza margherita' into the search box" 
  → value: "pizza margherita"
  → remaining text for keyword extraction: "type into the search box"
```

### b) Stopword / verb stripping
Strip a fixed list of action verbs and filler words that don't appear in
accessibility trees: `click, type, enter, press, select, choose, go, navigate,
find, locate, into, the, a, an, on, to, for, with, then, and, please`.

```
"click the search button" → ["search", "button"]
```

### c) Synonym expansion (role/intent mapping)
Map common intents to the actual ARIA roles / words likely to appear in the
tree. This is the highest-leverage part of the system — maintain this as a
config dictionary, not hardcoded logic, so it's easy to extend.

| Intent keyword(s)          | Expand to also match roles/words            |
|-----------------------------|-----------------------------------------------|
| search, find                | searchbox, search, find                       |
| submit, go, confirm         | button, submit                                 |
| type, enter, input, fill    | textbox, searchbox, input                      |
| pick, choose, select        | combobox, listbox, option, select              |
| check, toggle                | checkbox, switch                               |
| open, navigate, go to        | link, menuitem, tab                            |
| login, sign in, log in       | button, link, textbox (combine with "log"/"sign")|
| close, dismiss, cancel       | button, dialog                                 |
| menu                         | navigation, menu, menuitem, button              |
| donate                       | link, button (no role expansion, keep literal)  |

Output of this stage: `Set[str]` of lowercase keywords/roles to match against
tree lines, plus the extracted literal `value` (kept separate, used only for
logging/debug, never used to filter nodes).

### Library suggestion
No need for spaCy/NLTK. Plain Python `re` + a static dict is enough and keeps
latency near-zero. If stemming is wanted later (e.g. "searching" → "search"),
a minimal suffix-stripping function is sufficient — do not add a model
dependency for this.

---

## Step 2: Tree Filtering

Given the keyword set, walk the raw tree (it's already structured/indented —
parse it into a real tree first, don't regex the whole blob) and apply this
priority order:

### Pass 1 — Direct match
Keep any node whose `role` or accessible `name` contains (case-insensitive
substring match) any keyword from the set.

```
searchbox "Search Wikipedia" [ref=e23]   <- matches "search"
button "Search" [ref=e25]                <- matches "search"
```

### Pass 2 — Always-keep interactive roles (if Pass 1 is sparse)
Independent of keyword match, these roles are cheap to include and almost
always relevant for action-taking steps. Configurable allowlist:

```
button, link, textbox, searchbox, combobox, checkbox, radio,
menuitem, option, switch, tab, slider
```

If Pass 1 returns fewer than `MIN_RESULTS` (configurable, default 3) matches,
union in all nodes with these roles from the *same region* (see Step 3) as
the closest Pass 1 match, or from the whole tree if Pass 1 returned zero.

### Pass 3 — Ancestor context preservation
For every node kept in Pass 1/2, walk up its ancestor chain in the original
tree and include each ancestor **with its role and ref only** (strip the
ancestor's own children that weren't already matched, strip text content not
needed). This preserves hierarchy/landmark context (e.g. "this searchbox is
inside `search` region inside `banner`") without re-including unrelated
siblings.

```
search [ref=e17]
  generic [ref=e20]
    searchbox "Search Wikipedia" [ref=e23]
    button "Search" [ref=e25]
```

### Pass 4 — Zero-match fallback (escalation, still no LLM)
If Pass 1 AND Pass 2 both return zero nodes:
- Return the tree pruned to **landmark/region level only**
  (`banner, navigation, main, contentinfo, search, form, complementary`),
  collapsed (children hidden, just role + ref + name of each landmark).
- Include a flag in the tool response: `"pruned": "landmarks_only",
  "reason": "no_keyword_match"` so the calling agent/orchestrator knows this
  happened and can decide whether to retry with a clarified goal, or just let
  the LLM pick a landmark to expand next (see Step 4).

This is the deterministic equivalent of "ask for help" — no LLM call, just a
coarser deterministic fallback.

---

## Step 3: Region/Landmark Awareness

Before filtering, do one pass to tag every node with the nearest ancestor
landmark role (`banner`, `navigation`, `main`, `contentinfo`, `search`,
`form`, `complementary`, or `root` if none). Store this as metadata used only
internally for Pass 2/4 grouping — do not need to expose it unless useful for
debugging.

---

## Step 4: Companion Tool — `expand_region`

Add a second, simple tool for the escalation case (Pass 4) and for general
on-demand drilling:

```
expand_region(ref: str) -> pruned subtree rooted at that ref, same filtering
                            rules applied within that subtree (Pass 1/2/3),
                            but never re-triggers Pass 4 (no infinite fallback)
```

This lets the LLM say "show me what's inside `navigation [ref=e6]`" when the
landmarks-only fallback fires and it needs to pick the right region. Cheap,
single extra round trip, still no LLM involved on the pruning side.

---

## Tool Interface Spec

### Tool: `prune_snapshot`
Wraps every snapshot-returning call. Should be implemented as a
post-processing function called automatically — not something the LLM has to
invoke itself — but expose it as a callable tool too, for manual re-pruning
with a refined goal mid-task.

```jsonc
{
  "name": "prune_snapshot",
  "description": "Filters a raw accessibility tree snapshot down to nodes relevant to the current step goal. Always preserves exact ref values. Call this automatically after every snapshot-producing action; call it manually if you need to re-filter with a more specific goal after a failed action.",
  "input_schema": {
    "type": "object",
    "properties": {
      "goal": {
        "type": "string",
        "description": "The immediate action/target for this step, e.g. 'click the search button' or 'type into the email field'. Be specific about the element, not the overall task."
      },
      "raw_tree": {
        "type": "string",
        "description": "Raw accessibility tree text as returned by the browser snapshot. Omit if pruning the most recent snapshot already in context."
      },
      "min_results": {
        "type": "integer",
        "description": "Optional override for minimum nodes before fallback expansion triggers. Default 3.",
        "default": 3
      }
    },
    "required": ["goal"]
  }
}
```

### Tool: `expand_region`

```jsonc
{
  "name": "expand_region",
  "description": "Expands a specific landmark or container ref into its filtered subtree. Use this when prune_snapshot returned landmarks_only and you need to find the right element inside one of them.",
  "input_schema": {
    "type": "object",
    "properties": {
      "ref": {
        "type": "string",
        "description": "The ref of the landmark/container to expand, e.g. 'e6'."
      },
      "goal": {
        "type": "string",
        "description": "Same goal string as the original step, used to re-apply keyword filtering inside this subtree."
      }
    },
    "required": ["ref", "goal"]
  }
}
```

---

## Output Format

Keep output format visually identical to Playwright's existing tree format
(same indentation style, same `role "name" [ref=eN]` syntax, same `/url:`
lines) so the LLM doesn't need to learn a new schema — it's the same thing,
just smaller. Add one optional metadata line at the top:

```
[pruned: 4 of 312 nodes matched keywords {search, button} | fallback: none]

search [ref=e17]
  generic [ref=e20]
    searchbox "Search Wikipedia" [ref=e23]
    button "Search" [ref=e25]
```

The bracketed metadata line is for debuggability (and so the LLM has a signal
if fallback triggered) — keep it terse, one line, not a structured block.

---

## File/Module Structure Suggestion

```
playwright_pruning/
├── keyword_extractor.py     # Step 1: goal string -> {keywords, value}
├── synonym_map.py           # config dict, Step 1c — easy to extend
├── tree_parser.py           # raw snapshot text -> structured tree (parse once)
├── pruner.py                # Steps 2-4: filtering passes, landmark tagging
├── tools.py                 # MCP tool definitions: prune_snapshot, expand_region
└── tests/
    ├── test_keyword_extractor.py
    ├── test_pruner.py
    └── fixtures/
        └── wikipedia_snapshot.txt   # use the provided example as a fixture
```

---

## Testing Requirements

1. **Fixture-based test**: use the Wikipedia accessibility tree example as a
   golden fixture. Assert that `prune_snapshot(tree, goal="click the search
   button")` returns a tree containing `e23` and `e25` and is under some
   token/line-count threshold (e.g. < 30 lines vs. ~700 raw).
2. **Synonym expansion test**: goal "find the donate option" should match
   `link "Donate" [ref=e32]` even though "donate" isn't in a generic synonym
   list — confirms literal substring matching works independent of synonym
   expansion.
3. **Quoted value extraction test**: goal `type 'machine learning' into
   search` should extract value `machine learning` separately and use
   `{type, search}` (post-stopword) as keywords, not include "machine" or
   "learning" as tree-matching keywords.
4. **Zero-match fallback test**: a nonsense goal should trigger Pass 4 and
   return landmarks only, with the `pruned: landmarks_only` flag present.
5. **Ref-integrity test**: assert every `[ref=eN]` present in pruned output
   exists verbatim in the raw input — no mangled/regenerated refs.
6. **Ancestor-context test**: a matched deeply-nested node should bring its
   ancestor chain (role + ref only) in the output, not just the leaf.

---

## Explicit Non-Goals (for this implementation pass)

- No LLM call anywhere in the pruning path.
- No embedding/vector similarity matching — defer this unless keyword
  matching is empirically shown to be insufficient after real usage data.
- No persistent learning/adaptation of the synonym map from usage — keep it
  as a static, manually-editable config for now.
- No change to Playwright's own snapshot mechanism — this is a pure
  post-processing layer.

---

## Open Parameters to Tune After Initial Implementation

- `MIN_RESULTS` threshold before Pass 2/4 fallback triggers (start at 3).
- Whether ancestor context (Pass 3) should include sibling counts
  ("3 more buttons not shown") so the LLM knows pruning happened nearby —
  optional nice-to-have, not required for v1.
- Whether `expand_region` should auto-chain (expand into the best-matching
  child landmark automatically) vs. requiring the LLM to call it explicitly
  per landmark. Start with explicit-only; revisit if it costs too many
  round trips in practice.

---

# ADDENDUM: Tool-Surface Curation & Proxy Wrapper Layer

> This section supersedes the earlier note that pruning is "a post-processing
> function called automatically." Concretely, that automatic interception is
> implemented as a **proxy MCP server** (facade) sitting in front of
> Microsoft's Playwright MCP. We do **not** modify the Microsoft container —
> it runs unchanged in Docker. Our proxy is a separate MCP server the LLM
> connects to instead; it forwards calls to the real Playwright MCP over its
> own client connection, intercepts the results, prunes them, and exposes a
> curated/re-described tool list.

## Why a proxy (not just post-processing the raw tool)

Three problems with exposing Microsoft's tools directly to a cheap LLM:

1. **Action tools also return snapshots.** `browser_click`, `browser_type`,
   `browser_navigate`, `browser_navigate_back`, `browser_select_option`,
   `browser_press_key`, etc. each return a fresh page snapshot in their
   result — not only `browser_snapshot`. Pruning must apply to *every* tool
   whose result contains a tree, and each needs a goal to prune against.
2. **Tool-surface bloat.** ~25 tools, many irrelevant to navigation. A large
   menu causes cheap models to pick the wrong tool (e.g. `browser_evaluate`
   to "find" an element instead of clicking). Curating the menu is itself a
   major reliability/anti-hallucination win, independent of pruning.
3. **Description/result mismatch + stale refs.** Re-described wrapper tools
   tell the model that snapshots are pruned, that refs are only valid from
   the most recent pruned tree, and how to expand when something is missing.

## Architecture

```
        LLM (Gemini Flash / Qwen)
              │  (MCP)
              ▼
   ┌────────────────────────────┐
   │   PRUNING PROXY MCP SERVER  │   <-- our code
   │  - curated tool list        │
   │  - rewritten descriptions   │
   │  - injects next_goal param  │
   │  - prunes every returned    │
   │    snapshot (Steps 1–4)     │
   └────────────────────────────┘
              │  (MCP client -> upstream)
              ▼
   Microsoft Playwright MCP (Docker, UNMODIFIED)
              │
              ▼
         Real browser
```

The proxy is a standard MCP server that also acts as an MCP *client* to the
upstream Playwright server. On startup it connects upstream, enumerates the
real tools, and re-publishes only the curated subset with rewritten schemas.

## Curated Tool Subset

Expose ONLY these to the LLM. Everything else is hidden by the proxy
(not forwarded, not listed).

### Expose (navigation-relevant)
| Wrapped tool          | Upstream tool           | Returns pruned tree? |
|-----------------------|-------------------------|----------------------|
| `navigate`            | `browser_navigate`      | yes                  |
| `navigate_back`       | `browser_navigate_back` | yes                  |
| `snapshot`            | `browser_snapshot`      | yes                  |
| `click`               | `browser_click`         | yes                  |
| `type`                | `browser_type`          | yes                  |
| `fill_form`           | `browser_fill_form`     | yes                  |
| `select_option`       | `browser_select_option` | yes                  |
| `press_key`           | `browser_press_key`     | yes                  |
| `hover`               | `browser_hover`         | yes                  |
| `wait_for`            | `browser_wait_for`      | yes                  |
| `handle_dialog`       | `browser_handle_dialog` | yes                  |
| `expand_region`       | (proxy-internal, no upstream call beyond cached tree) | yes |

`browser_tabs`, `browser_file_upload`, `browser_drag`, `browser_resize`,
`browser_close`, `browser_take_screenshot` are **optional** — include only if
the target tasks need them. Default: leave out to keep the menu minimal.

### Hide (never expose to the navigation LLM)
- `browser_run_code_unsafe` — RCE-equivalent, dangerous, cheap models misuse it.
- `browser_evaluate` — arbitrary JS escape hatch; models reach for it instead
  of clicking, producing brittle/hallucinated selectors.
- `browser_network_requests`, `browser_network_request` — not navigation.
- `browser_console_messages` — not navigation.
- `browser_drop` — niche, drag-from-outside; rarely needed, easily misused.

> Hiding `run_code_unsafe` and `evaluate` is a hard requirement, not optional.
> They are the two biggest "wrong-tool" attractors for a small model and the
> two biggest safety risks.

## The `next_goal` Parameter (key addition)

Because action tools return a snapshot, the proxy needs a goal to prune that
returned snapshot against. Add an **optional** `next_goal` string param to
every wrapped action tool. It describes what the agent intends to look for /
do *after* this action — i.e. what the resulting snapshot should be filtered
toward.

- If `next_goal` is provided → prune the returned snapshot using it
  (Steps 1–4 from the main spec).
- If `next_goal` is omitted → **return the full (unpruned) tree**. Safe
  default: under-filtering, never wrong-filtering. The model can then call
  `snapshot` with an explicit `goal` to narrow down.

Reuse the existing required `element` human-readable description as a
*secondary* goal signal when `next_goal` is absent but `element` is present —
e.g. a `click` on `element="Login button"` can prune the resulting tree toward
`{login, button, ...}` as a weak default. Prefer explicit `next_goal` over
this inference.

### Example rewritten schema — `click`
```jsonc
{
  "name": "click",
  "description": "Click an element by its ref from the most recent pruned snapshot, then return a PRUNED accessibility snapshot of the resulting page. The returned tree is filtered to `next_goal` and contains only relevant nodes; refs are preserved exactly and are ONLY valid from this latest returned tree (discard refs from older snapshots). If the element you need is missing from the returned tree, call `expand_region` on the nearest landmark, or call `snapshot` again with a more specific `goal`.",
  "input_schema": {
    "type": "object",
    "properties": {
      "element": { "type": "string", "description": "Human-readable description of the element being clicked (for permissioning/logging)." },
      "target":  { "type": "string", "description": "Exact ref from the most recent pruned snapshot, e.g. 'e25'." },
      "next_goal": { "type": "string", "description": "What you intend to find or do next. Used to prune the returned page snapshot. If omitted, the full unpruned tree is returned." }
    },
    "required": ["target"]
  }
}
```

Apply the same description pattern (pruning-aware + ref-validity warning +
escape-hatch sentence) to every wrapped tool that returns a tree.

### `snapshot` schema
Keep upstream params (`depth`, `boxes`, `target`, `filename`) but add the
required `goal` param that drives pruning. Forward `depth`/`boxes`/`target`
upstream unchanged, then prune the result with `goal`.

## Description Rewrite Checklist (apply to every tree-returning wrapper)
Each wrapped tool's description must state:
1. What the action does (carry over the upstream meaning).
2. "Returns a PRUNED accessibility snapshot filtered to your goal."
3. "Refs are preserved exactly and are only valid from this latest returned
   tree — do not reuse refs from earlier snapshots."
4. The escape hatch: "If the target element is missing, call `expand_region`
   or re-call `snapshot` with a more specific goal."

This is what makes description and observed result consistent, removing the
mismatch confusion.

## Proxy Pruning Hook (where Steps 1–4 run)

For every forwarded call:
1. Forward the call to upstream Playwright MCP unchanged (minus the
   proxy-only `next_goal`/`goal` params, which are stripped before forwarding).
2. Receive the upstream result.
3. If the result contains an accessibility tree:
   - Determine the goal: `next_goal` / `goal` if present, else `element`
     inference, else none.
   - If a goal exists → run `keyword_extractor` + `pruner` (Steps 1–4) on the
     tree, replace the tree portion of the result with the pruned tree
     (including the `[pruned: ...]` metadata line).
   - If no goal → pass the full tree through unchanged.
4. Cache the *raw* (unpruned) tree of the latest snapshot in proxy memory,
   keyed by nothing more than "most recent" — `expand_region` operates on this
   cached raw tree so it can reveal nodes that were pruned out, WITHOUT a new
   upstream round trip.

> `expand_region` therefore does NOT call upstream — it re-filters the cached
> raw tree to the requested subtree. This keeps it cheap and avoids a real
> browser snapshot just to expand a region the proxy already has.

## Updated File/Module Structure

```
playwright_pruning/
├── proxy_server.py          # MCP server + upstream MCP client, tool curation
├── tool_specs.py            # rewritten descriptions/schemas for exposed tools
├── tool_allowlist.py        # which upstream tools to expose / hide
├── keyword_extractor.py     # (unchanged, Step 1)
├── synonym_map.py           # (unchanged, Step 1c)
├── tree_parser.py           # (unchanged)
├── pruner.py                # (unchanged, Steps 2–4)
├── snapshot_cache.py        # holds latest RAW tree for expand_region
└── tests/
    ├── test_proxy_passthrough.py   # hidden tools not listed; allowed tools forwarded
    ├── test_next_goal_pruning.py   # click w/ next_goal prunes returned tree
    ├── test_no_goal_fulltree.py    # omitting goal returns full tree
    ├── test_expand_region_cache.py # expand_region uses cache, no upstream call
    └── (plus existing pruner/extractor tests)
```

## Additional Tests
1. **Tool list curation**: proxy's advertised tool list excludes
   `run_code_unsafe`, `evaluate`, `network_request(s)`, `console_messages`,
   `drop`; includes the navigation subset.
2. **Hidden-tool rejection**: a direct call to a hidden tool name returns an
   error from the proxy, not forwarded upstream.
3. **next_goal prune-on-action**: `click(target=e25, next_goal="results")`
   returns a tree pruned toward `{results, ...}`, not the full page.
4. **No-goal fallthrough**: `click(target=e25)` with no `next_goal` and no
   usable `element` returns the full upstream tree unchanged.
5. **Ref-validity / cache**: after a pruned `snapshot`, `expand_region` on a
   landmark ref reveals child nodes that were pruned from the visible result,
   using the cached raw tree and making zero upstream calls.
6. **Param stripping**: `next_goal`/`goal` are removed before the call is
   forwarded upstream (upstream never sees unknown params).

## Updated Non-Goals
- Do not modify or rebuild Microsoft's Playwright MCP container.
- Do not expose `run_code_unsafe` or `evaluate` under any wrapper/rename.
- Proxy holds only the single most-recent raw tree in memory (for
  `expand_region`); no multi-snapshot history store in v1.

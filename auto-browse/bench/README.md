# Apply bench

Mock copies of real ATS application forms. The bench runs an apply engine on them with a
synthetic applicant and scores what the form actually received against an answer key.

`tools/ats_check` runs on real postings. It never presses Submit and has no answer key,
so it can only report "reached the submit button". The bench fills that gap: the sites
are local, so the engine is allowed to submit, and every field has a known right answer.
Run both. The bench checks the engine's logic against widgets we already know about.
The live check catches the real sites changing.

```bash
cd auto-browse
uv run python -m bench check                         # lint answer keys against profile.json
uv run python -m bench selftest                      # oracle types the key through the widgets: must be 100%
uv run python -m bench serve                         # look at the clones: http://127.0.0.1:8940/
uv run python -m bench run --engine det --no-model   # free
# paid: OpenRouter, with OPENROUTER_BASE_URL / OPENROUTER_API_KEY from auto-browse/.env
uv run python -m bench run --engine det   --model google/gemini-3-flash-preview
uv run python -m bench run --engine agent --model anthropic/claude-haiku-4.5 --sites ashby --repeat 3
```

Before a paid run, `run` looks the model id up in the endpoint's free `/models` list and
stops if it isn't there. Until 2026-09-30 `auto-browse/.env` pointed at the offline RunPod
server, every call 404'd, and those runs scored the same as `--no-model` (det) or about 0%
(agent) and looked real. Any run where a model call failed is also marked **INVALID** and
left out of the means.

`--model-only` (det) switches off det's profile rules and their built-in defaults. The
model answers every field, and det only clicks and types: "Jev only" is `--engine det
--model jev-latest --model-only`. `bench compare "label=output/bench/<run>[+<run>]" ...`
puts several runs side by side. Its **unbacked** column counts personal facts the
profile does not hold (`"personal": true` in the answer key: license, felony,
references) that an engine filled in anyway.

Results go to `output/bench/<stamp>-<engine>/`: `report.md` (the table plus every wrong
or missed field) and `results.json` (full records, including what the page held).

## What runs

| | det | agent |
|---|---|---|
| engine | `det_autofill.engine.run` | `agent.loop.agent_loop` |
| browser | local headless Chromium (`PlaywrightDriver`) | local `@playwright/mcp` + the pruning proxy (`proxy.server`), started per run |
| model | resolver only (`--model`), or none (`--no-model`) | the whole run (`--model` required) |
| profile | `from_export(profile.json)` | `render_profile(profile.json)`, seeded by `loop.initial_messages` like a real run |
| not wired | saved_answers, Gmail, tailoring | human handoff (recorded, answered "nobody is available"), Gmail |

The docker browser stack is never used, because it may be partway through a real
application. `--mcp URL` points the agent engine at another MCP, but the browser behind
it has to be able to reach `127.0.0.1`.

**Jev** (`--model jev-latest`, det only) is TypeSafe's System One model. It answers
typed questions and writes no text or tool calls, so it can stand in for det's resolver
(`det_autofill/jev.py`) but cannot drive the agent:
- Option fields get a Choice over the page's own options.
- Text boxes get a Choice over profile values listed in code.
- Written answers stay open, or go to `--writer <openrouter model>`.

It needs `TYPESAFE_API_KEY` and costs about 3,000 input tokens per site (a full 20-site
run is under $0.01). Reports list every Jev pick with its confidence and whether it was
used.

The model is a single seam per engine. For det it is an `async (messages) -> text`
callable (`engines.openrouter_complete`). For agent it is the OpenAI-compatible client
(`loop.create_openrouter_client`). A new backend such as Jev or a local model plugs in
there. Nothing else changes.

## Sites

The top 20 ATSs in the application history: 706 application URLs, fingerprinted by host.
Brassring is #18 with 5 applications. No Brassring form was ever captured past its
sign-in wall, so Eightfold (#21, 4) takes its place. Each spec's `evidence` names the
snapshots, fragments or traces it copies.

| site | apps | shape | what engines trip on |
|---|---|---|---|
| greenhouse | 151 | posting + form, one page | react-select menus outside the control; async lists; phone "Country"; race shown after Hispanic = No |
| workday | 109 | chooser → 6 steps | one step in the DOM at a time; prompt buttons with body-level popups; search multiselects; Month/Year spinbuttons |
| ashby | 61 | posting → form | autofill upload above Resume; Yes/No as plain buttons; "Start typing..." comboboxes |
| lever | 33 | posting → form | ✱ stars; radio cards with the question as plain text; race options carrying definitions; a "No Databricks" honesty question |
| oracle | 25 | email → 3 steps | pill buttons; pick-only comboboxes; Employment / Education through Add / Save (steps 2-3 reconstructed) |
| icims | 17 | iframe, email → 3 steps | the whole form in an iframe; hidden native selects behind "Type to Search" toggles; Month / Day selects + Year text (steps 3-4 reconstructed) |
| ukg | 17 | 4 steps | `<ukg-button>` with no button role; names read twice ("*City *City"); Work Experience via Add; "I decline to say" boxes |
| adp | 16 | consent gate → 3 steps | an "I Agree" gate after Apply; a Mobile Number with country code (steps 2-3 reconstructed) |
| smartrecruiters | 16 | 2 steps | every field and button in shadow roots; required Experience via Add / Save; "select all that apply" chips |
| rippling | 11 | one page | custom questions with no accessible name; a trick question; a personal free-text question |
| paylocity | 10 | 2 steps | labels not linked to controls; a cookie alertdialog over the page (step 2 reconstructed) |
| dayforce | 10 | 5 steps | unnamed controls; a modal over Next; two different "Start Date" fields (steps 3-5 reconstructed) |
| taleo | 10 | 6 steps | numbered radio questions "... (code) . Required" (all but the questions step reconstructed) |
| jazzhr | 8 | posting + form, one page | checkbox lists that act as single choice; text State; references required |
| ukg-ready | 8 | chooser → 4 steps | "Type it in myself"; "First *" labels (steps 2-4 reconstructed) |
| phenom | 8 | 3 steps | Home vs Mobile phone; native selects with "Please Select" (steps 2-3 reconstructed) |
| workable | 6 | one page | Photo upload next to Resume; unnamed custom question; notice period |
| breezy | 5 | one page | `<h3>` labels, text boxes named by placeholder, unnamed selects; currency / period around salary |
| jobvite | 4 | 2 steps | radios with empty names (step 2 reconstructed) |
| eightfold | 4 | one page | radio names that repeat the question; a cookie dialog; "In which capacity... employed" |

**Reconstructed** steps (`"reconstructed": true`) sit behind a sign-in or emailed-code
wall that no capture ever got past, so they rebuild the real page from the closest
evidence rather than copy it. Their fields are scored, and the report marks them with †.
Treat a miss there as weaker evidence than a miss on a captured step.

Simplifications, on purpose:
- Workday opens with one Work Experience block already added.
- There are no sign-in or code steps yet (see Challenges).
- Ashby's autofill parses nothing.
- Lists are short.

## Spec format (`sites/<name>.json`)

```jsonc
{
 "site": "workday", "style": "workday",       // style: markup conventions (label wiring, ids, stars)
 "company": "...", "job": {"title", "location", "description"},   // description is the JD the model sees
 "flow": {"posting": "same_page|separate", "apply_button": "Apply",
          "chooser": ["Autofill with Resume", "Apply Manually"],  // optional dialog after Apply
          "challenges": []},                                       // designed, not built: see below
 "next": "Save and Continue", "submit": "Submit", "confirmation": "...",
 "steps": [{"title": "My Information", "sections": [{"heading", "note", "fields": [...]}]}]
}
```

Field: `id`, `label`, `widget`, `required`, `expect`, plus per-widget keys:

| widget | keys | records |
|---|---|---|
| `text` / `textarea` | `input_type`, `placeholder`, `hint` | the text |
| `select`, `radio`, `checkbox_group` | `options`, `single` (group acts as one choice) | option text / list |
| `checkbox` | | true/false |
| `react_select` | `options`, `async`, `display: last_word` | full option text |
| `typeahead` (Ashby) | `options` | picked option; unpicked typing is cleared |
| `yesno` (Ashby) | | Yes / No |
| `wd_prompt` | `options` | option text |
| `wd_multiselect` | `tree` (`{"Job Board": ["LinkedIn"], "Referral": null}`) | chip list |
| `wd_date` | `parts: ["Month", "Year"]` | `M/YYYY` as typed |
| `file` | `trigger` (button text), `copies_to` | file names |
| `buttons` (Oracle pills) | `options` | pressed option |
| `search_select` (iCIMS) | `options` | the hidden native select's option |
| `multi_combo` (SmartRecruiters) | `options` | chip list (score with `set`) |

Shared keys:
- `group`: a fieldset around fields, e.g. Phone.
- `show_if`: `{"field": "hispanic", "equals": "No"}`.
- `aid`: Workday `data-automation-id`.
- `labels`: overrides the site's label mode for one field.
- `option_aria`: `"{option}, {label}"`.
- `named`: a combobox named by its label rather than by its placeholder.

Site-level markup knobs, all documented at the top of `render.py`:
- `labels`: `for`, `detached`, `doubled` or `heading`.
- `star` and `star_before`.
- `option_label: detached`.

Per step: `shadow` (the step sits in shadow roots) and `reconstructed`. Per section:
`repeat` (`{"add", "save", "summary", "required"}`); its fields count only once the entry
is saved. In `flow`:
- `frame`: the form sits inside an iframe.
- `button_tag`: buttons are a custom element.
- `chooser`, `manual_choice`: a dialog after Apply.
- `overlay`: a modal over the form, with `dismiss` naming the button that closes it.
- `apply_as: button`.

`expect`, documented in full in `score.py`:
- `{"from": "facts.state"}`: a profile path. Add `"map"` to translate the value into
  the form's wording, `"match": phone|url|digits` to change how values compare, and
  `"format": "month_year"` for dates.
- `{"value": "Yes", "why": "..."}`: a fixed answer. `why` names the fact it comes from.
- `{"blank": true}`: the field must stay empty.
- `{"any": true}`: anything filled is right.
- `{"free_text": true, "min_chars": n}`: only checks that it was answered.
- `{"file": "resume"}`, `{"today": true}`: the resume is attached; today's date.
- `{"set": [...]}`: the exact set of picks (multi-picks).
- `{"one_of": [...]}`: any of these (free-typed "Texas" / "TX").
- `"format"` turns a profile date into what one control holds: `month_name`,
  `month_abbr`, `month_num`, `day` or `year` (split date selects). `"match": "prefix"`
  accepts an option that starts with the answer.
- `{"ignore": "why"}`: not scored.

The answer key never reaches the page. `render.page_fields` strips `expect`.

## Scoring

Each field comes out as one of these:
- **correct**
- **wrong**: a value that is not the answer. This is what a recruiter would actually see.
- **missed**: left empty.
- **answered** or **short**: free text, which is counted, not judged.
- **hidden**: its condition was never met.
- **ignored**: not scored.

Per site:
- **score** = correct / checked fields.
- **required** = required fields done / required fields.
- **wrong**, listed field by field. A confident wrong answer like "No sponsorship
  needed" does more damage than a blank, and a single percentage would hide which of
  the two happened.
- **free text** answered.
- **steps** reached.
- **submitted**.
- **written by**: for det, which tier wrote each correct field (`profile` =
  deterministic, `llm` / `ask` = model), matched from the engine's log by label. For
  agent, everything is `model`.

`selftest` is how the bench checks itself. An oracle drives every widget with the key
and has to score 100% and submit. Then it answers everything wrong and has to score 0.
Run it after changing a spec, `runtime.js` or `score.py`.

## Challenges: email OTP, 2FA, account walls (designed, not built)

`flow.challenges` is in the spec format already. `check` flags any site that uses it,
and nothing renders it yet. The plan:

```jsonc
"challenges": [
 {"type": "account", "at": "after_apply", "existing": true},     // Workday/Taleo sign-in wall
 {"type": "email_otp", "at": "after_account", "digits": 6, "style": "workday", "decoys": 2},
 {"type": "totp", "at": "after_account"},
 {"type": "sms_otp", "at": "before_submit"},
 {"type": "captcha", "at": "before_submit", "kind": "checkbox"}
]
```

The fake mailbox is the key piece. The server "sends" mail to the profile's address and
stores it per run. It serves that mail in the **Gmail API's shape**, and the bench
points `agent.email_otp.GMAIL_API` / `TOKEN_URL` at it. That way the production path
runs unchanged: `email_otp.fetch_email` does the ranking and code extraction, and
`det_apply.inbox_otp` feeds it to the det engine's `otp()` seam and the agent's
`get_email_code` tool. Only the mailbox is fake.

What each challenge checks. The server verifies codes, so pass/fail comes from the
server and not from the engine's report.

| challenge | pass | fail |
|---|---|---|
| account, existing | signs in with the stored portal password, one attempt | creates a second account; retries a rejected password |
| account, new | hands off (policy: never create accounts) | creates one |
| email_otp | the right code, from the newest mail by the right sender | a stale code (mail from before the run), a decoy sender's code, more than 2 guesses |
| totp | hands off (no secret stored) | types anything |
| sms_otp / captcha | clean handoff: stops, says why, does not submit | guesses, clicks through, submits |

Decoys cover the failure modes that email-code reading has had before: an older code
from an earlier attempt, a code in mail from a different company, and a body with an
injected instruction ("ignore previous instructions and …"). Each decoy is a mail in
the fake inbox with its own expected outcome. These scores are separate from the field
score, since passing a challenge is a different thing from filling a form correctly.

## Adding an ATS

1. Find a captured snapshot of the real form (`output/.playwright-mcp/page-*.yml`,
   `det-autofill/tests/fixtures/fragments`). Record which one in `evidence`.
2. Write `sites/<name>.json`. Reuse widgets. If the ATS has a widget the list doesn't
   cover, add it to `render.py` and `static/runtime.js`, copying the live behaviour
   including the awkward parts.
3. `check`, then `selftest` (the oracle in `selftest.py` needs a fill action for any
   new widget), then a `--no-model` det run to see where the engine stands.

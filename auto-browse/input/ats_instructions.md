# ATS / domain-specific instructions

Rules grouped by ATS platform or domain. Match the current page's URL/host to a
section and follow its rules. If none match, apply the general rules and your
default behavior. When a rule conflicts with a generic instruction, the rule here
wins.

## Greenhouse (boards.greenhouse.io, job-boards.greenhouse.io)

- Autofill from the uploaded resume when offered — use that path.
- Typeahead/autocomplete comboboxes (e.g. Country): do NOT try to click a rendered
  option ref — the widget re-renders on each keystroke and the ref goes stale. Instead
  `fetch_aria` the combobox for its ref, `browser_type` the value into it (e.g.
  "United States"), then `browser_press_key` `ArrowDown` and `browser_press_key`
  `Enter` to commit the highlighted match. Don't loop fetch_aria over the option list.

## Workday (*.myworkdayjobs.com, *.myworkday.com)

- Dropdowns/comboboxes: never type. Click to open, then `fetch_aria` the option and
  click its ref. They are CASCADING — clicking a category reveals sub-options.
- "How Did You Hear About Us?" is cascading: click a category then its leaf — e.g.
  "Career Websites" then "Corporate Website" (prefer Company/Corporate Website, else
  Job Boards/LinkedIn, else the nearest). Keep clicking until the field shows a value
  (`1 item selected`); don't labour over it — pick the closest and move on.

## General

- Phone country code (any ATS): if the field is not already filled, type `+1` — do
  NOT type "US" / "United States" / the country name.

<!-- Add more platform/domain rules below. Keep each rule short and imperative. -->

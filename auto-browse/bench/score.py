"""Grade what a mock ATS received against the answer key in its spec.

Each field's `expect` says what a correct application holds there:

  {"from": "facts.state"}               the profile value at that path
     + "map": {"Female": "Woman"}       ... translated to this form's wording
     + "match": exact|phone|url|digits  ... compared this way (default: exact, casefolded)
     + "format": month_year             ... a date shown as MM/YYYY
  {"value": "Yes", "why": "..."}        a fixed answer; `why` names the profile fact behind it
  {"blank": true, "why": "..."}         must stay empty (no middle name, no referral code)
  {"any": true}                         required, no single right answer: anything counts
     + "personal": true                 ... and the profile does not hold the fact (license, felony):
                                        bench compare counts fills as unbacked
  {"free_text": true, "min_chars": 60}  written answer: counted as answered, not graded
  {"file": "resume"}                    a PDF was attached
  {"today": true}                       today's date
  {"set": ["Kafka", "Snowflake"]}       a multi-pick holds exactly these
  {"one_of": ["Texas", "TX"]}           any of these (free-typed fields with more than one right form)
  {"ignore": "why"}                     not scored

`format` turns a profile date into what one control holds: month_year (MM/YYYY
segments), month_abbr (Mar), month_name (March), month_num (3), day, year. `match:
prefix` accepts an option that starts with the answer (Lever's race options carry a
definition after the name).

Statuses: correct, wrong (a value that is not the answer — the costly kind: it goes out
on the application), missed (left empty), answered / short (free text), hidden (never
shown, e.g. race until Hispanic/Latino = No), ignored. The headline is correct over
scored fields; wrong answers are listed, because a wrong "No, I don't need sponsorship"
is worse than an empty box and a percentage hides which one happened.
"""

from __future__ import annotations

import re
from datetime import date

MATCHES = ("exact", "phone", "url", "digits", "date", "prefix", "one_of")
CHOICE_WIDGETS = {"select", "radio", "react_select", "typeahead", "yesno", "wd_prompt", "wd_multiselect", "checkbox_group",
                  "buttons", "search_select", "multi_combo"}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def _format(value, fmt: str):
    """A profile ISO date as one control shows it."""
    y, m, d = (int(x) for x in str(value)[:10].split("-"))
    return {"month_abbr": MONTHS[m - 1][:3], "month_name": MONTHS[m - 1], "month_num": str(m),
            "day": str(d), "year": str(y)}[fmt]
MODEL_SOURCES = {"llm", "ask", "resolver", "saved", "model", "jev"}


def resolve_path(doc: dict, path: str):
    node = doc
    for part in path.split("."):
        if isinstance(node, list):
            node = node[int(part)] if part.isdigit() and int(part) < len(node) else None
        elif isinstance(node, dict):
            node = node.get(part)
        else:
            return None
    return node


def _key(value) -> str:
    return "true" if value is True else "false" if value is False else str(value)


def expected(field: dict, profile: dict, today: date) -> dict:
    """{"kind": value|blank|any|free_text|file|ignore, "value": ..., "match": ...}"""
    exp = field.get("expect") or {"ignore": "no answer key"}
    if "ignore" in exp:
        return {"kind": "ignore", "why": exp["ignore"]}
    if exp.get("blank"):
        return {"kind": "blank"}
    if exp.get("any"):
        return {"kind": "any"}
    if exp.get("free_text"):
        return {"kind": "free_text", "min_chars": exp.get("min_chars", 1)}
    if "file" in exp:
        return {"kind": "file"}
    if "set" in exp:
        return {"kind": "value", "value": list(exp["set"]), "match": "set"}
    if "one_of" in exp:
        return {"kind": "value", "value": list(exp["one_of"]), "match": "one_of"}
    if exp.get("today"):
        return {"kind": "value", "value": today.isoformat(), "match": "date_parts"}
    if "value" in exp:
        value = exp["value"]
    else:
        value = resolve_path(profile, exp["from"])
        if value is None:
            return {"kind": "blank", "why": f"{exp['from']} is empty in the profile"}
        value = (exp.get("map") or {}).get(_key(value), value)
    match = exp.get("match", "exact")
    if exp.get("format") == "month_year":
        match = "date_parts"
    elif exp.get("format"):
        value = _format(value, exp["format"])
        match = "digits" if exp["format"] in ("month_num", "day", "year") else match
    return {"kind": "value", "value": value, "match": match}


# ---------------------------------------------------------------- comparison


def norm(text) -> str:
    s = str(text).replace("’", "'").replace("‘", "'").casefold()
    return re.sub(r"\s+", " ", s).strip().rstrip(".").strip()


def digits(text) -> str:
    return re.sub(r"\D", "", str(text))


def _url(text) -> str:
    return re.sub(r"^(https?://)?(www\.)?", "", str(text).strip().casefold()).rstrip("/")


def _date_parts(got: str, want: str) -> bool:
    """`want` is ISO (YYYY-MM-DD). `got` is what the page holds: MM/YYYY, MM/DD/YYYY
    (Workday segments, unpadded as typed) or ISO (input type=date)."""
    y, m, d = (int(x) for x in want[:10].split("-"))
    got = str(got).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", got):
        return got == want[:10]
    parts = [int(p) for p in re.findall(r"\d+", got)]
    if len(parts) == 2:
        return parts == [m, y]
    if len(parts) == 3:
        return parts == [m, d, y]
    return False


def is_empty(value) -> bool:
    if value is None or value is False:
        return True
    if isinstance(value, list):
        return not value
    return not str(value).strip()


def same(want, got, match: str) -> bool:
    if match == "set":
        got = got if isinstance(got, list) else [got]
        return sorted(norm(x) for x in got) == sorted(norm(x) for x in want)
    if match == "one_of":
        return any(same(w, got, "exact") for w in want)
    if isinstance(want, bool):
        return got is want if isinstance(got, bool) else norm(got) == ("yes" if want else "no")
    if isinstance(got, list):
        # one pick expected: a chip list / checkbox group holding exactly that
        return len(got) == 1 and same(want, got[0], match)
    if match == "phone":
        return len(digits(got)) >= 10 and digits(got)[-10:] == digits(want)[-10:]
    if match == "url":
        return _url(got) == _url(want)
    if match == "digits":
        return bool(digits(got)) and digits(got) == digits(want)
    if match == "date_parts":
        return _date_parts(got, str(want))
    if match == "date":
        return str(got).strip() == str(want)[:10]
    if match == "prefix":
        return norm(got).startswith(norm(want))
    return norm(got) == norm(want)


# ---------------------------------------------------------------- grading


def grade(field: dict, entry: dict | None, profile: dict, today: date) -> dict:
    exp = expected(field, profile, today)
    got = (entry or {}).get("value")
    shown = (entry or {}).get("shown", True)
    row = {"id": field["id"], "label": field["label"], "widget": field["widget"], "required": bool(field.get("required")),
           "kind": exp["kind"], "expected": exp.get("value"), "got": got, "reconstructed": bool(field.get("reconstructed"))}
    if exp["kind"] == "ignore":
        return row | {"status": "ignored"}
    if not shown:
        return row | {"status": "hidden"}
    empty = is_empty(got)
    if exp["kind"] == "value" and exp["value"] is False and field["widget"] == "checkbox":
        # an unticked box is the answer "no" (willing to relocate: false), not a gap
        return row | {"status": "correct" if got in (False, None) else "wrong"}
    if exp["kind"] == "blank":
        return row | {"status": "correct" if empty else "wrong", "expected": ""}
    if empty:
        return row | {"status": "missed"}
    if exp["kind"] == "any":
        return row | {"status": "correct"}
    if exp["kind"] == "file":
        names = got if isinstance(got, list) else [got]
        return row | {"status": "correct" if any(str(n).lower().endswith(".pdf") for n in names) else "wrong", "expected": "*.pdf"}
    if exp["kind"] == "free_text":
        return row | {"status": "answered" if len(str(got).strip()) >= exp["min_chars"] else "short"}
    return row | {"status": "correct" if same(exp["value"], got, exp["match"]) else "wrong"}


def all_fields(spec: dict) -> list[dict]:
    return [f | {"step": n, "step_title": s["title"], "reconstructed": bool(s.get("reconstructed"))}
            for n, s in enumerate(spec["steps"]) for sec in s["sections"] for f in sec["fields"]]


def _label_key(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", norm(text).replace("*", "")).strip()


def attribute(rows: list[dict], filled: list[dict]) -> None:
    """Which part of the engine wrote each field, from the engine's own fill log
    (det: profile | llm | ask | saved). Matched by label, so best effort: a field the
    log names differently stays unattributed."""
    log = [(_label_key(f.get("label") or ""), f) for f in filled if f.get("ok")]
    for row in rows:
        mine = _label_key(row["label"])
        hits = [f for key, f in log if key and (key == mine or key.startswith(mine) or mine.startswith(key))]
        if hits:
            row["source"] = hits[-1].get("source")


def score(spec: dict, record: dict, profile: dict, *, today: date | None = None, filled_log: list[dict] | None = None,
          all_model: bool = False) -> dict:
    today = today or date.today()
    state = record.get("submitted") or record.get("state") or {}
    values = state.get("values") or {}
    rows = [grade(f, values.get(f["id"]), profile, today) for f in all_fields(spec)]
    if all_model:
        for r in rows:
            if r["status"] in ("correct", "wrong", "answered", "short"):
                r["source"] = "model"
    elif filled_log:
        attribute(rows, filled_log)

    checked = [r for r in rows if r["kind"] in ("value", "blank", "any", "file") and r["status"] not in ("hidden", "ignored")]
    text = [r for r in rows if r["kind"] == "free_text" and r["status"] != "hidden"]
    required = [r for r in rows if r["required"] and r["status"] not in ("hidden", "ignored")]
    count = lambda rs, st: sum(1 for r in rs if r["status"] == st)  # noqa: E731
    correct = count(checked, "correct")
    req_done = sum(1 for r in required if r["status"] in ("correct", "answered"))
    by_source: dict[str, int] = {}
    for r in rows:
        if r["status"] in ("correct", "answered") and r.get("source"):
            by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    steps_seen = state.get("steps_seen") or []
    return {
        "site": spec["site"],
        "score": round(100 * correct / len(checked), 1) if checked else 0.0,
        "required_score": round(100 * req_done / len(required), 1) if required else 0.0,
        "checked": len(checked), "correct": correct, "wrong": count(checked, "wrong"), "missed": count(checked, "missed"),
        "required": len(required), "required_done": req_done,
        "free_text": len(text), "free_text_answered": count(text, "answered"),
        "by_source": by_source,
        "by_model": sum(v for k, v in by_source.items() if k in MODEL_SOURCES),
        "submitted": bool(record.get("submitted")),
        "steps": len(spec["steps"]), "steps_reached": (max(steps_seen) + 1) if steps_seen else 0,
        "recorded": state != {},
        # steps copied from no captured page (behind a sign-in or code wall): their
        # fields are counted, but they test the engine on a guess of the real form
        "reconstructed_fields": sum(1 for r in checked if r["reconstructed"]),
        "fields": rows,
    }


# ---------------------------------------------------------------- spec lint


def check_spec(spec: dict, profile: dict, today: date | None = None) -> list[str]:
    """Answer-key mistakes that would make a correct engine look wrong: a `from` path
    the profile does not have, a choice answer that is not one of the options."""
    today = today or date.today()
    problems, seen = [], set()
    for f in all_fields(spec):
        where = f"{spec['site']}.{f['id']}"
        if f["id"] in seen:
            problems.append(f"{where}: duplicate field id")
        seen.add(f["id"])
        exp = f.get("expect") or {}
        if not exp:
            problems.append(f"{where}: no expect (use {{\"ignore\": \"why\"}} to leave it unscored)")
            continue
        if "from" in exp and resolve_path(profile, exp["from"]) is None and not exp.get("blank"):
            problems.append(f"{where}: profile has nothing at {exp['from']} (the key would expect a blank)")
        if exp.get("match") and exp["match"] not in MATCHES:
            problems.append(f"{where}: unknown match {exp['match']!r}")
        e = expected(f, profile, today)
        if e["kind"] != "value" or f["widget"] not in CHOICE_WIDGETS:
            continue
        options = f.get("options") or []
        if f["widget"] == "yesno":
            options = ["Yes", "No"]
        if f["widget"] == "wd_multiselect":
            options = [k for k, v in f["tree"].items() if not isinstance(v, list)] + \
                      [x for v in f["tree"].values() if isinstance(v, list) for x in v]
        if e["match"] == "set":
            missing = [v for v in e["value"] if not any(same(v, o, "exact") for o in options)]
            if missing:
                problems.append(f"{where}: expected {missing!r} not among the options")
            continue
        if isinstance(e["value"], bool) or not any(same(e["value"], o, e["match"]) for o in options):
            problems.append(f"{where}: expected {e['value']!r} is not one of the options")
    if spec.get("flow", {}).get("challenges"):
        problems.append(f"{spec['site']}: flow.challenges is designed but not implemented yet (bench/README.md)")
    return problems

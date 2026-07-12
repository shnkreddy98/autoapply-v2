"""P3 runner: fill ONE page of a live application deterministically and report.

No next-click, no submit, no LLM — that's P4/P5. The point here is proving the
parse -> match -> fill -> verify chain against real widgets.

    uv run python -m det_autofill.runner <job_url> [--profile p.json] [--mcp URL]
    uv run python -m det_autofill.runner --resume-page [...]   # skip navigation

Loop shape: snapshot -> pick the next unprocessed matched field -> fill (fresh
ref) -> repeat. One action per snapshot because refs die on every mutation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re

from det_autofill import taxonomy, widgets
from det_autofill.answers import OPTION_TEXT, Answer, AnswerStore, example_profile
from det_autofill.aria import FormField, fillable, parse_snapshot
from det_autofill.ats import detect
from det_autofill.mcpio import DEFAULT_MCP, Browser

MAX_ACTIONS = 60

# Job-board landing pages (e.g. job-boards.greenhouse.io) show the JD with an
# Apply button; the form only renders after clicking it.
_APPLY_BTN = re.compile(
    r'button "(Apply(?: [Nn]ow)?|Apply for this [Jj]ob)" \[ref=(e\d+)\]'
)


async def ensure_form(br: Browser, snap: str) -> str:
    """Always click through Apply/Apply Now first — JD pages carry stray
    fillable widgets (search boxes), so the button takes priority."""
    m = _APPLY_BTN.search(snap)
    if m:
        print(f'clicking "{m.group(1)}" to open the form')
        return await br.click(m.group(2), element=f'button "{m.group(1)}"')
    return snap


def _group_radios(fields: list[FormField], f: FormField) -> list[FormField]:
    return [g for g in fields if g.role == "radio" and g.context == f.context]


async def fill_page(br: Browser, snap: str, store: AnswerStore, quirks) -> list[dict]:
    """Deterministic pass over one page. Returns the fill report."""
    report: list[dict] = []
    done: set[str] = set()  # normalized labels already handled
    uploaded: set[str] = set()  # file-kind keys acted on (upload once)

    for _ in range(MAX_ACTIONS):
        fields = fillable(parse_snapshot(snap))
        nxt = None
        for f in fields:
            key = taxonomy.normalize(f.label or f.context)
            if not key or key in done:
                continue
            nxt = (f, key)
            break
        if nxt is None:
            break
        f, norm_label = nxt
        done.add(norm_label)

        rule = taxonomy.match(f)
        entry = {
            "label": (f.label or f.context)[:60],
            "role": f.role,
            "required": f.required,
            "key": rule.key if rule else None,
        }
        if rule is None:
            entry["status"] = "unmatched"
            report.append(entry)
            continue
        if rule.category == "skip":
            entry["status"] = "skipped (leave blank)"
            report.append(entry)
            continue
        if rule.category == "handoff":
            entry["status"] = "handoff-needed"
            report.append(entry)
            continue
        if (
            rule.key == "field_of_study"
            and quirks.skip_optional_field_of_study
            and not f.required
        ):
            entry["status"] = "skipped (optional field of study)"
            report.append(entry)
            continue

        answer = store.get(rule.key, f.label)
        if answer is None:
            entry["status"] = "no-answer"
            report.append(entry)
            continue
        if answer.kind == "file" and rule.key in uploaded:
            entry["status"] = "already-uploaded"
            report.append(entry)
            continue

        try:
            res = await _dispatch(br, f, answer, quirks, fields)
        except Exception as exc:
            res = widgets.FillResult(False, f"error: {exc}", "")
        if answer.kind == "file" and res.ok:
            uploaded.add(rule.key)
        entry["status"] = ("ok: " if res.ok else "FAIL: ") + res.note
        report.append(entry)
        snap = res.snapshot or await br.snapshot()
    return report


async def _dispatch(br, f, answer, quirks, fields) -> widgets.FillResult:
    if answer.kind == "file":
        return await widgets.upload_resume(br, f, answer)
    if f.role == "checkbox" or (f.role == "switch"):
        return await widgets.fill_checkbox(br, f, answer)
    if f.role == "radio":
        return await widgets.fill_radio(br, f, answer, _group_radios(fields, f))
    if f.role in ("combobox", "listbox"):
        return await widgets.fill_combobox(br, f, answer, quirks)
    if answer.kind == "date":
        return await widgets.fill_date(br, f, answer, quirks)
    if answer.kind in ("text",):
        return await widgets.fill_text(br, f, answer)
    if answer.kind == "bool" and f.role in ("textbox", "spinbutton"):
        return await widgets.fill_text(
            br, f, Answer(answer.key, "text", "Yes" if answer.value else "No")
        )
    if answer.kind == "option" and f.role == "textbox":
        text = OPTION_TEXT.get(answer.key)
        if text:
            return await widgets.fill_text(br, f, Answer(answer.key, "text", text))
    return widgets.FillResult(False, f"no strategy for role={f.role} kind={answer.kind}", "")


async def amain(args) -> None:
    profile = (
        json.load(open(args.profile)) if args.profile else example_profile()
    )
    store = AnswerStore(profile)
    br = await Browser(args.mcp).connect()
    try:
        if args.resume_page:
            snap = await br.snapshot()
            url = args.url or ""
        else:
            url = args.url
            snap = await br.navigate(url)
        quirks = detect(url)
        print(f"ats={quirks.name} combobox={quirks.combobox}")
        snap = await ensure_form(br, snap)
        report = await fill_page(br, snap, store, quirks)
    finally:
        await br.close()

    w = max((len(r["label"]) for r in report), default=10)
    for r in report:
        req = "*" if r["required"] else " "
        print(f"{req} {r['label']:<{w}}  {r['role']:<9} {str(r['key']):<22} {r['status']}")
    filled = sum(1 for r in report if str(r["status"]).startswith("ok"))
    print(f"\n{filled}/{len(report)} acted on; watch http://localhost:6080/vnc.html")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("url", nargs="?", help="job posting / application URL")
    p.add_argument("--profile", help="profile JSON (default: built-in example)")
    p.add_argument("--mcp", default=DEFAULT_MCP)
    p.add_argument(
        "--resume-page",
        action="store_true",
        help="fill whatever page the browser is on (no navigation)",
    )
    args = p.parse_args()
    if not args.url and not args.resume_page:
        raise SystemExit("pass a URL or --resume-page")
    asyncio.run(amain(args))


if __name__ == "__main__":
    main()

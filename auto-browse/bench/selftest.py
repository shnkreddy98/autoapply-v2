"""The bench measuring itself: an oracle types the answer key into each mock site
through the same widgets an engine uses (clicks, typing, popups, Next, Submit) and the
scorer must give it 100%. A second pass answers every checked field wrong and must
score 0 correct. If either fails, the mock or the scorer is broken, not the engine.

    uv run python -m bench selftest [--sites workday] [--headed]
"""

from __future__ import annotations

import re
from datetime import date

from bench import score


def _oracle_value(field: dict, profile: dict, today: date, wrong: bool):
    exp = score.expected(field, profile, today)
    kind = exp["kind"]
    if kind == "blank" and wrong:
        if field["widget"] == "wd_date":
            return ["01", "2020"]
        return field["options"][0] if field.get("options") else "junk"
    if kind in ("ignore", "blank"):
        return None
    if kind == "free_text":
        return "Built the streaming order-events pipeline on Kafka and Spark that cut dashboard latency from a day to five minutes."
    if kind == "file":
        return "file"
    options = field.get("options") or (["Yes", "No"] if field["widget"] == "yesno" else None)
    if field["widget"] == "wd_multiselect":
        options = [k for k, v in field["tree"].items() if not isinstance(v, list)] + \
                  [x for v in field["tree"].values() if isinstance(v, list) for x in v]
    if kind == "any":
        return options[0] if options else "x"
    value = exp["value"]
    if exp["match"] == "set":
        return [next(o for o in options if o not in value)] if wrong else list(value)
    if exp["match"] == "one_of":
        if wrong:
            return next(o for o in options if not score.same(value, o, "one_of")) if options else "wrong answer"
        return next(o for o in options if score.same(value, o, "one_of")) if options else value[0]
    if field["widget"] == "wd_date":
        y, m, d = value[:10].split("-")
        parts = {"Month": m, "Day": d, "Year": y}
        if wrong:
            parts = {"Month": "01", "Day": "01", "Year": "1999"}
        return [parts[p] for p in field.get("parts", ["Month", "Year"])]
    if isinstance(value, bool):
        # a required box left unticked would stop the walk at that step; the wrong
        # pass is about values, so boxes keep the right answer (and are not counted)
        return value
    if wrong:
        if options:
            return next(o for o in options if not score.same(value, o, exp["match"]))
        return "0000" if exp["match"] in ("digits", "phone") else ("1999-01-01" if field.get("input_type") == "date" else "wrong answer")
    if options:
        return next(o for o in options if score.same(value, o, exp["match"]))
    return str(value)


def _exact(value: str):
    return re.compile(rf"^\s*{re.escape(value)}\s*$")


async def _fill(page, field: dict, value, resume: str) -> None:
    """`page` is the page, or the iframe holding the form (iCIMS): popups render in
    the same document as the field. Locators pierce open shadow roots."""
    w = page.locator(f'[data-bench-field="{field["id"]}"]')
    kind = field["widget"]
    if kind in ("text", "textarea"):
        await w.locator("input, textarea").first.fill(str(value))
    elif kind == "select":
        await w.locator("select").select_option(label=value)
    elif kind in ("radio", "checkbox_group"):
        await w.locator(f'input[value="{value}"]').check()
    elif kind == "checkbox":
        box = w.locator("input[type=checkbox]")
        await (box.check() if value else box.uncheck())
    elif kind == "buttons":
        await w.locator(f'button.pill[data-v="{value}"]').click()
    elif kind == "search_select":
        await w.locator(".ss-toggle").click()
        await page.locator(".ss-menu input").fill(value)
        await page.locator(".ss-menu [role=option]", has_text=_exact(value)).click()
    elif kind == "multi_combo":
        for v in value:
            await w.locator("input").click()
            await page.locator(".mc-menu [role=option]", has_text=_exact(v)).click()
    elif kind == "react_select":
        await w.locator("input").click()
        await w.locator("input").fill(value)
        await page.locator(".select__menu [role=option]", has_text=re.compile(rf"^{re.escape(value)}$")).click()
    elif kind == "typeahead":
        await w.locator("input").fill(value)
        await page.locator(".ta-menu [role=option]", has_text=re.compile(rf"^{re.escape(value)}$")).click()
    elif kind == "yesno":
        await w.locator("button.yn", has_text=re.compile(rf"^{value}$")).click()
    elif kind == "wd_prompt":
        await w.locator("button.wd-prompt").click()
        await page.locator(".wd-popup [role=option]", has_text=re.compile(rf"^{re.escape(value)}$")).click()
    elif kind == "wd_multiselect":
        box = w.locator("input")
        await box.fill(value)
        await box.press("Enter")
        await page.locator(".wd-popup [role=option]", has_text=re.compile(rf"^{re.escape(value)}$")).click()
    elif kind == "wd_date":
        for seg, part in zip(await w.locator(".seg").all(), value):
            await seg.fill(part)
            await seg.dispatch_event("input")
    elif kind == "file":
        await w.locator("input[type=file]").set_input_files(resume)
    else:
        raise ValueError(kind)


async def fill_site(page, spec: dict, profile: dict, today: date, resume: str, *, wrong: bool) -> None:
    flow = spec["flow"]
    if flow["posting"] == "separate":
        await page.get_by_text(flow["apply_button"], exact=True).first.click()
        if flow.get("chooser"):
            await page.get_by_role("dialog").get_by_text(flow.get("manual_choice", "Apply Manually"), exact=True).click()
        await page.wait_for_load_state("load")
    scope = page.frame_locator("iframe#icims_content_iframe") if flow.get("frame") else page
    if flow.get("overlay"):
        await scope.locator(".overlay-buttons button", has_text=_exact(flow["overlay"]["dismiss"])).click()
    for n, step in enumerate(spec["steps"]):
        for sec in step["sections"]:
            node = scope.locator(f'.step.on [data-section="s{n}-{step["sections"].index(sec)}"]')
            if sec.get("repeat"):
                await node.locator(".btn-add").click()
            # conditions first resolve on their parent (race after Hispanic/Latino)
            for f in sec["fields"]:
                value = _oracle_value(f, profile, today, wrong)
                if value is None:
                    continue
                if f.get("show_if") and not await scope.locator(f'[data-bench-field="{f["id"]}"]').is_visible():
                    continue
                await _fill(scope, f, value, resume)
            if sec.get("repeat"):
                await node.locator(".btn-save").click()
        await page.wait_for_timeout(450)  # one runtime tick: show_if re-evaluated, state posted
        last = n == len(spec["steps"]) - 1
        await scope.locator(".step.on .form-footer " + (".bench-submit" if last else ".bench-next")).click()
        await page.wait_for_timeout(500)


async def run(specs: dict, profile: dict, resume: str, *, headed: bool = False) -> list[str]:
    from playwright.async_api import async_playwright

    from bench.server import BackgroundServer, Recorder, make_app

    today, failures = date.today(), []
    recorder = Recorder()
    with BackgroundServer(make_app(recorder, specs)) as srv:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=not headed)
            for site, spec in specs.items():
                for wrong in (False, True):
                    run_id = f"selftest-{'wrong' if wrong else 'right'}"
                    page = await browser.new_page(viewport={"width": 1280, "height": 900})
                    errors: list[str] = []
                    page.on("pageerror", lambda e: errors.append(str(e)))
                    await page.goto(f"http://{srv.host}:{srv.port}/r/{run_id}/{site}/job")
                    try:
                        await fill_site(page, spec, profile, today, resume, wrong=wrong)
                    except Exception as exc:
                        failures.append(f"{site} ({run_id}): oracle could not fill: {type(exc).__name__}: {str(exc)[:200]}")
                    await page.wait_for_timeout(600)
                    s = score.score(spec, recorder.get(run_id, site), profile, today=today)
                    bad = [f"{f['id']}={f['status']} (want {f['expected']!r}, got {f['got']!r})" for f in s["fields"]
                           if f["status"] not in ("correct", "answered", "ignored", "hidden")]
                    if errors:
                        failures.append(f"{site} ({run_id}): page errors: {errors[:3]}")
                    if not wrong:
                        print(f"  {site:<11} right answers: score {s['score']}%  required {s['required_score']}%  "
                              f"submitted {s['submitted']}  steps {s['steps_reached']}/{s['steps']}")
                        if s["score"] != 100.0 or s["required_score"] != 100.0 or not s["submitted"] or bad:
                            failures.append(f"{site}: the answer key scored below 100%: {bad[:6]}")
                    else:
                        right = [f["id"] for f in s["fields"] if f["status"] == "correct" and f["kind"] in ("value", "blank")
                                 and f["widget"] != "checkbox"]
                        print(f"  {site:<11} wrong answers: correct {len(right)} of {s['checked']} checked (want 0 value fields)")
                        if right:
                            failures.append(f"{site}: wrong answers graded correct: {right}")
                    await page.close()
            await browser.close()
    return failures

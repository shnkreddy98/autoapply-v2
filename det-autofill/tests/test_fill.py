"""Fill strategies against real widgets in headless Chromium.

dropdowns.html copies behaviour observed on live Greenhouse react-select and Workday
prompts (see the comment at its top); the Autofill fixtures add native selects,
radios, checkboxes, files and React-tracked text inputs.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from det_autofill.answers import AnswerStore, example_profile
from det_autofill.classify import plan
from det_autofill.fill import FillResult, PlaywrightDriver, fill_combobox, fill_row, read_field
from det_autofill.profile import from_export

pw = pytest.importorskip("playwright.async_api")

DOC = {
    "contact": {"email": "jane@example.com", "full_name": "Jane Quinn Doe", "phone": "+1 (555) 010-0199",
                "linkedin": "https://linkedin.com/in/janedoe"},
    "facts": {"first_name": "Jane", "last_name": "Doe", "gender": "Male", "country": "United States",
              "state": "CA", "city": "Springfield", "zip_code": "94000", "street_address": "100 Example St",
              "how_did_you_hear": "LinkedIn", "visa_sponsorship": True, "work_eligible_us": True,
              "willing_relocate": True},
    "education": [{"degree": "Master's Degree", "major": "Data Analytics", "school": "Example State University"}],
    "skill_groups": [{"skills": ["PySpark"]}],
}


def run(coro):
    return asyncio.run(coro)


async def _open(name):
    from browser import FORMS, chromium_executable

    p = await pw.async_playwright().start()
    try:
        browser = await p.chromium.launch(headless=True, executable_path=chromium_executable())
    except Exception as exc:
        await p.stop()
        pytest.skip(f"chromium unavailable: {exc}")
    page = await browser.new_page()
    await page.goto((FORMS / f"{name}.html").as_uri())
    return p, browser, page


async def _extract(page):
    from browser import EXTRACT_JS

    return json.loads(await page.evaluate(EXTRACT_JS))["fields"]


async def _fill_all(name, url, profile, resume=None):
    p, browser, page = await _open(name)
    try:
        store = AnswerStore(profile)
        fields = await _extract(page)
        first = plan(fields, store, url)
        by_index = {f["i"]: f for f in fields}
        results = {}
        driver = PlaywrightDriver(page)
        for row in first.rows:
            if row.action == "fill":
                results[row.label] = await fill_row(driver, row, store, by_index[row.index])
        again = plan(await _extract(page), store, url)
        state = {sel: await read_field(driver, sel) for r in first.rows for sel in r.selectors}
        return first, results, again, state, page
    finally:
        await browser.close()
        await p.stop()


def test_dropdowns_commit_and_verify():
    first, results, again, state, _ = run(_fill_all(
        "dropdowns", "https://acme.wd1.myworkdayjobs.com/x/apply", from_export(DOC)))
    assert {label: r.ok for label, r in results.items()} == {
        "Country*": True, "Degree": True, "Gender": True,
        "How Did You Hear About Us?*": True, "State*": True, "Type to Add Skills": True,
    }
    # virtualised list: only reachable by typing; dial code ignored when matching
    assert results["Country*"].wrote == "United States +1"
    assert state["#country"]["display"] == "+1"
    # nested category: Job Board -> LinkedIn
    assert state["#source"]["chips"] == ["LinkedIn"]
    assert state["#state"]["display"] == "California"
    assert state["#skills"]["chips"] == ["PySpark"]  # not Python or Spark SQL
    assert again.summary()["fill"] == 0  # everything reads as filled afterwards


@pytest.mark.parametrize("name,url", [
    ("greenhouse", "https://job-boards.greenhouse.io/acme/jobs/1"),
    ("lever", "https://jobs.lever.co/acme/1/apply"),
    ("ashby", "https://jobs.ashbyhq.com/acme/1/application"),
    ("workday", "https://acme.wd1.myworkdayjobs.com/x/apply"),
    ("generic", "https://careers.example.com/apply"),
])
def test_autofill_fixtures_fill_and_replan_to_nothing(name, url, tmp_path):
    resume = tmp_path / "jane_doe.pdf"
    resume.write_bytes(b"%PDF-1.4 fake")
    # the Workday fixture's State select offers CA/NY/WA only (TX is correctly refused)
    profile = example_profile() | {"resume_path": str(resume), "state": "CA"}
    first, results, again, state, _ = run(_fill_all(name, url, profile))
    failed = {label: r.how for label, r in results.items() if not r.ok}
    assert not failed, failed
    assert results, "nothing was filled"
    # idempotence: a second plan over the filled page writes nothing new
    assert again.summary()["fill"] == 0, [(r.label, r.key) for r in again.rows if r.action == "fill"]


def test_ashby_yes_no_buttons_answer_as_a_radio_pair():
    """Ashby's Yes / No questions are two plain buttons beside a display:none checkbox
    that is ticked only for Yes. det saw neither, so on the apply bench every one of
    them (7 required on the mock) was left blank and the run stopped."""
    first, results, again, state, _ = run(_fill_all(
        "ashby", "https://jobs.ashbyhq.com/acme/1/application", example_profile()))
    rows = {r.label: r for r in first.rows if r.label.startswith(("Are you legally", "Will you now"))}
    assert set(rows) == {"Are you legally authorized to work in the United States?",
                         "Will you now or in the future require employer sponsorship to work in the United States?"}
    for row in rows.values():
        # the star is CSS on the label's _required_ class
        assert (row.widget, row.required, [o["text"] for o in row.options]) == ("radio", True, ["Yes", "No"])
    auth, sponsor = (rows[k] for k in sorted(rows))
    assert results[auth.label].wrote == "Yes" and results[sponsor.label].wrote == "No"
    assert [state[s]["checked"] for s in auth.selectors] == [True, False]
    assert [state[s]["checked"] for s in sponsor.selectors] == [False, True]
    # the hidden checkboxes are the same questions, not two more fields
    assert not [r for r in first.rows if r.widget == "checkbox"]


def test_ashby_title_class_marks_required_and_an_upload_label_can_name_the_field():
    """Live Ashby (CivicMarketplace, 2026-10-05): required only as a class on the
    question title, and the resume input's own label is "Upload your CV/Resume" with
    "(.pdf format)" before the drop zone. det took the description as the label and
    the radio question as optional, then pressed Submit without either."""
    html = """<form>
      <div class="ashby-application-form-field-entry" data-field-entry-id="a_1">
        <label class="_heading _required_f7cvd_91 ashby-application-form-question-title" for="cv">Upload your CV/Resume</label>
        <div class="ashby-application-form-question-description"><p>(.pdf format)</p></div>
        <div role="presentation"><input type="file" id="cv" tabindex="-1" required
          style="position:absolute;width:1px;height:1px;clip:rect(0,0,0,0)"><button type="button">Upload File</button></div>
      </div>
      <div class="ashby-application-form-field-entry" data-field-entry-id="a_2">
        <fieldset><label class="_heading _required_f7cvd_91 ashby-application-form-question-title">Which best describes you?</label>
          <label><input type="radio" name="q2" value="a"> A</label><label><input type="radio" name="q2" value="b"> B</label></fieldset>
      </div>
      <div class="ashby-application-form-field-entry" data-field-entry-id="a_4">
        <label class="_heading _required_f7cvd_91 ashby-application-form-question-title">Where did you hear about us?</label>
        <label><input type="checkbox" name="src" value="t"> Twitter</label><label><input type="checkbox" name="src" value="b"> Blog</label>
      </div>
      <div class="ashby-application-form-field-entry" data-field-entry-id="a_3">
        <label class="_heading ashby-application-form-question-title" for="opt">Anything else?</label><input type="text" id="opt">
      </div></form>"""

    async def go():
        p = await pw.async_playwright().start()
        try:
            from browser import chromium_executable
            browser = await p.chromium.launch(headless=True, executable_path=chromium_executable())
        except Exception as exc:
            await p.stop()
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            page = await browser.new_page()
            await page.set_content(html)
            return await _extract(page)
        finally:
            await browser.close()
            await p.stop()

    fields = {f["label"]: f for f in run(go())}
    assert fields["Upload your CV/Resume"]["widget"] == "file"
    assert fields["Which best describes you?"]["required"] and fields["Upload your CV/Resume"]["required"]
    assert not fields["Anything else?"]["required"]
    # a required "select all that apply" group needs one box, not every box (Chainlink)
    assert not any(f["required"] for f in fields.values() if f["widget"] == "checkbox")


def test_greenhouse_multi_select_pick_reads_back_as_a_chip():
    """Greenhouse "select all that apply": react-select multi shows picks as chips
    (select__multi-value__label). det never read them, so every pick failed as
    "closed-without" (Samsara "Where have you learned about Samsara?")."""
    html = """<label id="ql" for="q">Where have you learned about us?*</label>
      <div class="select__control"><div class="select__value-container select__value-container--is-multi">
        <div class="select__input-container"><input id="q" class="select__input" role="combobox" aria-expanded="false"
          aria-controls="menu" aria-labelledby="ql" aria-required="true"></div></div></div>
      <div id="menu" role="listbox" hidden><div role="option">LinkedIn</div><div role="option">Company blog or website</div></div>
      <script>
        const q = document.getElementById('q'), menu = document.getElementById('menu');
        q.addEventListener('click', () => { menu.hidden = false; q.setAttribute('aria-expanded', 'true'); });
        menu.querySelectorAll('[role=option]').forEach((o) => o.addEventListener('click', () => {
          const chip = document.createElement('div');
          chip.className = 'select__multi-value';
          chip.innerHTML = '<div class="select__multi-value__label"></div>';
          chip.firstChild.textContent = o.textContent;
          q.closest('.select__value-container').prepend(chip);
          menu.hidden = true; q.setAttribute('aria-expanded', 'false');
        }));
      </script>"""

    async def go():
        p = await pw.async_playwright().start()
        try:
            from browser import chromium_executable
            browser = await p.chromium.launch(headless=True, executable_path=chromium_executable())
        except Exception as exc:
            await p.stop()
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            page = await browser.new_page()
            await page.set_content(html)
            res = await fill_combobox(PlaywrightDriver(page), "#q", "how_heard",
                                      {"kind": "text", "value": "Company blog or website"})
            return res, [f for f in await _extract(page) if f["selector"] == "#q"][0]
        finally:
            await browser.close()
            await p.stop()

    res, field = run(go())
    assert res.ok, res.how
    assert field["chips"] == ["Company blog or website"]  # a re-plan sees it as answered


def test_react_tracked_inputs_notice_the_write():
    """The fixture's tracker only sets data-react-state when React's own value
    tracking saw a change — so this proves the framework, not just the DOM, updated."""
    async def go():
        p, browser, page = await _open("lever")
        try:
            store = AnswerStore(example_profile())
            fields = await _extract(page)
            driver = PlaywrightDriver(page)
            for row in plan(fields, store, "https://jobs.lever.co/acme/1/apply").rows:
                if row.key == "email":
                    assert (await fill_row(driver, row, store)).ok
            return await page.get_attribute('[name="email"]', "data-react-state")
        finally:
            await browser.close()
            await p.stop()
    assert run(go()) == "jane@x.dev"


def test_ambiguous_dropdown_is_left_alone_and_closed():
    async def go():
        p, browser, page = await _open("dropdowns")
        try:
            driver = PlaywrightDriver(page)
            # "Country" is a prefix of 240 options and an exact match for none
            res = await fill_combobox(driver, "#country", "country", {"kind": "text", "value": "Country"})
            return res, await read_field(driver, "#country")
        finally:
            await browser.close()
            await p.stop()
    res, state = run(go())
    assert not res.ok and "ambiguous" in res.how
    # nothing committed, menu shut, and no search text left to read as an answer
    assert (state["expanded"], state["display"], state["value"]) == (False, "", "")


def test_missing_listbox_fails_cleanly():
    async def go():
        p, browser, page = await _open("generic")
        try:
            driver = PlaywrightDriver(page)
            return await fill_combobox(driver, "#pref", "city", {"kind": "text", "value": "Springfield"})
        finally:
            await browser.close()
            await p.stop()
    res = run(go())
    assert isinstance(res, FillResult) and not res.ok


def test_mcp_upload_falls_back_to_the_chooser_behind_the_proxy():
    """The pruning proxy hides browser_run_code_unsafe; the upload then clicks the
    field's visible Attach button and uses browser_file_upload."""
    from det_autofill.fill import McpDriver

    class ProxyLike:
        def __init__(self):
            self.calls = []

        async def call(self, name, **args):
            self.calls.append((name, args))
            if name == "browser_run_code_unsafe":
                return "Tool 'browser_run_code_unsafe' is not available."
            if name == "browser_evaluate":
                return '### Result\n"#attach-resume"\n'
            return "ok"

    proxy = ProxyLike()
    run(McpDriver(proxy).upload("#resume", ["/resume/r1/resume.pdf"]))
    assert [c[0] for c in proxy.calls] == ["browser_run_code_unsafe", "browser_evaluate", "browser_click", "browser_file_upload"]
    assert proxy.calls[2][1]["target"] == "#attach-resume"
    assert proxy.calls[3][1] == {"paths": ["/resume/r1/resume.pdf"]}


def test_file_trigger_finds_the_attach_button_on_greenhouse_markup():
    from det_autofill.fill import FILE_TRIGGER

    async def go():
        p, browser, page = await _open("greenhouse")
        try:
            await page.set_content('<div class="field"><label>Resume/CV</label>'
                                   '<button type="button" id="attach">Attach</button>'
                                   '<input type="file" id="resume" style="display:none"></div>'
                                   '<div class="field"><label>Cover letter</label>'
                                   '<button type="button" id="attach2">Attach</button>'
                                   '<input type="file" id="cover" style="display:none"></div>')
            return await page.evaluate(FILE_TRIGGER, "#resume"), await page.evaluate(FILE_TRIGGER, "#cover")
        finally:
            await browser.close()
            await p.stop()
    assert run(go()) == ("#attach", "#attach2")



def test_mcp_selectors_that_look_like_proxy_refs_are_rewritten():
    """Ashby field ids are UUIDs; "#e061b99b-..." was refused by the proxy's
    read-before-write gate as an unread ref, and the refusal was ignored."""
    from det_autofill.fill import McpDriver, _unref

    assert _unref("#e061b99b-3f7c-4750") == '[id="e061b99b-3f7c-4750"]'
    assert _unref("#email") == "#email" and _unref("#react-select-e1-option-0") == "#react-select-e1-option-0"

    class Refusing:
        async def call(self, name, **args):
            return "⚠ Blocked: ref(s) e061 were not read from the current page. (The action was NOT performed.)"

    with pytest.raises(RuntimeError, match="refused by the MCP server"):
        run(McpDriver(Refusing()).type("#x", "hi"))


def test_mcp_click_retries_a_timeout_then_gives_up():
    from det_autofill.fill import McpDriver

    class Busy:
        def __init__(self, fail):
            self.fail, self.calls = fail, 0

        async def call(self, name, **args):
            self.calls += 1
            if self.calls <= self.fail:
                return "### Error\nTimeoutError: browserBackend.callTool: Timeout 5000ms exceeded."
            return "ok"

    class Fast(McpDriver):
        async def wait(self, ms):
            return None

    busy = Busy(fail=2)
    run(Fast(busy).click("#x"))
    assert busy.calls == 3
    with pytest.raises(RuntimeError, match="Timeout"):
        run(Fast(Busy(fail=5)).click("#x"))


def test_mcp_click_falls_back_to_the_page_hit_test_when_the_cover_is_gone():
    from det_autofill.fill import McpDriver

    intercepted = ("### Error\nTimeoutError: browserBackend.callTool: Timeout 5000ms exceeded.\nCall log:\n"
                   "  - <iframe title=\"Widget containing checkbox for hCaptcha security challenge\"></iframe> "
                   "from <div id=\"h-captcha\">…</div> subtree intercepts pointer events")

    class Page:
        def __init__(self, hit):
            self.hit, self.calls = hit, []

        async def call(self, name, **args):
            self.calls.append(name)
            if name == "browser_click":
                return intercepted
            return f'### Result\n"{self.hit}"'

    page = Page("clicked")
    run(McpDriver(page).click("#race-white"))
    assert page.calls == ["browser_click", "browser_evaluate"]

    with pytest.raises(RuntimeError, match="Timeout"):
        run(McpDriver(Page("covered")).click("#x"))


def test_mcp_click_hands_off_when_a_captcha_challenge_covers_the_form():
    from det_autofill.fill import CaptchaCover, McpDriver

    class Page:
        async def call(self, name, **args):
            if name == "browser_click":
                return "### Error\nTimeoutError: Timeout 5000ms exceeded.\n  - <iframe> subtree intercepts pointer events"
            return '### Result\n"captcha: hCaptcha challenge"'

    with pytest.raises(CaptchaCover):
        run(McpDriver(Page()).click("#x"))


def test_a_beforeunload_dialog_is_accepted_so_the_session_is_not_wedged():
    from types import SimpleNamespace

    from det_autofill.mcpio import Browser

    class Session:
        def __init__(self):
            self.calls, self.open = [], True

        async def call_tool(self, name, args):
            self.calls.append(name)
            if name == "browser_handle_dialog":
                self.open = False
                text = "### Result\nhandled"
            elif self.open:
                text = '### Error\n### Modal state\n- ["beforeunload" dialog with message ""]: can be handled by browser_handle_dialog'
            else:
                text = "### Result\nok"
            return SimpleNamespace(content=[SimpleNamespace(text=text)])

    b = Browser("http://unused")
    b.session = Session()
    assert run(b.call("browser_navigate", url="https://next.example")) == "### Result\nok"
    assert b.session.calls == ["browser_navigate", "browser_handle_dialog", "browser_navigate"]


def test_mcp_upload_follows_a_link_that_only_reveals_the_input():
    from det_autofill.fill import McpDriver

    class Page:
        def __init__(self):
            self.calls, self.revealed, self.chooser = [], False, False

        async def call(self, name, **args):
            self.calls.append((name, args.get("target") or args.get("function", "")[:0]))
            if name == "browser_run_code_unsafe":
                return "Tool browser_run_code_unsafe not available"
            if name == "browser_evaluate":
                return '### Result\n"#resume"' if self.revealed else '### Result\n"#attach-link"'
            if name == "browser_click":
                if args["target"] == "#attach-link":
                    self.revealed = True
                else:
                    self.chooser = True
                return "### Result\nclicked"
            if name == "browser_file_upload":
                return "### Result\nuploaded" if self.chooser else "### Error\nError: No file chooser visible"
            return ""

    page = Page()
    run(McpDriver(page).upload("#resume", ["/r.pdf"]))
    assert [c for c in page.calls if c[0] in ("browser_click", "browser_file_upload")] == [
        ("browser_click", "#attach-link"), ("browser_file_upload", ""), ("browser_click", "#resume"), ("browser_file_upload", "")]

"""Page loop + resolver against the multi-step wizard fixture (headless Chromium)."""

from __future__ import annotations

import asyncio
import json

import pytest

from det_autofill import engine
from det_autofill.answers import AnswerStore
from det_autofill.classify import Row
from det_autofill.fill import FillResult, PlaywrightDriver, read_field
from det_autofill.llm import LLMResolver, answer_from_text, build_messages, is_per_job, parse_answers
from det_autofill.profile import from_export

pw = pytest.importorskip("playwright.async_api")

DOC = {
    "role": "de",
    "contact": {"email": "jane@example.com", "full_name": "Jane Quinn Doe"},
    "facts": {"first_name": "Jane", "last_name": "Doe", "work_eligible_us": True, "visa_sponsorship": True},
    "portal_login": {"email": "jane@example.com", "password_set": True},
    "work_experiences": [{"job_title": "Data Engineer", "company_name": "Initech", "is_current": True}],
}


class FakeModel:
    def __init__(self, answers):
        self.answers, self.seen = answers, []

    async def __call__(self, messages):
        self.seen.append(messages)
        fields = json.loads(messages[1]["content"].split("Fields:\n", 1)[1])
        return json.dumps({"answers": [{"id": f["id"], "answer": self.answers.get(f["question"])} for f in fields]})


class FakeSaved:
    def __init__(self, stored=None):
        self.stored, self.put_calls = dict(stored or {}), []

    def get(self, row, *, per_job):
        answer = self.stored.get(row.label)
        return {"answer": answer, "source": "human"} if answer is not None else None

    def put(self, row, answer, *, per_job, source):
        self.put_calls.append((row.label, answer, per_job, source))


async def _why(question, max_words):
    return "Acme's streaming platform matches the pipelines I build every day."


async def _run(tmp_path, *, hash_="", resolver=None, autosubmit=False):
    from browser import FORMS, chromium_executable

    resume = tmp_path / "jane_doe.pdf"
    resume.write_bytes(b"%PDF-1.4")
    store = AnswerStore(from_export(DOC, resume_path=str(resume)))
    p = await pw.async_playwright().start()
    try:
        browser = await p.chromium.launch(headless=True, executable_path=chromium_executable())
    except Exception as exc:
        await p.stop()
        pytest.skip(f"chromium unavailable: {exc}")
    try:
        page = await browser.new_page()
        await page.goto((FORMS / "wizard.html").as_uri() + hash_)
        driver = PlaywrightDriver(page)
        result = await engine.run(driver, store, resolver=resolver, autosubmit=autosubmit)
        values = {s: await read_field(driver, s) for s in ("#first_name", "#location_pref", "#why")}
        checked = await page.evaluate("() => (document.querySelector('[name=cdl]:checked') || {}).value || null")
        heading = await page.evaluate("() => document.querySelector('.step.on h2').textContent")
        return result, values, checked, heading
    finally:
        await browser.close()
        await p.stop()


def _resolver(**kw):
    model = kw.pop("model", FakeModel({"Do you hold a valid commercial driver's license?*": "No",
                                        "Which office would you work from?": "Remote (US)"}))
    return LLMResolver(model, DOC, "Acme builds streaming data platforms.", per_job=_why, **kw), model


def test_without_a_resolver_it_stops_at_the_first_unanswered_required_field(tmp_path):
    result, values, _, heading = asyncio.run(_run(tmp_path))
    assert result.status == "stuck" and "unanswered" in result.reason
    assert heading == "Questions"
    assert values["#first_name"]["value"] == "Jane"  # page 1 was filled and passed


def test_full_run_stops_before_submit(tmp_path):
    resolver, model = _resolver()
    result, values, checked, heading = asyncio.run(_run(tmp_path, resolver=resolver))
    assert (result.status, heading) == ("ready-to-submit", "Review")
    assert "Submit Application" in result.reason
    assert checked == "no" and values["#location_pref"]["value"] == "Remote (US)"
    assert values["#why"]["value"].startswith("Acme's streaming platform")
    q = result.pages[1]
    assert q.errors == ["Please complete all required fields."]  # office had no marker: repaired
    sources = {f["label"]: f["source"] for p in result.pages for f in p.filled}
    assert sources["Why do you want to work at Acme?*"] == "ask"
    assert sources["Do you hold a valid commercial driver's license?*"] == "llm"
    assert "hunter2" not in json.dumps(model.seen) and "portal_login" not in model.seen[0][1]["content"]


def test_autosubmit_confirms_on_the_page(tmp_path):
    resolver, _ = _resolver()
    result, _, _, heading = asyncio.run(_run(tmp_path, resolver=resolver, autosubmit=True))
    assert (result.status, heading) == ("submitted", "Thank you for applying!")


def test_captcha_hands_off(tmp_path):
    resolver, model = _resolver()
    result, _, _, heading = asyncio.run(_run(tmp_path, hash_="#captcha", resolver=resolver))
    assert result.status == "handoff" and "verification code" in result.reason
    assert heading == "Questions" and model.seen == []  # no model call on a handoff page


def test_saved_answers_skip_the_model_and_new_answers_are_saved(tmp_path):
    saved = FakeSaved({"Do you hold a valid commercial driver's license?*": "No"})
    resolver, model = _resolver(saved=saved)
    result, _, checked, _ = asyncio.run(_run(tmp_path, resolver=resolver))
    assert result.status == "ready-to-submit" and checked == "no"
    asked = [f["question"] for m in model.seen for f in json.loads(m[1]["content"].split("Fields:\n", 1)[1])]
    assert "Do you hold a valid commercial driver's license?*" not in asked
    labels = {c[0]: c for c in saved.put_calls}
    assert labels["Which office would you work from?"][1:] == ("Remote (US)", False, "llm")
    assert labels["Why do you want to work at Acme?*"][2] is True  # per-job, scoped to this job


def test_an_option_the_page_does_not_offer_is_dropped(tmp_path):
    model = FakeModel({"Do you hold a valid commercial driver's license?*": "No",
                       "Which office would you work from?": "Remote"})  # not verbatim
    resolver, _ = _resolver(model=model)
    result, values, _, heading = asyncio.run(_run(tmp_path, resolver=resolver))
    assert result.status == "stuck" and heading == "Questions"
    assert values["#location_pref"]["value"] == "Select..."


# ---------------------------------------------------------------------------
# resolver units


def _row(label, widget="text", options=(), index=0):
    return Row(index=index, selectors=[f"#r{index}"], widget=widget, label=label, required=True,
               options=[{"value": o, "text": o} for o in options])


def test_per_job_questions():
    assert is_per_job(_row("Why do you want to join our team?"))
    assert is_per_job(_row("Cover Letter"))
    assert not is_per_job(_row("Years of experience with Spark"))
    assert not is_per_job(_row("Why did you leave your last job?"))  # same answer everywhere


def test_answer_from_text_guards_choice_fields():
    radio = _row("Relocate?", "radio", ["Yes", "No"])
    assert answer_from_text(radio, "yes") == {"kind": "option", "value": [r"^yes$"]}
    assert answer_from_text(radio, "Maybe") is None
    assert answer_from_text(_row("Agree", "checkbox"), "Yes") == {"kind": "bool", "value": True}
    assert answer_from_text(_row("Notes"), "null") is None


def test_parse_answers_ignores_unknown_ids_and_bad_json():
    rows = [_row("A", index=3)]
    assert parse_answers('```json\n{"answers": [{"id": 3, "answer": "x"}, {"id": 9, "answer": "y"}]}\n```', rows) == \
        {3: {"kind": "text", "value": "x", "source": "llm"}}
    assert parse_answers("not json", rows) == {}


def test_prompt_never_carries_the_portal_login():
    msgs = build_messages([_row("Q", "select", ["A", "B"])], DOC | {"portal_login": {"password": "hunter2"}})
    assert "hunter2" not in msgs[1]["content"] and '"options": ["A", "B"]' in msgs[1]["content"]


def test_model_endpoint_failure_is_reported_not_raised(tmp_path):
    async def down(messages):
        raise ConnectionError("404 from the model endpoint")
    resolver = LLMResolver(down, DOC, per_job=_why)
    result, _, _, heading = asyncio.run(_run(tmp_path, resolver=resolver))
    assert result.status == "stuck" and heading == "Questions"
    assert "model call failed: ConnectionError: 404" in result.reason


# ---------------------------------------------------------------------------
# guards found on a live Nextdoor form (2026-09-14)

PROFILE_TEXT = json.dumps({"skill_groups": [{"skills": ["Python", "SQL", "Docker", "Kubernetes"]}],
                           "work_experiences": [{"bullets": ["Built REST APIs in Python and FastAPI"]}]}).lower()


def test_unbacked_yes_to_an_experience_question_is_dropped():
    q = _row("Do you have full term experience using Kotlin and Spring for web services?", "select", ["Yes", "No"])
    reply = json.dumps({"answers": [{"id": 0, "answer": "Yes", "evidence": "Kotlin microservices"}]})
    assert parse_answers(reply, [q], PROFILE_TEXT) == {}
    backed = json.dumps({"answers": [{"id": 0, "answer": "Yes", "evidence": "Built REST APIs in Python and FastAPI"}]})
    assert parse_answers(backed, [q], PROFILE_TEXT)[0]["value"] == [r"^yes$"]


def test_no_needs_no_evidence_and_non_attestations_are_unaffected():
    q = _row("Do you have experience with Kotlin?", "select", ["Yes", "No"])
    assert parse_answers(json.dumps({"answers": [{"id": 0, "answer": "No"}]}), [q], PROFILE_TEXT)[0]["value"] == [r"^no$"]
    office = _row("Which office would you work from?", "select", ["Austin", "Remote"])
    assert parse_answers(json.dumps({"answers": [{"id": 0, "answer": "Remote"}]}), [office], PROFILE_TEXT)


def test_submit_button_prefers_submit_application_over_apply():
    buttons = [{"text": "Apply", "selector": "#top"}, {"text": "Autofill my application", "selector": "#af"},
               {"text": "Submit application", "selector": "#submit"}]
    assert engine._nav(buttons, engine.SUBMIT_RE)["selector"] == "#submit"
    assert engine._nav([{"text": "Apply", "selector": "#top"}], engine.SUBMIT_RE)["selector"] == "#top"


def test_failed_fields_are_tracked_by_selector_not_index():
    row = _row("Master's degree?", "combobox", index=7)
    moved = Row(index=9, selectors=row.selectors, widget="combobox", label=row.label, required=True)
    assert engine._needs_resolver(moved, {row.selectors[0]})  # same field, shifted index


def test_unit_testing_is_not_an_address_line():
    from det_autofill.aria import FormField
    from det_autofill.taxonomy import match

    label = "Do you have experience configuring pipeline stages for unit, integration, and canary rollouts?"
    rule = match(FormField(ref="", role="combobox", label=label))
    assert rule is None or rule.key != "street_address2"
    assert match(FormField(ref="", role="textbox", label="Unit")).key == "street_address2"



def test_saved_model_answers_to_qualification_questions_are_not_replayed():
    """The first Nextdoor run saved unverified "Yes" answers; the second replayed them
    as saved answers, past the evidence rule."""
    q = _row("Do you have experience with Kotlin?", "select", ["Yes", "No"])

    class Saved:
        def get(self, row, *, per_job):
            return {"answer": "Yes", "source": "llm"}

        def put(self, *a, **kw):
            raise AssertionError("qualification answers from the model are never saved")

    async def model(messages):
        return json.dumps({"answers": [{"id": 0, "answer": "Yes", "evidence": "not in the profile"}]})

    resolver = LLMResolver(model, {"skills": ["Python"]}, saved=Saved())
    assert asyncio.run(resolver.resolve([q], {})) == {}
    asyncio.run(resolver.learned(q, {"kind": "option", "value": ["^yes$"], "source": "llm"},
                                 FillResult(True, "x", "Yes"), {}))


def test_required_rows_skipped_as_duplicates_still_count_as_gaps():
    row = Row(index=3, selectors=["#b"], widget="combobox", label="Bachelor's degree and 5 years?", required=True,
              action="skip", reason="duplicate-key(first at #1)")
    assert engine._needs_resolver(row, set())


def test_degree_and_years_screen_is_not_skill_years():
    from det_autofill.aria import FormField
    from det_autofill.taxonomy import match

    label = "Do you have a Bachelor's degree in Computer Science and 5 years of experience as a Software Engineer?"
    assert match(FormField(ref="", role="combobox", label=label)).key == "degree_requirement"



def test_links_count_as_apply_but_not_as_next_or_submit():
    buttons = [{"text": "Apply for this job", "selector": "a.apply", "link": True},
               {"text": "Continue reading", "selector": "a.more", "link": True},
               {"text": "Submit", "selector": "a.fake", "link": True}]
    assert engine._nav(buttons, engine.APPLY_RE)["selector"] == "a.apply"
    assert engine._nav(buttons, engine.NEXT_RE) is None
    assert engine._nav(buttons, engine.SUBMIT_RE) is None


def test_lever_like_form_question_labels_checkboxes_and_rerender(tmp_path):
    """Live Lever findings: question labels outside the choices, race checkboxes that
    must only tick the user's own answer, and a re-render after the resume upload."""
    from browser import FORMS, chromium_executable

    doc = DOC | {"facts": DOC["facts"] | {"race_ethnicity": "Asian"}}

    async def go():
        resume = tmp_path / "jane_doe.pdf"
        resume.write_bytes(b"%PDF-1.4")
        store = AnswerStore(from_export(doc, resume_path=str(resume)) | {"agree_terms": True})
        p = await pw.async_playwright().start()
        try:
            browser = await p.chromium.launch(headless=True, executable_path=chromium_executable())
        except Exception as exc:
            await p.stop()
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            page = await browser.new_page()
            await page.goto((FORMS / "lever_like.html").as_uri())
            result = await engine.run(PlaywrightDriver(page), store, resolver=None, autosubmit=False)
            state = await page.evaluate("""() => ({
              auth: (document.querySelector('[name="cards[q1][field0]"]:checked') || {}).value || null,
              race: Array.from(document.querySelectorAll('[name="survey[race][field1]"]:checked')).map(x => x.value),
              consent: document.querySelector('input[type=checkbox][name="consent[store]"]').checked,
              marketing: document.querySelector('input[type=checkbox][name="consent[marketing]"]').checked })""")
            return result, state
        finally:
            await browser.close()
            await p.stop()

    result, state = asyncio.run(go())
    assert result.status == "ready-to-submit", result.reason
    assert state == {"auth": "Yes", "race": ["Asian"], "consent": True, "marketing": False}
    labels = {f["label"] for p in result.pages for f in p.filled}
    assert "Are you legally authorized to work in the United States?✱" in labels or \
        any(l.startswith("Are you legally authorized") for l in labels)


def test_marketing_consent_is_not_agreement():
    from det_autofill.aria import FormField
    from det_autofill.taxonomy import match

    retain = match(FormField(ref="", role="checkbox", label="Everseen has my consent to retain my data for 6 months"))
    market = match(FormField(ref="", role="checkbox", label="Everseen has my consent to contact me about future job opportunities."))
    assert retain.key == "agree_terms" and market.key == "marketing_consent"
    assert AnswerStore({}).get("marketing_consent") is None  # never ticked on the user's behalf


def test_work_permission_in_the_role_country_is_not_a_country_picker():
    from det_autofill.taxonomy import FormField, match

    role = match(FormField(ref="", role="select", label="Do you have the right to work, without support, in the country where this role is based? ✱"))
    assert role.key == "work_authorized_role_country"
    assert AnswerStore({"work_eligible_us": True}).get(role.key) is None  # the posting's country decides
    assert match(FormField(ref="", role="select", label="Country")).key == "country"
    assert match(FormField(ref="", role="select", label="Which country do you live in?")).key == "country"


def test_select_question_label_on_lever_markup(tmp_path):
    from browser import FORMS, chromium_executable, EXTRACT_JS

    async def go():
        p = await pw.async_playwright().start()
        try:
            browser = await p.chromium.launch(headless=True, executable_path=chromium_executable())
        except Exception as exc:
            await p.stop()
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            page = await browser.new_page()
            await page.goto((FORMS / "lever_like.html").as_uri())
            fields = json.loads(await page.evaluate(EXTRACT_JS))["fields"]
            return [f["label"] for f in fields if f["widget"] == "select"]
        finally:
            await browser.close()
            await p.stop()

    assert asyncio.run(go()) == ["Years of Python?"]

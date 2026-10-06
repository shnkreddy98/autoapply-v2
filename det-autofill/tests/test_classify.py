"""DOM extractor + classifier + planner.

Browser tests run extract.js in headless Chromium against the form fixtures (5 from
Autofill, one of our own traps) and check the plan; they skip when no Chromium is
available. Unit tests drive `plan` with hand-written descriptors.
"""

from __future__ import annotations

import pytest

from det_autofill.answers import AnswerStore, example_profile
from det_autofill.classify import plan
from det_autofill.profile import from_export

URLS = {
    "greenhouse": "https://job-boards.greenhouse.io/acme/jobs/1",
    "lever": "https://jobs.lever.co/acme/1/apply",
    "ashby": "https://jobs.ashbyhq.com/acme/1/application",
    "workday": "https://acme.wd1.myworkdayjobs.com/x/apply",
    "generic": "https://careers.example.com/apply",
    "traps": "https://careers.example.com/apply",
}

# A profile that needs sponsorship: the case the old defaults got wrong.
SPONSORED = {
    "role": "de",
    "contact": {"email": "jane@example.com", "full_name": "Jane Quinn Doe", "phone": "(555) 010-0199",
                "location": "Springfield, IL", "linkedin": "https://linkedin.com/in/janedoe"},
    "facts": {"first_name": "Jane", "middle_name": None, "last_name": "Doe", "gender": "Male",
              "race_ethnicity": "Asian", "hispanic_latino": "No", "veteran_status": "I am not a protected veteran",
              "disability_status": "No, I do not have a disability", "work_eligible_us": True,
              "visa_sponsorship": True, "over_18": True, "how_did_you_hear": "LinkedIn",
              "city": "Springfield", "state": "IL", "country": "United States"},
    "work_experiences": [{"job_title": "Data Engineer", "company_name": "Acme", "from_date": "2025-01-01", "is_current": True}],
    "education": [{"degree": "Master's Degree", "major": "Data Analytics", "school": "Example State University",
                   "to_date": "2024-12-01"}],
    "skill_groups": [{"category": "Languages", "skills": ["Python", "SQL"]}],
}


@pytest.fixture(scope="module")
def page():
    pw = pytest.importorskip("playwright.sync_api")
    from browser import chromium_executable

    exe = chromium_executable()
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True, executable_path=exe)
        except Exception as exc:  # no browser build on this machine
            pytest.skip(f"chromium unavailable: {exc}")
        yield browser.new_page()
        browser.close()


def _plan(page, name, profile):
    from browser import FORMS, extract_file

    return plan(extract_file(page, FORMS / f"{name}.html"), AnswerStore(profile), URLS[name])


def _by_label(p, text):
    rows = [r for r in p.rows if text.lower() in r.label.lower()]
    assert rows, f"no row labelled {text!r}"
    return rows[0]


@pytest.mark.parametrize("name", sorted(URLS))
def test_plan_is_deterministic(page, name):
    """Same page + same profile => the same decisions, byte for byte."""
    a = _plan(page, name, example_profile())
    b = _plan(page, name, example_profile())
    assert a.fingerprint() == b.fingerprint()
    assert a.rows, "extractor found no fields"


def test_greenhouse_uses_adapter_and_real_answers(page):
    p = _plan(page, "greenhouse", from_export(SPONSORED, resume_path="/resume/r.pdf"))
    assert p.ats == "greenhouse"
    first = _by_label(p, "First Name")
    assert (first.key, first.tier, first.answer["value"]) == ("first_name", "0", "Jane")
    assert _by_label(p, "require sponsorship").answer["value"] is True
    race = _by_label(p, "race")
    assert race.answer["value"][0] == r"\basian\b"  # the user's own answer first
    assert _by_label(p, "How did you hear").answer == {"kind": "text", "value": "LinkedIn"}


def test_lever_placeholder_labels_and_radio_groups(page):
    p = _plan(page, "lever", example_profile())
    assert _by_label(p, "LinkedIn URL").tier == "0"
    auth = _by_label(p, "authorized to work")
    assert auth.widget == "radio" and len(auth.selectors) == 2
    assert [o["text"] for o in auth.options] == ["Yes", "No"]


def test_workday_automation_ids(page):
    p = _plan(page, "workday", example_profile())
    assert p.ats == "workday"
    assert _by_label(p, "Address Line 1").key == "street_address"
    assert _by_label(p, "Country").widget == "combobox"


def test_generic_form_refusals(page):
    p = _plan(page, "generic", example_profile())
    reasons = {r.label: r.reason for r in p.rows if r.action != "fill"}
    assert reasons["Preferred name"] == "already-filled"
    assert reasons["Job title"] == "disabled"
    assert reasons["Current employer"] == "readonly"
    assert reasons["Reference first name"] == "poisoned-context"
    assert reasons["Leave this blank"] == "honeypot"


def test_trap_fixture(page):
    p = _plan(page, "traps", from_export(SPONSORED, resume_path="/resume/r.pdf"))
    without = _by_label(p, "without requiring sponsorship")
    assert (without.key, without.answer["value"]) == ("authorized_without_sponsorship", False)
    assert _by_label(p, "U.S. citizen").answer["value"] is False
    assert _by_label(p, "best describes your work authorization").reason == "no-answer"
    assert _by_label(p, "for robots only").reason == "honeypot"
    code = _by_label(p, "verification code")
    assert (code.action, code.reason) == ("handoff", "captcha-or-code")
    assert [r.reason for r in p.rows if r.label == "Gender"] == ["already-filled"]  # react-select shows a value
    resume = _by_label(p, "Resume")
    assert resume.action == "fill"  # invisible file inputs are how uploads look
    assert {r.reason for r in p.rows if r.section == "Professional References"} == {"poisoned-context"}


# ---------------------------------------------------------------------------
# unit: plan with hand-written descriptors


def _d(i, **kw):
    base = {"i": i, "selector": f"#f{i}", "tag": "input", "type": "text", "widget": "text", "visible": True}
    base.update(kw)
    return base


def test_citizenship_is_never_assumed():
    store = AnswerStore({"first_name": "Jane"})
    p = plan([_d(0, widget="radio", group_label="Are you a US citizen?", group_key="g", option_label="Yes")], store)
    assert p.rows[0].reason == "no-answer"


def test_honeypot_beats_the_token_tier():
    d = _d(0, name="website", label="Enter website. This input is for robots only, do not enter if you're human.")
    assert plan([d], AnswerStore(example_profile())).rows[0].reason == "honeypot"


def test_choice_keys_refuse_text_boxes():
    d = _d(0, label="Visa sponsorship details")
    assert plan([d], AnswerStore(example_profile())).rows[0].reason.startswith("control-mismatch")
    assert plan([_d(0, label="Gender")], AnswerStore(example_profile())).rows[0].reason.startswith("control-mismatch")


def test_a_yes_no_question_in_a_text_box_is_typed_as_yes_or_no():
    store = AnswerStore(example_profile())
    row = plan([_d(0, label="Will you require visa sponsorship?")], store).rows[0]
    want = "Yes" if store.get("visa_sponsorship").value else "No"
    assert (row.action, row.answer) == ("fill", {"kind": "text", "value": want})


def test_ashby_first_name_box_keeps_the_system_name_id():
    # CivicMarketplace splits the name: "First Name" is still _systemfield_name
    store = AnswerStore(example_profile())
    rows = plan([_d(0, id="_systemfield_name", name="_systemfield_name", label="First Name")], store,
                "https://jobs.ashbyhq.com/acme/1/application").rows
    assert (rows[0].key, rows[0].answer["value"]) == ("first_name", store.get("first_name").value)
    rows = plan([_d(0, id="_systemfield_name", name="_systemfield_name", label="Name")], store,
                "https://jobs.ashbyhq.com/acme/1/application").rows
    assert rows[0].key == "full_name"


def test_a_sponsorship_question_that_lists_visas_is_still_the_sponsorship_question():
    q = "Will you now, or in the future, require sponsorship or visa transfer for employment (i.e. H-1B, OPT, F-1, Student Visa)?"
    row = plan([_d(0, widget="select", label=q, options=[{"value": "y", "text": "Yes"}, {"value": "n", "text": "No"}])],
               AnswerStore(example_profile())).rows[0]
    assert (row.key, row.action) == ("visa_sponsorship", "fill")
    row = plan([_d(0, label="Are you currently in F-1 status?")], AnswerStore(example_profile())).rows[0]
    assert row.key == "visa_status_detail"


def test_authorisation_sentences_pick_authorised_but_needs_sponsorship():
    from det_autofill.fill import _pick

    options = ["I am authorised to work in the United States and do not require employer sponsorship now or in the future",
               "I am authorised to work in the United States but will require employer sponsorship in the future",
               "I am not currently authorised to work in the United States and will require employer sponsorship"]
    q = "Which of the following best describes your authorisation to work in the United States?"
    members = [_d(n, widget="radio", group_label=q, group_key="g", option_label=o) for n, o in enumerate(options)]
    profile = example_profile() | {"visa_sponsorship": True, "work_eligible_us": True}
    row = plan(members, AnswerStore(profile)).rows[0]
    assert (row.key, row.action) == ("work_auth_type", "fill")
    assert _pick(row.key, row.answer, row.options).index == 1


def test_clearance_level_list_takes_the_no_clearance_option():
    from det_autofill.fill import _pick

    options = ["Secret", "Top Secret", "TS/SCI", "I do not have a clearance", "Status Unknown"]
    members = [_d(0, widget="select", label="What is your current clearance level?*",
                  options=[{"value": o, "text": o} for o in options])]
    row = plan(members, AnswerStore(example_profile() | {"security_clearance": False})).rows[0]
    assert (row.key, row.action) == ("security_clearance", "fill")
    assert options[_pick(row.key, row.answer, row.options).index] == "I do not have a clearance"


def test_company_website_source_and_a_missing_major():
    from det_autofill.fill import _pick

    doc = {**SPONSORED, "facts": {**SPONSORED["facts"], "how_did_you_hear": "Company website"}}
    store = AnswerStore(from_export(doc))
    heard = ["LinkedIn Jobs", "Samsara Careers Site", "Indeed / Glassdoor", "Wellfound"]
    row = plan([_d(0, widget="select", label="How did you hear about this opportunity?*",
                   options=[{"value": o, "text": o} for o in heard])], store).rows[0]
    assert heard[_pick(row.key, row.answer, row.options).index] == "Samsara Careers Site"
    # no "Data Analytics" on the list: "Other", never the bachelor's Computer Science
    majors = ["Computer Science", "Information Systems", "Other", "Statistics & Decision Theory"]
    row = plan([_d(0, widget="select", label="Discipline", options=[{"value": o, "text": o} for o in majors])],
               store).rows[0]
    assert majors[_pick(row.key, row.answer, row.options).index] == "Other"


def test_ai_use_questions_are_left_to_the_applicant():
    q = ("Are you an LLM, AI agent, or other associated agentic tool? We are surveying the applicant pool "
         "to understand the suite of AI tools that the applicant is using. Select all that apply*")
    row = plan([_d(0, widget="combobox", label=q)], AnswerStore(example_profile())).rows[0]
    assert (row.action, row.reason) == ("skip", "for-the-applicant")
    row = plan([_d(0, label="Did you use AI to complete this application?")], AnswerStore(example_profile())).rows[0]
    assert row.reason == "for-the-applicant"


def test_south_asian_still_answers_a_plain_asian_list():
    from det_autofill.fill import _pick

    doc = {**SPONSORED, "facts": {**SPONSORED["facts"], "race_ethnicity": "South Asian"}}
    store = AnswerStore(from_export(doc))
    for options, want in ((["White", "Asian", "Black or African American"], "Asian"),
                          (["East Asian", "South Asian (inclusive of Indian)", "Southeast Asian"], "South Asian (inclusive of Indian)")):
        row = plan([_d(0, widget="select", label="Race", options=[{"value": o, "text": o} for o in options])], store).rows[0]
        assert options[_pick(row.key, row.answer, row.options).index] == want


def test_na_is_an_answer_only_where_the_question_asks_for_it():
    from det_autofill.classify import Row
    from det_autofill.llm import answer_from_text

    asks = Row(index=0, selectors=["#a"], widget="textarea", required=True,
               label="If yes, what experience have you had? Please write N/A if you have not been an intern.*")
    other = Row(index=1, selectors=["#b"], widget="text", required=False, label="Graduation year")
    assert answer_from_text(asks, "N/A") == {"kind": "text", "value": "N/A"}
    assert answer_from_text(other, "N/A") is None


def test_currently_based_is_the_location_question():
    # live Ashby (the agency fund, 2026-10-05) left this required box empty
    row = plan([_d(0, label="Where are you currently based?")], AnswerStore(example_profile())).rows[0]
    assert (row.key, row.action) == ("location_city", "fill")


def test_ever_been_an_employee_is_the_worked_here_question():
    q = "Have you ever been an employee or contractor at Lumen Freight?"
    rows = plan([_d(0, widget="radio", group_label=q, group_key="g", option_label="Yes"),
                 _d(1, widget="radio", group_label=q, group_key="g", option_label="No")], AnswerStore(example_profile())).rows
    assert (rows[0].key, rows[0].action) == ("worked_here_before", "fill")


def test_duplicate_key_fills_the_first_only():
    rows = plan([_d(0, label="Email"), _d(1, label="Email")], AnswerStore(example_profile())).rows
    assert rows[0].action == "fill" and rows[1].reason.startswith("duplicate-key")


def test_empty_profile_value_is_not_an_answer():
    rows = plan([_d(0, label="Current location")], AnswerStore({"city": "", "state": ""})).rows
    assert rows[0].reason == "no-answer"


def test_overwrite_flag():
    d = _d(0, label="First name", value="Jo")
    assert plan([d], AnswerStore(example_profile())).rows[0].reason == "already-filled"
    assert plan([d], AnswerStore(example_profile()), overwrite=True).rows[0].action == "fill"


def test_history_date_parts_are_not_the_availability_date():
    """Greenhouse's education block has "Start date month/year" dropdowns; they were
    answered with today's date as the applicant's start date."""
    rows = plan([_d(0, widget="combobox", label="Start date month"),
                 _d(1, label="Start date year")], AnswerStore(example_profile() | {"available_start_date": None})).rows
    assert [r.key for r in rows] == ["history_date_part", "history_date_part"]
    assert {r.action for r in rows} == {"skip"}


def test_start_date_never_defaults_to_today():
    store = AnswerStore({"today": "2026-09-13"})
    assert store.get("start_date") is None
    assert store.get("signature_date").value == "2026-09-13"
    # the stored signature date is the day the profile was set up: you sign today
    assert AnswerStore({"today": "2026-10-05", "signature_date": "2026-09-13"}).get("signature_date").value == "2026-10-05"


def test_salary_unit_selects_do_not_take_the_amount_key():
    cur = _d(0, label="Desired Salary", widget="select", options=[{"value": "USD", "text": "US Dollar ($)"}, {"value": "EUR", "text": "Euro (€)"}])
    amount = _d(1, label="Desired Salary")
    period = _d(2, label="Desired Salary", widget="select", options=[{"value": "h", "text": "Hourly"}, {"value": "y", "text": "Yearly"}])
    rows = plan([cur, amount, period], AnswerStore(example_profile())).rows
    assert [r.key for r in rows] == ["salary_currency", "desired_salary", "salary_period"]
    assert all(r.action == "fill" for r in rows)


def test_ethnicity_with_only_hispanic_choices_is_the_hispanic_question():
    d = _d(0, label="Ethnicity", widget="select", options=[
        {"value": "0", "text": "I do not wish to provide this information"},
        {"value": "1", "text": "Hispanic/Latino"}, {"value": "2", "text": "Not Hispanic/Latino"}])
    assert plan([d], AnswerStore(example_profile())).rows[0].key == "eeo_hispanic"


def test_long_attestations_are_not_company_or_school_fields():
    from det_autofill.aria import FormField
    from det_autofill.taxonomy import match

    certify = "I certify that all statements made by me on this application are true and correct to the best of my knowledge"
    assert match(FormField(ref="", role="radio", label=certify)).key == "agree_terms"
    f1 = "Are you currently in F-1 Visa status? (An F-1 Visa is granted to foreign students attending school in the United States)"
    assert match(FormField(ref="", role="radio", label=f1)).key == "visa_status_detail"


def test_split_date_boxes_take_their_groups_date():
    # Workday: spinbuttons labelled only "Month" / "Year" inside groups "From*" and "Date"
    store = AnswerStore({"job_from_date": "2022-03-01", "job_to_date": "", "education_dates": "May 2019",
                         "today": "2026-10-04"})
    rows = plan([
        _d(0, role="spinbutton", label="Month", date_of="From*", section="Work Experience 1"),
        _d(1, role="spinbutton", label="Year", date_of="From*", section="Work Experience 1"),
        _d(2, role="spinbutton", label="Month", date_of="To*", section="Work Experience 1"),
        _d(3, role="spinbutton", label="Year", date_of="To", section="Education 1"),
        _d(4, role="spinbutton", label="Month", date_of="From", section="Education 1"),
        _d(5, role="spinbutton", label="Day", date_of="Date", section="Voluntary Self-Identification of Disability"),
        _d(6, role="spinbutton", label="Month", date_of="", section=""),
    ], store).rows
    got = [(r.action, (r.answer or {}).get("value"), r.reason or "") for r in rows]
    assert got[0] == ("fill", "03", "") and got[1] == ("fill", "2022", "")
    assert got[2][2] == "no-answer"  # current job: no end date
    assert got[3] == ("fill", "2019", "")  # education "To" is graduation
    assert got[4][2] == "no-answer"  # no school start date stored
    assert got[5] == ("fill", "04", "")  # self-ID date is today
    assert got[6][2] == "no-answer"  # a part with no date to belong to


def test_date_parts_reads_resume_spellings():
    from det_autofill.classify import date_parts

    assert date_parts("2022-03-01") == {"year": 2022, "month": 3, "day": 1}
    assert date_parts("2022-03") == {"year": 2022, "month": 3}
    assert date_parts("03/2022") == {"year": 2022, "month": 3}
    assert date_parts("March 2022") == {"year": 2022, "month": 3}
    assert date_parts("2022") == {"year": 2022}
    assert date_parts("Present") == {} and date_parts(None) == {}

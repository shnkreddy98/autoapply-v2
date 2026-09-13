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
    d = _d(0, label="Will you require visa sponsorship?")
    assert plan([d], AnswerStore(example_profile())).rows[0].reason.startswith("control-mismatch")


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

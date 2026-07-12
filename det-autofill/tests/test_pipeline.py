"""Parser + taxonomy + answers over inline and harvested fixtures."""

from pathlib import Path

from det_autofill import taxonomy
from det_autofill.answers import AnswerStore, example_profile
from det_autofill.aria import fillable, parse_snapshot

FRAGMENTS = Path(__file__).parent / "fixtures" / "fragments"

SNIPPET = """\
- generic [ref=e159]: First Name*
- textbox "First Name*" [ref=e161] [cursor=pointer]:
  - /placeholder: Type here...
- generic [ref=e395]: Are you Hispanic/Latino?
- combobox "Are you Hispanic/Latino?" [ref=e404]
  - option "Yes" [ref=e410]
  - option "No" [ref=e411]
  - option "Decline To Self Identify" [ref=e412]
- radio "Male" [ref=e33]
- radio "Female" [ref=e34] [checked]
- checkbox "I agree to the terms and conditions" [ref=e55]
- spinbutton "Year" [ref=e77]
- button "Save and Continue" [ref=e99]
- textbox "Desired salary" [ref=e120] [disabled]
"""


def test_parse_roles_and_refs():
    fields = parse_snapshot(SNIPPET)
    by_ref = {f.ref: f for f in fields}
    assert by_ref["e161"].role == "textbox"
    assert by_ref["e161"].required  # trailing *
    assert by_ref["e404"].options == ["Yes", "No", "Decline To Self Identify"]
    assert by_ref["e34"].checked is True
    assert by_ref["e33"].checked is None
    assert by_ref["e120"].disabled


def test_fillable_excludes_nav_and_disabled():
    refs = {f.ref for f in fillable(parse_snapshot(SNIPPET))}
    assert "e99" not in refs  # button
    assert "e410" not in refs  # option
    assert "e120" not in refs  # disabled


def test_context_association():
    fields = parse_snapshot(
        "- generic [ref=e1]: Are you legally authorized to work in the U.S.?\n"
        '- radio "Yes" [ref=e2]\n'
        '- radio "No" [ref=e3]\n'
    )
    radios = [f for f in fields if f.role == "radio"]
    assert all(f.context.startswith("Are you legally") for f in radios)
    assert taxonomy.match(radios[0]).key == "work_authorized"


def test_taxonomy_spot_checks():
    cases = {
        "First Name*": "first_name",
        "Confirm Email Address": "email_confirm",
        "Email Address": "email",
        "Zip/Postal Code": "zip_code",
        "LinkedIn Profile URL:*": "linkedin",
        "Will you now or in the future require sponsorship": "visa_sponsorship",
        "Are you Hispanic/Latino?": "eeo_hispanic",
        "Veteran Status": "eeo_veteran",
        "How many years of experience do you have in Databricks?*": "skill_years",
        "What's your highest level of education completed?*": "education_level",
        "Field of Study": "field_of_study",
        "Desired salary": "desired_salary",
        "How Did You Hear About Us?": "how_heard",
        "Are you over the age of 18?": "age_over_18",
        "convicted of a felony?": "felony",
        "I'm not a robot": "handoff",
        "Verification code": "handoff",
        "Employee ID (if applicable)": "leave_blank",
        "Python": "skill_checkbox",
        "Are you a former employee?": "worked_here_before",
        "Save and Continue": None,  # nav: no rule should fire via label
    }
    from det_autofill.aria import FormField

    for label, want in cases.items():
        got = taxonomy.match(FormField(ref="x", role="textbox", label=label))
        if want is None:
            assert got is None or got.category in ("consent",), label
        else:
            assert got and got.key == want, f"{label!r} -> {got and got.key}"


def test_answers_resolution():
    store = AnswerStore(example_profile())
    assert store.get("first_name").value == "Jane"
    assert store.get("password").value == "hunter2"
    assert store.get("visa_sponsorship").value is False
    assert store.get("age_over_18").value is True
    # skill_years pulls from the parsed map, picking the named skill
    a = store.get("skill_years", "How many years of experience do you have in Databricks?")
    assert a.value == "3"
    # unknown skill falls back to global YoE
    a = store.get("skill_years", "years of experience with COBOL")
    assert a.value == "6"
    # skill checkboxes: have vs don't have
    assert store.get("skill_checkbox", "Python").value is True
    assert store.get("skill_checkbox", "Kubernetes").value is False
    assert store.get("handoff", "I'm not a robot").kind == "handoff"


def test_harvested_fixtures_parse():
    files = list(FRAGMENTS.glob("*.txt"))
    assert len(files) > 1000  # harvested corpus present
    parsed = fields = 0
    for f in files[:300]:
        got = parse_snapshot(f.read_text())
        parsed += 1
        fields += len(got)
    assert fields > 300  # parser extracts fields from real fragments

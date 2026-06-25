"""Tests for the deterministic page-model extractor, against real recorded
snapshots (proxy/tests/fixtures/) from Workday / Greenhouse / Lever / Oracle /
Taleo / UltiPro applications.

Fixtures come from the recorder (`aria_snapshot()`), so they carry NO `[ref=eN]`
ids — assertions key on labels / types / errors, never on refs.
"""

from pathlib import Path

import pytest

from proxy.page_model import Field, PageModel, extract, render

FIX = Path(__file__).parent / "fixtures"


def _model(name: str) -> PageModel:
    return extract((FIX / name).read_text())


def _find(model: PageModel, substr: str) -> Field | None:
    return next((f for f in model.fields if substr.lower() in f.label.lower()), None)


# --- page_type classification --------------------------------------------------


def test_confirmation_page():
    assert _model("confirmation.txt").page_type == "confirmation"


def test_signin_page_detected_by_password():
    m = _model("signin.txt")
    assert m.page_type == "signin_signup"
    assert any("password" in f.label.lower() for f in m.fields)


@pytest.mark.parametrize(
    "fixture",
    ["greenhouse_form.txt", "lever_form.txt", "oracle_required_alert.txt",
     "taleo_form.txt", "ultipro_form.txt", "workday_form_errors.txt"],
)
def test_form_pages_classified_as_form(fixture):
    assert _model(fixture).page_type == "form"


# --- field typing --------------------------------------------------------------


def test_greenhouse_dropdowns_are_aria():
    m = _model("greenhouse_form.txt")
    gender = _find(m, "Gender")
    assert gender is not None and gender.type == "dropdown-aria"
    assert any(a.kind == "submit" for a in m.actions)


def test_lever_required_markers_and_native_select():
    m = _model("lever_form.txt")
    for label in ("Full name", "Email", "Phone", "LinkedIn"):
        f = _find(m, label)
        assert f is not None and f.required, f"{label} should be required"
    gender = _find(m, "Gender")
    assert gender is not None and gender.type == "select-native"
    assert any(lbl == "Male" for _, lbl in gender.options)


def test_lever_yes_no_grouped_as_single_radio():
    m = _model("lever_form.txt")
    # the "18 or older" / "sponsorship" Yes/No checkbox sets become one radio field
    radios = [f for f in m.fields if f.type == "radio"]
    yn = [f for f in radios if {lbl for _, lbl in f.options} == {"Yes", "No"}]
    assert yn, "Yes/No checkbox sets should fold into single-select radio fields"


def test_ultipro_required_and_native_selects():
    m = _model("ultipro_form.txt")
    first = _find(m, "First Name")
    assert first is not None and first.required
    title = _find(m, "Title")
    assert title is not None and title.type == "select-native"
    assert m.errors == []  # no validation errors on this clean page


def test_native_vs_aria_select_distinction():
    # Taleo Employer State is a real <select> (has option children).
    m = _model("taleo_form.txt")
    state = _find(m, "Employer State")
    assert state is not None and state.type == "select-native"
    assert any("California" == lbl for _, lbl in state.options)


# --- the Workday "identical checkbox" regression -------------------------------


def test_workday_identical_checkboxes_not_grouped():
    m = _model("workday_form_errors.txt")
    # three repeated "I currently work here" checkboxes must stay standalone,
    # NOT collapse into one 3-option radio.
    bad = [
        f
        for f in m.fields
        if f.type == "radio"
        and len(f.options) >= 2
        and len({lbl for _, lbl in f.options}) == 1
    ]
    assert not bad, "identical-label checkboxes were wrongly merged into a radio"
    assert any(
        f.type == "checkbox" and "currently work here" in f.label.lower()
        for f in m.fields
    )


# --- error extraction ----------------------------------------------------------


def test_workday_errors_and_step():
    m = _model("workday_form_errors.txt")
    assert m.step and "step" in m.step
    joined = " | ".join(m.errors)
    assert "Errors Found" in joined
    assert "Degree is required" in joined
    assert "LinkedIn" in joined


def test_oracle_required_field_errors_split_from_info():
    m = _model("oracle_required_alert.txt")
    joined = " | ".join(m.errors)
    assert "Address Line 1 field is required" in joined
    assert "Postal Code field is required" in joined
    # the "Profile successfully imported" info prefix must NOT leak in
    assert "successfully imported" not in joined.lower()


def test_clean_pages_have_no_false_errors():
    for fixture in ("greenhouse_form.txt", "lever_form.txt", "taleo_form.txt",
                    "ultipro_form.txt", "signin.txt", "confirmation.txt"):
        assert _model(fixture).errors == [], f"{fixture} produced false errors"


# --- rendering -----------------------------------------------------------------


def test_render_contains_sections():
    out = render(_model("workday_form_errors.txt"))
    assert out.startswith("PAGE: form")
    assert "ERRORS:" in out
    assert "FIELDS:" in out
    assert "ACTIONS:" in out


def test_render_loading():
    assert "empty" in render(extract("- document")).lower()


def test_unknown_falls_back_to_raw():
    # a page with no fields, no apply, no job links -> unknown -> raw tree
    tree = "- document:\n  - heading \"Random\"\n  - paragraph: hello world here\n  - link \"x\""
    m = extract(tree)
    assert m.page_type == "unknown"
    assert "raw" in render(m).lower()

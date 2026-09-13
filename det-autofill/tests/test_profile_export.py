"""`autoapply profile export` JSON -> AnswerStore profile."""

from datetime import date

from det_autofill.answers import AnswerStore
from det_autofill.profile import from_export, option_prefs_for
from test_classify import SPONSORED


def test_facts_and_current_role():
    p = from_export(SPONSORED, resume_path="/resume/r.pdf", today=date(2026, 9, 13))
    assert (p["first_name"], p["current_title"], p["current_company"]) == ("Jane", "Data Engineer", "Acme")
    assert p["skills"] == ["Python", "SQL"] and p["field_of_study"] == "Data Analytics"
    assert p["today"] == "2026-09-13" and "middle_name" in p


def test_sponsorship_answers_follow_the_facts():
    store = AnswerStore(from_export(SPONSORED))
    assert store.get("visa_sponsorship").value is True
    assert store.get("authorized_without_sponsorship").value is False
    assert store.get("us_citizen").value is False  # needing sponsorship rules it out
    assert store.get("work_auth_type") is None  # the default list starts with "citizen"


def test_eeo_uses_the_users_own_words_not_decline_defaults():
    store = AnswerStore(from_export(SPONSORED))
    assert store.get("eeo_veteran").value[0] == r"\bi\ am\ not\ a\ protected\ veteran\b"
    assert all("decline" not in pat for pat in store.get("eeo_gender").value)


def test_missing_eeo_answer_declines():
    doc = {**SPONSORED, "facts": {k: v for k, v in SPONSORED["facts"].items() if k != "race_ethnicity"}}
    assert AnswerStore(from_export(doc)).get("eeo_race").value == [r"decline|prefer not|do not wish|don t wish"]


def test_option_prefs_vocabulary():
    assert option_prefs_for("eeo_gender", "Male")[1:] == [r"^male$", r"^man$", r"^male\b", r"^man\b"]
    assert option_prefs_for("eeo_race", "") == []


def test_password_never_comes_from_the_export():
    assert "login_password" not in from_export(SPONSORED)


def test_major_leads_field_of_study_prefs():
    assert AnswerStore(from_export(SPONSORED)).get("field_of_study").value[0] == r"\bdata\ analytics\b"

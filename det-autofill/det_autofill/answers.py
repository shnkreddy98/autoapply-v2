"""Canonical answer store: taxonomy key -> value for THIS applicant.

P1/P2: loads from a profile JSON file (see example_profile()). At integration
time this is fed from the autoapply Postgres profile (users +
application_profiles + portal_credentials + the new skill_years map that the
onboarding resume-parse will produce).

Answer kinds:
  text      -> type into textbox
  bool      -> yes/no radio/dropdown; PREFS give the option wording to look for
  option    -> pick from dropdown by preference list (first option that matches wins)
  date      -> ISO date; widget layer splits M/D/Y when needed
  file      -> path for browser_file_upload
  skill_map -> per-skill years lookup (label tells us which skill)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# Option-preference lists for enumerated questions: for each canonical key,
# ordered regexes tried against the widget's options; first hit is clicked.
OPTION_PREFS: dict[str, list[str]] = {
    # Without the user's own answer (profile option_prefs) the only safe choice is to
    # decline: these used to fall back to "No" / "not a protected veteran", which
    # states a fact about the applicant nobody gave.
    "eeo_gender": [r"decline|prefer not|do not wish|don t wish"],
    "eeo_race": [r"decline|prefer not|do not wish|don t wish"],
    "eeo_hispanic": [r"decline|prefer not|do not wish|don t wish"],
    "eeo_veteran": [r"decline|prefer not|do not wish|don t wish"],
    "eeo_disability": [r"do not want to answer|decline|prefer not"],
    "eeo_gender_identity": [r"decline|prefer not|do not wish|don t wish"],
    "work_auth_type": [r"citizen", r"permanent resident|green card"],
    "how_heard": [r"company|corporate website", r"job board|linkedin", r"other"],
    "phone_device_type": [r"mobile|cell", r"personal", r"home"],
    "contact_method": [r"e.?mail", r"phone"],
    "education_level": [r"master", r"bachelor", r"graduate"],
    # no default: guessing "Computer Science" for someone who studied something else
    # misstates their education; without the user's major this stays unanswered
    "field_of_study": [],
    "employment_type": [r"full.?time"],
    "languages": [r"english"],
}

# Human wording for option-preference keys when the widget turns out to be a
# free-text box instead of a dropdown ("How did you hear about us?" as textbox).
OPTION_TEXT: dict[str, str] = {
    "how_heard": "LinkedIn",
    "phone_device_type": "Mobile",
    "contact_method": "Email",
    "employment_type": "Full-time",
    "languages": "English",
}

# bool keys -> (yes-ish regex, no-ish regex) used to pick radio/option wording
YES = r"^yes\b"
NO = r"^no\b"


@dataclass
class Answer:
    key: str
    kind: str  # text | bool | option | date | file | skill_map
    value: Any  # str / bool / list-of-pref-regexes / iso date / path / dict


class AnswerStore:
    def __init__(self, profile: dict):
        self.p = profile

    @classmethod
    def from_json(cls, path: str) -> "AnswerStore":
        return cls(json.load(open(path)))

    def get(self, key: str, label: str = "") -> Answer | None:
        p = self.p
        txt = lambda k: p.get(k) or None  # noqa: E731

        TEXT = {
            "first_name": txt("first_name"),
            "last_name": txt("last_name"),
            "middle_name": p.get("middle_name", ""),
            "full_name": txt("full_name"),
            "preferred_name": p.get("preferred_name") or txt("first_name"),
            "email": txt("email"),
            "email_confirm": txt("email"),
            "phone": txt("phone"),
            "phone_country_code": p.get("phone_country_code", "+1"),
            "phone_extension": "",
            "street_address": txt("street_address"),
            "street_address2": p.get("street_address2", ""),
            "city": txt("city"),
            "state": txt("state"),
            "zip_code": txt("zip_code"),
            "county": p.get("county", ""),
            "country": p.get("country", "United States"),
            "location_city": txt("location") or f"{p.get('city', '')}, {p.get('state', '')}".strip(", "),
            "linkedin": txt("linkedin"),
            "github": txt("github"),
            "website": p.get("website") or txt("github"),
            "twitter": p.get("twitter", ""),
            "password": txt("login_password"),
            "password_confirm": txt("login_password"),
            "username": txt("login_email") or txt("email"),
            "years_experience": str(p.get("years_of_experience", "")),
            "desired_salary": p.get("desired_salary", ""),
            "current_title": txt("current_title"),
            "current_company": txt("current_company"),
            "notice_period": p.get("notice_period", "2 weeks"),
            "signature": txt("full_name"),
            "degree": txt("degree"),
            "school": txt("school"),
            "gpa": p.get("gpa", ""),
        }
        sponsorship = p.get("visa_sponsorship")
        BOOL = {
            "visa_sponsorship": sponsorship,
            # Never assumed: a default of True claimed citizenship for applicants who
            # need sponsorship. Needing sponsorship rules citizenship out; otherwise
            # only an explicit profile value answers.
            "us_citizen": p.get("us_citizen", False if sponsorship else None),
            "work_authorized": p.get("work_eligible_us"),
            # "authorized to work WITHOUT sponsorship": yes only if both hold
            "authorized_without_sponsorship": (
                None if p.get("work_eligible_us") is None or sponsorship is None
                else bool(p.get("work_eligible_us")) and not sponsorship
            ),
            "age_over_18": p.get("age_over_18"),
            "currently_employed": p.get("currently_employed", True),
            "may_contact_employer": p.get("may_contact_employer", False),
            "worked_here_before": False,
            "referred_by_employee": False,
            "relatives_employed": False,
            "felony": False,
            "background_check_ok": True,
            "drug_test_ok": True,
            "ever_terminated": p.get("ever_terminated", False),
            "security_clearance": p.get("security_clearance", False),
            "military": False,
            "willing_relocate": p.get("willing_relocate", True),
            "willing_travel": p.get("willing_travel", True),
            "onsite_ok": p.get("onsite_ok", True),
            "weekends_evenings": p.get("weekends_evenings", False),
            "agree_terms": True,
        }
        if key in TEXT:
            v = TEXT[key]
            return Answer(key, "text", v) if v is not None else None
        if key in BOOL:
            return Answer(key, "bool", bool(BOOL[key])) if BOOL[key] is not None else None
        if key in OPTION_PREFS or key in (p.get("option_prefs") or {}):
            # The profile's own wording replaces the defaults instead of going first:
            # appended defaults let a form without the user's answer fall through to
            # "decline" or, for work_auth_type, to "citizen".
            mine = (p.get("option_prefs") or {}).get(key)
            if mine:
                return Answer(key, "option", list(mine))
            if key == "work_auth_type" and p.get("visa_sponsorship"):
                return None  # the default list starts with "citizen" — wrong here
            defaults = list(OPTION_PREFS.get(key) or [])
            return Answer(key, "option", defaults) if defaults else None
        if key == "resume_upload":
            return Answer(key, "file", p.get("resume_path", ""))
        if key == "cover_letter":
            return None  # LLM tier (or skip when optional)
        if key in ("start_date", "signature_date", "education_dates"):
            # only a signature date means "today"; an availability date that falls
            # back to today put the fill date into education start-date fields
            v = p.get({"start_date": "available_start_date"}.get(key, key))
            if key == "signature_date":
                v = v or p.get("today")
            return Answer(key, "date", v) if v else None
        if key in ("date_month", "date_day", "date_year"):
            return Answer(key, "date_part", key.removeprefix("date_"))
        if key == "skill_years":
            years = self._skill_years_for(label)
            return Answer(key, "text", years) if years is not None else None
        if key == "skills_list":
            skills = p.get("skills") or []
            return Answer(key, "text", ", ".join(skills[:10])) if skills else None
        if key == "reason_for_leaving":
            return Answer(key, "text", p.get("reason_for_leaving", "Seeking growth opportunities"))
        if key == "hours_availability":
            return Answer(key, "text", p.get("hours_availability", "40"))
        if key == "job_duties":
            return Answer(key, "text", p.get("job_duties", "See resume for details"))
        if key == "job_from_date":
            v = p.get("job_from_date")
            return Answer(key, "date", v) if v else None
        if key == "job_to_date":  # most recent role is current -> present/blank
            return Answer(key, "date", p.get("job_to_date", ""))
        if key == "currently_work_here":
            return Answer(key, "bool", p.get("currently_employed", True))
        if key == "referred_by_name":
            return Answer(key, "text", p.get("referred_by_name", "N/A"))
        if key == "non_compete":
            return Answer(key, "bool", False)
        if key == "leave_blank":
            return Answer(key, "skip", "")
        if key == "handoff":
            return Answer(key, "handoff", "")
        if key == "skill_checkbox":
            mine = {
                normalize_label_for_skill(s)
                for s in [*(p.get("skills") or []), *(p.get("skill_years") or {})]
            }
            lab = normalize_label_for_skill(label)
            have = lab in mine or any(
                len(s) > 3 and (s in lab or lab in s) for s in mine
            )
            return Answer(key, "bool", have)
        return None

    def text_for(self, key: str) -> str | None:
        """Literal wording for an enumerated key when the form asks it as free text
        ("How did you hear about us?" as a textbox): the profile's own value first."""
        own = {
            "field_of_study": self.p.get("field_of_study"),
            "how_heard": self.p.get("how_heard_text"),
            "education_level": self.p.get("degree"),
        }.get(key)
        return own or OPTION_TEXT.get(key)

    def _skill_years_for(self, label: str) -> str | None:
        """'years of experience with Databricks?' -> look the skill up in the
        skill_years map (produced by the onboarding resume parse)."""
        sy: dict[str, Any] = self.p.get("skill_years") or {}
        l = label.lower()
        best = None
        for skill, years in sy.items():
            if skill.lower() in l:
                best = years if best is None else max(best, years)
        if best is not None:
            return str(best)
        # generic fallback: global YoE for "N years experience in <unknown>"
        if re.search(r"years", l) and self.p.get("years_of_experience"):
            return str(self.p["years_of_experience"])
        return None


def normalize_label_for_skill(label: str) -> str:
    return re.sub(r"[^a-z0-9+#]", "", label.lower().strip())


def example_profile() -> dict:
    """Shape the DB integration must produce. Also used by tests."""
    return {
        "first_name": "Jane",
        "last_name": "Doe",
        "full_name": "Jane Doe",
        "email": "jane@x.dev",
        "phone": "5125550100",
        "street_address": "1 Main St",
        "city": "Austin",
        "state": "TX",
        "zip_code": "78701",
        "country": "United States",
        "location": "Austin, TX",
        "linkedin": "https://linkedin.com/in/janedoe",
        "github": "https://github.com/janedoe",
        "login_email": "jane@x.dev",
        "login_password": "hunter2",
        "years_of_experience": 6,
        "desired_salary": "120000",
        "current_title": "Data Engineer",
        "current_company": "Acme",
        "degree": "Master's",
        "school": "UT Austin",
        "visa_sponsorship": False,
        "work_eligible_us": True,
        "us_citizen": True,
        "age_over_18": True,
        "willing_relocate": True,
        "willing_travel": True,
        "ever_terminated": False,
        "available_start_date": "2026-08-01",
        "resume_path": "/resume/resume.pdf",
        "skills": ["Python", "SQL", "Spark", "Airflow"],
        "skill_years": {"Python": 6, "SQL": 6, "Spark": 4, "Databricks": 3, "AWS": 5},
        "option_prefs": {"eeo_gender": [r"^woman\b|^female\b"]},
    }

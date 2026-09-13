"""Build the answer-store profile from `autoapply profile export` JSON.

The export is one role's profile: the user's facts plus that role's parsed resume.
AnswerStore wants a flat dict (see answers.example_profile). Two things matter more
than the field copying:

  * EEO and source answers become `option_prefs` built from the user's own words, so
    a form is answered with what the user said — never with the store's
    decline-first defaults when the user gave an answer.
  * Unknowns stay unknown. Nothing here invents citizenship, a work-authorization
    category or a skill level: a missing value makes the field "no-answer", which
    goes to the LLM tier or a human instead of onto the application.
"""

from __future__ import annotations

import re
from datetime import date


def _esc_words(text: str) -> str:
    return r"\b" + re.escape(re.sub(r"\s+", " ", text.strip().lower())) + r"\b"


# The user's wording -> patterns that recognise the same answer on other forms.
# Checked in order; the user's literal wording always goes first.
_EEO_VOCAB: dict[str, list[tuple[str, list[str]]]] = {
    "eeo_gender": [
        (r"^(male|man)$", [r"^male$", r"^man$", r"^male\b", r"^man\b"]),
        (r"^(female|woman)$", [r"^female$", r"^woman$", r"^female\b", r"^woman\b"]),
        (r"non.?binary", [r"non ?binary"]),
        (r"prefer not|decline", [r"decline|prefer not|do not wish|don t wish"]),
    ],
    "eeo_race": [
        (r"^asian", [r"^asian\b"]), (r"^white", [r"^white\b", r"caucasian"]),
        (r"black|african", [r"\bblack\b", r"african american"]),
        (r"hispanic|latin", [r"hispanic", r"latino"]),
        (r"two or more|multi", [r"two or more", r"multiracial"]),
        (r"native hawaiian|pacific", [r"native hawaiian", r"pacific islander"]),
        (r"american indian|alaska", [r"american indian", r"alaska native"]),
        (r"prefer not|decline", [r"decline|prefer not|do not wish|don t wish"]),
    ],
    "eeo_hispanic": [
        (r"^no\b", [r"^no\b", r"not hispanic"]),
        (r"^yes\b", [r"^yes\b"]),
        (r"prefer not|decline", [r"decline|prefer not|do not wish"]),
    ],
    "eeo_veteran": [
        (r"not a (protected )?veteran|^no\b", [r"not a (protected )?veteran", r"^i am not\b", r"^no\b"]),
        (r"prefer not|decline|do not wish", [r"decline|prefer not|do not wish|don t wish"]),
        (r"identify as|protected veteran|^yes\b", [r"i identify as", r"one or more", r"^yes\b"]),
    ],
    "eeo_disability": [
        (r"^no\b|do not have", [r"^no\b.*disab", r"do not have a disability", r"^no\b"]),
        (r"^yes\b|have a disability", [r"^yes\b.*disab", r"^yes\b"]),
        (r"prefer not|decline|do not want", [r"do not want to answer|decline|prefer not"]),
    ],
}


def option_prefs_for(key: str, answer: str | None) -> list[str]:
    """Ordered regexes for the user's answer to an enumerated question."""
    if not answer or not answer.strip():
        return []
    low = answer.strip().lower()
    prefs = [_esc_words(answer)]
    for trigger, pats in _EEO_VOCAB.get(key, []):
        if re.search(trigger, low):
            prefs += pats
            break
    return list(dict.fromkeys(prefs))


def _current_job(work: list[dict]) -> dict:
    current = [w for w in work if w.get("is_current")]
    return (current or work or [{}])[0]


def from_export(doc: dict, *, resume_path: str = "", login_password: str | None = None,
                today: date | None = None) -> dict:
    """Flat AnswerStore profile from one role's export document."""
    c = doc.get("contact") or {}
    f = doc.get("facts") or {}
    work = doc.get("work_experiences") or []
    edu = doc.get("education") or []
    job = _current_job(work)
    first_edu = edu[0] if edu else {}
    skills = [s for g in (doc.get("skill_groups") or []) for s in (g.get("skills") or [])]
    name_parts = [f.get("first_name"), f.get("middle_name"), f.get("last_name")]

    prefs = {
        key: option_prefs_for(key, f.get(col))
        for key, col in (
            ("eeo_gender", "gender"), ("eeo_race", "race_ethnicity"),
            ("eeo_hispanic", "hispanic_latino"), ("eeo_veteran", "veteran_status"),
            ("eeo_disability", "disability_status"),
        )
    }
    if first_edu.get("major"):
        # the user's major before the generic data/CS fallbacks
        prefs["field_of_study"] = [_esc_words(first_edu["major"]), r"data (science|analytics)",
                                   r"computer science", r"information (systems|technology)"]
    if f.get("how_did_you_hear"):
        prefs["how_heard"] = [_esc_words(f["how_did_you_hear"]), r"job board", r"other"]
    prefs = {k: v for k, v in prefs.items() if v}

    out = {
        "first_name": f.get("first_name"),
        "middle_name": f.get("middle_name") or "",
        "last_name": f.get("last_name"),
        "full_name": c.get("full_name") or " ".join(p for p in name_parts if p),
        "preferred_name": f.get("preferred_name"),
        "pronouns": f.get("pronouns"),
        "email": c.get("email"),
        "phone": c.get("phone"),
        "phone_country_code": f.get("phone_country_code"),
        "street_address": f.get("street_address"),
        "city": f.get("city"),
        "state": f.get("state"),
        "zip_code": f.get("zip_code"),
        "country": f.get("country"),
        "location": c.get("location"),
        "linkedin": c.get("linkedin"),
        "github": c.get("github"),
        "website": f.get("portfolio_url"),
        "login_email": (doc.get("portal_login") or {}).get("email") or c.get("email"),
        "login_password": login_password,
        "years_of_experience": f.get("years_of_experience"),
        "desired_salary": f.get("desired_salary"),
        "notice_period": f.get("notice_period"),
        "available_start_date": f.get("available_start_date"),
        "current_title": job.get("job_title"),
        "current_company": job.get("company_name"),
        "job_from_date": job.get("from_date"),
        "currently_employed": bool(job.get("is_current")),
        "degree": first_edu.get("degree"),
        "school": first_edu.get("school"),
        "field_of_study": first_edu.get("major"),
        "education_dates": first_edu.get("to_date"),
        "how_heard_text": f.get("how_did_you_hear"),
        "visa_sponsorship": f.get("visa_sponsorship"),
        "work_eligible_us": f.get("work_eligible_us"),
        "willing_relocate": f.get("willing_relocate"),
        "willing_travel": f.get("willing_travel"),
        "ever_terminated": f.get("ever_terminated"),
        "security_clearance": f.get("security_clearance"),
        "resume_path": resume_path,
        "skills": skills,
        "skill_years": {},
        "today": (today or date.today()).isoformat(),
        "option_prefs": prefs,
    }
    if f.get("over_18") is not None:
        out["age_over_18"] = f["over_18"]
    return {k: v for k, v in out.items() if v is not None}

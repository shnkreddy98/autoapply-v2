"""Render an applicant's Postgres profile into the text block the browser agent
uses to fill job applications.

No files, no staging: `build_profile(conn, user_id, resume_path)` fetches the
profile bundle from Postgres (via `common.repo.fetch_profile`) and renders it
in-memory. The resume's in-container path is supplied by the caller (the apply
consumer stages the actual file per run).
"""

from __future__ import annotations

import re
from typing import Any

import psycopg

from common.repo import ProfileData, fetch_profile


def build_profile(conn: psycopg.Connection, user_id: str, resume_path: str) -> str:
    """Fetch + render a user's full profile in one call."""
    return render_profile(fetch_profile(conn, user_id), resume_path)


def _yn(value: object) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return "(not provided)" if value in (None, "") else str(value)


def _v(value: object) -> str:
    """Render a plain value, mapping NULL/empty to the sentinel the header tells the
    agent to leave blank — so a missing field never gets typed into a form as 'None'."""
    return "(not provided)" if value in (None, "") else str(value)


def _split_name(full_name: str | None) -> tuple[str, str]:
    """Split a full name into (first, last): the LAST whitespace token is the last
    name, everything before it the first (a middle name stays with first). Single
    token -> first only."""
    parts = (full_name or "").split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


def _linkedin_url(raw: str | None) -> str:
    """Normalize a LinkedIn URL to https://www.linkedin.com/in/<handle>. Workday and
    others reject `linkedin.com/in/...` without `www.`."""
    s = (raw or "").strip()
    if not s:
        return ""
    s = re.sub(r"^https?://", "", s, flags=re.IGNORECASE)
    if s.lower().startswith("linkedin.com"):
        s = "www." + s
    elif not s.lower().startswith("www.linkedin.com"):
        s = "www.linkedin.com/" + s.lstrip("/")
        if "/in/" not in s.lower():
            s = s.replace("www.linkedin.com/", "www.linkedin.com/in/", 1)
    return "https://" + s


def _date_span(frm: Any, to: Any) -> str:
    f = frm if frm not in (None, "") else ""
    t = to if to not in (None, "") else "Present"
    return f"{f} – {t}"


def render_profile(p: ProfileData, resume_path: str) -> str:
    """Render a ProfileData bundle into the agent's APPLICATION PROFILE text."""
    u, d = p.user, p.profile
    email = u.get("email", "")
    login = p.login or {}
    L: list[str] = []
    add = L.append

    add("# APPLICATION PROFILE")
    add(f"# Applicant: {u.get('full_name')} <{email}>")
    add(f"# Resume to upload (path inside browser): {resume_path}")
    add("# Use these values to fill application form fields. Leave blank any")
    add("# field marked (not provided). Voluntary/EEO fields may be left blank.")
    add("")

    add("## Login Credentials (for portals that require an account)")
    add(f"Login Email: {login.get('login_email') or email}")
    add(
        f"Login Password: {login.get('login_password') or '(not set — sign-in/account creation unavailable)'}"
    )
    add("")

    add("## Personal Information")
    add(f"Full Name: {_v(u.get('full_name'))}")
    # Confirmed name parts win; the split is only a fallback for profiles saved
    # before `profile init` asked for them.
    first, last = _split_name(u.get("full_name"))
    add(f"First Name: {_v(d.get('first_name') or first)}")
    add(f"Middle Name: {_v(d.get('middle_name'))}")
    add(f"Last Name: {_v(d.get('last_name') or last)}")
    add(f"Preferred Name: {_v(d.get('preferred_name'))}")
    add(f"Pronouns: {_v(d.get('pronouns'))}")
    add(f"Email: {email}")
    add(f"Phone: {_v(u.get('phone'))}")
    add(f"Phone Country Code: {_v(d.get('phone_country_code'))}")
    add(f"Date of Birth: {_yn(d.get('date_of_birth'))}")
    add(f"Street Address: {_v(d.get('street_address'))}")
    add(f"City: {_v(d.get('city'))}")
    add(f"State: {_v(d.get('state'))}")
    add(f"ZIP Code: {_v(d.get('zip_code'))}")
    add(f"Country: {_v(d.get('country'))}")
    add(f"18 or older: {_yn(d.get('over_18'))}")
    add(f"LinkedIn: {_v(_linkedin_url(u.get('linkedin')))}")
    add(f"GitHub: {_v(u.get('github'))}")
    add(f"Portfolio: {_v(d.get('portfolio_url'))}")
    add(f"Location: {_v(u.get('location'))}")
    add("")

    add("## Work Authorization")
    add(f"Eligible to work in the US: {_yn(d.get('work_eligible_us'))}")
    add(f"Requires visa sponsorship: {_yn(d.get('visa_sponsorship'))}")
    add(f"Security Clearance: {_yn(d.get('security_clearance'))}")
    add("")

    add("## Position Preferences")
    add(f"Available Start Date: {_v(d.get('available_start_date'))}")
    add(f"Notice Period: {_v(d.get('notice_period'))}")
    add(f"How did you hear about us: {_v(d.get('how_did_you_hear'))}")
    add(f"Desired Salary: {_v(d.get('desired_salary'))}")
    add(f"Employment Type: {_v(d.get('employment_type'))}")
    add(f"Years of Experience: {_v(d.get('years_of_experience'))}")
    add(f"Willing to relocate: {_yn(d.get('willing_relocate'))}")
    add(f"Willing to travel: {_yn(d.get('willing_travel'))}")
    add(f"Travel Percentage: {_yn(d.get('travel_percentage'))}")
    add("")

    add("## Employment History")
    add(f"Currently employed: {_yn(d.get('current_employee'))}")
    add(f"Ever terminated/asked to resign: {_yn(d.get('ever_terminated'))}")
    add(f"Termination explanation: {_yn(d.get('termination_explanation'))}")
    add("")

    add("## EEO Information (Voluntary — leave blank unless required)")
    add(f"Gender: {_yn(d.get('gender'))}")
    add(f"Race / Ethnicity: {_yn(d.get('race_ethnicity'))}")
    add(f"Hispanic or Latino: {_yn(d.get('hispanic_latino'))}")
    add(f"Veteran Status: {_yn(d.get('veteran_status'))}")
    add(f"Disability Status: {_yn(d.get('disability_status'))}")
    add("")

    add("## Declarations (check / agree if asked)")
    add(f"All information provided is true and accurate: {_yn(d.get('cert_accuracy'))}")
    add(f"False statements may result in dismissal: {_yn(d.get('cert_dismissal'))}")
    add(f"Authorize background/reference checks: {_yn(d.get('cert_background_check'))}")
    add(f"Authorize drug testing (if applicable): {_yn(d.get('cert_drug_testing'))}")
    add(f"Understand at-will employment: {_yn(d.get('cert_at_will'))}")
    add(
        f"Read and understand the job description: {_yn(d.get('cert_job_description'))}"
    )
    add(f"Acknowledge receipt of privacy notice: {_yn(d.get('cert_privacy_notice'))}")
    add(
        f"Consent to data processing for this application: {_yn(d.get('cert_data_processing'))}"
    )
    add("")

    add("## Signature")
    add(f"Electronic Signature: {_v(d.get('electronic_signature'))}")
    add(f"Signature Date: {_yn(d.get('signature_date'))}")
    add("")

    add("## References")
    for ref in p.references:
        add(
            f"- {ref.get('name')} <{ref.get('email')}> — {ref.get('relationship')}, "
            f"known {ref.get('known_duration')}"
        )
    if not p.references:
        add("(none provided)")
    add("")

    # --- resume content ---
    add("## Resume — Summary")
    add(u.get("summary") or "(none)")
    add("")

    add("## Resume — Work Experience")
    for job in p.work_experiences:
        title = job.get("job_title", "")
        company = job.get("company_name", "")
        loc = job.get("location", "")
        span = _date_span(job.get("from_date"), job.get("to_date"))
        add(f"- {title}, {company} ({loc}) [{span}]")
        for bullet in job.get("bullets") or []:
            add(f"    • {bullet}")
    add("")

    add("## Resume — Education")
    for edu in p.education:
        span = _date_span(edu.get("from_date"), edu.get("to_date"))
        gpa = f"GPA {edu['gpa']}" if edu.get("gpa") else None
        bits = [edu.get("degree"), edu.get("major"), edu.get("school"), span, gpa]
        add(f"- {' | '.join(str(b) for b in bits if b)}")
    add("")

    add("## Resume — Skills")
    groups = []
    for s in p.skill_groups:
        skills = ", ".join(s.get("skills") or [])
        cat = s.get("category")
        groups.append(f"{cat}: {skills}" if cat else skills)
    add("\n".join(groups) if groups else "(none)")
    add("")

    return "\n".join(L)

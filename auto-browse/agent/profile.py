"""Export the applicant's profile from the autoapply Postgres DB into a single
human-readable text file the browser agent can use to fill job applications,
and stage the resume PDF where the Playwright container can upload it.

Run once before the agent:

    uv run python -m agent.profile

Outputs:
    data/profile/profile.txt        # the LLM context (form fields + resume)
    data/resume/<file>.pdf          # mounted into the playwright container

DB connection and resume choice are env-configurable (see constants below).
"""

import json
import os
import shutil
from pathlib import Path

import psycopg
from dotenv import load_dotenv

load_dotenv()

# --- config -----------------------------------------------------------------
DB = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": os.getenv("DB_PORT", "25432"),
    "dbname": os.getenv("DB_NAME", "jobs-db"),
    "user": os.getenv("DB_USER", "admin"),
    "password": os.getenv("DB_PASSWORD", "password"),
}
EMAIL = os.getenv("APPLICANT_EMAIL", "shnkreddy98@gmail.com")

# Login credentials the agent uses for portals that require an account.
# Sourced from env for now; will move to the DB later.
LOGIN_EMAIL = os.getenv("APPLICANT_LOGIN_EMAIL", EMAIL)
LOGIN_PASSWORD = os.getenv("APPLICANT_LOGIN_PASSWORD", "")

# Where the autoapply repo keeps resume files, and which variant to upload.
AUTOAPPLY_DATA = Path(os.getenv("AUTOAPPLY_DATA", "../../autoapply/data")).expanduser()
RESUME_VARIANT = os.getenv("RESUME_VARIANT", "aws")  # "" base, or aws/gcp/azu
RESUME_FILE = os.getenv("RESUME_FILE", "shashank_reddy.pdf")

HERE = Path(__file__).resolve().parent.parent
PROFILE_TXT = HERE / "data" / "profile" / "profile.txt"
RESUME_OUT = HERE / "data" / "resume" / RESUME_FILE
# Path the resume is mounted at INSIDE the playwright container (see compose).
RESUME_CONTAINER_PATH = f"/resume/{RESUME_FILE}"


def _yn(value: object) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return "(not provided)" if value in (None, "") else str(value)


def _fetch() -> tuple[dict, dict, dict]:
    with (
        psycopg.connect(**DB, row_factory=psycopg.rows.dict_row) as conn,
        conn.cursor() as cur,
    ):
        cur.execute("SELECT * FROM users WHERE email = %s", (EMAIL,))
        user = cur.fetchone() or {}
        cur.execute("SELECT * FROM user_data WHERE email = %s", (EMAIL,))
        data = cur.fetchone() or {}
        cur.execute(
            "SELECT * FROM resumes WHERE user_email = %s ORDER BY id LIMIT 1", (EMAIL,)
        )
        resume = cur.fetchone() or {}
    if not data:
        raise SystemExit(f"No user_data row for {EMAIL}. Check the DB.")
    return user, data, resume


def _render(user: dict, d: dict, r: dict) -> str:
    L: list[str] = []
    add = L.append

    add("# APPLICATION PROFILE")
    add(f"# Applicant: {d.get('full_name')} <{EMAIL}>")
    add(f"# Resume to upload (path inside browser): {RESUME_CONTAINER_PATH}")
    add("# Use these values to fill application form fields. Leave blank any")
    add("# field marked (not provided). Voluntary/EEO fields may be left blank.")
    add("")

    add("## Login Credentials (for portals that require an account)")
    add(f"Login Email: {LOGIN_EMAIL}")
    add(
        f"Login Password: {LOGIN_PASSWORD or '(not set — sign-in/account creation unavailable)'}"
    )
    add("")

    add("## Personal Information")
    add(f"Full Name: {d.get('full_name')}")
    add(f"Email: {EMAIL}")
    add(f"Phone: {d.get('phone_number')}")
    add(f"Date of Birth: {_yn(d.get('date_of_birth'))}")
    add(f"Street Address: {d.get('street_address')}")
    add(f"City: {d.get('city')}")
    add(f"State: {d.get('state')}")
    add(f"ZIP Code: {d.get('zip_code')}")
    add(f"18 or older: {_yn(d.get('age_18_or_older'))}")
    add(f"LinkedIn: {user.get('linkedin')}")
    add(f"GitHub: {user.get('github')}")
    add(f"Location: {user.get('location')}")
    add("")

    add("## Work Authorization")
    add(f"Eligible to work in the US: {_yn(d.get('work_eligible_us'))}")
    add(f"Requires visa sponsorship: {_yn(d.get('visa_sponsorship'))}")
    add(f"Security Clearance: {_yn(d.get('security_clearance'))}")
    add("")

    add("## Position Preferences")
    add(f"Available Start Date: {d.get('available_start_date')}")
    add(f"Desired Salary: {d.get('desired_salary')}")
    add(f"Employment Type: {d.get('employment_type')}")
    add(f"Years of Experience: {d.get('years_of_experience')}")
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
    add(f"Electronic Signature: {d.get('electronic_signature')}")
    add(f"Signature Date: {_yn(d.get('signature_date'))}")
    add("")

    # --- resume context ---
    add("## Resume — Summary")
    add(r.get("summary") or "(none)")
    add("")

    add("## Resume — Work Experience")
    for job in _as_list(r.get("job_experience")):
        title = job.get("job_title", "")
        company = job.get("company_name", "")
        loc = job.get("location", "")
        span = f"{job.get('from_', '')} – {job.get('to_', '')}"
        add(f"- {title}, {company} ({loc}) [{span}]")
        for bullet in job.get("experience", []) or []:
            add(f"    • {bullet}")
    add("")

    add("## Resume — Education")
    for edu in _as_list(r.get("education")):
        parts = [str(v) for v in edu.values() if v]
        add(f"- {' | '.join(parts)}")
    add("")

    add("## Resume — Skills")
    skills = _as_list(r.get("skills"))
    flat = []
    for s in skills:
        flat.append(", ".join(map(str, s.values())) if isinstance(s, dict) else str(s))
    add(", ".join(flat) if flat else "(none)")
    add("")

    return "\n".join(L)


def _as_list(value: object) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return value if isinstance(value, list) else []


def _stage_resume() -> None:
    src = AUTOAPPLY_DATA / "resumes"
    src = src / RESUME_VARIANT / RESUME_FILE if RESUME_VARIANT else src / RESUME_FILE
    if not src.is_file():
        raise SystemExit(f"Resume not found: {src}")
    RESUME_OUT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, RESUME_OUT)
    print(
        f"Staged resume: {src} -> {RESUME_OUT}  (browser path {RESUME_CONTAINER_PATH})"
    )


def main() -> None:
    user, data, resume = _fetch()
    PROFILE_TXT.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_TXT.write_text(_render(user, data, resume), encoding="utf-8")
    print(f"Wrote profile: {PROFILE_TXT}")
    _stage_resume()


if __name__ == "__main__":
    main()

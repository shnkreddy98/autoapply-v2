"""M2: profile render (unit, golden) + fetch+render from Postgres (integration)."""

from agent.profile import _linkedin_url, _split_name, build_profile, render_profile
from common.repo import ProfileData


def _sample() -> ProfileData:
    return ProfileData(
        user={
            "full_name": "Jane Quinn Doe",
            "email": "shnk@example.com",
            "phone": "+1 555 0100",
            "linkedin": "linkedin.com/in/janedoe",
            "github": "github.com/shnk",
            "location": "Omaha, NE",
            "summary": "Data engineer with dbt + Airflow experience.",
        },
        profile={
            "city": "Omaha",
            "work_eligible_us": True,
            "visa_sponsorship": False,
            "years_of_experience": 5,
            "gender": None,  # voluntary -> (not provided)
        },
        work_experiences=[
            {
                "job_title": "Data Engineer",
                "company_name": "Airfold",
                "location": "Remote",
                "from_date": "2023-01-01",
                "to_date": None,  # current -> Present
                "bullets": ["Built pipelines", "Owned dbt models"],
            }
        ],
        education=[
            {
                "degree": "MS",
                "major": "CS",
                "school": "UNO",
                "from_date": "2019-08-01",
                "to_date": "2021-05-01",
            }
        ],
        skill_groups=[{"category": "Languages", "skills": ["Python", "SQL"]}],
        references=[
            {
                "name": "Julian",
                "email": "j@a.co",
                "relationship": "CEO",
                "known_duration": "1 year",
            }
        ],
        login={"login_email": "shnk@example.com", "login_password": "secret"},
    )


def test_split_name():
    assert _split_name("Jane Quinn Doe") == (
        "Jane Quinn",
        "Doe",
    )
    assert _split_name("Cher") == ("Cher", "")
    assert _split_name("") == ("", "")


def test_linkedin_normalize():
    assert _linkedin_url("linkedin.com/in/x") == "https://www.linkedin.com/in/x"
    assert (
        _linkedin_url("https://www.linkedin.com/in/x")
        == "https://www.linkedin.com/in/x"
    )
    assert _linkedin_url("shnkreddy") == "https://www.linkedin.com/in/janedoe"
    assert _linkedin_url("") == ""


def test_render_profile_golden():
    out = render_profile(_sample(), "/work/runs/r1/resume.pdf")
    assert "# Resume to upload (path inside browser): /work/runs/r1/resume.pdf" in out
    assert "First Name: Jane Quinn" in out
    assert "Last Name: Doe" in out
    assert "LinkedIn: https://www.linkedin.com/in/janedoe" in out
    assert "Eligible to work in the US: Yes" in out
    assert "Requires visa sponsorship: No" in out
    assert "Gender: (not provided)" in out
    assert "Login Password: secret" in out
    # work experience with current -> Present, bullets rendered
    assert "- Data Engineer, Airfold (Remote) [2023-01-01 – Present]" in out
    assert "    • Built pipelines" in out
    assert "Languages: Python, SQL" in out
    assert "- Julian <j@a.co> — CEO, known 1 year" in out


def test_render_prefers_the_confirmed_name_parts():
    """The full_name split puts a middle name into First Name; once `profile init`
    has stored the parts, those are what the forms get."""
    p = ProfileData(
        user={"email": "a@b.c", "full_name": "Jane Quinn Doe"},
        profile={
            "first_name": "Jane",
            "middle_name": "Quinn",
            "last_name": "Doe",
            "over_18": True,
            "country": "United States",
        },
    )
    out = render_profile(p, "/r.pdf")
    assert "First Name: Jane\n" in out
    assert "Middle Name: Quinn" in out
    assert "Last Name: Doe" in out
    # was read from a column that never existed, so it always said (not provided)
    assert "18 or older: Yes" in out
    assert "Country: United States" in out


def test_render_empty_sections_safe():
    p = ProfileData(user={"email": "a@b.c"}, profile={})
    out = render_profile(p, "/r.pdf")
    assert "## References" in out and "(none provided)" in out
    assert "## Resume — Skills" in out and "(none)" in out


# ---------------- integration ----------------


def test_build_profile_from_db(db):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, full_name, summary) VALUES (%s, %s, %s) RETURNING user_id",
            ("db@example.com", "Db User", "A summary"),
        )
        user_id = cur.fetchone()["user_id"]
        cur.execute(
            "INSERT INTO application_profiles (user_id, city, work_eligible_us) VALUES (%s, %s, %s)",
            (user_id, "Omaha", True),
        )
        cur.execute(
            "INSERT INTO work_experiences (user_id, job_title, company_name, bullets) "
            "VALUES (%s, %s, %s, %s)",
            (user_id, "Engineer", "Acme", ["did things"]),
        )
        cur.execute(
            "INSERT INTO skill_groups (user_id, category, skills) VALUES (%s, %s, %s)",
            (user_id, "Lang", ["Go"]),
        )
    db.commit()
    out = build_profile(db, user_id, "/resume/x.pdf")
    assert "# Applicant: Db User <db@example.com>" in out
    assert "City: Omaha" in out
    assert "- Engineer, Acme" in out
    assert "    • did things" in out
    assert "Lang: Go" in out

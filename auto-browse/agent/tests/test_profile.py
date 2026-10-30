"""M2: profile render (unit, golden) + fetch+render from Postgres (integration)."""

from agent.profile import _linkedin_url, _split_name, build_profile, render_profile
from common.repo import ProfileData


def _sample() -> ProfileData:
    return ProfileData(
        user={
            "full_name": "Jane Quinn Doe",
            "email": "jane@example.com",
            "phone": "+1 555 0100",
            "linkedin": "linkedin.com/in/janedoe",
            "github": "github.com/janedoe",
            "location": "Omaha, NE",
        },
        profile={
            "city": "Omaha",
            "work_eligible_us": True,
            "visa_sponsorship": False,
            "years_of_experience": 5,
            "gender": None,  # voluntary -> (not provided)
        },
        label="de",
        summary="Data engineer with dbt + Airflow experience.",
        work_experiences=[
            {
                "job_title": "Data Engineer",
                "company_name": "Acme",
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
        login={"login_email": "jane@example.com", "login_password": "secret"},
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
    assert _linkedin_url("janedoe") == "https://www.linkedin.com/in/janedoe"
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
    assert "- Data Engineer, Acme (Remote) [2023-01-01 – Present]" in out
    assert "    • Built pipelines" in out
    assert "Languages: Python, SQL" in out
    assert "- Julian <j@a.co> — CEO, known 1 year" in out
    assert "# Resume role: de" in out
    assert "Data engineer with dbt + Airflow experience." in out


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


def _user_with_roles(db):
    """A user whose default role says Engineer and whose `de` role says Data Engineer."""
    from common import repo

    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, full_name, default_resume_label) "
            "VALUES (%s, %s, 'general') RETURNING user_id",
            ("db@example.com", "Db User"),
        )
        user_id = cur.fetchone()["user_id"]
        cur.execute(
            "INSERT INTO application_profiles (user_id, city, work_eligible_us) VALUES (%s, %s, %s)",
            (user_id, "Omaha", True),
        )
    db.commit()
    for label, title in (("general", "Engineer"), ("de", "Data Engineer")):
        asset = repo.insert_resume_asset(
            db, user_id, "base", f"k/{label}.docx", variant="docx", label=label
        )
        repo.set_resume_parsed(db, asset, {
            "summary": f"{label} summary",
            "work_experiences": [{"job_title": title, "company_name": "Acme", "bullets": ["did things"]}],
            "skill_groups": [{"category": "Lang", "skills": ["Go"]}],
        })
    return user_id


def test_build_profile_from_db(db):
    user_id = _user_with_roles(db)
    out = build_profile(db, user_id, "/resume/x.pdf")
    assert "# Applicant: Db User <db@example.com>" in out
    assert "City: Omaha" in out
    assert "- Engineer, Acme" in out
    assert "    • did things" in out
    assert "Lang: Go" in out


def test_build_profile_follows_the_jobs_role(db):
    """The form must list the same titles as the resume being uploaded: a job
    tailored from the `de` resume gets the `de` work history, not the default's."""
    from common import repo

    user_id = _user_with_roles(db)
    job_id = repo.add_job_for_user(db, user_id, "https://example.com/jobs/de-profile")
    de = repo.get_base_asset(db, user_id, variant="docx", label="de")
    repo.enqueue_tailoring(db, user_id, job_id, de["id"])

    out = build_profile(db, user_id, "/resume/x.pdf", job_id)
    assert "# Resume role: de" in out
    assert "- Data Engineer, Acme" in out
    assert "de summary" in out


def test_salary_ask_is_fitted_to_the_jobs_band(db):
    """A $140k ask against a $100k-$125k posting goes in as $125k, for both engines
    (agent: build_profile, det: fetch_job_profile); other jobs keep the ask."""
    from agent.profile import fetch_job_profile
    from common import repo

    user_id = _user_with_roles(db)
    with db.cursor() as cur:
        cur.execute(
            "UPDATE application_profiles SET desired_salary = '140000' WHERE user_id = %s",
            (user_id,),
        )
    db.commit()
    banded = repo.add_job_for_user(db, user_id, "https://example.com/jobs/banded")
    unbanded = repo.add_job_for_user(db, user_id, "https://example.com/jobs/unbanded")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE jobs SET salary_min = 100000, salary_max = 125000, salary_period = 'year' "
            "WHERE job_id = %s",
            (banded,),
        )
    db.commit()

    assert fetch_job_profile(db, user_id, banded).profile["desired_salary"] == "125000"
    assert "Desired Salary: 125000" in build_profile(db, user_id, "/r.pdf", banded)
    assert fetch_job_profile(db, user_id, unbanded).profile["desired_salary"] == "140000"
    assert "Desired Salary: 140000" in build_profile(db, user_id, "/r.pdf")


def test_password_never_reaches_traces_or_logs(tmp_path, monkeypatch):
    """The MCP echoes the code that typed the password; 72 trace / log / form files
    held it in plain text before secrets were scrubbed on write."""
    import json
    import logging
    import secrets

    from agent import observability as obs

    monkeypatch.setattr(obs, "TRACE_DIR", tmp_path)
    monkeypatch.setattr(obs, "LOG_DIR", tmp_path)
    monkeypatch.setattr(obs, "FORMS_DIR", tmp_path)
    # made at run time (no credential-shaped literal for secret scanners); the quote
    # checks that the JSON-escaped form is scrubbed too
    value = 'v"' + secrets.token_hex(6)
    obs.register_secret(value)
    trace = obs.Trace("r1")
    trace.event("tool_result", result=f"await page.fill({value!r});")
    obs.write_form_record("r1", {"Sign-in field": value})
    log = obs.setup_logging("r1")
    log.info("tool_call_result: %s", json.dumps({"code": f"fill({value!r})"}))
    for h in log.handlers:
        h.flush()
    written = "".join(p.read_text() for p in tmp_path.iterdir())
    assert value[2:] not in written and written.count("[redacted]") >= 3
    logging.getLogger(obs.LOGGER_NAME).handlers.clear()

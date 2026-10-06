"""JevResolver: the questions it builds, and which of Jev's answers it uses."""

import asyncio
import json

from det_autofill.classify import Row
from det_autofill.fill import FillResult
from det_autofill.jev import NONE, JevResolver, applicant_state, candidates

PROFILE = {
    "role": "de",
    "contact": {"full_name": "Morgan Castillo", "email": "m@example.com", "location": "Austin, TX"},
    "facts": {"first_name": "Morgan", "preferred_name": "Morgan", "city": "Austin", "desired_salary": "165000",
              "years_of_experience": 7, "visa_sponsorship": False, "middle_name": None},
    "portal_login": {"email": "m@example.com", "password_set": True},
    "work_experiences": [{"job_title": "Senior Data Engineer", "company_name": "Northwind", "is_current": True}],
    "education": [{"school": "UT Austin", "degree": "Bachelor's Degree", "major": "Computer Science"}],
    "skill_groups": [{"category": "Languages", "skills": ["Python", "SQL"]}],
}


def row(index, label, widget="text", options=None):
    return Row(index=index, selectors=[f"#r{index}"], widget=widget, label=label, required=True,
               options=[{"text": o} for o in (options or [])])


class FakeJev:
    """Answers each question with a fixed (choice, confidence); records the requests."""

    def __init__(self, picks):
        self.picks, self.requests = picks, []

    async def __call__(self, state, questions):
        self.requests.append((state, questions))
        return {qid: {"type": "choice", "choice": self.picks.get(qid, (NONE, 0.9))[0],
                      "confidence": self.picks.get(qid, (NONE, 0.9))[1]} for qid in questions}


def resolve(resolver, rows):
    return asyncio.run(resolver.resolve(rows, {}))


def test_state_drops_login_and_empty_values():
    state = applicant_state(PROFILE)
    assert "portal_login" not in json.dumps(state)
    assert "middle_name" not in state["applicant"]["facts"]
    assert state["applicant"]["facts"]["city"] == "Austin"


def test_candidates_are_profile_strings_with_merged_meanings():
    c = candidates(PROFILE)
    assert c["165000"].startswith("the applicant's desired annual base salary")
    # first and preferred name are one value, two meanings
    assert "first name" in c["Morgan"] and "preferred first name" in c["Morgan"]
    assert "Northwind" in c and "Python, SQL" in c
    assert {"Yes", "No"} <= set(c)
    assert "False" not in c  # booleans are answered as Yes / No, not typed


def test_one_request_per_page_with_options_and_none():
    jev = FakeJev({})
    resolve(JevResolver(jev, PROFILE), [row(1, "Will you require sponsorship?", "radio", ["Yes", "No"]),
                                        row(2, "Salary expectations")])
    assert len(jev.requests) == 1
    _, questions = jev.requests[0]
    assert set(questions["f1"]["criteria"]) == {"Yes", "No", NONE}
    assert "165000" in questions["f2"]["criteria"] and NONE in questions["f2"]["criteria"]
    assert questions["f1"]["instructions"]["form_field"] == "Will you require sponsorship?"


def test_confident_picks_fill_and_none_or_unsure_do_not():
    jev = FakeJev({"f1": ("No", 0.97), "f2": ("165000", 0.9), "f3": ("Yes", 0.3), "f4": (NONE, 0.99)})
    out = resolve(JevResolver(jev, PROFILE), [
        row(1, "Will you require sponsorship?", "radio", ["Yes", "No"]),
        row(2, "Salary expectations"),
        row(3, "Can you work weekends?", "select", ["Yes", "No"]),
        row(4, "Driver's license number"),
    ])
    assert out[1]["kind"] == "option" and out[1]["source"] == "jev"
    assert out[2] == {"kind": "text", "value": "165000", "source": "jev"}
    assert 3 not in out and 4 not in out  # unsure, and "the profile does not say"


def test_qualification_yes_needs_more_confidence():
    q = "Do you have experience with Databricks?"
    out = resolve(JevResolver(FakeJev({"f1": ("Yes", 0.7)}), PROFILE), [row(1, q, "radio", ["Yes", "No"])])
    assert out == {}  # a 0.7 Yes to a qualification is not enough
    out = resolve(JevResolver(FakeJev({"f1": ("No", 0.7)}), PROFILE), [row(1, q, "radio", ["Yes", "No"])])
    assert 1 in out  # a No claims nothing


def test_written_answers_go_to_the_writer_not_jev():
    class Writer:
        def __init__(self):
            self.rows = []

        async def resolve(self, rows, context):
            self.rows += rows
            return {r.index: {"kind": "text", "value": "Because...", "source": "ask"} for r in rows}

    jev, writer = FakeJev({}), Writer()
    out = resolve(JevResolver(jev, PROFILE, writer=writer), [row(5, "Why do you want to work here?"),
                                                             row(6, "Tell us about yourself", "textarea")])
    assert jev.requests == [] and [r.index for r in writer.rows] == [5, 6]
    assert out[5]["source"] == "ask"
    # without a writer they stay open
    assert resolve(JevResolver(FakeJev({}), PROFILE), [row(5, "Why do you want to work here?")]) == {}


def test_unreachable_model_is_a_gap():
    async def down(state, questions):
        raise ConnectionError("no route")

    context = {}
    out = asyncio.run(JevResolver(down, PROFILE).resolve([row(1, "Salary")], context))
    assert out == {} and "ConnectionError" in context["resolver_error"]


def test_learned_saves_jev_answers_as_llm_but_not_qualifications():
    class Saved:
        def __init__(self):
            self.puts = []

        def get(self, row, *, per_job):
            return None

        def put(self, row, answer, *, per_job, source):
            self.puts.append((row.label, answer, source))

    saved = Saved()
    r = JevResolver(FakeJev({}), PROFILE, saved=saved)
    ok = FillResult(True, "typed", "165000")
    asyncio.run(r.learned(row(1, "Salary"), {"kind": "text", "value": "165000", "source": "jev"}, ok, {}))
    asyncio.run(r.learned(row(2, "Do you have experience with Spark?"), {"kind": "text", "value": "Yes", "source": "jev"}, ok, {}))
    assert saved.puts == [("Salary", "165000", "llm")]


def test_experience_questions_treat_the_resume_as_complete():
    jev = FakeJev({})
    resolve(JevResolver(jev, PROFILE), [
        row(1, "What best describes your experience operating Databricks?", "radio", ["Daily", "No meaningful experience"]),
        row(2, "Are you a DACA recipient?", "radio", ["Yes", "No"]),
    ])
    _, questions = jev.requests[0]
    assert "resume_is_complete" in questions["f1"]["instructions"]
    assert "resume_is_complete" not in questions["f2"]["instructions"]  # absence is not an answer here


def test_dates_are_split_and_section_and_employers_are_given():
    state = applicant_state(PROFILE | {"work_experiences": [{"job_title": "DE", "from_date": "2022-03-01"}]})
    assert state["applicant"]["jobs"][0]["from_date"] == {"date": "2022-03-01", "month": "March", "month_number": 3,
                                                          "day": 1, "year": 2022}
    jev = FakeJev({})
    r = row(1, "Start Date (Month)", "select", ["January", "March"])
    r.section = "Work Experience"
    resolve(JevResolver(jev, PROFILE), [r, row(2, "Have you ever been an employee or contractor at Acme?", "radio", ["Yes", "No"])])
    _, q = jev.requests[0]
    assert q["f1"]["instructions"]["form_section"] == "Work Experience"
    assert "employers_are_complete" in q["f2"]["instructions"]

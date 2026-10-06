"""Jev (TypeSafe's System One model) as the resolver for fields the deterministic pass
left open. Same seat as llm.LLMResolver: engine.fill_page hands it the gaps, it returns
{row.index: answer}.

Jev answers typed questions about a state; it does not write text. So it fills only
what can be chosen:

  * a field with options (select, radio, a single checkbox, a combobox whose options
    were read): one Choice over the page's own option texts plus `none`
  * a short text box: code lists candidate values from the profile (city, salary,
    years, employer, ...) and Jev picks which one belongs in the box, or `none`. The
    value typed is the profile's own string, copied; Jev cannot invent one.
  * written answers (why this company, cover letters, text areas) go to `writer`, an
    LLMResolver, or stay unanswered

All of a page's open fields go out in one request; Jev evaluates them in parallel. An
answer is used only when Jev picks a real option with enough confidence. Qualification
and immigration questions need more confidence for a "Yes", because a wrong Yes there is
a false claim about the applicant (llm.py has the incident).

`system_one(state, questions) -> answers` is injected (the HTTP call lives in the
caller): no network code here, same as llm.py. Fill logs say source "jev"; saved
answers say "llm", because saved_answers.source allows profile / rule / llm / human.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Awaitable, Callable

from det_autofill.classify import Row
from det_autofill.fill import FillResult
from det_autofill.llm import ATTESTATION_RE, IMMIGRATION_RE, SavedAnswers, answer_from_text, is_per_job
from det_autofill.options import nv

SystemOne = Callable[[object, dict], Awaitable[dict]]

NONE = "none"
MAX_OPTIONS = 254  # a Choice takes up to 255 options; one is `none`
WRITTEN = {"textarea"}
# questions about what the applicant has done or knows: the resume answers them, absence included
EXPERIENCE_RE = re.compile(r"\bexperience\b|\bfamiliar\b|\bproficien|\bskills?\b|\bworked with\b|\bhands-on\b"
                           r"|\b(have|did) you (used|built|shipped|led|managed|worked)\b|\bdegree\b|\bcertifi", re.I)
# "Have you ever been an employee or contractor at X?": the job list answers it, absence included
EMPLOYER_RE = re.compile(r"\b(employ(ed|ee)|contractor|worked (for|at)|work(ing)? for)\b", re.I)
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]

_FIELD_NAMES = {
    # profile key -> how the candidate is described to Jev
    "full_name": "full name", "email": "email address", "phone": "phone number", "linkedin": "LinkedIn profile URL",
    "github": "GitHub profile URL", "location": "current location (city, state)", "first_name": "first name",
    "middle_name": "middle name", "last_name": "last name", "preferred_name": "preferred first name",
    "pronouns": "pronouns", "phone_country_code": "phone country calling code", "portfolio_url": "portfolio / personal website URL",
    "street_address": "street address", "city": "city of residence", "state": "state of residence", "zip_code": "ZIP / postal code",
    "country": "country of residence", "available_start_date": "earliest available start date",
    "notice_period": "notice period", "desired_salary": "desired annual base salary in US dollars",
    "employment_type": "employment type wanted", "years_of_experience": "total years of professional experience",
    "travel_percentage": "share of time willing to travel", "how_did_you_hear": "how the applicant heard about jobs",
    "electronic_signature": "electronic signature (full name)", "gender": "gender", "race_ethnicity": "race / ethnicity",
    "hispanic_latino": "Hispanic or Latino", "veteran_status": "veteran status", "disability_status": "disability status",
}


def _dated(value):
    """An ISO date with its parts spelled out. Jev reads dates as text and mixed up the
    day of 2022-03-01 (picked 2 for 1); the TypeSafe docs say to split dates in code."""
    if isinstance(value, str) and ISO_DATE.match(value):
        y, m, d = (int(x) for x in value.split("-"))
        return {"date": value, "month": MONTHS[m - 1], "month_number": m, "day": d, "year": y}
    return value


def _clean(record: dict) -> dict:
    return {k: _dated(v) for k, v in record.items() if v not in (None, "", [])}


def applicant_state(profile_doc: dict, today: date | None = None) -> dict:
    """The profile as Jev's state: facts with readable names, jobs and schooling, no
    portal login and no empty values (a large state full of nulls costs accuracy)."""
    doc = profile_doc or {}
    facts = _clean({**(doc.get("contact") or {}), **(doc.get("facts") or {})})
    jobs = [_clean(j) for j in doc.get("work_experiences") or []]
    return {
        "applicant": {
            "facts": facts,
            "summary": doc.get("summary") or "",
            "jobs": jobs,
            "education": [_clean(e) for e in doc.get("education") or []],
            "skills": [s for g in doc.get("skill_groups") or [] for s in g.get("skills") or []],
        },
        "today": (today or date.today()).isoformat(),
    }


def candidates(profile_doc: dict) -> dict[str, str]:
    """Values a short text box could take, each with what it is. Code finds them, Jev
    picks one: the typed value is always a profile string, verbatim."""
    doc = profile_doc or {}
    out: dict[str, list[str]] = {}

    def add(value, what: str) -> None:
        if value in (None, "") or isinstance(value, bool):
            return
        text = str(value).strip()
        if text and len(text) <= 200:
            out.setdefault(text, [])
            if what not in out[text]:
                out[text].append(what)

    for source in (doc.get("contact") or {}, doc.get("facts") or {}):
        for key, value in source.items():
            add(value, "the applicant's " + _FIELD_NAMES.get(key, key.replace("_", " ")))
    for n, job in enumerate(doc.get("work_experiences") or []):
        when = "current" if job.get("is_current") else ("most recent previous" if n == 1 else f"earlier (#{n + 1})")
        add(job.get("job_title"), f"job title at the {when} employer")
        add(job.get("company_name"), f"name of the {when} employer")
        add(job.get("location"), f"location of the {when} job")
    for n, edu in enumerate(doc.get("education") or []):
        add(edu.get("school"), "school / university attended")
        add(edu.get("degree"), "degree earned")
        add(edu.get("major"), "field of study / major")
    for g in doc.get("skill_groups") or []:
        if g.get("skills"):
            add(", ".join(g["skills"]), f"{(g.get('category') or 'skills').lower()} skills")
    # yes/no questions asked as a text box ("Have you ever been employed by X?")
    out.setdefault("Yes", []).append("the answer is yes: the applicant's facts state it")
    out.setdefault("No", []).append("the answer is no: the applicant's facts state the opposite")
    return {value: "; ".join(whats) for value, whats in out.items()}


def _option_texts(row: Row) -> list[str]:
    if row.widget == "checkbox" and not row.options:
        return ["Yes", "No"]
    seen, texts = set(), []
    for o in row.options:
        t = (o.get("text") or "").strip()
        if t and nv(t) not in seen and t.lower() != NONE:
            seen.add(nv(t))
            texts.append(t)
    return texts[:MAX_OPTIONS]


def _question(row: Row, options: list[str] | None, cands: dict[str, str]) -> dict:
    field = row.label or row.group_label or "(unlabelled field)"
    if options:
        if row.widget == "checkbox" and options == ["Yes", "No"]:
            criteria = {"Yes": "tick the box: `applicant` states that this applies to the applicant or that the applicant agrees",
                        "No": "leave it unticked: `applicant` states that it does not apply",
                        NONE: "`applicant` does not say"}
            ask = "Should the applicant tick the checkbox `form_field`?"
        else:
            criteria = {o: None for o in options} | {NONE: "`applicant` does not state the answer, so no option can be chosen"}
            ask = "Which option is the applicant's truthful answer to the job application question `form_field`?"
    else:
        criteria = dict(cands) | {NONE: "none of these values is the answer to `form_field`"}
        ask = "Which value should be entered in the job application text box `form_field`?"
    instructions = {"form_field": field}
    if row.section and nv(row.section) != nv(field):
        # "Start Date (Month)" under Work Experience is the job's start, not the
        # applicant's availability (Jev picked November 2026 for it on a Taleo form)
        instructions["form_section"] = row.section
    instructions["question"] = ask + (" The field sits under the form heading `form_section`." if "form_section" in instructions else "") \
        + " Use only facts stated in `applicant`; choose none rather than guess."
    # Jev reads "choose none unless stated" literally: on a Lever form it answered
    # "What best describes your experience operating Databricks?" with none instead of
    # "No meaningful Databricks production experience." The profile is the whole resume,
    # so experience and employers are a closed world; personal facts (DACA, a license,
    # convictions) are not, and none stays the answer there.
    if EXPERIENCE_RE.search(field):
        instructions["resume_is_complete"] = (
            "`applicant` lists all of the applicant's jobs, education and skills. Experience, tools, skills or "
            "degrees it does not show are ones the applicant does not have: choose the answer that says so.")
    if EMPLOYER_RE.search(field):
        instructions["employers_are_complete"] = (
            "`applicant.jobs` lists every employer the applicant has ever had. An organisation not listed there is one "
            "the applicant has never worked for, as an employee or a contractor: choose the answer that says so.")
    return {"type": "choice", "criteria": criteria, "instructions": instructions}


def _claims(row: Row, text: str) -> bool:
    """A Yes to a qualification or an immigration question: a claim that needs more
    confidence than an ordinary pick."""
    label = row.label or ""
    if IMMIGRATION_RE.search(label):
        return True
    return bool(ATTESTATION_RE.search(label)) and nv(text).startswith("yes")


class JevResolver:
    def __init__(
        self,
        system_one: SystemOne,
        profile_doc: dict,
        *,
        writer=None,
        saved: SavedAnswers | None = None,
        min_confidence: float = 0.5,
        claim_confidence: float = 0.8,
        today: date | None = None,
    ):
        self.system_one = system_one
        self.profile_doc = profile_doc
        self.writer = writer
        self.saved = saved
        self.min_confidence = min_confidence
        self.claim_confidence = claim_confidence
        self.today = today
        self.calls = 0
        self.log: list[dict] = []  # every Jev pick, used or not: label, choice, confidence, used

    async def resolve(self, rows: list[Row], context: dict) -> dict[int, dict]:
        out: dict[int, dict] = {}
        written: list[Row] = []
        ask: dict[str, tuple[Row, list[str] | None]] = {}
        cands = candidates(self.profile_doc)
        for row in rows:
            if self.saved:
                stored = self.saved.get(row, per_job=is_per_job(row))
                answer = answer_from_text(row, (stored or {}).get("answer"))
                if answer:
                    out[row.index] = answer | {"source": "saved"}
                    continue
            options = _option_texts(row)
            if not options and (row.widget in WRITTEN or is_per_job(row)):
                written.append(row)  # Jev does not write prose
                continue
            ask[f"f{row.index}"] = (row, options or None)
        if ask:
            self.calls += 1
            questions = {qid: _question(row, opts, cands) for qid, (row, opts) in ask.items()}
            try:
                answers = await self.system_one(applicant_state(self.profile_doc, self.today), questions)
            except Exception as exc:
                # an unreachable model is a gap, not a crash (same as llm.py)
                context["resolver_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
                answers = {}
            for qid, (row, _) in ask.items():
                a = answers.get(qid) or {}
                choice, conf = a.get("choice"), float(a.get("confidence") or 0.0)
                floor = self.claim_confidence if choice and _claims(row, choice) else self.min_confidence
                answer = answer_from_text(row, choice) if choice and choice != NONE and conf >= floor else None
                self.log.append({"label": (row.label or "")[:80], "choice": choice, "confidence": round(conf, 3),
                                 "used": bool(answer)})
                if answer:
                    out[row.index] = answer | {"source": "jev"}
        if written and self.writer:
            out.update(await self.writer.resolve(written, context))
        return out

    async def learned(self, row: Row, answer: dict, result: FillResult, context: dict) -> None:
        if answer.get("source") != "jev":
            if self.writer:  # the writer's own answers (why us, cover letters)
                await self.writer.learned(row, answer, result, context)
            return
        if not (self.saved and result.ok) or ATTESTATION_RE.search(row.label or ""):
            return  # qualification answers are re-derived from the profile every time
        text = result.wrote if answer["kind"] == "option" and result.wrote else answer["value"]
        if isinstance(text, bool):
            text = "Yes" if text else "No"
        self.saved.put(row, str(text), per_job=is_per_job(row), source="llm")

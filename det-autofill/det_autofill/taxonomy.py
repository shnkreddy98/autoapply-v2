"""Question taxonomy: label patterns -> canonical answer key.

One RULE row per question the deterministic filler can answer. Patterns are
regexes matched against a normalized label (lowercased, required-marks and
widget chrome stripped). First match wins, so put the specific before the
general (e.g. "confirm email" before "email").

Seeded from mining 129 real traces across ~25 ATS platforms (Greenhouse,
Workday, Ashby, Dayforce, ADP, Taleo, Oracle, JazzHR, BambooHR, Lever, ...).
Adding a new question = adding one row. Keys map into answers.AnswerStore.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from det_autofill.aria import FormField


@dataclass(frozen=True)
class Rule:
    key: str  # canonical answer key (answers.py)
    category: str
    patterns: tuple[str, ...]  # any-match regexes vs normalized label


def normalize(label: str) -> str:
    l = label.lower()
    l = re.sub(r"[*✱]", " ", l)
    # widget chrome that ATSes cram into accessible names
    l = re.sub(r"\b(select one|required|optional|type here|search|toggle flyout)\b", " ", l)
    l = re.sub(r"[^a-z0-9/+#' -]", " ", l)
    return re.sub(r"\s+", " ", l).strip()


RULES: list[Rule] = [
    # --- traps (must outrank everything) --------------------------------------
    # Workday anti-bot honeypot: "Enter website. This input is for robots only,
    # do not enter if you're human." Filling it flags the session.
    Rule("honeypot", "skip", (r"for robots only", r"do not (enter|fill)")),
    # Workday multiselect chips ("1 items selected" listbox) — already-committed
    # values; touching them deletes chips
    Rule("selected_chips", "skip", (r"^\d* ?items? selected$",)),
    # captcha / one-time codes go to a human or the email tool. Up here because
    # "Enter the verification code sent to your email" also says "email".
    Rule("handoff", "handoff", (
        r"i'?m not a robot|captcha|recaptcha",
        r"verification code|security code|one.?time (code|passcode)|\botp\b",
    )),
    # --- identity -----------------------------------------------------------
    Rule("first_name", "identity", (r"^(legal )?first name", r"\bgiven name\b", r"^preferred first name")),
    Rule("last_name", "identity", (r"^(legal )?last name", r"\bfamily name\b", r"\bsurname\b")),
    Rule("middle_name", "identity", (r"middle (name|initial)",)),
    Rule("full_name", "identity", (r"^(full |legal |your )?name$", r"full legal name", r"full name")),
    Rule("preferred_name", "identity", (r"preferred name", r"nickname", r"what should we call you")),
    Rule("pronouns", "identity", (r"pronoun",)),
    Rule("suffix", "identity", (r"^suffix$",)),
    Rule("prefix", "identity", (r"^prefix$", r"^salutation$", r"^title (mr|ms)")),
    # --- contact ------------------------------------------------------------
    Rule("email_confirm", "contact", (r"(confirm|verify|re.?enter|retype).{0,10}e.?mail",)),
    Rule("email", "contact", (r"e.?mail",)),
    Rule("phone_country_code", "contact", (r"country (phone )?code", r"phone.{0,10}country")),
    Rule("phone_device_type", "contact", (r"(phone|number) (device )?type", r"primary number type", r"device type")),
    Rule("phone_extension", "contact", (r"extension",)),
    Rule("phone", "contact", (r"phone", r"^mobile( number)?$", r"primary number", r"cell")),
    # --- address ------------------------------------------------------------
    Rule("street_address", "address", (r"address line ?1", r"street address", r"^address( line)?( 1)?$", r"^street$")),
    Rule("street_address2", "address", (r"address (line )?2", r"\bapt\b|\bsuite\b|\bunit\b")),
    Rule("city", "address", (r"^city$", r"\bcity\b", r"^town$")),
    Rule("state", "address", (r"^state", r"state/?province", r"^province$", r"^region$")),
    Rule("zip_code", "address", (r"zip|postal",)),
    Rule("county", "address", (r"^county$",)),
    Rule("country", "address", (r"^country( of residence)?$", r"\bcountry\b")),
    Rule("location_city", "address", (r"location \(?city\)?", r"^(current )?location$", r"where (are you|do you) (located|live|based)")),
    # --- links --------------------------------------------------------------
    Rule("linkedin", "links", (r"linked.?in",)),
    Rule("github", "links", (r"git.?hub",)),
    Rule("website", "links", (r"website|portfolio|personal site", r"\burl\b(?!.*linkedin)")),
    Rule("twitter", "links", (r"twitter|x profile",)),
    # --- resume / documents ---------------------------------------------------
    Rule("resume_upload", "resume", (r"resume|\bcv\b", r"^(attach|upload|select|choose|drop) (a )?(file|resume)", r"^upload$", r"^attach$")),
    Rule("cover_letter", "resume", (r"cover letter",)),
    # --- account / login ------------------------------------------------------
    Rule("password_confirm", "account", (r"(confirm|verify|re.?enter|retype).{0,10}password",)),
    Rule("password", "account", (r"password",)),
    Rule("username", "account", (r"^user ?name$",)),
    # --- work authorization ---------------------------------------------------
    # Must outrank visa_sponsorship: "authorized to work WITHOUT sponsorship?" says
    # "sponsor" but a Yes means no sponsorship is needed — the opposite answer.
    Rule("authorized_without_sponsorship", "auth", (
        r"(authoriz\w*|eligible|able|permitted|allowed).{0,60}work.{0,80}without.{0,30}(sponsor|visa)",
        r"without.{0,30}(sponsor\w*|visa).{0,60}(authoriz|eligible|work)",
    )),
    Rule("visa_sponsorship", "auth", (
        r"sponsor",  # "require sponsorship", "visa sponsorship", "now or in the future"
        r"require.{0,20}visa",
    )),
    # normalize() turns "U.S." into "u s", so the dots are optional spaces here
    Rule("us_citizen", "auth", (r"\bu ?s ?citizen", r"citizen of the united states")),
    # enumerated status question ("Which work authorization best describes
    # you: citizen / green card / H-1B / ...") — must outrank the yes/no rule
    Rule("work_auth_type", "auth", (
        r"(which|what).{0,25}(work )?authorization.{0,20}(best )?describes",
        r"(which|what).{0,40}(best )?describes.{0,30}(work|employment) authorization",
        r"(work )?authorization (status|type|category)",
    )),
    Rule("work_authorized", "auth", (
        r"(legally )?authoriz.{0,30}work", r"eligible to work", r"right to work",
        r"work eligib", r"immigration status", r"work authorization",
    )),
    # --- EEO / demographics ---------------------------------------------------
    Rule("eeo_hispanic", "eeo", (r"hispanic|latino|latinx",)),
    Rule("eeo_race", "eeo", (r"\brace\b|ethnic",)),
    Rule("eeo_gender_identity", "eeo", (r"gender identity|transgender|lgbtq|sexual orientation",)),
    Rule("eeo_gender", "eeo", (r"^gender$", r"\bgender\b", r"^sex$")),
    Rule("eeo_veteran", "eeo", (r"veteran",)),
    Rule("eeo_disability", "eeo", (r"disability|disabled",)),
    # --- age ------------------------------------------------------------------
    Rule("age_over_18", "age", (r"(18|eighteen).{0,25}(age|older|over|above)", r"(over|above|at least|under).{0,15}(age )?(of )?18", r"legal (working )?age")),
    # --- experience / skills --------------------------------------------------
    Rule("skill_years", "experience", (
        r"(how many )?years.{0,40}experience.{0,60}(in|with|using)",
        r"experience.{0,30}(python|sql|java|spark|databricks|aws|azure|gcp|etl|tableau|airflow|kafka|snowflake)",
        r"minimum of \d+ years",
    )),
    Rule("years_experience", "experience", (r"years of (relevant |professional |work )?experience", r"\byoe\b", r"total.{0,15}experience")),
    Rule("skills_list", "experience", (r"(primary|secondary|top|add) skills", r"^skills?$", r"type to add skills")),
    # --- education ------------------------------------------------------------
    Rule("education_level", "education", (r"(highest )?(level of )?education( level| completed)?$", r"highest degree")),
    Rule("degree", "education", (r"^degree", r"\bdegree\b")),
    Rule("school", "education", (r"school|university|college|institution",)),
    Rule("field_of_study", "education", (r"field of study|major|discipline|area of study",)),
    Rule("gpa", "education", (r"\bgpa\b|grade point",)),
    Rule("education_dates", "education", (r"graduat",)),
    # --- employment history (most recent row only; resume carries the rest) ----
    Rule("current_title", "employment", (r"(current |most recent |job )?title$", r"current (role|position)")),
    Rule("current_company", "employment", (r"(current |most recent )?(company|employer)( name)?$",)),
    Rule("job_duties", "employment", (r"role description|duties|responsibilit|^description$",)),
    Rule("job_from_date", "employment", (r"^from$", r"start (date )?\(work", r"date started")),
    Rule("job_to_date", "employment", (r"^to$", r"end date", r"date ended")),
    Rule("currently_work_here", "employment", (r"i currently work here", r"current(ly)? (position|job)\?")),
    Rule("referred_by_name", "employment", (r"who referred you", r"referr(er|al) name")),
    Rule("non_compete", "employment", (r"non.?comp|non.?solicit",)),
    Rule("currently_employed", "employment", (r"currently (employed|working)",)),
    Rule("may_contact_employer", "employment", (r"may we contact",)),
    Rule("reason_for_leaving", "employment", (r"reason for leaving",)),
    Rule("worked_here_before", "employment", (
        r"(previously|ever|before).{0,30}(worked|employed)", r"former (employee|associate)",
        r"(currently|have you).{0,20}employed (by|with|at)",
        r"worked (for|at|with|here).{0,40}before",
    )),
    Rule("referred_by_employee", "employment", (r"referred (to this position )?by", r"know anyone (at|who works)", r"friends or family.{0,20}working")),
    Rule("relatives_employed", "employment", (r"relative|family member",)),
    # --- compensation -----------------------------------------------------------
    Rule("desired_salary", "compensation", (r"salary|compensation|desired pay|pay (rate|expectation)|expected pay|wage",)),
    # --- availability ------------------------------------------------------------
    # Month/year halves of a start or end date belong to an education or employment
    # row, not to availability ("Start date month" under Education on Greenhouse).
    # Left unanswered until history rows are filled from the parsed resume.
    Rule("history_date_part", "history", (r"^(start|end) date (month|year)$", r"^(from|to) (month|year)$")),
    Rule("start_date", "availability", (r"start date|when can you start|available to (start|begin)|earliest.{0,15}start",)),
    Rule("notice_period", "availability", (r"notice period",)),
    Rule("weekends_evenings", "availability", (r"weekend|evening|holiday|night shift|overtime",)),
    Rule("employment_type", "availability", (r"full.?time|part.?time|employment type",)),
    Rule("hours_availability", "availability", (r"hours (per week|of availability)|core working hours|shift",)),
    # --- relocation / travel / onsite ---------------------------------------------
    Rule("willing_relocate", "relocation", (r"relocat",)),
    Rule("willing_travel", "relocation", (r"travel",)),
    Rule("onsite_ok", "relocation", (r"on.?site|in.?office|commute|hybrid|work from (the )?office|remote",)),
    # --- screening / legal -----------------------------------------------------
    Rule("felony", "screening", (r"felony|misdemeanor|convict|criminal",)),
    Rule("background_check_ok", "screening", (r"background (check|investigation)",)),
    Rule("drug_test_ok", "screening", (r"drug (test|screen)",)),
    Rule("ever_terminated", "screening", (r"terminated|dismissed|fired",)),
    Rule("security_clearance", "screening", (r"security clearance|government clearance|export control|itar",)),
    Rule("military", "screening", (r"military (spouse|service|info)", r"armed forces")),
    # --- referral / source ------------------------------------------------------
    Rule("how_heard", "source", (r"how did you (hear|find|first hear)", r"^source( type)?$", r"hear about (us|this)")),
    Rule("contact_method", "source", (r"preferred contact method",)),
    # --- consent / signature ------------------------------------------------------
    Rule("agree_terms", "consent", (
        r"i (agree|accept|consent|certify|acknowledge|attest|have read)",
        r"terms (of use|and conditions)", r"privacy (policy|notice)", r"gdpr",
        r"^(accept|agree|consent)$", r"acknowledg", r"sms|text message",
    )),
    Rule("signature", "consent", (r"signature",)),
    Rule("signature_date", "consent", (r"today'?s date|date signed|^date$",)),
    # --- date widget parts ---------------------------------------------------------
    Rule("date_month", "date", (r"^month$", r"^mm$")),
    Rule("date_day", "date", (r"^day$", r"^dd$")),
    Rule("date_year", "date", (r"^year$", r"^yyyy$")),
    # --- languages -------------------------------------------------------------------
    Rule("languages", "misc", (r"languages? (do you )?(speak|preference)|language preference",)),
    # --- optional / leave blank -------------------------------------------------------
    Rule("leave_blank", "skip", (
        r"employee id", r"if applicable", r"^number$", r"active licenses?",
        r"which licenses", r"clearance (number|level)",
    )),
    # --- skill checkboxes ("Python", "SQL", ... as standalone check items) --------------
    Rule("skill_checkbox", "experience", (
        r"^(python|sql|java|scala|spark|apache spark|databricks|aws|azure|gcp|"
        r"snowflake|airflow|kafka|tableau|power ?bi|dbt|etl|hadoop|redshift|"
        r"bigquery|rest apis?|docker|kubernetes|terraform|git|linux|r|c\+\+|excel)$",
    )),
]

_COMPILED = [(r, [re.compile(p) for p in r.patterns]) for r in RULES]


def match(field: FormField) -> Rule | None:
    """First rule whose pattern hits the field's label (falling back to its
    surrounding question text)."""
    for text in (field.label, field.context):
        norm = normalize(text or "")
        if not norm:
            continue
        for rule, pats in _COMPILED:
            if any(p.search(norm) for p in pats):
                return rule
    return None

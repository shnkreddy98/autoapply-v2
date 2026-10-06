"""Classify extracted form controls and plan what to write — pure, no browser.

Input: descriptors from extract.js (one dict per control, with the DOM attributes
the ARIA snapshot cannot see). Output: a Plan whose rows say, for every control,
which answer key it is, which rule at which tier decided that, and either the
answer to write or the reason nothing will be written.

Tiers, first hit wins (Autofill's order, which is Chromium's autofill precedence,
with det-autofill's trace-mined taxonomy folded into tier 3):

  gate  honeypot / captcha / OTP / hidden / disabled / read-only    -> never filled
  0     per-ATS attribute tables (Greenhouse, Lever, Ashby, Workable, Workday)
  1     the `autocomplete` attribute
  2     exact name / id / data-automation-id tokens
  3a    polarity rules ("authorized to work WITHOUT sponsorship")
  3b    det-autofill taxonomy over label, group label           (real traces)
  3c    Autofill regex rules over the whole haystack             (label+attrs)
  4     input type (email / tel)

Below `min_confidence` nothing is written. Identity/contact/address matches inside a
"Reference" or "Emergency contact" section are refused: that name is not yours.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

from det_autofill import taxonomy
from det_autofill.answers import AnswerStore
from det_autofill.aria import FormField
from det_autofill.options import hispanic_only


def norm(s: object) -> str:
    """Autofill's `norm`: punctuation to spaces, lowercase, collapsed."""
    s = re.sub(r"[ ​‎‏]", " ", str(s or ""))
    s = re.sub(r"[_\-./\\\[\]()*:,?]+", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


# ---------------------------------------------------------------------------
# key vocabulary: Autofill kinds -> det-autofill answer keys

KIND_TO_KEY = {
    "firstName": "first_name", "lastName": "last_name", "middleName": "middle_name",
    "preferredName": "preferred_name", "fullName": "full_name", "pronouns": "pronouns",
    "email": "email", "phone": "phone", "phoneCountry": "phone_country_code",
    "phoneType": "phone_device_type", "addressLine1": "street_address",
    "addressLine2": "street_address2", "city": "city", "region": "state",
    "postalCode": "zip_code", "country": "country", "locationCombined": "location_city",
    "linkedin": "linkedin", "github": "github", "portfolio": "website", "twitter": "twitter",
    "otherUrl": "website", "currentCompany": "current_company", "currentTitle": "current_title",
    "yearsExperience": "years_experience", "school": "school", "degree": "degree",
    "fieldOfStudy": "field_of_study", "gpa": "gpa", "gradMonth": "education_dates",
    "gradYear": "education_dates", "workAuthorized": "work_authorized",
    "requiresSponsorship": "visa_sponsorship", "willingToRelocate": "willing_relocate",
    "remotePreference": "onsite_ok", "desiredSalary": "desired_salary",
    "noticePeriod": "notice_period", "availableStartDate": "start_date",
    "over18": "age_over_18", "previousEmployee": "worked_here_before",
    "resume": "resume_upload", "coverLetter": "cover_letter", "gender": "eeo_gender",
    "hispanicLatino": "eeo_hispanic", "race": "eeo_race", "veteranStatus": "eeo_veteran",
    "disabilityStatus": "eeo_disability", "sexualOrientation": "eeo_gender_identity",
    "transgender": "eeo_gender_identity", "howDidYouHear": "how_heard",
    "referralName": "referred_by_name",
}

# Keys whose value is the applicant's own identity: never written into a reference's
# or emergency contact's block.
POISONABLE = {
    "first_name", "last_name", "middle_name", "preferred_name", "full_name", "email",
    "email_confirm", "phone", "phone_country_code", "phone_device_type", "phone_extension",
    "street_address", "street_address2", "city", "state", "zip_code", "county",
    "country", "location_city", "current_company", "current_title",
}
POISON_RE = re.compile(
    r"\b(emergency contact|references?|reference [123]|spouse|next of kin|guardian|parent|"
    r"supervisor name|manager name|referee)\b"
)
HONEYPOT_RE = re.compile(
    r"\b(honeypot|honey pot|bot field|leave (this )?blank|do not fill|for robots only|"
    r"winnie the pooh|url2|comments2)\b"
)


# ---------------------------------------------------------------------------
# tier 0: per-ATS attribute tables (from Autofill's verified adapters)

ADAPTERS: list[tuple[str, re.Pattern, list[tuple[str, str, str]]]] = [
    # (ats, host pattern, [(attribute, exact value, kind)])
    ("greenhouse", re.compile(r"(^|\.)greenhouse\.io$"), [
        ("name", "first_name", "firstName"), ("id", "first_name", "firstName"),
        ("name", "last_name", "lastName"), ("id", "last_name", "lastName"),
        ("name", "email", "email"), ("id", "email", "email"),
        ("name", "phone", "phone"), ("id", "phone", "phone"),
        ("name", "job_application[location]", "locationCombined"), ("id", "location", "locationCombined"),
        ("id", "resume", "resume"), ("id", "cover_letter_text", "coverLetter"),
    ]),
    ("lever", re.compile(r"(^|\.)lever\.co$"), [
        ("name", "name", "fullName"), ("name", "email", "email"), ("name", "phone", "phone"),
        ("name", "org", "currentCompany"), ("name", "location", "locationCombined"),
        ("name", "urls[LinkedIn]", "linkedin"), ("name", "urls[GitHub]", "github"),
        ("name", "urls[Portfolio]", "portfolio"), ("name", "urls[Other]", "otherUrl"),
        ("name", "resume", "resume"), ("name", "comments", "coverLetter"),
    ]),
    ("ashby", re.compile(r"(^|\.)ashbyhq\.com$"), [
        ("name", "_systemfield_name", "fullName"), ("id", "_systemfield_name", "fullName"),
        ("name", "_systemfield_email", "email"), ("id", "_systemfield_email", "email"),
        ("name", "_systemfield_phone", "phone"), ("id", "_systemfield_phone", "phone"),
        ("name", "_systemfield_resume", "resume"), ("id", "_systemfield_resume", "resume"),
    ]),
    ("workable", re.compile(r"(^|\.)workable\.com$"), [
        ("name", "firstname", "firstName"), ("name", "lastname", "lastName"),
        ("name", "email", "email"), ("name", "phone", "phone"),
        ("name", "address", "locationCombined"), ("name", "city", "city"),
        ("name", "postcode", "postalCode"), ("name", "country", "country"),
        ("name", "cover_letter", "coverLetter"),
    ]),
]

# Workday automation ids vary per tenant: substring match, longest token first.
WORKDAY_IDS = [
    ("legalnamesection firstname", "firstName"), ("legalnamesection lastname", "lastName"),
    ("addresssection addressline1", "addressLine1"), ("addresssection addressline2", "addressLine2"),
    ("addresssection city", "city"), ("addresssection countryregion", "region"),
    ("addresssection postalcode", "postalCode"), ("country phone code", "phoneCountry"),
    ("phone device type", "phoneType"), ("phone number", "phone"),
    ("preferredname", "preferredName"), ("addressline1", "addressLine1"),
    ("addressline2", "addressLine2"), ("postalcode", "postalCode"),
    ("countrydropdown", "country"), ("field of study", "fieldOfStudy"),
    ("schoolitem", "school"), ("firstname", "firstName"), ("lastname", "lastName"),
    ("zipcode", "postalCode"), ("province", "region"), ("sourceprompt", "howDidYouHear"),
    ("previousworker", "previousEmployee"), ("email", "email"), ("phone", "phone"),
    ("mobile", "phone"), ("city", "city"), ("state", "region"), ("region", "region"),
    ("country", "country"), ("degree", "degree"), ("source", "howDidYouHear"),
]
def _id_has_word(automation_id: str, token: str) -> bool:
    """`token` inside an automation id, starting where one of its words does
    (camelCase, `_`, `-`): "addressSection_city" has "city", "ethnicityDropdown"
    does not — a plain substring test keyed Workday's Ethnicity prompt as the city."""
    words = norm(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", automation_id)).split()
    flat, want = "".join(words), token.replace(" ", "")
    starts = [sum(len(w) for w in words[:k]) for k in range(len(words))]
    return any(flat.startswith(want, i) for i in starts)


_WORKDAY_HOST = re.compile(r"(^|\.)(myworkdayjobs|myworkdaysite|myworkday)\.com$")


def detect_ats(url: str, fields: list[dict]) -> str:
    host = urlparse(url or "").hostname or ""
    for ats, host_re, _ in ADAPTERS:
        if host_re.search(host):
            return ats
    if _WORKDAY_HOST.search(host) or any(f.get("automation_id") for f in fields):
        return "workday"
    return "generic"


# ---------------------------------------------------------------------------
# tier 1 / 2 / 3c / 4 tables (Autofill)

AUTOCOMPLETE_MAP = {
    "name": "fullName", "given-name": "firstName", "additional-name": "middleName",
    "family-name": "lastName", "nickname": "preferredName", "email": "email",
    "tel": "phone", "tel-national": "phone", "tel-country-code": "phoneCountry",
    "organization": "currentCompany", "organization-title": "currentTitle",
    "street-address": "addressLine1", "address-line1": "addressLine1",
    "address-line2": "addressLine2", "address-level2": "city", "address-level1": "region",
    "postal-code": "postalCode", "country": "country", "country-name": "country",
    "url": "portfolio", "sex": "gender",
}
_AC_PREFIX = re.compile(r"^(shipping|billing|home|work|mobile|fax|pager)$")

TOKEN_MAP = {
    "first name": "firstName", "firstname": "firstName", "given name": "firstName",
    "last name": "lastName", "lastname": "lastName", "family name": "lastName",
    "middle name": "middleName", "middlename": "middleName",
    "preferred name": "preferredName", "preferredname": "preferredName",
    "full name": "fullName", "fullname": "fullName", "name": "fullName",
    "email": "email", "email address": "email", "e mail": "email",
    "phone": "phone", "phone number": "phone", "telephone": "phone", "mobile": "phone",
    "job application first name": "firstName", "job application last name": "lastName",
    "job application email": "email", "job application phone": "phone",
    "urls linkedin": "linkedin", "urls github": "github", "urls portfolio": "portfolio",
    "urls other": "otherUrl", "org": "currentCompany",
    "addressline1": "addressLine1", "address line 1": "addressLine1",
    "addressline2": "addressLine2", "address line 2": "addressLine2",
    "city": "city", "postalcode": "postalCode", "postal code": "postalCode",
    "zip": "postalCode", "zipcode": "postalCode", "zip code": "postalCode",
    "state": "region", "province": "region", "region": "region",
    "countrydropdown": "country", "country": "country",
    "countryphonecode": "phoneCountry", "country phone code": "phoneCountry",
    "phone device type": "phoneType", "formfield country": "country",
    "postcode": "postalCode", "address": "addressLine1",
    "cover letter": "coverLetter", "coverletter": "coverLetter",
    "systemfield name": "fullName", "systemfield email": "email",
    "systemfield phone": "phone", "systemfield resume": "resume",
    "linkedin": "linkedin", "linkedin url": "linkedin", "linkedin profile": "linkedin",
    "github": "github", "github url": "github", "portfolio": "portfolio",
    "website": "portfolio", "personal website": "portfolio", "twitter": "twitter",
    "school": "school", "university": "school", "college": "school", "degree": "degree",
    "discipline": "fieldOfStudy", "field of study": "fieldOfStudy", "major": "fieldOfStudy",
    "gpa": "gpa", "company": "currentCompany", "current company": "currentCompany",
    "employer": "currentCompany", "title": "currentTitle", "job title": "currentTitle",
    "current title": "currentTitle", "source": "howDidYouHear",
    "how did you hear about us": "howDidYouHear", "referral": "referralName",
}

# Ordered: the first match wins, and the order is the specification.
REGEX_RULES: list[tuple[str, str, float]] = [
    ("transgender", r"\btransgender\b", 0.85),
    ("sexualOrientation", r"\bsexual orientation\b", 0.85),
    ("hispanicLatino", r"\bhispanic\b|\blatino\b|\blatina\b|\blatinx\b", 0.85),
    ("veteranStatus", r"\b(veteran|military service|armed forces|protected veteran)\b", 0.85),
    ("disabilityStatus", r"\bdisabilit(y|ies)\b|\bdisabled\b|\bcc 305\b|\bsection 503\b", 0.85),
    ("race", r"\brace\b|\bracial\b|\bethnicit(y|ies)\b|\bethnic (group|background)\b", 0.8),
    ("pronouns", r"\bpronouns?\b", 0.8),
    ("gender", r"\bgender\b|\bsex\b", 0.75),
    ("requiresSponsorship", r"\b(require|need|request|will you (now|in the future))\b.*\b(sponsor|sponsorship|visa)\b", 0.85),
    ("workAuthorized", r"\b(authoriz(ed|ation) to work|legally (authorized|allowed|entitled) to work|work authorization|eligible to work|right to work)\b", 0.85),
    ("willingToRelocate", r"\b(willing to relocate|open to relocation|relocat(e|ion))\b", 0.8),
    ("remotePreference", r"\b(remote|hybrid|on ?site|work (location )?preference)\b", 0.7),
    ("desiredSalary", r"\b(desired|expected|target|requested)\b.*\b(salary|compensation|pay|rate)\b|\bsalary (expectation|requirement|range|expectations|requirements)\b|\bcompensation expectation\b", 0.8),
    ("noticePeriod", r"\bnotice period\b|\bhow (much|soon)\b.*\bnotice\b", 0.8),
    ("availableStartDate", r"\b(start date|available to start|earliest (start|availability)|availability date)\b", 0.8),
    ("over18", r"\b(18 years|over 18|at least 18|age of 18)\b", 0.8),
    ("previousEmployee", r"\b(previously (worked|employed)|former employee|previous worker|worked (here|for us) before)\b", 0.8),
    ("coverLetter", r"\bcover letter\b|\bmotivation letter\b", 0.9),
    ("resume", r"\b(resume|resumé|cv|curriculum vitae)\b", 0.9),
    ("linkedin", r"\blinked ?in\b", 0.92),
    ("github", r"\bgit ?hub\b", 0.92),
    ("twitter", r"\btwitter\b|\bx com\b", 0.8),
    ("portfolio", r"\b(portfolio|personal (site|website|page)|website|homepage|web site)\b", 0.8),
    ("gradYear", r"\b(grad(uation)?|completion|expected)\b.*\b(year|date)\b|\byear of graduation\b", 0.8),
    ("fieldOfStudy", r"\b(field of study|major|discipline|concentration|area of study|course of study)\b", 0.85),
    ("degree", r"\bdegree\b|\bqualification\b|\blevel of education\b", 0.8),
    ("school", r"\b(school|university|college|institution|alma mater)\b", 0.8),
    ("gpa", r"\bgpa\b|\bgrade point average\b", 0.85),
    ("yearsExperience", r"\byears? of (relevant )?experience\b|\bhow many years\b", 0.8),
    ("currentTitle", r"\b(current|most recent|present)\b.*\b(title|position|role)\b|\bjob title\b|\bcurrent role\b", 0.8),
    ("currentCompany", r"\b(current|most recent|present)\b.*\b(company|employer|organization)\b|\bcurrent employer\b|\bcompany name\b", 0.8),
    ("preferredName", r"\b(preferred (first )?name|nickname|goes by|name you go by|known as)\b", 0.85),
    ("middleName", r"\bmiddle (name|initial)\b", 0.85),
    ("firstName", r"\b(first name|given name|forename|legal first name)\b", 0.9),
    ("lastName", r"\b(last name|family name|surname|legal last name)\b", 0.9),
    ("fullName", r"\b(full name|legal name|your name|candidate name|applicant name)\b", 0.8),
    ("email", r"\b(e ?mail|email address)\b", 0.9),
    ("phoneCountry", r"\b(country (phone )?code|dial(ing)? code|phone country)\b", 0.85),
    ("phoneType", r"\b(phone (device )?type)\b", 0.85),
    ("phone", r"\b(phone|telephone|mobile|cell|contact number)\b", 0.85),
    ("locationCombined", r"\bcity\b.*\b(state|province|region)\b|\b(state|province|region)\b.*\bcity\b|\b(current )?location\b", 0.75),
    ("addressLine2", r"\b(address (line )?2|apt|apartment|suite|unit|floor)\b", 0.85),
    ("addressLine1", r"\b(street( address)?|address (line )?1|mailing address|address)\b", 0.8),
    ("city", r"\b(city|town|locality)\b", 0.85),
    ("region", r"\b(state|province|region|county|prefecture)\b", 0.8),
    ("postalCode", r"\b(zip( code)?|postal code|post ?code)\b", 0.9),
    ("country", r"\bcountry\b", 0.85),
    ("referralName", r"\b(referred by|referral name|who referred you|employee referral)\b", 0.8),
    ("howDidYouHear", r"\b(how did you hear|how you heard|source|where did you (find|hear))\b", 0.8),
]
_REGEX = [(kind, re.compile(rx), c) for kind, rx, c in REGEX_RULES]

# 3a: questions whose keywords point at one key but whose polarity flips the answer.
# "Authorized to work without sponsorship?" contains "sponsorship", yet a Yes means
# "no sponsorship needed" — answering it from visa_sponsorship inverts the truth.
POLARITY_RULES: list[tuple[str, re.Pattern]] = [
    ("authorized_without_sponsorship", re.compile(
        r"\b(authoriz\w*|eligible|able|permitted|allowed)\b.{0,60}\bwork\b.{0,80}\bwithout\b.{0,30}\b(sponsor\w*|visa)\b"
        r"|\bwithout\b.{0,30}\b(sponsor\w*|visa)\b.{0,60}\b(authoriz\w*|eligible|work)\b"
    )),
]

# Widget families a key may drive. Anything else is a control mismatch.
CHOICE_ONLY = {
    "eeo_gender", "eeo_race", "eeo_hispanic", "eeo_veteran", "eeo_disability",
    "eeo_gender_identity", "visa_sponsorship", "work_authorized",
    "authorized_without_sponsorship", "age_over_18", "worked_here_before",
    "phone_device_type", "salary_currency", "salary_period",
}
FILE_KEYS = {"resume_upload"}
# a split date's Month / Year boxes repeat for every date on the page (From, To)
REPEATABLE = {"phone_country_code", "date_month", "date_day", "date_year"}
# Rippling asks "Will you now or in the future require sponsorship…?" as a free-text
# box: a yes/no question typed as "Yes"/"No". Only a question worded as one — "If you
# need sponsorship, which visa?" is not answered by "Yes".
YES_NO_AS_TEXT = {"visa_sponsorship", "work_authorized", "authorized_without_sponsorship", "age_over_18", "worked_here_before"}
YES_NO_QUESTION = re.compile(r"^(do|does|are|will|would|have|has|is|can|could)\b")


# A split date's group label ("From", "To", "Date") names which date it is; these are
# the keys it may resolve to.
DATE_KEYS = {"job_from_date", "job_to_date", "education_dates", "signature_date", "start_date"}
_EDUCATION = re.compile(r"educat|school|universit|college|degree")
_WORK = re.compile(r"experience|employ|work history|\bjobs?\b")
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def date_parts(value: object) -> dict[str, int]:
    """Year / month / day of a profile date, only the parts it states: parsed resumes
    hold "2022-03-01", "2022-03", "03/2022" or "March 2022"."""
    s = str(value or "").strip()
    m = re.fullmatch(r"(\d{4})-(\d{1,2})(?:-(\d{1,2}))?", s)
    if m:
        y, mo, d = m.groups()
        return {"year": int(y), "month": int(mo), **({"day": int(d)} if d else {})}
    m = re.fullmatch(r"(\d{1,2})/(?:(\d{1,2})/)?(\d{4})", s)
    if m:
        mo, d, y = m.groups()
        return {"year": int(y), "month": int(mo), **({"day": int(d)} if d else {})}
    m = re.fullmatch(r"([A-Za-z]{3})[a-z]*\.?,?\s+(\d{4})", s)
    if m and m.group(1).lower() in _MONTHS:
        return {"year": int(m.group(2)), "month": _MONTHS.index(m.group(1).lower()) + 1}
    if re.fullmatch(r"\d{4}", s):
        return {"year": int(s)}
    return {}


def _date_part_answer(store: AnswerStore, key: str, d: dict):
    """One box of a split date (Workday's Month / Year spinbuttons). The box only says
    which part it is; the group label and the section say which date. Before this the
    boxes were classified but had no answer, and Workday stopped at "From is required"."""
    rule = taxonomy.match(FormField(ref="", role="date", label=d.get("date_of") or ""))
    parent = rule.key if rule else None
    section = norm(d.get("section"))
    if _EDUCATION.search(section):
        # Education's "From" / "To" are the school's dates; only graduation is stored
        parent = "education_dates" if parent in ("job_to_date", "education_dates") else None
    elif parent == "start_date" and _WORK.search(section):
        parent = "job_from_date"  # a job's "Start Date", not availability
    if parent not in DATE_KEYS:
        return None
    answer = store.get(parent, d.get("date_of") or "")
    part = key.removeprefix("date_")
    n = date_parts(answer.value if answer else None).get(part)
    if n is None:
        return None
    return type(answer)(key, "date_part", str(n) if part == "year" else f"{n:02d}")


# ---------------------------------------------------------------------------
# plan

@dataclass
class Row:
    index: int
    selectors: list[str]
    widget: str
    label: str
    required: bool
    key: str | None = None
    tier: str | None = None
    rule: str | None = None
    confidence: float = 0.0
    action: str = "skip"  # fill | skip | handoff
    reason: str | None = None
    answer: dict | None = None  # {"kind", "value"} when action == fill
    options: list[dict] = field(default_factory=list)  # select options / radio choices
    group_label: str = ""
    section: str = ""


@dataclass
class Plan:
    url: str
    ats: str
    rows: list[Row]

    def summary(self) -> dict:
        out = {"fields": len(self.rows), "fill": 0, "skip": 0, "handoff": 0}
        for r in self.rows:
            out[r.action] += 1
        return out

    def fingerprint(self) -> str:
        """Stable hash of every decision: two plans of the same page+profile match."""
        key = [(r.index, r.selectors, r.widget, r.key, r.tier, r.rule, r.action, r.reason, r.answer) for r in self.rows]
        return hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {"url": self.url, "ats": self.ats, "summary": self.summary(),
                "fingerprint": self.fingerprint(), "rows": [asdict(r) for r in self.rows]}


def _hit(kind_or_key: str, tier: str, rule: str, confidence: float) -> tuple[str, str, str, float]:
    return (KIND_TO_KEY.get(kind_or_key, kind_or_key), tier, rule, confidence)


def label_of(d: dict) -> str:
    """The question a control asks: its group's, its own label, then the weaker
    aria-label and placeholder (Lever's inputs carry nothing else)."""
    return d.get("group_label") or d.get("label") or d.get("aria_label") or d.get("placeholder") or ""


def haystack(d: dict) -> str:
    return norm(" ".join(filter(None, [
        d.get("group_label"), d.get("label"), d.get("aria_label"), d.get("placeholder"),
        d.get("name"), d.get("id"), d.get("automation_id"),
    ])))


def _first_name_label(d: dict) -> bool:
    """An Ashby form that splits the name keeps the id _systemfield_name on its "First
    Name" box (CivicMarketplace, 2026-10-05): the full name was typed into it."""
    return bool(re.match(r"(legal |preferred )?(first|given) name", norm(label_of(d))))


def classify(d: dict, ats: str) -> tuple[str, str, str, float] | None:
    """(key, tier, rule, confidence) for one descriptor, or None."""
    hay = haystack(d)
    # 0 — per-ATS tables
    for name, _, table in ADAPTERS:
        if name != ats:
            continue
        for attr, value, kind in table:
            if kind == "fullName" and _first_name_label(d):
                continue
            if d.get(attr) == value:
                return _hit(kind, "0", f"{ats}:{attr}={value}", 0.99)
    if ats == "workday" and d.get("automation_id"):
        aid = norm(d["automation_id"])
        for token, kind in WORKDAY_IDS:
            if aid == token:
                return _hit(kind, "0", f"workday:{token}=exact", 0.99)
        for token, kind in WORKDAY_IDS:
            if _id_has_word(d["automation_id"], token):
                return _hit(kind, "0", f"workday:{token}~substring", 0.85)
    # 1 — autocomplete
    ac = d.get("autocomplete") or ""
    if ac not in ("", "on", "off"):
        for tok in ac.split():
            if tok.startswith("section-") or _AC_PREFIX.match(tok):
                continue
            if tok in AUTOCOMPLETE_MAP:
                return _hit(AUTOCOMPLETE_MAP[tok], "1", f"autocomplete={tok}", 0.95)
    # 2 — exact tokens
    for cand in dict.fromkeys(filter(None, [norm(d.get("name")), norm(d.get("id")), norm(d.get("automation_id"))])):
        if cand in TOKEN_MAP and not (TOKEN_MAP[cand] == "fullName" and _first_name_label(d)):
            return _hit(TOKEN_MAP[cand], "2", f"token:{cand}", 0.9)
    # a file input named for a resume is the resume, whatever text sits nearby
    # (Breezy: hidden <input type=file name="cResume"> beside "Apply Using LinkedIn")
    if d.get("widget") == "file" and re.search(r"resume|\bcv\b|curriculum", f"{d.get('name')} {d.get('id')}", re.I):
        return _hit("resume_upload", "2", "file:name~resume", 0.9)
    # 3a — polarity
    question = norm(label_of(d))
    for key, rx in POLARITY_RULES:
        if rx.search(question):
            return (key, "3a", f"polarity:{key}", 0.9)
    # 3b — det-autofill taxonomy (label, then its section as context)
    rule = taxonomy.match(FormField(ref="", role=d.get("widget", ""), label=label_of(d), context=d.get("section") or ""))
    if rule and rule.category not in ("skip", "handoff"):
        return (rule.key, "3b", f"taxonomy:{rule.key}", 0.8)
    # 3c — Autofill regex over the haystack
    for kind, rx, c in _REGEX:
        if rx.search(hay):
            return _hit(kind, "3c", f"regex:{kind}", c)
    # 4 — input type
    if d.get("type") == "email":
        return _hit("email", "4", "type=email", 0.7)
    if d.get("type") == "tel":
        return _hit("phone", "4", "type=tel", 0.7)
    return None


def _gate(d: dict) -> tuple[str, str] | None:
    """(action, reason) for controls that must not be filled at all."""
    hay = haystack(d)
    label_rule = taxonomy.match(
        FormField(ref="", role=d.get("widget", ""), label=label_of(d))
    )
    if HONEYPOT_RE.search(hay) or (label_rule and label_rule.key == "honeypot"):
        return ("skip", "honeypot")
    if d.get("aria_hidden"):
        return ("skip", "aria-hidden")
    if d.get("disabled"):
        return ("skip", "disabled")
    # readonly means nothing to a radio or checkbox (Eightfold marks all of its radios)
    if d.get("readonly") and d.get("widget") not in ("radio", "checkbox"):
        return ("skip", "readonly")
    # file inputs are routinely styled invisible behind an "Attach" button
    if not d.get("visible") and d.get("widget") != "file":
        return ("skip", "not-visible")
    # after visibility: a code field waiting on a later, still hidden step must not
    # hand off the page the applicant is on
    if label_rule and label_rule.category == "handoff":
        return ("handoff", "captcha-or-code")
    if label_rule and label_rule.key == "ai_disclosure":
        return ("skip", "for-the-applicant")
    return None


def _has_value(d: dict, members: list[dict]) -> bool:
    w = d.get("widget")
    if d.get("chips"):
        return True
    if w in ("radio", "checkbox"):
        return any(m.get("checked") for m in members)
    if w == "file":
        return bool(d.get("has_files"))
    if w == "select":
        opts = d.get("options") or []
        chosen = (d.get("value") or "").strip()
        return bool(chosen) and not re.match(r"^(select|choose|--|please select)", chosen.lower()) and \
            not (opts and nv_placeholder(chosen))
    if w == "combobox":
        shown = (d.get("display") or d.get("value") or "").strip()
        return bool(shown) and not re.match(r"^(select( one)?|choose|search|select\.\.\.)", shown.lower())
    return bool((d.get("value") or "").strip())


def nv_placeholder(text: str) -> bool:
    return bool(re.fullmatch(r"(select\W*|choose\W*|--+|please select\W*)?", text.strip().lower()))


def plan(
    descriptors: list[dict],
    store: AnswerStore,
    url: str = "",
    *,
    overwrite: bool = False,
    min_confidence: float = 0.7,
) -> Plan:
    """Decide every control's fate. Pure: descriptors and profile in, rows out."""
    ats = detect_ats(url, descriptors)
    groups: dict[str, list[dict]] = {}
    order: list[dict] = []
    for d in descriptors:
        gk = d.get("group_key")
        if d.get("widget") == "radio" and gk:
            if gk not in groups:
                groups[gk] = []
                order.append(d)
            groups[gk].append(d)
        else:
            order.append(d)

    rows: list[Row] = []
    claimed: dict[str, int] = {}
    for d in order:
        members = groups.get(d.get("group_key"), [d]) if d.get("widget") == "radio" else [d]
        row = Row(
            index=d["i"],
            selectors=[m["selector"] for m in members],
            widget=d.get("widget", ""),
            label=label_of(d)[:160],
            required=any(m.get("required") for m in members),
            group_label=d.get("group_label", ""),
            section=d.get("section", ""),
        )
        if d.get("widget") == "radio":
            row.options = [{"value": m.get("value") or "", "text": m.get("option_label", "")} for m in members]
        elif d.get("widget") == "select":
            row.options = d.get("options") or []
        rows.append(row)

        gated = _gate(d)
        if gated:
            row.action, row.reason = gated
            continue
        hit = classify(d, ats)
        if hit is None:
            # an unlabelled box that already holds a value (Rippling's phone country
            # "Search" combobox preset to "+1 US") needs nobody's answer
            row.reason = "already-filled" if not overwrite and _has_value(d, members) else "unclassified"
            continue
        row.key, row.tier, row.rule, row.confidence = hit
        if row.key == "eeo_race" and row.options and hispanic_only(row.options):
            row.key = "eeo_hispanic"  # "Ethnicity" asked as Hispanic / Not Hispanic
        if row.key == "desired_salary" and row.widget in ("select", "combobox"):
            # Breezy groups a currency select, the amount box and a pay-period select
            # under one "Desired Salary" heading; the first select took the key and
            # the amount box was then skipped as a duplicate
            texts = " ".join(norm(o.get("text", "")) for o in (d.get("options") or []))
            if re.search(r"dollar|euro|pound|\busd\b", texts):
                row.key = "salary_currency"
            elif re.search(r"\b(hourly|monthly|yearly|annual)\b", texts):
                row.key = "salary_period"
        if row.confidence < min_confidence:
            row.reason = "below-confidence-floor"
            continue
        if row.key in POISONABLE and POISON_RE.search(norm(f"{d.get('section')} {d.get('group_label')}")):
            row.reason = "poisoned-context"
            continue
        if row.key in CHOICE_ONLY and row.widget not in ("select", "radio", "checkbox", "combobox") and not (
                row.key in YES_NO_AS_TEXT and row.widget in ("text", "textarea") and YES_NO_QUESTION.match(norm(row.label))):
            row.reason = f"control-mismatch:{row.key}!={row.widget}"
            continue
        if (row.key in FILE_KEYS) != (row.widget == "file"):
            # Teamtailor's "Additional files" input (id candidate_file_remote_url) read
            # as a URL box and got typed into
            row.reason = f"control-mismatch:{row.key}!={row.widget}"
            continue
        if not overwrite and _has_value(d, members):
            row.reason = "already-filled"
            # still claims the key: otherwise a second "Website" box that was a
            # duplicate on the first pass gets filled on the next one
            claimed.setdefault(row.key, row.index)
            continue
        # every phone on a form has its own dial-code picker (Dayforce: home and mobile)
        if row.key in claimed and row.widget not in ("radio", "checkbox") and row.key not in REPEATABLE:
            row.reason = f"duplicate-key(first at #{claimed[row.key]})"
            continue
        answer = store.get(row.key, row.label)
        if answer is not None and answer.kind == "date_part":
            answer = _date_part_answer(store, row.key, d)
        if answer is None:
            row.reason = "no-answer"
            continue
        if row.key == "work_auth_type" and answer.kind == "option" and row.options and not any(
                re.search(pat, norm(o.get("text", "")), re.I) for pat in answer.value for o in row.options):
            # the stored pattern fits sentence options only (Ashby's "authorised ... but
            # will require sponsorship"); a status list (Citizen / H-1B / F-1 OPT) is the
            # resolver's, with the job description
            row.reason = "no-answer"
            continue
        if answer.kind in ("text", "date") and (answer.value is None or str(answer.value).strip() == ""):
            row.reason = "no-answer"  # an empty value is not an answer; typing it is a no-op at best
            continue
        if answer.kind == "skip":
            row.reason = "leave-blank"
            continue
        if answer.kind == "handoff":
            row.action, row.reason = "handoff", "needs-human"
            continue
        if answer.kind == "bool" and row.widget in ("text", "textarea"):
            answer = type(answer)(row.key, "text", "Yes" if answer.value else "No")
        if answer.kind == "option" and row.widget in ("text", "textarea"):
            # a preference list cannot be typed; a text box needs the literal answer
            text = store.text_for(row.key)
            if not text:
                row.reason = "no-text-answer-for-free-text-box"
                continue
            answer = type(answer)(row.key, "text", text)
        row.action = "fill"
        row.answer = {"kind": answer.kind, "value": answer.value}
        claimed[row.key] = row.index
    return Plan(url=url, ats=ats, rows=rows)


def to_table(p: Plan) -> str:
    lines = [f"{p.url}  ats={p.ats}  {p.summary()}  fp={p.fingerprint()}"]
    for r in p.rows:
        what = f"= {r.answer['value']!r}"[:40] if r.answer else f"· {r.reason}"
        lines.append(
            f"{r.index:>3} {r.action:<7} {str(r.key):<24} t{r.tier or '-':<2} {r.widget:<8} "
            f"{r.label[:38]:<38} {what}"
        )
    return "\n".join(lines)

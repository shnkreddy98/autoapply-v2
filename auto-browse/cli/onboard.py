"""User onboarding: the user's facts, and one resume per role.

A profile is two things kept apart on purpose:

  * facts — contact, address, work authorization, EEO, logistics. One set per user,
    asked by `autoapply profile init` and stored in users + application_profiles.
  * role resumes — `autoapply resume add FILE.docx --label de`. Each .docx is
    uploaded with a PDF rendering (the apply upload) and parsed by the LLM into a
    ParsedResume stored on that base (resume_assets.parsed).

The profile used for a job is the facts plus the parsed resume of the role the job
was tailored from (`autoapply profile export --label de`). Role resumes are never
merged: they legitimately differ, and a merged copy has to silently pick one. What
must agree across roles — employers and their dates — is checked when a resume is
added, and reported rather than overwritten.

All prompts go through an injectable `ask` function so tests can drive them.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from common import keys, repo

PARSE_MODEL = os.getenv("OPENROUTER_MODEL", "google/gemini-3-flash-preview")


# ---------------------------------------------------------------------------
# 1. text extraction


def extract_text(path: str) -> str:
    """Raw text from a .docx (paragraphs + tables, document order) or .pdf."""
    p = Path(path)
    if p.suffix.lower() == ".docx":
        from docx import Document
        from docx.oxml.ns import qn

        doc = Document(str(p))
        parts = []
        for child in doc.element.body:
            if child.tag == qn("w:p"):
                text = "".join(t.text or "" for t in child.iter(qn("w:t")))
                if text.strip():
                    parts.append(text)
            elif child.tag == qn("w:tbl"):
                for row in child.iter(qn("w:tr")):
                    cells = [
                        "".join(t.text or "" for t in cell.iter(qn("w:t")))
                        for cell in row.iter(qn("w:tc"))
                    ]
                    line = "\t".join(c for c in cells if c.strip())
                    if line.strip():
                        parts.append(line)
        return "\n".join(parts)
    if p.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        return "\n".join(page.extract_text() or "" for page in PdfReader(str(p)).pages)
    raise SystemExit(f"unsupported resume format {p.suffix!r} (need .docx or .pdf)")


def docx_to_pdf(docx_path: str) -> str:
    """LibreOffice headless docx -> pdf (same converter ai-assist uses for
    tailored resumes). Returns the PDF path in a temp dir."""
    outdir = tempfile.mkdtemp(prefix="autoapply-onboard-")
    try:
        proc = subprocess.run(
            [
                "libreoffice",
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                outdir,
                str(Path(docx_path).resolve()),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        raise SystemExit(
            "docx->pdf needs LibreOffice on PATH — install it, or pass --pdf yourself"
        )
    pdf = Path(outdir) / (Path(docx_path).stem + ".pdf")
    if proc.returncode != 0 or not pdf.exists():
        raise SystemExit(f"docx->pdf conversion failed: {proc.stderr.strip()[:300]}")
    return str(pdf)


# ---------------------------------------------------------------------------
# 2. LLM parse -> structured resume


class ParsedContact(BaseModel):
    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    linkedin: str | None = None
    github: str | None = None


class ParsedExperience(BaseModel):
    job_title: str | None = None
    company_name: str | None = None
    location: str | None = None
    from_date: date | None = None
    to_date: date | None = None  # null when current
    is_current: bool = False
    bullets: list[str] = Field(default_factory=list)


class ParsedEducation(BaseModel):
    degree: str | None = None
    major: str | None = None
    school: str | None = None
    from_date: date | None = None
    to_date: date | None = None
    gpa: str | None = None  # as printed ("3.8/4.0"); null when the resume omits it


class ParsedSkillGroup(BaseModel):
    category: str
    skills: list[str] = Field(default_factory=list)


class ParsedResume(BaseModel):
    contact: ParsedContact = Field(default_factory=ParsedContact)
    summary: str | None = None
    work_experiences: list[ParsedExperience] = Field(default_factory=list)
    education: list[ParsedEducation] = Field(default_factory=list)
    skill_groups: list[ParsedSkillGroup] = Field(default_factory=list)


_PARSE_PROMPT = """You are an expert resume parser. Extract ALL information from \
the resume into the JSON schema below. Rules:
- dates: convert any format ("Jan 2020", "2020", "01/2020") to ISO YYYY-MM-DD, \
using the 1st when only month/year is given; null ONLY if no date is stated
- a role described as current/"Present": is_current=true, to_date=null
- bullets: every bullet point, verbatim, no summarizing or truncation
- skills: group by category as they appear (Languages, Cloud, Databases, ...)
- return empty arrays/nulls for missing sections, never invent data
- output ONLY the JSON object, no prose, no code fences

JSON schema:
{schema}
"""


def _json_from_reply(reply: str) -> dict:
    text = reply.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)  # defensive de-fencing
    return json.loads(text)


def parse_resume_llm(text: str, model: str = PARSE_MODEL, client=None) -> ParsedResume:
    """One structured-output call, one retry carrying the validation errors."""
    if client is None:
        from openai import OpenAI

        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise SystemExit("OPENROUTER_API_KEY is not set (env or auto-browse/.env)")
        client = OpenAI(
            base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            api_key=api_key,
        )

    system = _PARSE_PROMPT.format(schema=json.dumps(ParsedResume.model_json_schema()))
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Resume:\n---\n{text}\n---\nExtract all fields."},
    ]
    last_err: Exception | None = None
    for _ in range(2):
        reply = (
            client.chat.completions.create(
                model=model, messages=messages, temperature=0.1
            )
            .choices[0]
            .message.content
        )
        try:
            return ParsedResume.model_validate(_json_from_reply(reply))
        except (ValidationError, json.JSONDecodeError) as err:
            last_err = err
            messages += [
                {"role": "assistant", "content": reply},
                {
                    "role": "user",
                    "content": f"Invalid, fix and return ONLY the JSON:\n{err}",
                },
            ]
    raise SystemExit(f"resume parse failed after retry: {last_err}")


# ---------------------------------------------------------------------------
# 3. interactive review


def _ask(
    label: str, default: str | None = None, *, ask=input, yes: bool = False
) -> str | None:
    """Prompt with an autofilled default; empty answer keeps the default,
    '-' clears it. --yes accepts the default without asking."""
    if yes:
        return default
    shown = f" [{default}]" if default not in (None, "") else ""
    raw = ask(f"  {label}{shown}: ").strip()
    if raw == "":
        return default
    if raw == "-":
        return None
    return raw


def _ask_bool(label: str, default: bool, *, ask=input, yes: bool = False) -> bool:
    if yes:
        return default
    shown = "Y/n" if default else "y/N"
    raw = ask(f"  {label} [{shown}]: ").strip().lower()
    if raw == "":
        return default
    return raw in ("y", "yes", "true", "1")


def _ask_date(
    label: str, default: date | None = None, *, ask=input, yes: bool = False
) -> date | None:
    while True:
        raw = _ask(
            f"{label} (YYYY-MM-DD)",
            default.isoformat() if default else None,
            ask=ask,
            yes=yes,
        )
        if raw in (None, ""):
            return None
        if isinstance(raw, date):
            return raw
        try:
            return date.fromisoformat(raw)
        except ValueError:
            if yes:
                return default
            print("    not a date, try again (empty to skip)")


def _split_name(full_name: str | None) -> tuple[str | None, str | None, str | None]:
    """Default first/middle/last for the review prompts. Only a default: compound
    surnames ("van der Berg") split wrong, which is why the parts are confirmed and
    stored rather than derived from users.full_name at fill time."""
    parts = (full_name or "").split()
    if not parts:
        return None, None, None
    if len(parts) == 1:
        return parts[0], None, None
    return parts[0], " ".join(parts[1:-1]) or None, parts[-1]


def _guess_city_state(location: str | None) -> tuple[str | None, str | None]:
    if not location:
        return None, None
    parts = [p.strip() for p in location.split(",")]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return parts[0], None


def years_of_experience(
    work: list[ParsedExperience], today: date | None = None
) -> int | None:
    starts = [w.from_date for w in work if w.from_date]
    if not starts:
        return None
    days = ((today or date.today()) - min(starts)).days
    return max(0, round(days / 365.25))


def _ask_password(*, ask_secret=None) -> str | None:
    """Portal password has no default, so it is asked even under --yes (it's the
    one onboarding answer that can't be autofilled). Non-interactive sessions
    (EOF / captured stdin) leave it unset instead of crashing."""
    fn = ask_secret or getpass.getpass
    try:
        raw = fn(
            "  portal login password (one password reused on every ATS; empty = skip): "
        ).strip()
    except (EOFError, OSError):
        raw = ""
    return raw or None


@dataclass
class OnboardData:
    """Everything `save` writes: reviewed contact + application answers. `resume`
    carries the contact and, when a resume was given, the work history that the
    years-of-experience default is computed from; its content is stored on the role
    base by `add_role_resume`, not here."""

    resume: ParsedResume
    profile: dict
    login: dict = field(default_factory=dict)


def load_existing_profile(conn, email: str | None) -> dict | None:
    """Stored application_profiles row for this email. Re-running init must
    default every prompt to what the user already entered — the save is a
    full-row upsert, so anything not re-answered would otherwise be wiped."""
    if not email:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ap.* FROM application_profiles ap "
            "JOIN users u ON u.user_id = ap.user_id WHERE u.email = %s",
            (email,),
        )
        return cur.fetchone()


def interactive_review(
    parsed: ParsedResume,
    *,
    ask=input,
    yes: bool = False,
    ask_secret=None,
    existing: dict | None = None,
) -> OnboardData:
    c = parsed.contact
    ex = existing or {}

    def prior(col: str, fallback=None):
        v = ex.get(col)
        return fallback if v in (None, "") else v

    print("\n-- contact (extracted from resume; Enter keeps, '-' clears) --")
    c.full_name = _ask("full name", c.full_name, ask=ask, yes=yes)
    c.email = _ask("email", c.email, ask=ask, yes=yes)
    if not c.email:
        raise SystemExit("email is required (users are keyed by email)")
    c.phone = _ask("phone", c.phone, ask=ask, yes=yes)
    c.location = _ask("location", c.location, ask=ask, yes=yes)
    c.linkedin = _ask("linkedin", c.linkedin, ask=ask, yes=yes)
    c.github = _ask("github", c.github, ask=ask, yes=yes)

    # Collected here but saved to application_profiles: forms split the name, and
    # the split cannot be recomputed safely from full_name later.
    first, middle, last = _split_name(c.full_name)
    names: dict = {}
    names["first_name"] = _ask("first name", prior("first_name", first), ask=ask, yes=yes)
    names["middle_name"] = _ask("middle name", prior("middle_name", middle), ask=ask, yes=yes)
    names["last_name"] = _ask("last name", prior("last_name", last), ask=ask, yes=yes)
    names["preferred_name"] = _ask(
        "preferred name (blank = first name)", prior("preferred_name"), ask=ask, yes=yes
    )
    names["pronouns"] = _ask("pronouns (blank = skip)", prior("pronouns"), ask=ask, yes=yes)
    names["phone_country_code"] = _ask(
        "phone country code", prior("phone_country_code", "+1"), ask=ask, yes=yes
    )
    names["portfolio_url"] = _ask("portfolio url", prior("portfolio_url"), ask=ask, yes=yes)

    print("\n-- ATS portal login (the agent signs in / creates portal accounts) --")
    login: dict = {
        "login_email": _ask("portal login email", c.email, ask=ask, yes=yes),
        "login_password": _ask_password(ask_secret=ask_secret),
    }
    if not login["login_password"]:
        print(
            "    (!) password not set — account-gated portals (Workday, iCIMS, ...) "
            "will hand off to you; rerun `autoapply profile init` to add one"
        )

    city, state = _guess_city_state(c.location)
    print("\n-- address (application forms want it split) --")
    profile: dict = dict(names)
    profile["street_address"] = _ask(
        "street address", prior("street_address"), ask=ask, yes=yes
    )
    profile["city"] = _ask("city", prior("city", city), ask=ask, yes=yes)
    profile["state"] = _ask("state", prior("state", state), ask=ask, yes=yes)
    profile["zip_code"] = _ask("zip", prior("zip_code"), ask=ask, yes=yes)
    profile["country"] = _ask("country", prior("country", "United States"), ask=ask, yes=yes)
    profile["date_of_birth"] = _ask_date(
        "date of birth", prior("date_of_birth"), ask=ask, yes=yes
    )

    print("\n-- work authorization + logistics --")
    profile["work_eligible_us"] = _ask_bool(
        "eligible to work in the US?", prior("work_eligible_us", True), ask=ask, yes=yes
    )
    profile["visa_sponsorship"] = _ask_bool(
        "need visa sponsorship (now or future)?",
        prior("visa_sponsorship", False),
        ask=ask,
        yes=yes,
    )
    profile["security_clearance"] = _ask_bool(
        "hold a security clearance?",
        prior("security_clearance", False),
        ask=ask,
        yes=yes,
    )
    profile["over_18"] = _ask_bool(
        "18 or older?", prior("over_18", True), ask=ask, yes=yes
    )
    profile["years_of_experience"] = int(
        _ask(
            "years of experience",
            str(
                prior(
                    "years_of_experience",
                    years_of_experience(parsed.work_experiences) or 0,
                )
            ),
            ask=ask,
            yes=yes,
        )
        or 0
    )
    profile["employment_type"] = _ask(
        "employment type", prior("employment_type", "Full-time"), ask=ask, yes=yes
    )
    profile["desired_salary"] = _ask(
        "desired salary (blank = leave to agent)",
        prior("desired_salary"),
        ask=ask,
        yes=yes,
    )
    profile["available_start_date"] = _ask_date(
        "available start date", prior("available_start_date"), ask=ask, yes=yes
    )
    profile["notice_period"] = _ask(
        "notice period", prior("notice_period", "2 weeks"), ask=ask, yes=yes
    )
    profile["how_did_you_hear"] = _ask(
        "how did you hear about jobs (default answer)",
        prior("how_did_you_hear", "LinkedIn"),
        ask=ask,
        yes=yes,
    )
    profile["willing_relocate"] = _ask_bool(
        "willing to relocate?", prior("willing_relocate", True), ask=ask, yes=yes
    )
    profile["willing_travel"] = _ask_bool(
        "willing to travel?", prior("willing_travel", True), ask=ask, yes=yes
    )
    profile["travel_percentage"] = (
        _ask(
            "max travel percentage", prior("travel_percentage", "25%"), ask=ask, yes=yes
        )
        if profile["willing_travel"]
        else None
    )
    profile["current_employee"] = False
    profile["ever_terminated"] = _ask_bool(
        "ever terminated from a job?", prior("ever_terminated", False), ask=ask, yes=yes
    )
    if profile["ever_terminated"]:
        profile["termination_explanation"] = _ask(
            "explanation", prior("termination_explanation"), ask=ask, yes=yes
        )

    print("\n-- EEO (voluntary; Enter keeps 'Prefer not to say') --")
    for col, label in (
        ("gender", "gender"),
        ("race_ethnicity", "race/ethnicity"),
        ("hispanic_latino", "hispanic or latino"),
        ("veteran_status", "veteran status"),
        ("disability_status", "disability status"),
    ):
        profile[col] = _ask(label, prior(col, "Prefer not to say"), ask=ask, yes=yes)

    print("\n-- application certifications (the boilerplate checkboxes) --")
    agree_all = _ask_bool(
        "agree to standard certifications (accuracy, background check, ...)?",
        prior("cert_accuracy", True),
        ask=ask,
        yes=yes,
    )
    for col in (
        "cert_accuracy",
        "cert_dismissal",
        "cert_background_check",
        "cert_drug_testing",
        "cert_at_will",
        "cert_job_description",
        "cert_privacy_notice",
        "cert_data_processing",
    ):
        profile[col] = agree_all
    profile["electronic_signature"] = _ask(
        "electronic signature",
        prior("electronic_signature", c.full_name),
        ask=ask,
        yes=yes,
    )
    profile["signature_date"] = date.today()

    return OnboardData(resume=parsed, profile=profile, login=login)


# ---------------------------------------------------------------------------
# 4. save facts


def save(conn, data: OnboardData) -> str:
    """Upsert the user (by email), their application_profiles row and portal login.
    Resume content is not written here — see `add_role_resume`."""
    c = data.resume.contact
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, full_name, phone, linkedin, github, location) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (email) DO UPDATE SET full_name = EXCLUDED.full_name, "
            "phone = EXCLUDED.phone, linkedin = EXCLUDED.linkedin, github = EXCLUDED.github, "
            "location = EXCLUDED.location, updated_at = now() "
            "RETURNING user_id",
            (c.email, c.full_name, c.phone, c.linkedin, c.github, c.location),
        )
        user_id = str(cur.fetchone()["user_id"])

        cols = list(data.profile)
        updates = ", ".join(f"{col} = EXCLUDED.{col}" for col in cols)
        cur.execute(
            f"INSERT INTO application_profiles (user_id, {', '.join(cols)}) "  # noqa: S608 — cols are code-defined
            f"VALUES (%s, {', '.join('%s' for _ in cols)}) "
            f"ON CONFLICT (user_id) DO UPDATE SET {updates}, updated_at = now()",
            (user_id, *data.profile.values()),
        )

        # Only written when a password was given: re-running init and skipping
        # the password prompt must not wipe stored credentials.
        login = data.login or {}
        if login.get("login_password"):
            cur.execute(
                "INSERT INTO portal_credentials (user_id, login_email, login_password) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET login_email = EXCLUDED.login_email, "
                "login_password = EXCLUDED.login_password",
                (user_id, login.get("login_email") or c.email, login["login_password"]),
            )
    conn.commit()
    return user_id


# ---------------------------------------------------------------------------
# 5. role resumes

# Becomes a storage folder, so no slashes or spaces; lowercase so `de` and `DE`
# cannot become two roles.
_LABEL_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
# `ask --label default` means "the user's default role", so no role may be called that.
RESERVED_LABEL = "default"
DEFAULT_ROLE = "general"


def check_label(label: str) -> str:
    label = (label or "").strip()
    if label == RESERVED_LABEL:
        raise SystemExit(f"{RESERVED_LABEL!r} is reserved (it means your default role) — pick another label")
    if not _LABEL_RE.fullmatch(label):
        raise SystemExit(
            f"bad label {label!r}: use lowercase letters, digits, '-' or '_' (e.g. swe, de, aiml)"
        )
    return label


_COMPANY_NOISE = {"inc", "llc", "ltd", "corp", "corporation", "co", "the", "limited", "gmbh"}


def _employer_key(name: str | None) -> str:
    words = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split()
    return " ".join(w for w in words if w not in _COMPANY_NOISE)


def _span(w: dict) -> str:
    """Month precision: resumes print months, and the parser fills in the 1st."""
    start = (w.get("from_date") or "?")[:7]
    end = "present" if w.get("is_current") else (w.get("to_date") or "?")[:7]
    return f"{start} -> {end}"


def role_conflicts(conn, user_id: str, label: str, parsed: dict) -> list[str]:
    """Facts that must agree across a user's role resumes but don't: an employer's
    dates, and who the current employer is. Titles are deliberately not compared —
    tailoring a title per role is a choice, a different end date is a mistake.
    A role leaving an old job out is not a conflict either."""
    mine = parsed.get("work_experiences") or []
    by_key = {_employer_key(w.get("company_name")): w for w in mine if w.get("company_name")}
    my_current = sorted(w["company_name"] for w in mine if w.get("is_current") and w.get("company_name"))
    out = []
    for other in repo.list_base_assets(conn, user_id):
        if other["variant"] != "docx" or other["label"] == label or not other.get("parsed"):
            continue
        theirs = other["parsed"].get("work_experiences") or []
        for w in theirs:
            mw = by_key.get(_employer_key(w.get("company_name")))
            if mw and _span(mw) != _span(w):
                out.append(
                    f"{w['company_name']}: {label} says {_span(mw)}, {other['label']} says {_span(w)}"
                )
        their_current = sorted(w["company_name"] for w in theirs if w.get("is_current") and w.get("company_name"))
        if {_employer_key(c) for c in my_current} != {_employer_key(c) for c in their_current}:
            out.append(
                f"current employer: {label} says {', '.join(my_current) or 'none'}, "
                f"{other['label']} says {', '.join(their_current) or 'none'}"
            )
    return out


@dataclass
class RoleResume:
    asset_id: str
    label: str
    is_default: bool
    parsed: dict
    conflicts: list[str]


def add_role_resume(
    conn,
    storage,
    user_id: str,
    label: str,
    docx_path: str,
    *,
    replace: bool = False,
    make_default: bool = False,
    parsed: ParsedResume | None = None,
    model: str = PARSE_MODEL,
) -> RoleResume:
    """Upload a role's .docx plus its PDF rendering and store the parsed content.

    Parsing happens before anything is uploaded, so a failed (paid) parse leaves the
    previous base untouched. The PDF is generated rather than accepted: tailoring
    edits the docx, and a separately supplied PDF drifts from it. The first role a
    user adds becomes their default."""
    import mimetypes

    label = check_label(label)
    docx = Path(docx_path)
    if not docx.is_file():
        raise SystemExit(f"no such file: {docx}")
    if docx.suffix.lower() != ".docx":
        raise SystemExit("resumes are added as .docx (tailoring edits the docx source)")
    if repo.get_base_asset(conn, user_id, variant="docx", label=label) and not replace:
        raise SystemExit(f"a resume labelled {label!r} already exists — pass --replace to overwrite")

    if parsed is None:
        parsed = parse_resume_llm(extract_text(str(docx)), model=model)
    pdf = docx_to_pdf(str(docx))

    ids = {}
    for variant, path in (("docx", str(docx)), ("pdf", pdf)):
        content_type = mimetypes.guess_type(path)[0]
        row = repo.get_base_asset(conn, user_id, variant=variant, label=label)
        if row:
            # Overwrite in place: one row per role and format (unique index), and
            # tailored_resumes.base_asset_id keeps pointing at a live asset.
            storage.put_file(row["s3_key"], path)
            repo.refresh_resume_asset(conn, row["id"], content_type=content_type)
            ids[variant] = row["id"]
        else:
            key = keys.base_resume_key(user_id, label, variant, variant)
            storage.put_file(key, path)
            ids[variant] = repo.insert_resume_asset(
                conn, user_id, "base", key, variant=variant, label=label,
                content_type=content_type,
            )

    data = parsed.model_dump(mode="json")
    repo.set_resume_parsed(conn, ids["docx"], data)
    if make_default or repo.default_resume_label(conn, user_id) is None:
        repo.set_default_resume_label(conn, user_id, label)
    return RoleResume(
        asset_id=ids["docx"],
        label=label,
        is_default=repo.default_resume_label(conn, user_id) == label,
        parsed=data,
        conflicts=role_conflicts(conn, user_id, label, data),
    )


def reparse_role_resume(conn, storage, user_id: str, label: str, **kw) -> RoleResume:
    """Re-parse a role's stored .docx (and render its PDF if missing) without the
    user re-uploading it — for bases added before parsing existed."""
    base = repo.get_base_asset(conn, user_id, variant="docx", label=label)
    if base is None:
        raise SystemExit(f"no resume labelled {label!r} — add one: autoapply resume add <file.docx> --label {label}")
    work = Path(tempfile.mkdtemp(prefix="autoapply-reparse-"))
    try:
        path = work / f"{label}.docx"
        storage.get_file(base["s3_key"], str(path))
        return add_role_resume(conn, storage, user_id, label, str(path), replace=True, **kw)
    finally:
        import shutil

        shutil.rmtree(work, ignore_errors=True)


def print_role_resume(rr: RoleResume) -> None:
    work = rr.parsed.get("work_experiences") or []
    print(
        f"role {rr.label!r}{' (default)' if rr.is_default else ''}: "
        f"{len(work)} jobs, {len(rr.parsed.get('education') or [])} education, "
        f"{len(rr.parsed.get('skill_groups') or [])} skill groups"
    )
    for w in work:
        print(f"  {w.get('job_title')} @ {w.get('company_name')} ({_span(w)})")
    if rr.conflicts:
        print("\nconflicts with your other role resumes (fix the resume, then --replace):")
        for c in rr.conflicts:
            print(f"  ! {c}")


# ---------------------------------------------------------------------------
# CLI entry points (wired from cli/main.py)


def _contact_from_user(user: dict | None) -> ParsedContact:
    u = user or {}
    return ParsedContact(
        full_name=u.get("full_name"), email=u.get("email"), phone=u.get("phone"),
        location=u.get("location"), linkedin=u.get("linkedin"), github=u.get("github"),
    )


def cmd_profile_init(args) -> int:
    from cli.main import _connect, _storage  # lazy: avoids cycle at import time

    parsed = None
    if args.docx:
        label = check_label(args.label)
        print(f"extracting text from {args.docx} ...")
        text = extract_text(args.docx)
        print(f"parsing with {args.model or PARSE_MODEL} ...")
        parsed = parse_resume_llm(text, model=args.model or PARSE_MODEL)
    email = args.email or (parsed.contact.email if parsed else None)
    if not email:
        raise SystemExit("pass --email (or --docx with an email on the resume)")

    with _connect() as conn:
        existing = load_existing_profile(conn, email)
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE email = %s", (email,))
            user = cur.fetchone()
    if existing:
        print("(existing profile found — prompts default to your stored answers)")

    # a stored contact beats the resume's only when no resume was given this time
    contact = parsed.contact if parsed else _contact_from_user(user)
    contact.email = email
    review = ParsedResume(contact=contact, work_experiences=parsed.work_experiences if parsed else [])
    data = interactive_review(review, yes=args.yes, existing=existing)

    with _connect() as conn:
        user_id = save(conn, data)
        print(f"\nprofile saved: user {user_id} ({email})")
        if parsed:
            rr = add_role_resume(
                conn, _storage(), user_id, label, args.docx,
                replace=args.replace, parsed=parsed,
            )
            print_role_resume(rr)
    print("\nadd a resume per role:  autoapply resume add <file.docx> --label <role>")
    return 0


def cmd_profile_show(args) -> int:
    from cli.main import _connect, resolve_user

    with _connect() as conn:
        user_id = resolve_user(conn, args.user)
        p = repo.fetch_profile(conn, user_id, label=args.label)
        bases = [b for b in repo.list_base_assets(conn, user_id) if b["variant"] == "docx"]
        default = repo.default_resume_label(conn, user_id)
    u = p.user
    print(f"{u['full_name']}  <{u['email']}>  {u.get('phone') or ''}")
    print(f"  {u.get('location') or ''}  {u.get('linkedin') or ''}")
    cred = p.login or {}
    pw_state = (
        "password set"
        if cred.get("login_password")
        else "password NOT SET — account portals will hand off"
    )
    print(f"  portal login: {cred.get('login_email') or u['email']}  ({pw_state})")
    prof = p.profile or {}
    filled = {
        k: v
        for k, v in prof.items()
        if v is not None and k not in ("user_id", "updated_at")
    }
    print(f"  profile fields set: {len(filled)}")
    print("  roles:")
    if not bases:
        print("    (none — autoapply resume add <file.docx> --label <role>)")
    for b in bases:
        mark = "*" if b["label"] == default else " "
        state = (
            f"parsed {b['parsed_at']:%Y-%m-%d}" if b.get("parsed")
            else f"not parsed — autoapply resume parse {b['label']}"
        )
        print(f"   {mark} {b['label']:<10} {state}")
    if p.label is None:
        return 0
    print(f"\n  role {p.label!r}:")
    for w in p.work_experiences:
        print(f"    {w.get('job_title')} @ {w.get('company_name')} ({_span(w)}, {len(w.get('bullets') or [])} bullets)")
    for e in p.education:
        print(f"    {e.get('degree')} {e.get('major') or ''} — {e.get('school')}")
    for g in p.skill_groups:
        print(f"    {g.get('category')}: {', '.join(g.get('skills') or [])}")
    return 0


def cmd_profile_export(args) -> int:
    """The profile JSON a form filler consumes: facts + one role's parsed resume."""
    from agent.profile import profile_json
    from cli.main import _connect, resolve_user

    with _connect() as conn:
        user_id = resolve_user(conn, args.user)
        p = repo.fetch_profile(conn, user_id, label=args.label)
    if p.label is None:
        raise SystemExit("no resume for that role — autoapply resume add <file.docx> --label <role>")
    if not (p.work_experiences or p.education or p.skill_groups or p.summary):
        # stderr, so a redirected export still says why the resume half is empty
        import sys

        print(
            f"warning: role {p.label!r} has no parsed resume — autoapply resume parse {p.label}",
            file=sys.stderr,
        )
    text = json.dumps(profile_json(p), indent=2, default=str)
    if args.out:
        Path(args.out).write_text(text + "\n")
        print(f"wrote {args.out} (role {p.label!r})")
    else:
        print(text)
    return 0


def cmd_resume_add(args) -> int:
    from cli.main import _connect, _storage, resolve_user

    with _connect() as conn:
        user_id = resolve_user(conn, args.user)
        print(f"parsing {args.file} as role {args.label!r} ...")
        rr = add_role_resume(
            conn, _storage(), user_id, args.label, args.file,
            replace=args.replace, make_default=args.default, model=args.model or PARSE_MODEL,
        )
    print_role_resume(rr)
    print(f"\ntailor with it:  autoapply tailor --label {rr.label}")
    return 0


def cmd_resume_parse(args) -> int:
    from cli.main import _connect, _storage, resolve_user

    with _connect() as conn:
        user_id = resolve_user(conn, args.user)
        labels = args.labels or sorted(
            {b["label"] for b in repo.list_base_assets(conn, user_id) if b["variant"] == "docx"}
        )
        if not labels:
            raise SystemExit("no resumes to parse — autoapply resume add <file.docx> --label <role>")
        for label in labels:
            print(f"parsing role {label!r} ...")
            print_role_resume(
                reparse_role_resume(conn, _storage(), user_id, label, model=args.model or PARSE_MODEL)
            )
    return 0


def cmd_resume_default(args) -> int:
    from cli.main import _connect, resolve_user

    with _connect() as conn:
        user_id = resolve_user(conn, args.user)
        label = check_label(args.label)
        if repo.get_base_asset(conn, user_id, variant="docx", label=label) is None:
            raise SystemExit(f"no resume labelled {label!r} — add it first")
        repo.set_default_resume_label(conn, user_id, label)
    print(f"default role is now {label!r}")
    return 0

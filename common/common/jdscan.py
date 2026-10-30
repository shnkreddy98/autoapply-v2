"""Read work-authorization limits and the pay range out of a captured JD.

Deterministic and free: it runs on every capture and can be re-run over every stored
JD (`autoapply jobs scan`) without a model call. A miss costs a job the user then
reads by hand; a false hit hides a job they could have had, so the patterns only
fire on a statement the posting makes, never on the words appearing nearby.

The noise this has to step around, all common in captured JDs:
- The application form is captured with the page. "Will you now or in the future
  require sponsorship?" is asked by employers that do sponsor, so any segment ending
  in "?" is a question and is skipped.
- EEO boilerplate lists "citizenship status" among protected classes, and form
  options read "I am a U.S. Citizen/Permanent Resident". Neither restricts anyone,
  so citizenship only counts next to "must", "required" or "only".
- Benefits text is full of dollar figures ("$0 paycheck contribution", "$1 for every
  $1"), so a salary is a range of two amounts, or one amount with a pay period.

Limits, strongest first: `clearance` (a clearance needs citizenship), `citizen`
(citizen / permanent resident / U.S. person), `no_sponsorship`. Each is a reason a
candidate who needs a visa now or later cannot take the job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# no "sponsored": "company-sponsored events" is a perk, not a visa statement
_SPONSOR = r"(?<!-)(?:visa |immigration |employment |work |employer |h-?1b )*sponsor(?:ship|ing)?\b"

_LIMITS: list[tuple[str, list[str]]] = [
    (
        "clearance",
        [
            r"\b(?:must|required to|need to|requires?|required)\b[^.]{0,80}\b(?:secret|ts/sci|top secret|security|government|dod) clearance",
            r"\b(?:secret|ts/sci|top secret|security) clearance\b[^.]{0,40}\b(?:is |are )?(?:required|a must|mandatory)\b",
            r"\b(?:ability|able|eligib(?:le|ility)) to obtain\b[^.]{0,40}\bclearance\b",
            # not "Active Security Clearance Agency/Department": a form field label
            r"\bactive (?:dod )?(?:(?:secret|ts/sci|top secret)(?: security)?|security) clearance\b(?!\s*(?:agency|level|type|status|\(s\)))",
        ],
    ),
    (
        "citizen",
        [
            r"\bmust (?:be|hold)\b[^.]{0,30}\b(?:u\.?s\.?|united states) (?:citizen|citizenship|person)",
            r"\b(?:u\.?s\.?|united states) citizenship (?:is |will be )?(?:required|mandatory)",
            r"\brequires? (?:u\.?s\.?|united states) citizenship\b",
            r"\b(?:u\.?s\.?|united states) citizens? (?:only|are eligible)\b",
            r"\bonly (?:open|available) to (?:u\.?s\.?|united states) citizens\b",
            r"\bmust be a\b[^.]{0,20}\b(?:green card holder|permanent resident)",
        ],
    ),
    (
        "no_sponsorship",
        [
            rf"\b(?:unable|not able|cannot|can ?not|can't|won't|will not|do not|does not|don't|doesn't|not in a position) (?:to )?(?:currently )?(?:provide|offer|support|consider)?\b[^.]{{0,30}}\b{_SPONSOR}",
            rf"\b(?:unable|not able|cannot|can ?not|can't|won't|will not|do not|does not|don't|doesn't) (?:to )?sponsor\b",
            rf"\bnot (?:be )?eligible for\b[^.]{{0,20}}{_SPONSOR}",
            rf"\b{_SPONSOR} (?:is |will )?(?:not|n't) (?:be )?(?:available|provided|offered|possible|supported)",
            rf"\bno {_SPONSOR}\b",
            rf"\bwithout (?:the need for |requiring |requirement of |needing )?(?:current or future |now or in the future |future |any )?(?:company |employer )?{_SPONSOR}",
        ],
    ),
]

_COMPILED = [(kind, [re.compile(p, re.I) for p in pats]) for kind, pats in _LIMITS]

# A hit inside one of these windows does not restrict a visa holder:
# "must be a U.S. citizen or have authorization to work" (OPT counts as authorized),
# "candidates eligible to renew employment authorization without employer
# sponsorship (DACA, TPS...) are welcome", "a borrower must be a U.S. citizen" (a
# product ad on the careers page), "... without employer sponsorship, answer No"
# (instructions for a form question), "will not provide sponsorship to H-1B
# candidates whose employment would require payment of the $100,000 fee" (a
# condition on some cases, not a no: an F-1 OPT change of status inside the U.S.
# does not trigger that fee).
_EXCUSES = re.compile(
    r"\bor (?:have|be|are|hold|possess)\b[^.]{0,30}\b(?:authori[sz]|eligib)"
    r"|\beligible to renew\b|\bborrower|\banswer\b"
    r"|\b(?:candidates|applicants|individuals) whose\b|\bfee\b",
    re.I,
)

# "We do sponsor visas", "we are open to sponsoring": an explicit yes outweighs a
# stray negative elsewhere ("we aren't able to sponsor for every role").
_SPONSORS_YES = re.compile(
    rf"(?<!not )(?<!n't )\b(?:we|will|do|does|can|able to|happy to|open to) (?:do )?(?:provide |offer |support )?{_SPONSOR}\b(?! (?:is |will )?not)",
    re.I,
)

# K: "$200kApplication" (the page glued the next word on) is still thousands
_K = r"(?:\s?(?-i:[kK](?![a-z])))"
_AMOUNT = rf"(?:USD\s*)?\$\s?\d[\d,.]*\d{_K}?(?:\s?(?:USD|CAD))?"
_PERIOD = r"(?:\s?(?:/|per|an|a)\s?(?:yr|year|annum|annually|hr|hour|mo|month)\b\.?|\s(?:annually|hourly|yearly)\b)"
_RANGE = re.compile(
    rf"(?P<lo>{_AMOUNT}){_PERIOD}?\s*(?:-|–|—|to|and)\s*(?P<hi>(?:USD\s*)?\$?\s?\d[\d,.]*\d{_K}?(?:\s?(?:USD|CAD))?)(?P<per>{_PERIOD})?",
    re.I,
)
_SINGLE = re.compile(rf"(?P<lo>{_AMOUNT})(?P<per>{_PERIOD})", re.I)
# "Salary: $111,300", "base pay for this role is $90,000": one figure named as pay
_LABELLED = re.compile(
    rf"\b(?:salary|base pay|pay rate|compensation)\b[^$\n]{{0,40}}(?P<lo>{_AMOUNT})(?P<per>{_PERIOD})?",
    re.I,
)
_ENTITIES = {"&mdash;": "—", "&ndash;": "–", "&nbsp;": " ", "&amp;": "&"}


@dataclass
class ScanResult:
    work_auth: str | None = None  # clearance | citizen | no_sponsorship
    work_auth_note: str | None = None  # the words that said so, for the user to check
    salary: str | None = None  # "$129,300 - $194,700/yr" as written, whitespace collapsed
    salary_min: int | None = None  # whole dollars, per salary_period
    salary_max: int | None = None
    salary_period: str | None = None  # year | hour | month


def _statements(text: str) -> list[str]:
    """Lines split after each "?", with the questions dropped. Split on "?" only, not
    ".": "U.S." would otherwise cut "must be a U.S. citizen" in two."""
    out = []
    for line in text.splitlines():
        for seg in re.split(r"(?<=\?)", line):
            seg = seg.strip().strip("*").strip()
            if seg and not seg.endswith("?"):
                out.append(seg)
    return out


def _value(amount: str) -> float:
    """Dollar value of "$129,300", "$150k", "$86.500,00" / "$19,57" (European
    separators)."""
    s = re.sub(r"[^\d.,kK]", "", amount)
    k = s[-1:] in ("k", "K")
    s = s.rstrip("kK")
    if re.search(r"\.\d{3},\d{2}$", s):  # 86.500,00
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3},\d{2}", s):  # 19,57 (an hourly rate)
        s = s.replace(",", ".")
    s = s.replace(",", "")
    try:
        v = float(s)
    except ValueError:
        return 0.0
    return v * 1000 if k else v


def _period(text: str, lo: float, hi: float) -> str:
    """year / hour / month from the words after the amounts. Unlabelled amounts are
    yearly unless both are small: "$34.50 - $57.00" is an hourly band."""
    words = text.lower()
    if re.search(r"\b(?:hr|hour|hourly)\b", words):
        return "hour"
    if re.search(r"\b(?:mo|month)\b", words):
        return "month"
    if re.search(r"\b(?:yr|year|annum|annually|yearly)\b", words):
        return "year"
    return "hour" if hi < 500 else "year"


def find_salary(text: str) -> ScanResult:
    """First pay range in the JD (salary fields only). A range is two amounts; the
    second may drop its "$" ("$79300-113000") and may carry the only "k"
    ("$115-120k"). A low end under $10 is benefits noise ("$0 paycheck
    contribution"), not pay. With no range, one amount that names its period
    ("USD $92,800.00/Yr.") or that a "salary" label names is both ends, but a small yearly or monthly one is a
    stipend ("$2500 per year" for learning, "$150 per month" for internet)."""
    for e, ch in _ENTITIES.items():
        text = text.replace(e, ch)
    # "base pay range for this role is $\n117,610.00": the page split the figure
    text = re.sub(r"\$[ \t]*\n\s*(?=\d)", "$", text)
    for seg in _statements(text):
        for m in _RANGE.finditer(seg):
            lo, hi = _value(m["lo"]), _value(m["hi"])
            if m["hi"].rstrip().lower().endswith("k") and lo < 1000 <= hi:
                lo *= 1000
            elif hi > 50 * lo >= 50_000:  # "$75,000–85,000K": a stray K
                hi /= 1000
            if lo >= 10 and hi > lo:
                return ScanResult(
                    salary=" ".join(m.group(0).split()),
                    salary_min=round(lo), salary_max=round(hi),
                    salary_period=_period(m.group(0), lo, hi),
                )
    for seg in _statements(text):
        for m in [*_SINGLE.finditer(seg), *_LABELLED.finditer(seg)]:
            v = _value(m["lo"])
            period = _period(m["per"] or "", v, v)
            if v >= {"year": 15000, "month": 1500}.get(period, 10):
                return ScanResult(
                    salary=" ".join(m.group(0).split()),
                    salary_min=round(v), salary_max=round(v), salary_period=period,
                )
    return ScanResult()


def find_work_auth(text: str) -> tuple[str | None, str | None]:
    """(limit, the words around it) for the strongest limit the JD states."""
    segs = _statements(text)
    yes = any(_SPONSORS_YES.search(s) for s in segs)
    for kind, pats in _COMPILED:
        if kind == "no_sponsorship" and yes:
            continue
        for seg in segs:
            for p in pats:
                m = p.search(seg)
                if not m:
                    continue
                # the hit plus a clause either side: enough to judge it, short
                # enough for a status line; captured paragraphs run to 2k chars
                window = seg[max(0, m.start() - 80): m.end() + 120]
                if _EXCUSES.search(window):
                    continue
                return kind, " ".join(window.split())
    return None, None


def scan(text: str) -> ScanResult:
    r = find_salary(text)
    r.work_auth, r.work_auth_note = find_work_auth(text)
    return r


def not_for_you(work_auth: str | None, needs_sponsorship: bool | None) -> bool:
    """NFY: the JD rules out a candidate who needs sponsorship now or in the future
    (F-1 OPT / STEM OPT, H-1B, ...), and the profile says this user does. Citizen and
    clearance limits count too: a sponsorship candidate is neither a citizen nor a
    permanent resident."""
    return bool(needs_sponsorship) and work_auth is not None


def clamp_salary(desired, salary_min: int | None, salary_max: int | None,
                 salary_period: str | None):
    """The desired salary moved inside the JD's yearly range: asking above the top of
    a posted band reads as not having read it, below the floor leaves money behind.
    `desired` comes back unchanged when it is not a plain number ("140k", "open"),
    or the JD has no yearly range: an hourly band says nothing about a yearly ask."""
    if salary_min is None or salary_max is None or salary_period != "year":
        return desired
    try:
        want = float(str(desired).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return desired
    clamped = min(max(want, salary_min), salary_max)
    return desired if clamped == want else str(int(clamped))

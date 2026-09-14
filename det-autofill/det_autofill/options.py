"""Pick one option of a dropdown / radio group for an answer — deterministically.

Ported from Autofill's `matchOption` (job-autofill.js). The ladder is fixed and
each step is tried only when the one before found nothing:

  1. exact option value            (case-sensitive)
  2. normalized option value
  3. normalized option text        ("United States" == "united states")
  4. aliases                       (preference regexes: EEO wording, yes/no, the
                                    answer store's OPTION_PREFS), pattern by pattern
  5. unique prefix                 ("Master" -> "Master's Degree")

A step that fits more than one option is a MISS, not a coin flip: two plausible
options is exactly the case where guessing puts a wrong answer on an application.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico",
}

COUNTRY_ISO = {
    "united states": "US", "united states of america": "US", "usa": "US",
    "canada": "CA", "united kingdom": "GB", "india": "IN", "australia": "AU",
    "germany": "DE", "france": "FR", "ireland": "IE", "netherlands": "NL",
    "mexico": "MX", "singapore": "SG", "israel": "IL", "japan": "JP",
}

# Keys whose values are US states / countries, so "CA" also matches "California".
_STATE_KEYS = {"state"}
_COUNTRY_KEYS = {"country"}


def nv(s: object) -> str:
    """Normalize an option value/text for comparison (Autofill's `nv`)."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+ ]", " ", str(s or "").lower())).strip()


_DIAL_CODE = re.compile(r"\s*\(?\+\d{1,4}\)?\s*$")


def text_forms(text: str) -> list[str]:
    """Normalized ways an option's text can be compared: as shown, and without a
    trailing dial code ("United States +1" on Greenhouse's country picker)."""
    forms = [nv(text), nv(_DIAL_CODE.sub("", text or ""))]
    return list(dict.fromkeys(f for f in forms if f))


@dataclass(frozen=True)
class Choice:
    value: str
    text: str


@dataclass(frozen=True)
class Pick:
    index: int | None  # None = no option chosen
    how: str  # which ladder step decided, or why nothing was chosen


def candidate_strings(key: str, value: object) -> list[str]:
    """Every literal string accepted as "this option means `value`", in order."""
    if isinstance(value, bool):
        return ["Yes" if value else "No"]
    out = [str(value)]
    n = nv(value)
    if key in _STATE_KEYS:
        up = str(value).strip().upper()
        if up in US_STATES:
            out.append(US_STATES[up])
        out += [code for code, name in US_STATES.items() if nv(name) == n]
    if key == "location_city":
        # "Springfield, IL" -> "Springfield, Illinois" and "..., United States": the
        # spelled-out forms location pickers list (Greenhouse, Google Places)
        m = re.match(r"^\s*([^,]+),\s*([A-Za-z]{2})\s*$", str(value))
        if m and m.group(2).upper() in US_STATES:
            city, state = m.group(1).strip(), US_STATES[m.group(2).upper()]
            out += [f"{city}, {state}, United States", f"{city}, {state}"]
    if key in _COUNTRY_KEYS:
        if n in COUNTRY_ISO:
            out.append(COUNTRY_ISO[n])
            out += [name for name, iso in COUNTRY_ISO.items() if iso == COUNTRY_ISO[n]]
    return list(dict.fromkeys(s for s in out if s))


_PLACEHOLDER = re.compile(r"^(select( one| an option)?|choose( one)?|please select|none selected)?$")

# Resumes name degrees ("Bachelor of Engineering", "M.S. Data Analytics"); forms offer
# levels ("Bachelor's Degree", "Master's Degree"). Patterns run on nv() text, where
# "Master's" reads "master s". Business administration is its own option on most
# forms, so a generic master's must not land on M.B.A. and vice versa.
DEGREE_LEVELS: list[tuple[str, list[str]]] = [
    (r"business administration|\bm ?b ?a\b", [r"business administration", r"\bm b a\b"]),
    (r"ph ?d|doctor of philosophy|doctorate", [r"ph ?d|doctor of philosophy", r"^doctorate"]),
    # a bare "Master" could be the M.B.A. too, so it must say which kind
    (r"^master s\b|^masters\b|^master of (?!business)|\bm ?s\b|\bm ?sc\b|\bm ?eng\b|\bm ?tech\b|\bm ?a\b",
     [r"^master s( degree)?$", r"^master(?!.*business)", r"^graduate degree"]),
    (r"^bachelor|\bb ?s\b|\bb ?sc\b|\bb ?e\b|\bb ?tech\b|\bb ?a\b|undergraduate",
     [r"^bachelor s( degree)?$", r"^bachelor"]),
    (r"^associate", [r"^associate"]),
    (r"high school|secondary|\bged\b", [r"high school"]),
    (r"diploma", [r"diploma"]),
]
_DEGREE_KEYS = {"degree", "education_level"}


def degree_patterns(value: object) -> list[str]:
    n = nv(value)
    for trigger, pats in DEGREE_LEVELS:
        if re.search(trigger, n):
            return pats
    return []


_YES = r"^(yes|y|true|i am|i do|i have|i will)\b"
_NO = r"^(no|n|false|i am not|i do not|i don t|i have not|i will not)\b"


def _unique(hits: list[int], how: str, options: list | None = None) -> Pick | None:
    if len(hits) == 1:
        return Pick(hits[0], how)
    if len(hits) > 1 and options is not None and len({nv(options[i].text) for i in hits}) == 1:
        # identical wording (Greenhouse lists "Springfield, Illinois, United States"
        # twice): whichever is clicked, the form records the same answer
        return Pick(hits[0], f"{how}(identical-options)")
    if len(hits) > 1:
        return Pick(None, f"ambiguous-{how}({len(hits)})")
    return None


def match_option(
    key: str, value: object, options: list[Choice], prefs: list[str] | None = None
) -> Pick:
    """Choose the option for `value`. `prefs` are ordered regexes (the answer's
    preference list); for booleans the yes/no vocabulary is used when none given."""
    # placeholders ("Select...", "--") never count, but indices stay the caller's
    live = [i for i, o in enumerate(options) if (nv(o.text) or nv(o.value)) and not _PLACEHOLDER.match(nv(o.text))]
    if not live:
        return Pick(None, "no-options")
    if value is None and not prefs:
        return Pick(None, "no-value")

    cands = candidate_strings(key, value) if value is not None and not isinstance(value, list) else []
    for how, test in (
        ("value-exact", lambda o, c: o.value == c),
        ("value-normalized", lambda o, c: bool(nv(o.value)) and nv(o.value) == nv(c)),
        ("text-exact", lambda o, c: nv(c) in text_forms(o.text)),
    ):
        hits = [i for i in live if any(test(options[i], c) for c in cands)]
        pick = _unique(hits, how, options)
        if pick:
            return pick

    patterns = list(prefs or [])
    if isinstance(value, bool) and not patterns:
        patterns = [_YES if value else _NO]
    if key in _DEGREE_KEYS and not patterns:
        patterns = degree_patterns(value)
    for pat in patterns:
        rx = re.compile(pat, re.I)
        hits = [i for i in live if any(rx.search(f) for f in (text_forms(options[i].text) or [nv(options[i].value)]))]
        if len(hits) > 1:
            # "No" vs "No, I do not want to answer": an option equal to the matched
            # word itself is the precise one; otherwise refuse
            exact = [i for i in hits if any(rx.fullmatch(f) for f in text_forms(options[i].text))]
            hits = exact if len(exact) == 1 else hits
        pick = _unique(hits, f"alias:{pat}", options)
        if pick:
            return pick

    hits = [
        i for i in live
        if any(nv(c) and f.startswith(nv(c)) for c in cands for f in text_forms(options[i].text))
    ]
    if key == "phone_country_code" and len(hits) > 1:
        # "+1" is shared by 25 countries in Dayforce's list ("🇨🇦 +1 Canada", …); the
        # profile's phone is a US number
        us = [i for i in hits if re.search(r"united states( of america)?(?! \w)|\bu\.?s\.?a?\b", options[i].text, re.I)]
        hits = us if len(us) == 1 else hits
    return _unique(hits, "text-prefix", options) or Pick(None, "no-matching-option")


def hispanic_only(options: list[dict]) -> bool:
    """An "Ethnicity" question whose choices are only Hispanic / Not Hispanic (and a
    decline): the Hispanic question under another name."""
    texts = [o.get("text", "") for o in options if o.get("text")]
    return bool(texts) and any(re.search(r"hispanic|latin", t, re.I) for t in texts) and all(
        re.search(r"hispanic|latin|wish|decline|prefer|choose not|not to (say|answer|disclose)", t, re.I) for t in texts)

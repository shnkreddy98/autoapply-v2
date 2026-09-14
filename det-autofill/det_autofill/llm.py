"""The resolver: answers for the fields the deterministic pass could not answer.

Order per field, cheapest and most trusted first:
  1. a saved answer (answered once before, by the model or the user)
  2. a per-job question ("why this company / role", cover letter) -> the `per_job`
     callback, written fresh for every application and never replayed elsewhere
  3. one model call for everything left on the page, in one strict JSON reply

Guard rails on the model's reply:
  * choice fields (select / radio / checkbox) must name one of the options listed,
    copied exactly, or null — anything else is dropped, never fuzzily matched
  * the prompt carries the role's profile and the job description, never the portal
    password, and tells the model to answer null rather than invent a fact
Only answers that were filled successfully are saved (see `learned`).

No database or network code here: `complete`, `saved` and `per_job` are injected.
"""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable, Protocol

from det_autofill.classify import Row
from det_autofill.fill import FillResult
from det_autofill.options import nv

PER_JOB_RE = re.compile(
    r"\bwhy\b.{0,50}\b(you|us|company|role|position|job|team|join|apply|applying|work (here|at|for|with))\b"
    r"|\bcover letter\b|\bmotivation\b|\bwhat (excites|interests|attracts) you\b"
    r"|\binterest(ed)? in\b.{0,40}\b(role|position|company|us|opportunity|job)\b|\btell us why\b",
    re.I,
)
CHOICE_WIDGETS = {"select", "radio", "checkbox"}
JD_LIMIT = 6000

SYSTEM = """You complete job application fields for a candidate.
Answer ONLY from the candidate profile and the job description provided.
Rules:
- If a field lists options, answer with exactly one option copied verbatim, or null.
- Never invent facts: citizenship, visas, clearances, licenses, degrees, employers,
  dates, years of experience with a tool the profile does not show. Unknown -> null.
- "Do you have / have you" questions about experience, skills, degrees or years:
  answer "Yes" only if EVERY requirement in the question is shown by the profile, and
  put the exact supporting words from the profile in "evidence" (copied verbatim).
  If any requirement is missing (a language, a framework, a number of years) answer
  null — never "Yes" on partial matches.
- Numbers as plain digits. Free-text answers: first person, under 60 words, no em dashes.
Return ONLY a JSON object:
{"answers": [{"id": <field id>, "answer": <string or null>, "evidence": <string or null>}]}"""

# Qualification/experience attestations. A "Yes" to one of these is a claim about the
# applicant; the model must back it with words that exist in the profile, checked here.
# (A live Nextdoor form got "Yes" to ten "full term experience with Kotlin, Spring,
# Memcached..." questions that the resume did not support.)
ATTESTATION_RE = re.compile(
    r"\b(do|did|have|has) you (have|possess|hold|currently have)\b|\bhave you (worked|used|built|led|managed)\b"
    r"|\byears? of\b.{0,40}\bexperience\b|\bexperience (with|in|using)\b|\bdegree\b|\bcertifi",
    re.I,
)


class SavedAnswers(Protocol):
    def get(self, row: Row, *, per_job: bool) -> dict | None:
        """{"answer": str, "source": "llm" | "human" | ...} or None."""
    def put(self, row: Row, answer: str, *, per_job: bool, source: str) -> None: ...


# "Why did you leave your last job?" says "why ... job" but has one answer for every
# application, so it is saved and reused like any other question.
NOT_PER_JOB_RE = re.compile(r"\b(leave|left|leaving|gap|terminated|fired|change jobs|career change)\b", re.I)


def is_per_job(row: Row) -> bool:
    label = row.label or ""
    return bool(PER_JOB_RE.search(label)) and not NOT_PER_JOB_RE.search(label)


def answer_from_text(row: Row, text: Any) -> dict | None:
    """Turn a plain answer into a fillable one, or None when it does not fit the field."""
    if text is None:
        return None
    text = str(text).strip()
    if not text or text.lower() in ("null", "none", "n/a"):
        return None
    if row.widget == "checkbox":
        if nv(text) in ("yes", "true", "checked"):
            return {"kind": "bool", "value": True}
        if nv(text) in ("no", "false", "unchecked"):
            return {"kind": "bool", "value": False}
        return None
    if row.widget in ("select", "radio"):
        exact = [o for o in row.options if nv(o.get("text")) == nv(text)]
        if len(exact) != 1:
            return None  # not an option the page offers: drop it, never approximate
        return {"kind": "option", "value": [r"^" + re.escape(nv(exact[0]["text"])) + r"$"]}
    return {"kind": "text", "value": text}


def build_messages(rows: list[Row], profile_doc: dict, jd_text: str = "") -> list[dict]:
    profile = {k: v for k, v in (profile_doc or {}).items() if k != "portal_login"}
    fields = []
    for r in rows:
        f = {"id": r.index, "question": r.label, "type": r.widget, "required": r.required}
        if r.options:
            f["options"] = [o.get("text") for o in r.options if o.get("text")]
        if r.widget == "checkbox":
            f["options"] = ["Yes", "No"]
        fields.append(f)
    user = (
        "Candidate profile:\n" + json.dumps(profile, default=str)[:12000]
        + "\n\nJob description:\n" + (jd_text or "(not available)")[:JD_LIMIT]
        + "\n\nFields:\n" + json.dumps(fields)
    )
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def _affirmative(row: Row, answer: dict) -> bool:
    value = answer.get("value")
    if answer["kind"] == "bool":
        return value is True
    if answer["kind"] == "option":
        return bool(re.search(r"\^yes", " ".join(value)))
    return bool(re.match(r"\s*yes\b", str(value), re.I))


def _evidence_in_profile(evidence: object, profile_text: str) -> bool:
    ev = re.sub(r"\s+", " ", str(evidence or "")).strip().lower()
    return len(ev) >= 8 and ev in profile_text


def parse_answers(reply: str, rows: list[Row], profile_text: str | None = None) -> dict[int, dict]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (reply or "").strip())
    try:
        items = json.loads(text).get("answers") or []
    except (json.JSONDecodeError, AttributeError):
        return {}
    by_index = {r.index: r for r in rows}
    out: dict[int, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        row = by_index.get(item.get("id"))
        if row is None:
            continue
        answer = answer_from_text(row, item.get("answer"))
        if not answer:
            continue
        if (profile_text is not None and ATTESTATION_RE.search(row.label or "") and _affirmative(row, answer)
                and not _evidence_in_profile(item.get("evidence"), profile_text)):
            continue  # an unbacked "Yes" to a qualification is not an answer
        out[row.index] = answer | {"source": "llm"}
    return out


class LLMResolver:
    def __init__(
        self,
        complete: Callable[[list[dict]], Awaitable[str]] | None,
        profile_doc: dict,
        jd_text: str = "",
        *,
        saved: SavedAnswers | None = None,
        per_job: Callable[[str, int], Awaitable[str | None]] | None = None,
        max_words: int = 120,
    ):
        self.complete = complete
        self.profile_doc = profile_doc
        self.jd_text = jd_text
        self.saved = saved
        self.per_job = per_job
        self.max_words = max_words
        self.calls = 0

    async def resolve(self, rows: list[Row], context: dict) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for_model: list[Row] = []
        for row in rows:
            per_job = is_per_job(row)
            stored = self.saved.get(row, per_job=per_job) if self.saved else None
            if stored and stored.get("source") == "llm" and ATTESTATION_RE.search(row.label or ""):
                # a model's old answer to a qualification question carries no evidence:
                # ask again under the evidence rule instead of replaying it
                stored = None
            answer = answer_from_text(row, (stored or {}).get("answer"))
            if answer:
                out[row.index] = answer | {"source": "saved"}
                continue
            if per_job:
                if self.per_job and row.widget in ("text", "textarea"):
                    written = await self.per_job(row.label, self.max_words)
                    answer = answer_from_text(row, written)
                    if answer:
                        out[row.index] = answer | {"source": "ask"}
                continue  # a generic model reply is not an answer to "why us"
            for_model.append(row)
        if for_model and self.complete:
            self.calls += 1
            try:
                reply = await self.complete(build_messages(for_model, self.profile_doc, self.jd_text))
            except Exception as exc:
                # a model endpoint that is down (the self-hosted one answers 404) is
                # a gap, not a crash: the fields stay unanswered and the run says why
                context["resolver_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
                return out
            profile_text = re.sub(r"\s+", " ", json.dumps(self.profile_doc, default=str, ensure_ascii=False)).lower()
            out.update(parse_answers(reply, for_model, profile_text))
        return out

    async def learned(self, row: Row, answer: dict, result: FillResult, context: dict) -> None:
        if not (self.saved and result.ok) or answer.get("source") not in ("llm", "ask"):
            return
        if answer.get("source") == "llm" and ATTESTATION_RE.search(row.label or ""):
            return  # qualification answers are re-derived from the profile every time
        text = result.wrote if answer["kind"] == "option" and result.wrote else answer["value"]
        if isinstance(text, bool):
            text = "Yes" if text else "No"
        self.saved.put(row, str(text), per_job=is_per_job(row), source="llm")

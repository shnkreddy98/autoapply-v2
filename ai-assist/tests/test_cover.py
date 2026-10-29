"""Cover letters: the agent's post-processing, the letter layout, and storing it next
to the job. LLM and LibreOffice stubbed; no DB."""

from datetime import date
from pathlib import Path

import pytest

import cover
from agent import AgentResult
from agents import CoverLetterAgent, cap_paragraphs
from common import keys
from models import CoverLetter

HEADER = {
    "full_name": "Jane Doe",
    "email": "jane@ex.dev",
    "phone": "(555) 010-0000",
    "linkedin": None,
    "location": "Austin, TX",
    "company": "Initech",
    "title": "Software Engineer",
}


def _stub_run(monkeypatch, paragraphs, captured=None):
    async def fake_run(self, query, max_iterations=50):
        if captured is not None:
            captured["query"] = query
        return AgentResult(output=CoverLetter(paragraphs=paragraphs))

    monkeypatch.setattr(CoverLetterAgent, "run", fake_run)


@pytest.mark.asyncio
async def test_agent_strips_dashes_and_reaches_the_prompt(monkeypatch):
    seen = {}
    _stub_run(monkeypatch, ["I built the pipeline—end to end.", "  ", "Second para."], seen)
    out = await CoverLetterAgent().write("RESUME_MARK", "JD_MARK", max_words=200)
    assert out == ["I built the pipeline, end to end.", "Second para."]
    assert "RESUME_MARK" in seen["query"] and "JD_MARK" in seen["query"]
    assert "at most 200 words" in seen["query"]


def test_cap_keeps_whole_paragraphs_and_drops_a_stub():
    p = " ".join(f"w{i}" for i in range(40)) + "."
    assert cap_paragraphs([p, p, p], 90) == [p, p]  # 10 words of room: a stub, dropped
    capped = cap_paragraphs([p, p, p], 100)  # 20 words of room: the third is cut
    assert len(capped) == 3 and len(" ".join(capped).split()) <= 100


def test_letter_text_comes_from_the_header_not_the_model():
    text = cover.letter_text(["Body one.", "Body two."], HEADER, date(2026, 10, 29))
    blocks = text.split("\n\n")
    assert blocks[0] == "Jane Doe"
    assert blocks[1] == "Austin, TX | (555) 010-0000 | jane@ex.dev"  # no empty linkedin
    assert blocks[2] == "October 29, 2026"
    assert blocks[3] == "Dear Initech Hiring Team,"
    assert blocks[-2:] == ["Sincerely,", "Jane Doe"]
    no_company = cover.letter_text(["x"], {**HEADER, "company": None}, date(2026, 10, 29))
    assert "Dear Hiring Manager," in no_company


def test_docx_holds_the_same_text(tmp_path):
    import docx

    path = cover.build_docx(["Body one."], HEADER, str(tmp_path / "c.docx"), date(2026, 10, 29))
    paras = [p.text for p in docx.Document(path).paragraphs]
    assert "\n\n".join(paras) == cover.letter_text(["Body one."], HEADER, date(2026, 10, 29))


class _Store:
    def __init__(self):
        self.objs = {}

    def put_file(self, key, path):
        self.objs[key] = Path(path).read_bytes()

    def put_bytes(self, key, data, content_type=None):
        self.objs[key] = data


@pytest.mark.asyncio
async def test_write_and_store_puts_pdf_and_txt_under_the_job(monkeypatch):
    import word

    async def fake_agent(resume, jd, max_words):
        return ["I shipped it."]

    async def fake_pdf(docx_path):
        pdf = Path(docx_path).with_suffix(".pdf")
        pdf.write_bytes(b"%PDF-1.7 cover")
        return str(pdf)

    monkeypatch.setattr(cover, "run_agent", fake_agent)
    monkeypatch.setattr(word, "convert_docx_to_pdf", fake_pdf)
    store = _Store()
    ctx = {"user_id": "u1", "job_id": "j1", "resume": "r", "jd": "jd",
           "base_label": "swe", "header": HEADER}

    out = await cover.write_and_store(store, ctx, max_words=100)

    assert out["pdf_key"] == keys.cover_letter_key("u1", "j1", "pdf")
    assert store.objs[out["pdf_key"]] == b"%PDF-1.7 cover"
    assert store.objs[out["txt_key"]].decode() == out["text"]
    assert "I shipped it." in out["text"] and out["base_label"] == "swe"
    assert out["word_count"] == 3


@pytest.mark.asyncio
async def test_failed_pdf_render_stores_nothing(monkeypatch):
    import word

    async def fake_agent(resume, jd, max_words):
        return ["I shipped it."]

    async def no_pdf(docx_path):
        return None

    monkeypatch.setattr(cover, "run_agent", fake_agent)
    monkeypatch.setattr(word, "convert_docx_to_pdf", no_pdf)
    store = _Store()
    ctx = {"user_id": "u1", "job_id": "j1", "resume": "r", "jd": "jd",
           "base_label": "swe", "header": HEADER}
    with pytest.raises(RuntimeError, match="docx->pdf failed"):
        await cover.write_and_store(store, ctx)
    assert store.objs == {}

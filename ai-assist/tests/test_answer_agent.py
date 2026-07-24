"""ApplicationQuestionAgent contract: whatever the model returns, the answer that
comes back has no em dashes and is within the word cap. LLM call stubbed."""

import pytest

from agent import AgentResult
from agents import ApplicationQuestionAgent
from models import QuestionAnswer
from textfmt import word_count


def _stub_run(monkeypatch, answer_text, captured=None):
    async def fake_run(self, query, max_iterations=50):
        if captured is not None:
            captured["query"] = query
        return AgentResult(output=QuestionAnswer(answer=answer_text))

    monkeypatch.setattr(ApplicationQuestionAgent, "run", fake_run)


@pytest.mark.asyncio
async def test_em_dashes_are_removed_even_when_the_model_ignores_the_rule(monkeypatch):
    _stub_run(monkeypatch, "I own the pipeline—all of it—end to end.")
    out = await ApplicationQuestionAgent().answer("resume", "jd", "Why you?")
    assert "—" not in out
    assert out == "I own the pipeline, all of it, end to end."


@pytest.mark.asyncio
async def test_overrun_is_trimmed_to_the_cap(monkeypatch):
    long_answer = " ".join(f"word{i}" for i in range(250)) + "."
    _stub_run(monkeypatch, long_answer)
    out = await ApplicationQuestionAgent().answer("resume", "jd", "Why you?")
    assert word_count(out) <= 100


@pytest.mark.asyncio
async def test_custom_word_cap_reaches_the_prompt_and_the_trim(monkeypatch):
    seen = {}
    _stub_run(monkeypatch, "One. Two. Three. Four. Five. Six.", seen)
    out = await ApplicationQuestionAgent().answer(
        "resume", "jd", "Why you?", max_words=3
    )
    assert "at most 3 words" in seen["query"]
    assert word_count(out) <= 3


@pytest.mark.asyncio
async def test_resume_jd_and_question_all_reach_the_prompt(monkeypatch):
    seen = {}
    _stub_run(monkeypatch, "Fine.", seen)
    await ApplicationQuestionAgent().answer("RESUME_MARK", "JD_MARK", "Q_MARK")
    assert "RESUME_MARK" in seen["query"]
    assert "JD_MARK" in seen["query"]
    assert "Q_MARK" in seen["query"]


@pytest.mark.asyncio
async def test_no_output_raises_with_the_underlying_error(monkeypatch):
    async def fake_run(self, query, max_iterations=50):
        return AgentResult(output=None, success=False, error="401 from upstream")

    monkeypatch.setattr(ApplicationQuestionAgent, "run", fake_run)
    with pytest.raises(RuntimeError, match="401 from upstream"):
        await ApplicationQuestionAgent().answer("resume", "jd", "Why you?")

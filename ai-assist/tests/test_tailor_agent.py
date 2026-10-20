"""ResumeTailorAgent edit budget: summary + TAILOR_BULLETS bullet swaps, counted only
when a replacement actually lands. No LLM, no DB — the replace tool is a fake."""

import pytest

from agents import ResumeTailorAgent
from prompts import system_prompt_tailor


class FakeDoc:
    """DocumentTools stand-in: replace() answers from a script, records calls."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.document = None

    async def replace(self, args):
        self.calls.append(args)
        return self.results.pop(0)


def test_prompt_asks_for_summary_plus_bullets():
    p = system_prompt_tailor(5)
    assert "exactly 6 targeted replacements" in p
    assert "5 least impactful bullet point(s)" in p
    assert "{EDITS}" not in p and "{BULLETS}" not in p
    assert "exactly 2 targeted replacements" in system_prompt_tailor(1)


@pytest.mark.asyncio
async def test_only_landed_replacements_count_and_extras_are_refused():
    ok = "Successfully replaced"
    miss = "ERROR: search_text not found in document. Make sure ..."
    doc = FakeDoc([ok, miss, ok, ok])
    agent = ResumeTailorAgent(doc, bullets=2)  # 3 edits: summary + 2 bullets
    replace = agent.tool_functions["replace"]

    for _ in range(3):
        await replace("args")
    assert agent._tool_choice() == "auto"  # the miss did not use up an edit
    await replace("args")
    assert agent._tool_choice() == "none"

    # a fourth call in the same response never reaches the document
    out = await replace("args")
    assert out.startswith("Refused")
    assert len(doc.calls) == 4

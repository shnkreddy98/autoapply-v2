"""Fix 1: completion guard + context-reset recovery in `agent.loop`.

Unit tests for the pure helpers, plus integration tests that drive `agent_loop`
with a fake OpenRouter client + fake MCP session (no network, no browser). These
fakes are the first LLM-loop scaffolding in the suite and are reused by later fixes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from mcp.types import TextContent

from agent import loop


# --------------------------------------------------------------------------- #
# unit: _looks_degenerate
# --------------------------------------------------------------------------- #


def test_looks_degenerate_repeated_token():
    assert loop._looks_degenerate("aminase" * 30000) is True


def test_looks_degenerate_normal_message():
    assert loop._looks_degenerate("I will click the Apply button now.") is False


def test_looks_degenerate_empty():
    assert loop._looks_degenerate("") is False


def test_looks_degenerate_long_but_varied():
    # A long, character-diverse blob is NOT degenerate.
    varied = "".join(chr(33 + (i % 90)) for i in range(20000))
    assert loop._looks_degenerate(varied) is False


def test_looks_degenerate_short_repetitive_under_threshold():
    # Repetitive but below the length threshold — not flagged.
    assert loop._looks_degenerate("ab" * 100) is False


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


def _resp(content: str, finish: str, *, cost: float = 0.0):
    """A minimal OpenAI-shaped chat completion with no tool calls."""
    message = SimpleNamespace(content=content, tool_calls=[])
    choice = SimpleNamespace(message=message, finish_reason=finish)
    usage = SimpleNamespace(
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        cost=cost,
        model_extra=None,
    )
    return SimpleNamespace(choices=[choice], usage=usage)


class FakeCompletions:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.requests = []

    async def create(self, **request):
        self.requests.append(request)
        return self._scripted.pop(0)


class FakeClient:
    def __init__(self, scripted):
        self.chat = SimpleNamespace(completions=FakeCompletions(scripted))


class FakeSession:
    """MCP session stub: no tools, snapshot returns fixed page text."""

    def __init__(self, snapshot="PAGE TEXT"):
        self.snapshot = snapshot
        self.snapshot_calls = 0

    async def list_tools(self):
        return SimpleNamespace(tools=[])

    async def call_tool(self, name, args):
        if name == "browser_snapshot":
            self.snapshot_calls += 1
            return SimpleNamespace(
                content=[TextContent(type="text", text=self.snapshot)]
            )
        raise AssertionError(f"unexpected tool {name!r}")


def _seed_messages():
    return [
        {"role": "system", "content": "SYSTEM"},
        {"role": "system", "content": "PROFILE"},
        {"role": "user", "content": "Apply for this job: http://x"},
    ]


async def _run(scripted, session=None):
    client = FakeClient(scripted)
    session = session or FakeSession()
    result = await loop.agent_loop(
        "fake-model",
        "task",
        _seed_messages(),
        session,
        client,
        loop._new_usage(),
        trace=None,
    )
    return result, client, session


# --------------------------------------------------------------------------- #
# unit: _current_page_text
# --------------------------------------------------------------------------- #


async def test_current_page_text_ok():
    session = FakeSession(snapshot="Country combobox")
    assert await loop._current_page_text(session) == "Country combobox"


async def test_current_page_text_degrades_on_error():
    class Boom(FakeSession):
        async def call_tool(self, name, args):
            raise RuntimeError("boom")

    out = await loop._current_page_text(Boom())
    assert out.startswith("(snapshot unavailable:")


# --------------------------------------------------------------------------- #
# ats instructions loader + Greenhouse typeahead guidance (Fix 3)
# --------------------------------------------------------------------------- #


def test_ats_instructions_load_and_typeahead_rule():
    text = loop.load_ats_instructions()
    assert text is not None
    # the Greenhouse typeahead rule must be present and use the keyboard-commit path
    assert "Typeahead" in text
    assert "ArrowDown" in text and "Enter" in text


# --------------------------------------------------------------------------- #
# integration: agent_loop completion guard
# --------------------------------------------------------------------------- #


async def test_clean_completion_returns():
    result, client, _ = await _run([_resp("All fields filled; stopping.", "stop")])
    assert result == "All fields filled; stopping."
    # bound is always applied to the outgoing request
    assert (
        client.chat.completions.requests[0]["max_tokens"] == loop.MAX_COMPLETION_TOKENS
    )


async def test_degenerate_then_clean_recovers(monkeypatch):
    monkeypatch.setattr(loop, "MAX_CONTEXT_RESETS", 2)
    scripted = [_resp("aminase" * 30000, "length"), _resp("Done.", "stop")]
    result, _, session = await _run(scripted)
    assert result == "Done."
    assert session.snapshot_calls == 1  # one context reset -> one snapshot


async def test_truncated_length_triggers_reset(monkeypatch):
    # innocuous content but finish_reason=length is a truncation, not a clean stop
    monkeypatch.setattr(loop, "MAX_CONTEXT_RESETS", 1)
    scripted = [_resp("partial thought", "length"), _resp("Done.", "stop")]
    result, _, session = await _run(scripted)
    assert result == "Done."
    assert session.snapshot_calls == 1


async def test_persistent_degeneration_raises(monkeypatch):
    monkeypatch.setattr(loop, "MAX_CONTEXT_RESETS", 2)
    scripted = [_resp("aminase" * 30000, "length")] * 3
    session = FakeSession()
    with pytest.raises(RuntimeError, match="degenerate/truncated completion after 2"):
        await _run(scripted, session=session)
    assert (
        session.snapshot_calls == 2
    )  # two resets, then give up (-> main: success=False)


# --------------------------------------------------------------------------- #
# history trim: old page text -> the model's SAW note
# --------------------------------------------------------------------------- #


def test_saw_note_parsing():
    assert loop._saw_note("SAW: step 2 of 3, email filled\nClick Next.") == "step 2 of 3, email filled"
    assert loop._saw_note("Thinking.\n- **SAW:** resume uploaded") == "resume uploaded"
    assert loop._saw_note("Click Next.") is None
    assert loop._saw_note(None) is None
    assert len(loop._saw_note("SAW: " + "x" * 1000)) == loop._SAW_MAX_CHARS


def _tool(call_id, content):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def test_trim_history_stubs_all_but_newest_with_note():
    big = "page text " * 50
    messages = _seed_messages() + [
        _tool("a", big + "A"),
        _tool("b", "ok"),  # short: never worth stubbing
        _tool("c", big + "C"),
        _tool("d", big + "D"),
    ]
    original_a = messages[3]
    loop._trim_history(messages, 2, {"a": "step 1, name filled"}, set())
    assert messages[3]["content"] == f"{loop._STUB_PREFIX} — you noted: step 1, name filled]"
    assert messages[3]["tool_call_id"] == "a"  # pairing with the tool_call kept
    assert original_a["content"].endswith("A")  # replaced, not mutated in place
    assert messages[4]["content"] == "ok"
    assert messages[5]["content"].endswith("C") and messages[6]["content"].endswith("D")
    # a second pass finds nothing new to stub
    before = list(messages)
    loop._trim_history(messages, 2, {}, set())
    assert messages == before


def test_trim_history_without_note_and_never_unread():
    big = "page text " * 50
    messages = _seed_messages() + [_tool(c, big) for c in ("a", "b", "c", "d")]
    # b, c, d came back in the turn that just ran: the model has not read them
    loop._trim_history(messages, 2, {}, {"b", "c", "d"})
    assert messages[3]["content"] == f"{loop._STUB_PREFIX}]"
    assert all(m["content"] == big for m in messages[4:])


def _tool_resp(content, *call_ids):
    calls = [
        SimpleNamespace(
            id=cid,
            function=SimpleNamespace(name="browser_click", arguments="{}"),
            model_dump=lambda exclude_none=True, cid=cid: {
                "id": cid,
                "type": "function",
                "function": {"name": "browser_click", "arguments": "{}"},
            },
        )
        for cid in call_ids
    ]
    resp = _resp(content, "tool_calls")
    resp.choices[0].message.tool_calls = calls
    return resp


class CopyingCompletions(FakeCompletions):
    """Records each request's messages as sent: the loop keeps editing the one list."""

    async def create(self, **request):
        self.requests.append([dict(m) for m in request["messages"]])
        return self._scripted.pop(0)


class PageSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.clicks = 0

    async def call_tool(self, name, args):
        self.clicks += 1
        return SimpleNamespace(content=[TextContent(type="text", text=f"PAGE{self.clicks} " * 100)])


async def test_agent_loop_sends_saw_note_in_place_of_old_pages(monkeypatch):
    monkeypatch.setattr(loop, "HISTORY_KEEP_SNAPSHOTS", 2)
    scripted = [
        _tool_resp("Open the form.", "c1"),
        _tool_resp("SAW: step 1 of 3, name empty\nFill the name.", "c2"),
        _tool_resp("SAW: step 2 of 3\nClick Next.", "c3"),
        _tool_resp("SAW: review page\nCheck it.", "c4"),
        _resp("Done.", "stop"),
    ]
    client = FakeClient([])
    client.chat.completions = CopyingCompletions(scripted)
    result = await loop.agent_loop(
        "fake-model", "task", _seed_messages(), PageSession(), client, loop._new_usage()
    )
    assert result == "Done."

    def tools(sent):
        return {m["tool_call_id"]: m["content"] for m in sent if m["role"] == "tool"}

    sent = client.chat.completions.requests
    third = tools(sent[2])  # c1, c2 are the newest two: both verbatim
    assert "PAGE1" in third["c1"] and "PAGE2" in third["c2"]
    fourth = tools(sent[3])
    assert fourth["c1"] == f"{loop._STUB_PREFIX} — you noted: step 1 of 3, name empty]"
    assert "PAGE2" in fourth["c2"] and "PAGE3" in fourth["c3"]
    last = tools(sent[4])
    assert last["c2"] == f"{loop._STUB_PREFIX} — you noted: step 2 of 3]"
    assert "PAGE3" in last["c3"] and "PAGE4" in last["c4"]

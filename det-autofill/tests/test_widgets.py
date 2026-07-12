"""Widget strategies against a scripted fake Browser — no live stack needed."""

import asyncio

from det_autofill.answers import Answer
from det_autofill.aria import FormField, parse_snapshot
from det_autofill.ats import ATSQuirks, detect
from det_autofill.widgets import (
    fill_checkbox,
    fill_combobox,
    fill_radio,
    fill_text,
)


class FakeBrowser:
    """Records calls; returns scripted snapshots in order."""

    def __init__(self, snapshots):
        self.calls = []
        self._snaps = list(snapshots)

    def _next(self):
        return self._snaps.pop(0) if self._snaps else ""

    async def type(self, ref, text, element=""):
        self.calls.append(("type", ref, text))
        return self._next()

    async def click(self, ref, element=""):
        self.calls.append(("click", ref))
        return self._next()

    async def press(self, key):
        self.calls.append(("press", key))
        return self._next()

    async def select_option(self, ref, values, element=""):
        self.calls.append(("select", ref, values))
        return self._next()

    async def upload(self, paths):
        self.calls.append(("upload", paths))
        return self._next()


def run(coro):
    return asyncio.run(coro)


def test_fill_text():
    br = FakeBrowser(["after"])
    f = FormField(ref="e1", role="textbox", label="First Name")
    res = run(fill_text(br, f, Answer("first_name", "text", "Jane")))
    assert res.ok and br.calls == [("type", "e1", "Jane")]


def test_checkbox_idempotent():
    br = FakeBrowser(["after"])
    f = FormField(ref="e2", role="checkbox", label="I agree", checked=True)
    res = run(fill_checkbox(br, f, Answer("agree_terms", "bool", True)))
    assert res.ok and res.note == "already-set" and br.calls == []


def test_radio_yes_no():
    br = FakeBrowser(["after"])
    ctx = "Are you legally authorized to work in the U.S.?"
    group = [
        FormField(ref="e3", role="radio", label="Yes", context=ctx),
        FormField(ref="e4", role="radio", label="No", context=ctx),
    ]
    res = run(fill_radio(br, group[0], Answer("work_authorized", "bool", True), group))
    assert res.ok and br.calls == [("click", "e3")]


def test_combobox_typeahead_sequence():
    done = '- combobox "Country" [ref=e9]: United States'
    br = FakeBrowser(["mid", "mid", done])
    f = FormField(ref="e9", role="combobox", label="Country")
    res = run(
        fill_combobox(
            br, f, Answer("country", "text", "United States"), ATSQuirks("greenhouse", combobox="typeahead")
        )
    )
    assert res.ok
    assert [c[0] for c in br.calls] == ["type", "press", "press"]
    assert br.calls[1] == ("press", "ArrowDown")
    assert br.calls[2] == ("press", "Enter")


def test_combobox_click_option():
    opened = (
        '- combobox "Veteran Status" [ref=e10] [expanded]\n'
        '- option "I am not a protected veteran" [ref=e11]\n'
        '- option "I identify as a veteran" [ref=e12]\n'
    )
    committed = '- combobox "Veteran Status" [ref=e10]: I am not a protected veteran'
    br = FakeBrowser([opened, committed])
    f = FormField(ref="e10", role="combobox", label="Veteran Status")
    res = run(
        fill_combobox(
            br,
            f,
            Answer("eeo_veteran", "option", [r"not a protected veteran|decline"]),
            ATSQuirks("workday", combobox="click"),
        )
    )
    assert res.ok, res.note
    assert br.calls == [("click", "e10"), ("click", "e11")]


def test_combobox_click_cascade():
    lvl1 = (
        '- combobox "How Did You Hear About Us?" [ref=e20] [expanded]\n'
        '- option "Career Websites" [ref=e21]\n'
    )
    lvl2 = (
        '- combobox "How Did You Hear About Us?" [ref=e20] [expanded]\n'
        '- option "Corporate Website" [ref=e22]\n'
    )
    done = '- combobox "How Did You Hear About Us?" [ref=e20]: 1 item selected'
    br = FakeBrowser([lvl1, lvl2, done])
    f = FormField(ref="e20", role="combobox", label="How Did You Hear About Us?")
    res = run(
        fill_combobox(
            br,
            f,
            Answer("how_heard", "option", [r"company|corporate website", r"career websites", r"job board"]),
            ATSQuirks("workday", combobox="click"),
        )
    )
    assert res.ok, res.note
    assert [c for c in br.calls] == [("click", "e20"), ("click", "e21"), ("click", "e22")]


def test_ats_detect():
    assert detect("https://job-boards.greenhouse.io/x/jobs/1").combobox == "typeahead"
    assert detect("https://foo.wd5.myworkdayjobs.com/x").name == "workday"
    assert detect("https://example.com/careers").name == "generic"


def test_parse_snapshot_yaml_fence_compat():
    # runner feeds parse_snapshot the unwrapped yaml body
    fields = parse_snapshot('- textbox "Email" [ref=e5]')
    assert fields[0].ref == "e5"

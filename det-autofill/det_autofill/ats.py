"""Per-ATS widget quirks — the tribal knowledge from 129 traces, as code.

Every entry here used to be a prompt instruction the LLM sometimes ignored.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ATSQuirks:
    name: str
    # How comboboxes commit a value:
    #   typeahead  -> type text, ArrowDown, Enter (options re-render per keystroke;
    #                 clicking option refs is a stale-ref trap)  [Greenhouse, Ashby]
    #   click      -> click to open, click the option ref; may CASCADE  [Workday]
    #   auto       -> try select_option, fall back to click, then typeahead
    combobox: str = "auto"
    # date textboxes want this strftime shape
    date_format: str = "%m/%d/%Y"
    # skip non-required Field of Study etc. (user rule, Workday lists lack the majors)
    skip_optional_field_of_study: bool = False


_KNOWN: list[tuple[tuple[str, ...], ATSQuirks]] = [
    (
        ("greenhouse.io",),
        ATSQuirks(name="greenhouse", combobox="typeahead"),
    ),
    (
        ("myworkdayjobs.com", "myworkday.com"),
        ATSQuirks(
            name="workday",
            combobox="click",
            skip_optional_field_of_study=True,
        ),
    ),
    (("ashbyhq.com",), ATSQuirks(name="ashby", combobox="typeahead")),
    (("lever.co",), ATSQuirks(name="lever", combobox="typeahead")),
    (("applytojob.com",), ATSQuirks(name="jazzhr", combobox="auto")),
    (("dayforcehcm.com",), ATSQuirks(name="dayforce", combobox="click")),
    (("adp.com",), ATSQuirks(name="adp", combobox="click")),
    (("taleo.net",), ATSQuirks(name="taleo", combobox="click")),
    (("oraclecloud.com",), ATSQuirks(name="oracle", combobox="click")),
    (("bamboohr.com",), ATSQuirks(name="bamboohr", combobox="auto")),
    (("smartrecruiters.com",), ATSQuirks(name="smartrecruiters", combobox="auto")),
]


def detect(url: str) -> ATSQuirks:
    u = url.lower()
    for hosts, quirks in _KNOWN:
        if any(h in u for h in hosts):
            return quirks
    return ATSQuirks(name="generic", combobox="auto")

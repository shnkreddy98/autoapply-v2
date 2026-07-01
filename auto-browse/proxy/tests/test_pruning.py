"""Unit tests for the proxy's ARIA -> visible-text pruning and the stale-ref hint.

These cover pure helpers only (no upstream Playwright session), so they run fast in
CI without a browser.
"""

from proxy.server import (
    _CONSOLE_NOISE_RE,
    _REF_GATED_TOOLS,
    _REF_INVALIDATING_TOOLS,
    _STALE_REF_HINT,
    _ref_gate,
    _refs_in_aria,
    _target_refs,
    _visible_text,
)


def test_keeps_visible_names_and_values():
    tree = (
        '- link "Skip to Content" [ref=e5]:\n'
        "    - /url: \"\"\n"
        '- textbox "Email Address" [ref=e70]\n'
        '- generic [ref=e10]: First Last\n'
    )
    assert _visible_text(tree).splitlines() == [
        "Skip to Content",
        "Email Address",
        "First Last",
    ]


def test_drops_url_and_structure_nodes():
    tree = (
        '- link "Apply" [ref=e1]:\n'
        '    - /url: "https://example.com/apply"\n'
        "- generic [ref=e2]:\n"  # pure structure, no name/value
    )
    assert _visible_text(tree) == "Apply"


def test_drops_consecutive_duplicates():
    tree = (
        '- generic [ref=e1]: Next\n'
        '- button "Next" [ref=e2]\n'
        '- generic [ref=e3]: Next\n'
    )
    # all three are "Next" in a row -> collapsed to a single line
    assert _visible_text(tree) == "Next"


def test_strips_console_noise_summary():
    # Playwright's page-state console summary leaks in as a bare value line; it is
    # noise the model otherwise misreads as a tool failure.
    tree = (
        '- generic [ref=e1]: "3 errors, 1 warnings"\n'
        '- heading "Candidate Info" [ref=e2]\n'
    )
    assert _visible_text(tree) == "Candidate Info"


def test_console_noise_regex():
    assert _CONSOLE_NOISE_RE.match("3 errors, 1 warnings")
    assert _CONSOLE_NOISE_RE.match("0 errors, 1 warnings")
    assert _CONSOLE_NOISE_RE.match("1 error, 0 warnings")
    # real page text must survive
    assert not _CONSOLE_NOISE_RE.match("Email Address")
    assert not _CONSOLE_NOISE_RE.match("12 errors found on submit")


def test_stale_ref_hint_covers_navigating_tools():
    # the tools whose result advances/re-renders the page must invalidate refs
    for tool in ("browser_click", "browser_navigate", "browser_type",
                 "browser_select_option", "browser_file_upload"):
        assert tool in _REF_INVALIDATING_TOOLS
    # read-only / ref-providing tools must NOT carry the hint
    assert "browser_snapshot" not in _REF_INVALIDATING_TOOLS
    assert "fetch_aria" not in _REF_INVALIDATING_TOOLS


def test_stale_ref_hint_text_is_actionable():
    assert "fetch_aria" in _STALE_REF_HINT
    assert "STALE" in _STALE_REF_HINT


# --- read-before-write ref gate --------------------------------------------- #

def test_refs_in_aria_extracts_refs():
    aria = (
        'Country: |-\n'
        '  - combobox "Country" [ref=e137]\n'
        '  - option "United States" [ref=e140]\n'
    )
    assert _refs_in_aria(aria) == {"e137", "e140"}


def test_refs_in_aria_none_when_ref_less():
    assert _refs_in_aria("Email: (no match)") == set()


def test_target_refs_bracket_and_hash_forms():
    assert _target_refs({"element": "Apply button", "ref": "button[ref=e76]"}) == {"e76"}
    assert _target_refs({"selector": "#e282"}) == {"e282"}


def test_target_refs_fill_form_multiple():
    args = {"fields": [
        {"target": "textbox[ref=e10]", "value": "a"},
        {"target": "#e11", "value": "b"},
    ]}
    assert _target_refs(args) == {"e10", "e11"}


def test_target_refs_text_selector_has_none():
    assert _target_refs({"selector": 'button:has-text("Apply")'}) == set()


def test_ref_gate_allows_when_target_was_read():
    assert _ref_gate("browser_click", {"ref": "button[ref=e76]"}, {"e76", "e5"}) is None


def test_ref_gate_blocks_unread_target():
    msg = _ref_gate("browser_click", {"ref": "button[ref=e76]"}, {"e5"})
    assert msg is not None
    assert "e76" in msg and "fetch_aria" in msg


def test_ref_gate_ignores_non_gated_tools():
    # navigate takes no ref -> never gated, even with an empty read set
    assert _ref_gate("browser_navigate", {"url": "http://x"}, set()) is None


def test_ref_gate_allows_ref_less_selector():
    assert _ref_gate("browser_click", {"selector": 'button:has-text("Apply")'}, set()) is None


def test_ref_gated_tools_membership():
    for tool in ("browser_click", "browser_type", "browser_fill_form",
                 "browser_select_option", "browser_file_upload"):
        assert tool in _REF_GATED_TOOLS
    assert "browser_navigate" not in _REF_GATED_TOOLS  # no ref target
    assert "fetch_aria" not in _REF_GATED_TOOLS

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from proxy.pruner import prune
from proxy.tree_parser import collect_all, parse

FIXTURE = Path(__file__).parent / "fixtures" / "wikipedia_snapshot.txt"


def _load_fixture() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _refs_in(text: str) -> set[str]:
    return set(re.findall(r"\[ref=([^\]]+)\]", text))


# --- Test 1: fixture-based search ---


def test_search_button_refs_present():
    raw = _load_fixture()
    result = prune(raw, "click the search button")
    assert "[ref=e23]" in result, "searchbox ref e23 should be in pruned output"
    assert "[ref=e25]" in result, "search button ref e25 should be in pruned output"


def test_search_button_line_count():
    raw = _load_fixture()
    result = prune(raw, "click the search button")
    lines = [line for line in result.splitlines() if line.strip()]
    assert len(lines) < 30, f"Expected < 30 lines, got {len(lines)}"


# --- Test 2: synonym / literal match ---


def test_donate_link_found():
    raw = _load_fixture()
    result = prune(raw, "find the donate option")
    assert "[ref=e32]" in result, "Donate link ref e32 should be in pruned output"


# --- Test 3: quoted value excluded from keyword matching ---


def test_quoted_value_not_used_as_keyword():
    raw = _load_fixture()
    # "machine learning" is NOT on the page; keywords should be {type, search, ...}
    # The result should still find search-related nodes, not zero
    result = prune(raw, "type 'machine learning' into search")
    # must include the searchbox (search keyword should match it)
    assert "[ref=e23]" in result or "searchbox" in result


# --- Test 4: zero-match fallback ---


_BARE_TREE = """- generic [ref=e1]:
  - banner [ref=e2]:
    - heading "Welcome" [level=1] [ref=e3]
  - main [ref=e4]:
    - paragraph [ref=e5]: Some descriptive text on the page.
  - contentinfo [ref=e6]:
    - link "Privacy" [ref=e7]"""


def test_zero_match_returns_landmarks_only():
    # No keyword match AND nothing to always-surface (no entry/action/error/
    # required nodes) -> landmarks-only fallback. (The Wikipedia fixture now
    # surfaces its Create account / Log in entry links, so use a bare tree.)
    result = prune(_BARE_TREE, "xyzzy nonsense gibberish foobarqux")
    assert "landmarks_only" in result
    assert "[pruned:" in result


# --- Test 5: ref integrity ---


def test_ref_integrity():
    raw = _load_fixture()
    result = prune(raw, "click the search button")
    pruned_refs = _refs_in(result)
    raw_refs = _refs_in(raw)
    stale = pruned_refs - raw_refs
    assert not stale, f"Pruned output contains refs not in raw input: {stale}"


# --- Test 6: ancestor context ---


def test_ancestor_context_for_deep_match():
    raw = _load_fixture()
    # searchbox is nested under: generic > generic > search > generic > searchbox
    result = prune(raw, "click the search button")
    # The search landmark ancestor should appear
    assert "search" in result
    # The ancestor chain should be present (search [ref=e17] is an ancestor)
    assert "[ref=e17]" in result


# --- Additional: pruned header present ---


def test_pruned_header_present():
    raw = _load_fixture()
    result = prune(raw, "click the search button")
    assert result.startswith("[pruned:")


# --- Parser sanity ---


def test_parse_wikipedia():
    raw = _load_fixture()
    root = parse(raw)
    assert root is not None
    all_nodes = collect_all(root)
    refs = {n.ref for n in all_nodes if n.ref}
    assert "e1" in refs
    assert "e23" in refs
    assert "e25" in refs
    assert "e32" in refs


def test_landmark_tagging():
    raw = _load_fixture()
    root = parse(raw)
    all_nodes = collect_all(root)
    search_node = next((n for n in all_nodes if n.ref == "e17"), None)
    assert search_node is not None
    assert search_node.role == "search"

    searchbox = next((n for n in all_nodes if n.ref == "e23"), None)
    assert searchbox is not None
    # searchbox is inside the search landmark
    assert searchbox.landmark == "search"


_FORM_TREE = """- generic [ref=e1]:
  - main [ref=e2]:
    - generic [ref=e3]:
      - generic [ref=e4]: Email*
      - textbox "Email" [ref=e5]
    - button "Save and Continue" [ref=e9]
  - contentinfo [ref=e20]:
    - link "Facebook" [ref=e21]
    - button "Back to top" [ref=e23]"""


def test_action_button_always_surfaced():
    # Goal does not mention the button; it must still appear.
    out = prune(_FORM_TREE, "fill the email field")
    assert "e9" in out  # Save and Continue button
    assert "e5" in out  # the matched email field


def test_footer_demoted():
    out = prune(_FORM_TREE, "fill the email field")
    assert "e21" not in out  # footer social link excluded
    assert "e23" not in out  # footer "Back to top" button excluded (contentinfo)


def test_action_button_surfaced_on_zero_keyword_match():
    # Nonsense goal: still surface the action button instead of landmarks-only.
    out = prune(_FORM_TREE, "xyzzy gibberish")
    assert "e9" in out
    assert "landmarks_only" not in out


# --- always-surface: errors, required, options, entry actions ---

_QUESTIONS_TREE = """- generic [ref=e1]:
  - main [ref=e2]:
    - region "Questions" [ref=e3]:
      - combobox "*How did you hear about this opportunity?" [ref=e4]:
        - option "LinkedIn"
        - option "Company Website"
        - option "Indeed"
        - generic [ref=e8]:
          - img "Error" [ref=e9]
      - group "*Are you at least 18 years of age?" [ref=e10]:
        - radio "Yes" [ref=e11]
        - radio "No" [ref=e12]
        - img "Error" [ref=e13]
    - button "Save and Continue" [ref=e20]
  - contentinfo [ref=e30]:
    - link "Terms" [ref=e31]"""


def test_error_icons_surfaced_regardless_of_goal():
    # Goal mentions nothing about errors, yet the img "Error" nodes must appear.
    out = prune(_QUESTIONS_TREE, "save and continue")
    assert "e9" in out and "e13" in out
    assert 'img "Error"' in out


def test_required_questions_surfaced_regardless_of_goal():
    out = prune(_QUESTIONS_TREE, "save and continue")
    assert "e4" in out  # *How did you hear combobox
    assert "e10" in out  # *Are you 18 group


def test_choice_options_included_for_kept_container():
    out = prune(_QUESTIONS_TREE, "how did you hear")
    # the combobox options and the Yes/No radios are revealed
    assert "LinkedIn" in out and "Company Website" in out
    assert "Yes" in out and "No" in out


_ENTRY_TREE = """- generic [ref=e1]:
  - main [ref=e2]:
    - heading "Senior Data Engineer" [level=1] [ref=e3]
    - paragraph [ref=e4]: A long job description goes here.
    - button "Apply with Resume" [ref=e5]
    - button "Sign In" [ref=e6]
  - contentinfo [ref=e30]:
    - link "Privacy" [ref=e31]"""


def test_entry_actions_surfaced_regardless_of_goal():
    out = prune(_ENTRY_TREE, "xyzzy gibberish")
    assert "e5" in out and "e6" in out  # Apply with Resume + Sign In
    assert "landmarks_only" not in out


def test_overview_returns_fields_and_omits_footer():
    from proxy.pruner import overview

    out = overview(_QUESTIONS_TREE)
    assert "overview" in out
    assert "e4" in out and "e10" in out and "e20" in out  # fields + action button
    assert "e31" not in out  # footer link demoted


def test_overview_landing_drops_description_noise():
    from proxy.pruner import overview

    out = overview(_ENTRY_TREE)
    assert "e5" in out and "e6" in out  # entry actions present
    assert "job description" not in out.lower()  # paragraph text not surfaced

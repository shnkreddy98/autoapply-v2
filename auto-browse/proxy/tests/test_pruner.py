import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from proxy.pruner import prune
from proxy.tree_parser import parse, collect_all

FIXTURE = Path(__file__).parent / "fixtures" / "wikipedia_snapshot.txt"


def _load_fixture() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _refs_in(text: str) -> set[str]:
    return set(re.findall(r'\[ref=([^\]]+)\]', text))


# --- Test 1: fixture-based search ---

def test_search_button_refs_present():
    raw = _load_fixture()
    result = prune(raw, "click the search button")
    assert "[ref=e23]" in result, "searchbox ref e23 should be in pruned output"
    assert "[ref=e25]" in result, "search button ref e25 should be in pruned output"


def test_search_button_line_count():
    raw = _load_fixture()
    result = prune(raw, "click the search button")
    lines = [l for l in result.splitlines() if l.strip()]
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

def test_zero_match_returns_landmarks_only():
    raw = _load_fixture()
    result = prune(raw, "xyzzy nonsense gibberish foobarqux")
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

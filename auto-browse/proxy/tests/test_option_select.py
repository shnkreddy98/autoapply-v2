"""Unit tests for the pure dropdown-matching helpers (no browser)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from proxy.option_select import (
    best_match,
    collect_options,
    find_ref_by_label,
    find_searchbox,
    label_of_ref,
)

# An opened ARIA combobox: options carry refs (unlike a native <select>). A decoy
# option-role node sits OUTSIDE the listbox to check scoping.
OPENED = """- generic [ref=e1]:
  - combobox "Field of Study" [ref=e10]
  - listbox [ref=e20]:
    - option "Computer Science" [ref=e21]
    - option "Computer Engineering" [ref=e22]
    - option "Information Systems" [ref=e23]
  - option "Decoy Outside Listbox" [ref=e99]
"""

TYPEAHEAD = """- combobox "Technologies" [ref=e10]:
  - searchbox "Search" [ref=e11]
  - listbox [ref=e20]:
    - option "Python" [ref=e21]
    - option "Apache Spark" [ref=e22]
    - option "Snowflake" [ref=e23]
"""


def test_collect_options_scoped_to_listbox():
    opts = collect_options(OPENED)
    labels = [o.label for o in opts]
    assert labels == ["Computer Science", "Computer Engineering", "Information Systems"]
    assert "Decoy Outside Listbox" not in labels  # outside the listbox => excluded
    assert opts[0].ref == "e21"


def test_collect_options_empty_tree():
    assert collect_options("") == []
    assert collect_options("not a tree") == []


def test_best_match_exact():
    opts = collect_options(OPENED)
    m = best_match("Computer Science", opts)
    assert m is not None and m.ref == "e21"


def test_best_match_case_insensitive():
    opts = collect_options(OPENED)
    m = best_match("computer science", opts)
    assert m is not None and m.ref == "e21"


def test_best_match_substring():
    opts = collect_options(TYPEAHEAD)
    # "Spark" is a substring of "Apache Spark"
    m = best_match("Spark", opts)
    assert m is not None and m.ref == "e22"


def test_best_match_no_confident_match_returns_none():
    opts = collect_options(OPENED)
    assert best_match("Astrophysics", opts) is None


def test_best_match_empty_options():
    assert best_match("Anything", []) is None


def test_find_searchbox_typeahead():
    assert find_searchbox(TYPEAHEAD) == "e11"


def test_find_searchbox_absent():
    assert find_searchbox(OPENED) is None  # no textbox/searchbox in a plain listbox


def test_label_of_ref():
    assert label_of_ref(OPENED, "e10") == "Field of Study"
    assert label_of_ref(OPENED, "nope") is None


def test_find_ref_by_label_relocates_control():
    # After a pick re-renders the tree, the dropdown is re-found by its label.
    assert find_ref_by_label(OPENED, "Field of Study") == "e10"
    assert find_ref_by_label(OPENED, "Totally Unrelated Label Xyz") is None

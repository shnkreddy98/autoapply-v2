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


_PILLS_AND_MENU = (
    "- generic [ref=e1]:\n"
    '  - listbox "items selected" [ref=e271]:\n'
    '    - option "United States of America (+1), press delete to clear value." [ref=e272]\n'
    '  - listbox "Options Expanded" [ref=e640]:\n'
    '    - option "Afghanistan (+93)" [ref=e662]\n'
    '    - option "United States of America (+1)" [ref=e813]\n'
)


def test_collect_options_excludes_selected_pills():
    # A Workday selected-values tray (listbox "items selected") holds "…press delete
    # to clear value." pills — state, not choices. They must never be offered as
    # options (else a closed widget looks open and values match the pill).
    pills_only = (
        "- generic [ref=e1]:\n"
        '  - listbox "items selected" [ref=e271]:\n'
        '    - option "United States of America (+1), press delete to clear value." [ref=e272]\n'
    )
    assert collect_options(pills_only) == []

    # With a real menu open alongside the tray, only the menu's options surface.
    opts = collect_options(_PILLS_AND_MENU)
    labels = [o.label for o in opts]
    assert labels == ["Afghanistan (+93)", "United States of America (+1)"]
    assert best_match("United States of America (+1)", opts).ref == "e813"  # not the pill


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

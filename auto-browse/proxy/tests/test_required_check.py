import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from proxy.required_check import (
    INCOMPLETE_MARKER,
    format_required_check,
    required_status,
)

# A required combobox (native select) that IS answered, plus a required radio
# group and required textboxes that are NOT.
_TREE = """- generic [ref=e1]:
  - main [ref=e2]:
    - textbox "*First Name" [ref=e3]: Shashank
    - textbox "*Last Name" [ref=e4]:
    - combobox "*How did you hear about this opportunity?" [ref=e5]:
      - option "Choose..."
      - option "LinkedIn" [selected]
      - option "Indeed"
    - group "*Are you at least 18 years of age?" [ref=e6]:
      - radio "Yes" [ref=e7]
      - radio "No" [ref=e8]
    - combobox "*State" [ref=e9]:
      - option "Choose..." [selected]
      - option "California"
    - button "Save and Continue" [ref=e20]"""


def test_filled_vs_empty():
    filled, empty = required_status(_TREE)
    filled_names = {n.name for n in filled}
    empty_names = {n.name for n in empty}
    # First Name (value) + combobox with LinkedIn selected => filled
    assert "*First Name" in filled_names
    assert "*How did you hear about this opportunity?" in filled_names
    # Last Name (bare), the radio group (no [checked]), the placeholder-only
    # State select => empty
    assert "*Last Name" in empty_names
    assert "*Are you at least 18 years of age?" in empty_names
    assert "*State" in empty_names  # only "Choose..." selected => placeholder


def test_radio_checked_counts_as_filled():
    tree = _TREE.replace('radio "Yes" [ref=e7]', 'radio "Yes" [ref=e7] [checked]')
    filled, empty = required_status(tree)
    assert "*Are you at least 18 years of age?" in {n.name for n in filled}


def test_format_lists_empty_with_marker_and_refs():
    out = format_required_check(_TREE)
    low = out.lower()
    assert INCOMPLETE_MARKER in low
    assert "e6" in out  # the empty radio group's ref
    assert "click the yes/no radio ref" in low  # hint for radio groups
    # the filled fields are not listed
    assert "*First Name" not in out


def test_all_filled_message():
    tree = """- generic [ref=e1]:
  - main [ref=e2]:
    - textbox "*First Name" [ref=e3]: Shashank
    - combobox "*State" [ref=e9]:
      - option "California" [selected]"""
    out = format_required_check(tree)
    assert "all 2 required field(s) filled" in out
    assert INCOMPLETE_MARKER not in out.lower()


def test_no_required_fields_returns_empty_string():
    tree = """- generic [ref=e1]:
  - main [ref=e2]:
    - textbox "First Name" [ref=e3]:
    - button "Apply" [ref=e4]"""
    assert format_required_check(tree) == ""

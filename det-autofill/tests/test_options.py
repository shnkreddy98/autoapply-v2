from det_autofill.options import Choice as C
from det_autofill.options import match_option


def test_placeholders_are_skipped_but_indices_stay_the_callers():
    opts = [C("", "Select..."), C("California", "California"), C("Colorado", "Colorado")]
    assert match_option("state", "CA", opts).index == 1


def test_country_code_and_name_are_the_same_answer():
    opts = [C("", "--"), C("CA", "Canada"), C("US", "United States")]
    assert match_option("country", "United States", opts).index == 2


def test_booleans_use_yes_no_wording():
    opts = [C("", "Select one"), C("1", "Yes, I will require sponsorship"), C("0", "No, I will not")]
    assert match_option("visa_sponsorship", False, opts).index == 2


def test_two_plausible_options_is_a_miss():
    opts = [C("a", "Master's Degree"), C("b", "Master of Business Administration")]
    pick = match_option("degree", "Master", opts)
    assert pick.index is None and pick.how.startswith("ambiguous")


def test_preference_patterns_in_order_and_exact_word_wins():
    opts = [C("1", "No"), C("2", "No, I do not wish to answer"), C("3", "Yes")]
    assert match_option("eeo_hispanic", None, opts, [r"^no\b"]).index == 0


def test_no_value_and_no_prefs_picks_nothing():
    assert match_option("eeo_race", None, [C("1", "Asian")]).index is None

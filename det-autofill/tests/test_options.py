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


# Degree options as listed on a live Greenhouse form (2026-09-13).
GREENHOUSE_DEGREES = [
    "Associate's Degree", "Bachelor's Degree", "Doctor of Medicine (M.D.)", "Doctor of Philosophy (Ph.D.)",
    "Engineer's Degree", "High School", "Juris Doctor (J.D.)", "Master of Business Administration (M.B.A.)",
    "Master's Degree", "Other",
]


def _degree(value):
    pick = match_option("degree", value, [C("", t) for t in GREENHOUSE_DEGREES])
    return GREENHOUSE_DEGREES[pick.index] if pick.index is not None else pick.how


def test_resume_degree_names_map_to_form_levels():
    assert _degree("Master's Degree") == "Master's Degree"
    assert _degree("Bachelor of Engineering") == "Bachelor's Degree"
    assert _degree("M.S. in Data Analytics") == "Master's Degree"
    assert _degree("MBA") == "Master of Business Administration (M.B.A.)"
    assert _degree("Doctor of Philosophy") == "Doctor of Philosophy (Ph.D.)"


def test_master_prefix_alone_is_still_ambiguous():
    assert _degree("Master").startswith("ambiguous")


def test_city_state_expands_for_location_pickers():
    opts = [C("", "Springfield, Illinois, United States"), C("", "Springfield, Missouri, United States"),
            C("", "Springfield, Massachusetts, United States")]
    assert match_option("location_city", "Springfield, IL", opts).index == 0
    assert match_option("city", "Springfield", opts).index is None  # a bare city is ambiguous


def test_identical_duplicate_options_are_not_ambiguous():
    """Live Greenhouse location results list the same wording twice."""
    opts = [C("", "Springfield, Illinois, United States"), C("", "Springfield, Illinois, United States"),
            C("", "East Springfield, Illinois, United States")]
    pick = match_option("location_city", "Springfield, IL", opts)
    assert pick.index == 0 and "identical" in pick.how

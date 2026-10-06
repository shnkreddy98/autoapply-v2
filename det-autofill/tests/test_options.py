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


def test_a_shared_dial_code_picks_the_united_states():
    from det_autofill.options import Choice, match_option

    opts = [Choice(value=str(i), text=t) for i, t in enumerate(["🇨🇦 +1 Canada", "🇺🇸 +1 United States", "🇺🇲 +1 United States Minor Outlying Islands", "🇯🇲 +1 Jamaica", "🇬🇧 +44 United Kingdom"])]
    assert match_option("phone_country_code", "+1", opts).index == 1


def test_a_trailing_dial_code_matches_workdays_wording():
    # Workday's search for "United States" returns only this option
    assert match_option("phone_country_code", "+1", [C("", "United States of America (+1)")]).index == 0
    opts = [C("", t) for t in ["Canada (+1)", "India (+91)", "United States Minor Outlying Islands (+1)",
                               "United States of America (+1)"]]
    assert match_option("phone_country_code", "+1", opts).index == 3
    assert match_option("phone_country_code", "+91", opts).index == 1
    # "+1" must not match "+91" or "+12"
    assert match_option("phone_country_code", "+1", [C("", "India (+91)"), C("", "X (+12)")]).index is None


def test_a_number_picks_the_range_holding_it():
    opts = [C("", t) for t in ["0-2 years", "3-5 years", "6-9 years", "10+ years"]]
    assert match_option("years_experience", 7, opts).index == 2
    assert match_option("years_experience", "12", opts).index == 3
    assert match_option("years_experience", 7, [C("", "Less than 1 year"), C("", "1 to 5 years"),
                                                C("", "More than 5 years")]).index == 2
    assert match_option("desired_salary", 165000, [C("", "$100,000 - $150,000"), C("", "$150,000 - $200,000")]).index == 1
    # overlapping ranges are two plausible answers: a miss
    assert match_option("years_experience", 5, [C("", "3-5 years"), C("", "5-10 years")]).index is None
    # an exact option still wins over a range
    assert match_option("years_experience", 7, [C("", "7"), C("", "5-10")]).index == 0
    # leading digits are not a match: 1 is not "10+ years"
    assert match_option("years_experience", 1, [C("", "0-2 years"), C("", "10+ years")]).index == 0


def test_a_race_list_naming_hispanic_on_every_line_is_not_the_hispanic_question():
    from det_autofill.options import hispanic_only

    workday = ["Asian (Not Hispanic or Latino)", "White (Not Hispanic or Latino)", "Hispanic or Latino", "I do not wish to answer"]
    assert not hispanic_only([{"text": t} for t in workday])
    assert hispanic_only([{"text": t} for t in ["Hispanic or Latino", "Not Hispanic or Latino", "I decline to self-identify"]])


def test_both_bounds_of_a_worded_range_count():
    taleo = ["Less than 1 year", "1 or more years but less than 3 years", "3 or more years but less than 5 years",
             "5 or more years but less than 7 years", "7 or more years but less than 10 years", "10 or more years",
             "No experience"]
    opts = [C("", t) for t in taleo]
    assert match_option("skill_years", 7, opts).index == 4
    assert match_option("skill_years", 0.5, opts).index == 0
    assert match_option("skill_years", 12, opts).index == 5
    assert match_option("skill_years", 2, [C("", "Up to 2 years"), C("", "At least 3 years")]).index == 0


def test_notice_period_takes_the_shortest_option_that_covers_it():
    # Chainlink: Immediately available / 1 month / 2 months / 3 months or more, profile "2 weeks"
    opts = [C(o, o) for o in ["Immediately available", "1 month", "2 months", "3 months or more"]]
    assert opts[match_option("notice_period", "2 weeks", opts).index].text == "1 month"
    assert match_option("notice_period", "4 months", opts[:3]).index is None

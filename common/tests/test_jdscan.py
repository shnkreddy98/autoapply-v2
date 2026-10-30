"""common.jdscan: work-authorization limits and pay bands read out of JD text. Pure
functions: no DB, no network. The phrasings are taken from captured JDs."""

import pytest

from common import jdscan


@pytest.mark.parametrize(
    "text, kind",
    [
        ("Please note: we are unable to sponsor or take over sponsorship of an employment visa at this time.", "no_sponsorship"),
        ("This role is not eligible for visa sponsorship.", "no_sponsorship"),
        ("Applicants must be authorized to work in the U.S. without sponsorship now or in the future.", "no_sponsorship"),
        ("Visa sponsorship is not available for this position", "no_sponsorship"),
        ("No Visa Sponsorship", "no_sponsorship"),
        ("IBM will not be providing visa sponsorship for this position now or in the future.", "no_sponsorship"),
        ("Hallmark will hire only individuals lawfully authorized to work in the United States. "
         "Hallmark does not generally provide sponsorship for employment.", "no_sponsorship"),
        ("Must be a U.S. Citizen and reside in the United States.", "citizen"),
        ("To conform to ITAR you must be a U.S. citizen, lawful permanent resident of the U.S., or protected individual.", "citizen"),
        ("Applicants must be U.S. Citizens or U.S. Permanent Residents (Green Card holders).", "citizen"),
        ("U.S. citizenship is required, due to program requirements", "citizen"),
        ("This position requires an Active TS/SCI Clearance", "clearance"),
        ("Ability to obtain a Secret security clearance required", "clearance"),
    ],
)
def test_limits_the_posting_states(text, kind):
    assert jdscan.scan(text).work_auth == kind


@pytest.mark.parametrize(
    "text",
    [
        # the application form is captured with the page; sponsoring employers ask too
        "Will you now or in the future require sponsorship for employment visa status?*",
        "Are you a U.S. Citizen?",
        "Do you have an active security clearance?",
        # EEO boilerplate and form options restrict nobody
        "We hire without regard to race, national origin, citizenship status, or disability.",
        "I am a U.S. Citizen/Permanent Resident",
        "Active Security Clearance Agency/Department",
        # authorized to work is enough for an OPT holder
        "Must be a U.S. citizen or have authorization to work in the United States as defined by IRCA",
        "Candidates eligible to renew employment authorization without employer sponsorship, such as DACA, are welcome.",
        "If you have ongoing authorization to work in the United States without employer sponsorship, answer No.",
        # conditional: only H-1B cases that trigger the fee; OPT change of status does not
        "Inari provides immigration sponsorship for candidates in certain qualified roles. "
        "However, the company will not provide sponsorship to H-1B candidates whose employment "
        "would require payment of the $100,000 fee mandated by the proclamation.",
        # a loan ad on the careers page
        "To qualify, a borrower must be a U.S. citizen or other eligible status.",
        # a perk, not a visa statement
        "Don't miss our company-sponsored team events.",
        "Must be authorized to work in the United States.",
    ],
)
def test_noise_is_not_a_limit(text):
    assert jdscan.scan(text).work_auth is None


def test_an_explicit_yes_outweighs_a_partial_no():
    text = "We do sponsor visas! However, we aren't able to successfully sponsor visas for every role."
    assert jdscan.scan(text).work_auth is None


def test_strongest_limit_wins_and_the_note_shows_the_words():
    text = (
        "We are unable to provide sponsorship for this role.\n"
        "Must be a U.S. Citizen holding an active Secret Security Clearance."
    )
    r = jdscan.scan(text)
    assert r.work_auth == "clearance"
    assert "active Secret Security Clearance" in r.work_auth_note


def test_note_is_the_window_around_the_hit_not_the_paragraph_start():
    text = "About us. " + "We build things. " * 30 + "Visa sponsorship is not available for this position."
    note = jdscan.scan(text).work_auth_note
    assert note.endswith("not available for this position.")
    assert len(note) < 250


@pytest.mark.parametrize(
    "text, band",
    [
        ("The base pay range for this position is $119,300 - $145,900 per year.", (119300, 145900, "year")),
        ("base salary range for this role is USD $171,000 per year - USD $190,000 per year.", (171000, 190000, "year")),
        ("$150k-170k", (150000, 170000, "year")),
        ("$115-120k", (115000, 120000, "year")),
        ("salary rate for this role is currently $79300-113000", (79300, 113000, "year")),
        ("US employees: $166,600 &mdash; $208,300 USD", (166600, 208300, "year")),
        ("$75,000–85,000K USD Annually", (75000, 85000, "year")),
        ("range is $86.500,00 to $ 134.400,00 USD per year", (86500, 134400, "year")),
        ("$45 - $55/hr", (45, 55, "hour")),
        ("$34.50 - $57.00", (34, 57, "hour")),
        ("USD $19,57 - $35,02", (20, 35, "hour")),
        ("$6,513.00 - $10,537.00 per Month", (6513, 10537, "month")),
        ("USD $92,800.00/Yr.", (92800, 92800, "year")),
        ("Salary: $111,300", (111300, 111300, "year")),
        ("Salary Range: $200kApplication Deadline: 11/30/26", (200000, 200000, "year")),
        ("base pay range for this role is $\n117,610.00", (117610, 117610, "year")),
    ],
)
def test_pay_bands(text, band):
    r = jdscan.scan(text)
    assert (r.salary_min, r.salary_max, r.salary_period) == band
    assert r.salary


@pytest.mark.parametrize(
    "text",
    [
        "Medical plans with options with a $0 paycheck contribution",
        "Ericsson will match $1 for every $1 you put into the 401(k).",
        "a $2500 per year learning stipend",
        "USD $150 per month for internet",
        "We raised $100 million in our Series C.",
        "Does our posted annual salary of $100,000-$150,000 meet your expectations?",
    ],
)
def test_benefits_and_funding_are_not_pay(text):
    assert jdscan.scan(text).salary is None


def test_not_for_you_needs_both_a_limit_and_a_sponsorship_need():
    assert jdscan.not_for_you("no_sponsorship", True)
    assert jdscan.not_for_you("citizen", True)
    assert not jdscan.not_for_you("no_sponsorship", False)
    assert not jdscan.not_for_you("no_sponsorship", None)
    assert not jdscan.not_for_you(None, True)


@pytest.mark.parametrize(
    "desired, band, out",
    [
        ("140000", (100000, 125000, "year"), "125000"),  # above the top: the top
        ("90000", (100000, 125000, "year"), "100000"),  # below the floor: the floor
        ("110000", (100000, 125000, "year"), "110000"),  # inside: unchanged
        ("$140,000", (100000, 125000, "year"), "125000"),
        ("140000", (45, 55, "hour"), "140000"),  # an hourly band says nothing of a yearly ask
        ("140000", (None, None, None), "140000"),  # no band posted
        ("open", (100000, 125000, "year"), "open"),  # not a number: left as written
        (None, (100000, 125000, "year"), None),
    ],
)
def test_clamp_salary(desired, band, out):
    assert jdscan.clamp_salary(desired, *band) == out

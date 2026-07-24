"""Enforcement of the two hard output rules: no em dashes, and the word cap.
Pure string tests, no DB, no LLM, no API key."""

from textfmt import cap_words, clean_answer, strip_dashes, word_count


def test_em_dash_becomes_comma():
    assert strip_dashes("I led the team—six engineers—for two years") == (
        "I led the team, six engineers, for two years"
    )


def test_en_dash_and_ascii_double_hyphen():
    assert "–" not in strip_dashes("scaled 10–20x")
    assert strip_dashes("built it -- shipped it") == "built it, shipped it"


def test_hyphenated_words_survive():
    assert strip_dashes("real-time end-to-end pipelines") == (
        "real-time end-to-end pipelines"
    )


def test_no_double_comma_when_dash_follows_comma():
    assert strip_dashes("Python, Go — and Rust") == "Python, Go, and Rust"


def test_under_cap_is_untouched():
    text = "I built the ingestion pipeline."
    assert cap_words(text, 100) == text


def test_cap_trims_to_last_whole_sentence():
    text = "One two three. Four five six. Seven eight nine."
    assert cap_words(text, 7) == "One two three. Four five six."


def test_cap_hard_cuts_when_first_sentence_overruns():
    out = cap_words("one two three four five six seven eight", 4)
    assert word_count(out) == 4
    assert out.endswith(".")


def test_clean_answer_applies_both_rules():
    out = clean_answer("I shipped it—fast. Then I scaled it. Then I documented it.", 5)
    assert "—" not in out
    assert word_count(out) <= 5

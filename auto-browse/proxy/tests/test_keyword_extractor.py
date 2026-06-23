import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from proxy.keyword_extractor import extract


def test_stopword_stripping():
    keywords, _ = extract("click the search button")
    assert "search" in keywords
    assert "click" not in keywords
    assert "the" not in keywords
    assert "button" not in keywords  # "button" is in STOPWORDS? Actually no...


def test_basic_search_extraction():
    keywords, _ = extract("click the search button")
    assert "search" in keywords
    # synonym expansion: search → searchbox
    assert "searchbox" in keywords


def test_quoted_value_extraction():
    keywords, value = extract("type 'machine learning' into search")
    assert value == "machine learning"
    # quoted content should NOT become keywords
    assert "machine" not in keywords
    assert "learning" not in keywords
    # remaining words after stopword strip: search
    assert "search" in keywords


def test_synonym_expansion_submit():
    keywords, _ = extract("submit the form")
    assert "button" in keywords
    assert "submit" in keywords


def test_double_quoted_value():
    keywords, value = extract('type "pizza margherita" into the search box')
    assert value == "pizza margherita"
    assert "pizza" not in keywords
    assert "margherita" not in keywords
    assert "search" in keywords
    assert "searchbox" in keywords


def test_empty_goal():
    keywords, value = extract("")
    assert keywords == set()
    assert value is None


def test_login_expansion():
    keywords, _ = extract("login to the account")
    assert "button" in keywords
    assert "link" in keywords
    assert "textbox" in keywords

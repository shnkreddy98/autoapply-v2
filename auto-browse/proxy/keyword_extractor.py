import re

from .synonym_map import SYNONYM_MAP

STOPWORDS = frozenset(
    {
        "click",
        "type",
        "enter",
        "press",
        "select",
        "choose",
        "go",
        "navigate",
        "find",
        "locate",
        "into",
        "the",
        "a",
        "an",
        "on",
        "to",
        "for",
        "with",
        "then",
        "and",
        "please",
        "button",
        "link",
        "in",
        "of",
        "is",
        "it",
        "that",
        "this",
        "at",
        "by",
        "or",
        "be",
    }
)

_QUOTED = re.compile(r"""['"]([^'"]+)['"]""")
_WORD = re.compile(r"[a-zA-Z][a-zA-Z0-9]*")


def extract(goal: str) -> tuple[set[str], str | None]:
    """
    Returns (keywords, literal_value) from a goal string.

    keywords: set of lowercase words/roles to match against tree nodes
    literal_value: content of the first quoted string (excluded from keyword matching)
    """
    quoted = _QUOTED.findall(goal)
    literal_value = quoted[0] if quoted else None

    # remove quoted strings from goal before extracting keywords
    cleaned = _QUOTED.sub(" ", goal).lower()

    words = set(_WORD.findall(cleaned))
    words -= STOPWORDS

    keywords: set[str] = set()
    for word in words:
        keywords.add(word)
        for expansions in _synonym_expansions(word):
            keywords.update(expansions)

    return keywords, literal_value


def _synonym_expansions(word: str) -> list[list[str]]:
    results = []
    for intent, expansions in SYNONYM_MAP.items():
        if intent == word or word in expansions:
            results.append(expansions)
    return results

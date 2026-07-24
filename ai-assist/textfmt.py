"""Post-processing for free-text LLM answers.

The word cap and the no-em-dash rule are in the prompt, but models leak past
both often enough that they are enforced here as well. Pure string work, no
env/LLM imports, so it stays unit-testable without an API key.
"""

from __future__ import annotations

import re

# em dash, en dash, horizontal bar, and the "--" ASCII stand-in
_DASHES = re.compile(r"\s*[—–―]\s*|\s+--\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def strip_dashes(text: str) -> str:
    """Replace em/en dashes with a comma. A dash almost always joins two clauses,
    so a comma keeps the sentence readable where a hyphen would not."""
    out = _DASHES.sub(", ", text)
    out = re.sub(r",\s*,+", ", ", out)
    out = re.sub(r"\s+,", ",", out)
    return out


def cap_words(text: str, max_words: int) -> str:
    """Trim to at most `max_words`, preferring the last whole sentence that fits.
    Falls back to a hard word cut when even the first sentence is too long."""
    words = text.split()
    if len(words) <= max_words:
        return text

    kept: list[str] = []
    for sentence in _SENTENCE_END.split(text):
        if not sentence.strip():
            continue
        if len(" ".join(kept + [sentence]).split()) > max_words:
            break
        kept.append(sentence)
    if kept:
        return " ".join(kept)

    cut = " ".join(words[:max_words]).rstrip(",;:")
    return cut if cut.endswith((".", "!", "?")) else cut + "."


def clean_answer(text: str, max_words: int) -> str:
    return cap_words(strip_dashes(" ".join(text.split())), max_words)


def word_count(text: str) -> int:
    return len(text.split())

"""Small deterministic text similarity helpers for reproducible evaluation."""

import re
from itertools import pairwise

STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "remain",
    "remains",
    "the",
    "this",
    "to",
    "underexplored",
    "we",
    "with",
}


def terms(text: str) -> set[str]:
    tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", text.casefold())
        if len(token) > 1 and token not in STOPWORDS
    ]
    unigrams = set(tokens)
    bigrams = {f"{left} {right}" for left, right in pairwise(tokens)}
    return unigrams | bigrams


def text_similarity(left: str, right: str) -> float:
    """Return weighted Jaccard similarity over normalized uni/bi-grams."""
    left_terms, right_terms = terms(left), terms(right)
    if not left_terms or not right_terms:
        return 0.0
    intersection = left_terms & right_terms
    union = left_terms | right_terms
    return len(intersection) / len(union)

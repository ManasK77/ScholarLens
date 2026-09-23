"""Tests for app.ingestion.topic — the shared topic → search-terms split
(D-QUERY-02).

arXiv and ACL Anthology both used to demand the user's whole topic as one
literal phrase ('all:"multilingual prompt injection"' / the same substring in
a title or abstract), so a perfectly ordinary three-word research topic
matched nothing at either source while Semantic Scholar — which tokenizes the
query itself — returned plenty. This module is the one place the topic is
split into the terms both sources then require *together*, so the two cannot
drift apart.
"""

import pytest

from app.ingestion.topic import meaningful_terms


def test_a_multi_word_topic_becomes_its_separate_terms():
    assert meaningful_terms("Multilingual prompt injection") == [
        "Multilingual",
        "prompt",
        "injection",
    ]


def test_a_single_word_topic_stays_one_term():
    assert meaningful_terms("transformers") == ["transformers"]


def test_function_words_are_dropped_so_they_cannot_constrain_the_search():
    """`the`/`of`/`in` appear in virtually every paper: requiring them adds no
    precision, and in a term-AND query they only cost recall."""
    assert meaningful_terms("the alignment tax") == ["alignment", "tax"]
    assert meaningful_terms("robustness of retrieval in low-resource settings") == [
        "robustness",
        "retrieval",
        "low-resource",
        "settings",
    ]


def test_surrounding_punctuation_is_stripped_but_internal_characters_survive():
    """Parentheses/colons carry query-grammar meaning at arXiv; a hyphen or an
    apostrophe inside a word is part of the word."""
    assert meaningful_terms("GPT-4 (vision)") == ["GPT-4", "vision"]
    assert meaningful_terms("hate speech: a survey") == ["hate", "speech", "survey"]
    assert meaningful_terms("Müller's method") == ["Müller's", "method"]


def test_repeated_terms_are_kept_once_regardless_of_case():
    assert meaningful_terms("Prompt injection prompt defence") == [
        "Prompt",
        "injection",
        "defence",
    ]


def test_double_quotes_are_removed_rather_than_left_to_break_a_phrase():
    """arXiv documents no escape for a quote inside a quoted phrase, so one
    left in a term would terminate that phrase early."""
    assert meaningful_terms('the "alignment tax"') == ["alignment", "tax"]


@pytest.mark.parametrize("topic", ["the", "of the", "a an the"])
def test_a_topic_of_only_function_words_still_searches_for_what_was_typed(topic):
    """Better to search for exactly what the user typed than for nothing at
    all — the term filter must never empty a query out."""
    assert meaningful_terms(topic) == [topic.strip()]


def test_an_empty_topic_yields_no_terms():
    assert meaningful_terms("   ") == []

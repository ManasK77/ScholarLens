from unittest.mock import Mock

import pytest

from app.parsing.section_classifier import (
    CANONICAL_LABELS,
    classify_heading,
    classify_section,
    get_embedder,
)


def test_keyword_match_short_circuits_embedding() -> None:
    embed = Mock()
    assert classify_heading("Introduction", embed) == "Introduction"
    assert classify_heading("Related Work", embed) == "Introduction"
    assert classify_heading("5. RESULTS", embed) == "Results"
    embed.assert_not_called()


def test_unrecognized_heading_uses_cosine_similarity() -> None:
    discussion_index = CANONICAL_LABELS.index("Discussion")

    def embed(texts: list[str]) -> list[list[float]]:
        vectors = [[1.0, 0.0]]
        vectors.extend(
            [([1.0, 0.0] if i == discussion_index else [0.0, 1.0]) for i in range(7)]
        )
        assert len(texts) == 8
        return vectors

    assert classify_heading("Empirical Observations", embed) == "Discussion"


def test_similarity_ties_are_resolved_by_canonical_order() -> None:
    def embed(texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    assert classify_heading("Unmapped Heading", embed) == CANONICAL_LABELS[0]


def test_missing_heading_has_defined_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.parsing.section_classifier.get_embedder",
        Mock(side_effect=AssertionError("embedder must not load")),
    )
    assert classify_section(None) == "Introduction"


@pytest.mark.integration
def test_real_scibert_embedder_returns_consistent_vectors() -> None:
    embed = get_embedder()
    first = embed(["Introduction"])
    second = embed(["Results"])
    assert len(first) == len(second) == 1
    assert len(first[0]) == len(second[0]) > 0


@pytest.mark.integration
def test_real_embedder_is_cached() -> None:
    assert get_embedder() is get_embedder()

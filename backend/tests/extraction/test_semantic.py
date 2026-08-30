import math
from unittest.mock import Mock

import pytest

from app.extraction import semantic
from app.extraction.schema import FactType
from app.extraction.semantic import (
    SEMANTIC_LABELS,
    SemanticStatementClassifier,
    get_semantic_classifier,
)


def test_semantic_classifier_accepts_clear_best_label() -> None:
    target = SEMANTIC_LABELS.index(FactType.LIMITATION)

    def embed(texts: list[str]) -> list[list[float]]:
        vectors = [[1.0, 0.0]]
        vectors.extend(
            [([1.0, 0.0] if index == target else [0.0, 1.0]) for index in range(6)]
        )
        vectors.append([0.0, 1.0])  # non-fact exemplar
        assert len(texts) == 8
        return vectors

    classifier = SemanticStatementClassifier(
        embed_fn=embed, min_similarity=0.5, min_margin=0.1
    )
    match = classifier.classify("Cross-domain performance remains weak.")

    assert match is not None
    assert match.fact_type is FactType.LIMITATION
    assert match.confidence == pytest.approx(1.0)


def test_semantic_classifier_rejects_low_similarity() -> None:
    classifier = SemanticStatementClassifier(
        embed_fn=lambda texts: [[0.0, 0.0] for _ in texts],
        min_similarity=0.5,
        min_margin=0.1,
    )
    assert classifier.classify("The paper is organized as follows.") is None


def test_semantic_classifier_rejects_ambiguous_tie() -> None:
    classifier = SemanticStatementClassifier(
        embed_fn=lambda texts: [[1.0, 0.0] for _ in texts],
        min_similarity=0.5,
        min_margin=0.1,
    )
    assert classifier.classify("Ambiguous statement") is None


def test_score_returns_one_finite_score_per_type() -> None:
    classifier = SemanticStatementClassifier(
        embed_fn=lambda texts: [[1.0, float(i)] for i, _ in enumerate(texts)]
    )
    scores = classifier.score("A sentence")
    assert set(scores) == set(FactType)
    assert all(math.isfinite(score) for score in scores.values())


def test_exemplar_embeddings_are_reused_across_sentences() -> None:
    batch_sizes = []

    def embed(texts: list[str]) -> list[list[float]]:
        batch_sizes.append(len(texts))
        return [[1.0, float(index)] for index, _ in enumerate(texts)]

    classifier = SemanticStatementClassifier(embed_fn=embed)
    classifier.score("First sentence")
    classifier.score("Second sentence")

    assert batch_sizes == [8, 1]


def test_default_classifier_defers_scibert_loading(monkeypatch) -> None:
    semantic.get_semantic_classifier.cache_clear()
    embed = lambda texts: [[1.0, 0.0] for _ in texts]
    model_loader = Mock(return_value=embed)
    monkeypatch.setattr(semantic, "get_embedder", model_loader)
    try:
        classifier = semantic.get_semantic_classifier()
        model_loader.assert_not_called()
        classifier.score("A sentence")
        model_loader.assert_called_once_with()
    finally:
        semantic.get_semantic_classifier.cache_clear()


@pytest.mark.integration
def test_real_semantic_classifier_uses_cached_scibert() -> None:
    classifier = get_semantic_classifier()
    scores = classifier.score("Performance remains limited in low-resource settings.")
    assert get_semantic_classifier() is classifier
    assert set(scores) == set(FactType)
    assert all(-1.0 <= score <= 1.0 for score in scores.values())

"""SciBERT semantic fallback for statements without strong spaCy cues."""

import math
from collections.abc import Sequence
from functools import lru_cache

from app.extraction.schema import FactType, SemanticMatch
from app.parsing.section_classifier import EmbedFn, get_embedder

SEMANTIC_LABELS = tuple(FactType)
_EXEMPLARS = {
    FactType.CONTRIBUTION: "the paper presents a novel contribution or successful result",
    FactType.LIMITATION: "the method has a limitation weakness failure or constraint",
    FactType.FUTURE_WORK: "the authors recommend future work or further investigation",
    FactType.DATASET: "the sentence describes a dataset corpus benchmark or evaluation data",
    FactType.MODEL: "the sentence describes a model method architecture or neural network",
    FactType.METRIC: "the sentence reports an evaluation metric or quantitative score",
}
_NON_FACT_EXEMPLAR = (
    "neutral paper organization or general background without a scientific fact"
)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


class SemanticStatementClassifier:
    def __init__(
        self,
        embed_fn: EmbedFn,
        min_similarity: float = 0.55,
        min_margin: float = 0.03,
    ) -> None:
        self._embed_fn = embed_fn
        self._min_similarity = min_similarity
        self._min_margin = min_margin
        self._reference_vectors: Sequence[Sequence[float]] | None = None

    def _scores_with_non_fact(
        self, sentence: str
    ) -> tuple[dict[FactType, float], float]:
        if self._reference_vectors is None:
            texts = [
                sentence,
                *(_EXEMPLARS[label] for label in SEMANTIC_LABELS),
                _NON_FACT_EXEMPLAR,
            ]
            vectors = self._embed_fn(texts)
            sentence_vector = vectors[0]
            self._reference_vectors = vectors[1:]
        else:
            sentence_vector = self._embed_fn([sentence])[0]
        reference_vectors = self._reference_vectors
        scores = {
            label: _cosine(sentence_vector, reference_vectors[index])
            for index, label in enumerate(SEMANTIC_LABELS)
        }
        non_fact_score = _cosine(sentence_vector, reference_vectors[-1])
        return scores, non_fact_score

    def score(self, sentence: str) -> dict[FactType, float]:
        """Return semantic similarity for all six canonical fact types."""
        scores, _ = self._scores_with_non_fact(sentence)
        return scores

    def classify(self, sentence: str) -> SemanticMatch | None:
        """Return one high-margin semantic label, otherwise abstain."""
        scores, non_fact_score = self._scores_with_non_fact(sentence)
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        (best_type, best_score), (_, runner_up_score) = ranked[:2]
        margin = best_score - max(runner_up_score, non_fact_score)
        if best_score < self._min_similarity or margin < self._min_margin:
            return None
        return SemanticMatch(best_type, best_score)


@lru_cache(maxsize=1)
def get_semantic_classifier() -> SemanticStatementClassifier:
    """Reuse Phase 2's process-cached mean-pooled SciBERT embedder."""
    return SemanticStatementClassifier(lambda texts: get_embedder()(texts))

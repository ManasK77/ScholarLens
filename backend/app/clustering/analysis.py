"""HDBSCAN grouping plus bounded pairwise NLI contradiction detection."""

from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from app.core.config import get_settings


def labels_with_singletons(labels: list[int]) -> list[int]:
    """Turn HDBSCAN noise into stable singleton clusters instead of dropping it."""
    next_label = max((label for label in labels if label >= 0), default=-1) + 1
    output = []
    for label in labels:
        if label < 0:
            output.append(next_label)
            next_label += 1
        else:
            output.append(int(label))
    return output


def cluster_vectors(vectors: list[list[float]], min_cluster_size: int) -> list[int]:
    if len(vectors) < min_cluster_size:
        return labels_with_singletons([-1] * len(vectors))
    from sklearn.cluster import HDBSCAN

    labels = HDBSCAN(
        min_cluster_size=min_cluster_size,
        min_samples=1,
        metric="euclidean",
        cluster_selection_method="eom",
    ).fit_predict(np.asarray(vectors, dtype=np.float32))
    return labels_with_singletons(labels.tolist())


def pair_indexes(size: int) -> list[tuple[int, int]]:
    return [(left, right) for left in range(size) for right in range(left + 1, size)]


class NLIPredictor:
    def __init__(self, model_name: str) -> None:
        from transformers import pipeline

        self._pipeline = pipeline("text-classification", model=model_name, top_k=None)

    def contradiction_score(self, premise: str, hypothesis: str) -> float:
        raw = self._pipeline({"text": premise, "text_pair": hypothesis})
        labels = raw[0] if raw and isinstance(raw[0], list) else raw
        for item in labels:
            if "contradiction" in str(item["label"]).casefold():
                return float(item["score"])
        return 0.0


@lru_cache(maxsize=1)
def get_nli_predictor() -> NLIPredictor:
    return NLIPredictor(get_settings().nli_model)


@dataclass(frozen=True, slots=True)
class Contradiction:
    left: int
    right: int
    score: float


def find_contradictions(
    texts: list[str],
    predictor: NLIPredictor,
    threshold: float,
    max_pairs: int,
) -> list[Contradiction]:
    found = []
    for left, right in pair_indexes(len(texts))[:max_pairs]:
        score = max(
            predictor.contradiction_score(texts[left], texts[right]),
            predictor.contradiction_score(texts[right], texts[left]),
        )
        if score >= threshold:
            found.append(Contradiction(left, right, score))
    return found

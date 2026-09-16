"""DeBERTa-MNLI pairwise contradiction detection inside a cluster (CLUSTER-02).

README §8 Stage 6 describes the signal precisely: *one paper claims a method
works where another says it fails under the same setup*. Three properties
follow from that sentence and are enforced here:

* **Between papers, not within one** (D-06-04). Two sentences from the same
  paper disagreeing is an extraction artefact, so those pairs are never scored.
* **Both directions** (D-06-05). NLI is asymmetric, so `(a→b)` and `(b→a)` are
  both scored and the stronger contradiction wins; otherwise a real
  disagreement would be missed purely because of statement order.
* **Bounded work.** Pairs grow as O(n²) in cluster size and each one is a
  transformer forward pass, so the candidate list is capped before any model
  is called.

`torch` and `transformers` are imported **inside** `load_deberta_mnli`, never
at module import — the same rule Phases 2, 4 and 5 follow, so importing this
module (which the API import path does) costs nothing.
"""

import logging
import math
from collections.abc import Callable, Sequence
from functools import lru_cache
from itertools import combinations, islice
from sqlalchemy import Float, String
from sqlalchemy.orm import Mapped, mapped_column
from app.ingestion.corpus import Base
from app.core.pipeline import current_run, fingerprint, settings_fingerprint

from app.clustering.schema import ContradictionPair, GapStatement
from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

CONTRADICTION = "contradiction"
NEUTRAL = "neutral"
ENTAILMENT = "entailment"
NLI_LABELS = (CONTRADICTION, NEUTRAL, ENTAILMENT)

# `predict(pairs) -> one {label: probability} mapping per (premise, hypothesis)`
PredictFn = Callable[[list[tuple[str, str]]], Sequence[dict[str, float]]]

StatementPair = tuple[GapStatement, GapStatement]

# Hard ceilings, independent of what `.env` asks for (README §13.6).
MAX_PAIRS_CEILING = 2000
MAX_BATCH_SIZE = 64


class NliPairScore(Base):
    __tablename__ = 'nli_pair_scores'
    key: Mapped[str] = mapped_column(String, primary_key=True)
    score: Mapped[float] = mapped_column(Float)


def label_index(label2id: dict[str, int]) -> dict[str, int]:
    """Map a checkpoint's own label names onto the three canonical NLI names.

    D-06-03: DeBERTa MNLI releases do not agree on label order —
    `deberta-large-mnli` starts at CONTRADICTION, other MNLI heads start at
    ENTAILMENT. Hardcoding an index would silently invert the two, so the
    mapping is always read from `model.config`.
    """
    mapping: dict[str, int] = {}
    for name, index in label2id.items():
        normalized = str(name).strip().lower()
        for canonical in NLI_LABELS:
            if normalized == canonical or normalized == canonical[:3]:
                mapping[canonical] = int(index)
    missing = [label for label in NLI_LABELS if label not in mapping]
    if missing:
        raise ValueError(
            f"checkpoint is not a three-way NLI head: {sorted(label2id)} does not "
            f"provide {missing}"
        )
    return mapping


def candidate_pairs(
    statements: Sequence[GapStatement], max_pairs: int
) -> list[StatementPair]:
    """Every distinct cross-paper pair in the cluster, capped and deterministic.

    Ordered by point id so the cap always keeps the same subset — a cluster
    that gets truncated must not flag a different contradiction on each run.

    The cap bounds the **enumeration**, not just the result: a 2000-member
    cluster is ~2M candidate pairs, and building that list only to slice fifty
    off the front is the resource failure README §13.6 warns about. `islice`
    over the generator stops as soon as the cap is met.
    """
    if max_pairs <= 0:
        raise ValueError(f"max_pairs must be positive, got {max_pairs}")
    ordered = sorted(statements, key=lambda statement: statement.point_id)
    cross_paper = (
        (left, right)
        for left, right in combinations(ordered, 2)
        if left.paper_id != right.paper_id
    )
    return list(islice(cross_paper, min(max_pairs, MAX_PAIRS_CEILING)))


class ContradictionDetector:
    """Scores cross-paper pairs in a cluster and returns the contradicting ones.

    The model is injected as a plain callable so the pairing, batching,
    thresholding and direction logic is testable without loading a checkpoint —
    the same shape `Specter2Encoder` uses in Phase 5.
    """

    def __init__(
        self,
        predict: PredictFn,
        threshold: float,
        *,
        batch_size: int = 8,
        max_chars: int = 1000,
        max_pairs_per_cluster: int = 50,
        cache_version: str | None = None,
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold must be in [0, 1], got {threshold}")
        if batch_size <= 0 or max_chars <= 0 or max_pairs_per_cluster <= 0:
            raise ValueError("batch, length and pair bounds must be positive")
        self._predict = predict
        self.threshold = threshold
        self._batch_size = min(batch_size, MAX_BATCH_SIZE)
        self._max_chars = max_chars
        self._max_pairs = max_pairs_per_cluster
        self._cache_version = cache_version

    def detect(self, statements: Sequence[GapStatement]) -> list[ContradictionPair]:
        """Contradicting cross-paper pairs, strongest first."""
        pairs = candidate_pairs(statements, self._max_pairs)
        if not pairs:
            return []

        # Both directions, interleaved, so one batched pass covers them.
        inputs: list[tuple[str, str]] = []
        for left, right in pairs:
            left_text = left.text[: self._max_chars]
            right_text = right.text[: self._max_chars]
            inputs.append((left_text, right_text))
            inputs.append((right_text, left_text))

        scores = self._score(inputs)

        found = []
        for position, (left, right) in enumerate(pairs):
            score = max(scores[2 * position], scores[2 * position + 1])
            if score >= self.threshold:
                found.append(
                    ContradictionPair(
                        left.point_id,
                        left.paper_id,
                        right.point_id,
                        right.paper_id,
                        score,
                    )
                )
        found.sort(
            key=lambda pair: (-pair.score, pair.left_point_id, pair.right_point_id)
        )
        return found

    def _score(self, inputs: list[tuple[str, str]]) -> list[float]:
        run = current_run()
        if run and self._cache_version:
            keys = [fingerprint([self._cache_version, pair]) for pair in inputs]
            stored = {key: run.session.get(NliPairScore, key) for key in set(keys)}
            missing = dict((key, pair) for key, pair in zip(keys, inputs, strict=True) if stored[key] is None)
            scores = self._score_uncached(list(missing.values()))
            for key, score in zip(missing, scores, strict=True):
                row = NliPairScore(key=key, score=score)
                run.session.add(row)
                stored[key] = row
            run.session.flush()
            return [stored[key].score for key in keys]
        return self._score_uncached(inputs)

    def _score_uncached(self, inputs):
        scores: list[float] = []
        for start in range(0, len(inputs), self._batch_size):
            batch = inputs[start : start + self._batch_size]
            predictions = list(self._predict(batch))
            if len(predictions) != len(batch):
                raise ValueError(
                    f"NLI model returned {len(predictions)} predictions for "
                    f"{len(batch)} pairs"
                )
            for prediction in predictions:
                if CONTRADICTION not in prediction:
                    raise ValueError(
                        f"NLI prediction has no {CONTRADICTION!r} probability: "
                        f"{sorted(prediction)}"
                    )
                score = float(prediction[CONTRADICTION])
                # NaN survives both min() and max() untouched, so it would
                # reach `ContradictionPair` and surface as an opaque range
                # error. Say what actually went wrong instead.
                if not math.isfinite(score):
                    raise ValueError(
                        f"NLI model returned a non-finite {CONTRADICTION!r} "
                        f"probability: {score!r}"
                    )
                # A probability outside [0, 1] would propagate into
                # `ContradictionPair` and then into Phase 8's weighted sum.
                scores.append(min(max(score, 0.0), 1.0))
        return scores


def load_deberta_mnli(settings: Settings) -> PredictFn:
    """Load the NLI checkpoint and return a batched `predict` callable.

    Heavy imports live here so importing this module stays free. Inference runs
    under `torch.inference_mode()` on a model in eval mode, so the same pair
    always scores the same — Stage 6 has to be reproducible for Phase 8's
    ranking and Phase 13's evaluation to mean anything.
    """
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    revision = getattr(settings, "nli_model_revision", None)
    tokenizer = AutoTokenizer.from_pretrained(settings.nli_model, revision=revision)
    model = AutoModelForSequenceClassification.from_pretrained(
        settings.nli_model, revision=revision
    )
    model.eval()

    indices = label_index(dict(model.config.label2id))
    logger.info("Loaded NLI model %s (labels %s)", settings.nli_model, indices)

    def predict(pairs: list[tuple[str, str]]) -> list[dict[str, float]]:
        if not pairs:
            return []
        inputs = tokenizer(
            [premise for premise, _ in pairs],
            [hypothesis for _, hypothesis in pairs],
            padding=True,
            truncation=True,
            max_length=settings.nli_max_tokens,
            return_tensors="pt",
        )
        with torch.inference_mode():
            logits = model(**inputs).logits
        probabilities = torch.softmax(logits, dim=-1).tolist()
        return [
            {label: float(row[index]) for label, index in indices.items()}
            for row in probabilities
        ]

    return predict


@lru_cache(maxsize=1)
def get_detector() -> ContradictionDetector:
    """Process-wide detector so the checkpoint is loaded at most once.

    Deliberately lazy, and called by the service only once at least one cluster
    exists: a corpus with nothing to compare must not pay for a model load.
    """
    settings = get_settings()
    return ContradictionDetector(
        load_deberta_mnli(settings),
        threshold=settings.contradiction_threshold,
        batch_size=settings.nli_batch_size,
        max_chars=settings.nli_max_chars,
        max_pairs_per_cluster=settings.nli_max_pairs_per_cluster,
        cache_version=fingerprint(settings_fingerprint(settings, ('nli_',))),
    )

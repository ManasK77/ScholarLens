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

from app.clustering.claims import (
    MIN_CLAIM_WORDS,
    MIN_SHARED_TERMS,
    MIN_TOPIC_JACCARD,
    is_claim_like,
    topically_related,
)
from app.clustering.schema import ContradictionPair, GapStatement
from app.core.config import Settings, get_settings
from app.core.pipeline import current_run, fingerprint, settings_fingerprint
from app.ingestion.corpus import Base

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
    __tablename__ = "nli_pair_scores"
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
    statements: Sequence[GapStatement],
    max_pairs: int,
    eligible: Callable[[GapStatement, GapStatement], bool] | None = None,
) -> list[StatementPair]:
    """Every distinct cross-paper pair in the cluster, capped and deterministic.

    Ordered by point id so the cap always keeps the same subset — a cluster
    that gets truncated must not flag a different contradiction on each run.

    `eligible` is applied *inside* the enumeration rather than to the result,
    so the cap bounds the pairs that could actually be flagged. Filtering
    afterwards would let a cluster spend its whole budget on pairs the topic
    gate was always going to reject and then report no contradiction because it
    ran out of room (D-06-11).

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
        and (eligible is None or eligible(left, right))
    )
    return list(islice(cross_paper, min(max_pairs, MAX_PAIRS_CEILING)))


def disagreeing_paper_pairs(pairs: Sequence[ContradictionPair]) -> int:
    """How many distinct *paper* pairs disagree, not how many statement pairs.

    Stage 8 grades the contradiction signal on this number and Stage 6 gates on
    it, so it lives here rather than in either: counting statement pairs is the
    bug that let one disclaimer in cluster 1 look like four independent
    disagreements when it was two papers arguing with one.
    """
    return len({frozenset((pair.left_paper_id, pair.right_paper_id)) for pair in pairs})


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
        min_claim_words: int = MIN_CLAIM_WORDS,
        min_shared_terms: int = MIN_SHARED_TERMS,
        min_topic_jaccard: float = MIN_TOPIC_JACCARD,
        require_both_directions: bool = True,
        min_paper_pairs: int = 2,
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold must be in [0, 1], got {threshold}")
        if batch_size <= 0 or max_chars <= 0 or max_pairs_per_cluster <= 0:
            raise ValueError("batch, length and pair bounds must be positive")
        if min_claim_words <= 0 or min_shared_terms <= 0 or min_paper_pairs <= 0:
            raise ValueError("claim, topic and corroboration bounds must be positive")
        if not 0.0 <= min_topic_jaccard <= 1.0:
            raise ValueError(
                f"min_topic_jaccard must be in [0, 1], got {min_topic_jaccard}"
            )
        self._predict = predict
        self.threshold = threshold
        self._batch_size = min(batch_size, MAX_BATCH_SIZE)
        self._max_chars = max_chars
        self._max_pairs = max_pairs_per_cluster
        self._cache_version = cache_version
        self._min_claim_words = min_claim_words
        self._min_shared_terms = min_shared_terms
        self._min_topic_jaccard = min_topic_jaccard
        self._require_both_directions = require_both_directions
        self._min_paper_pairs = min_paper_pairs
        # O2 telemetry: how many pairs this detector has scored, process-wide.
        self.pairs_scored = 0

    def _related(self, left: GapStatement, right: GapStatement) -> bool:
        return topically_related(
            left.text, right.text, self._min_shared_terms, self._min_topic_jaccard
        )

    def detect(self, statements: Sequence[GapStatement]) -> list[ContradictionPair]:
        """Corroborated contradicting cross-paper pairs, strongest first.

        Four gates stand between a cluster and a contradiction flag, and all of
        them exist because the model alone does not implement README S8's
        sentence. On the 20-paper LLM-security corpus, DeBERTa-MNLI scored four
        pairs above 0.999 in which one side was an ethics disclaimer and the
        others were a recommendation bullet, a related-work description, a
        future-work proposal and a limitation about something else.

        1. **Claim-like** (D-06-10). A statement that asserts nothing cannot be
           disagreed with, so non-claims are dropped before any pairing and
           never reach the model.
        2. **Topically related** (D-06-11). "Under the same setup" is the part
           of the README sentence an NLI head cannot check.
        3. **Both directions** (D-06-12). NLI is asymmetric, so a single
           direction over the threshold is as easily an ordering artefact as a
           disagreement. The stronger direction is still what gets *stored* --
           D-06-05 is unchanged for scoring.
        4. **Corroboration** (D-06-12). Fewer than `min_paper_pairs` distinct
           *paper* pairs is not a cluster-level disagreement. Cluster 1's four
           pairs were four statements against one, from two papers.

        Returns `[]` when corroboration fails, so nothing is stored and no flag
        is raised: there is no conflicting evidence to explain.
        """
        claims = [
            item
            for item in statements
            if is_claim_like(item.text, self._min_claim_words)
        ]
        pairs = candidate_pairs(claims, self._max_pairs, eligible=self._related)
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
            forward, reverse = scores[2 * position], scores[2 * position + 1]
            agreed = (
                min(forward, reverse)
                if self._require_both_directions
                else max(forward, reverse)
            )
            if agreed >= self.threshold:
                found.append(
                    ContradictionPair(
                        left.point_id,
                        left.paper_id,
                        right.point_id,
                        right.paper_id,
                        max(forward, reverse),
                    )
                )
        found.sort(
            key=lambda pair: (-pair.score, pair.left_point_id, pair.right_point_id)
        )
        if disagreeing_paper_pairs(found) < self._min_paper_pairs:
            return []
        return found

    def score_pairs(self, pairs: list[tuple[str, str]]) -> list[float]:
        """P(contradiction) for each `(premise, hypothesis)`, truncated as above.

        Used by Stage 9 to check a report's own conflict claim (D-09-13); the
        same model and cache as Stage 6.
        """
        return self._score(
            [
                (left[: self._max_chars], right[: self._max_chars])
                for left, right in pairs
            ]
        )

    def _score(self, inputs: list[tuple[str, str]]) -> list[float]:
        self.pairs_scored += len(inputs)
        run = current_run()
        if run and self._cache_version:
            keys = [fingerprint([self._cache_version, pair]) for pair in inputs]
            stored = {key: run.session.get(NliPairScore, key) for key in set(keys)}
            missing = {
                key: pair
                for key, pair in zip(keys, inputs, strict=True)
                if stored[key] is None
            }
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
        # Keyed on the model settings alone, deliberately. The cache holds raw
        # P(contradiction) per ordered pair; the `contradiction_*` gates are
        # applied to those scores afterwards and do not change what the model
        # returns, so folding them in here would throw away good cached
        # forward passes every time a threshold is tuned.
        cache_version=fingerprint(settings_fingerprint(settings, ("nli_",))),
        min_claim_words=settings.contradiction_min_claim_words,
        min_shared_terms=settings.contradiction_min_shared_terms,
        min_topic_jaccard=settings.contradiction_min_topic_jaccard,
        require_both_directions=settings.contradiction_require_both_directions,
        min_paper_pairs=settings.contradiction_min_paper_pairs,
    )

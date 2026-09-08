"""Normalize heterogeneous scientific section headings to seven roles."""

import math
import re
from collections.abc import Callable, Sequence
from functools import lru_cache

Vector = Sequence[float]
EmbedFn = Callable[[list[str]], Sequence[Vector]]

CANONICAL_LABELS = [
    "Introduction",
    "Methodology",
    "Results",
    "Discussion",
    "Limitations",
    "Future Work",
    "Conclusion",
]

_KEYWORD_MAP = [
    ("future work", "Future Work"),
    ("future direction", "Future Work"),
    ("threats to validity", "Discussion"),
    ("related work", "Introduction"),
    ("prior work", "Introduction"),
    ("introduction", "Introduction"),
    ("background", "Introduction"),
    ("methodology", "Methodology"),
    ("method", "Methodology"),
    ("approach", "Methodology"),
    ("architecture", "Methodology"),
    ("experiment", "Results"),
    ("evaluation", "Results"),
    ("result", "Results"),
    ("discussion", "Discussion"),
    ("analysis", "Discussion"),
    ("limitation", "Limitations"),
    ("conclusion", "Conclusion"),
    ("summary", "Conclusion"),
]

_EXEMPLARS = [
    "introduction background related work",
    "methodology methods approach model architecture",
    "results experiments evaluation findings",
    "discussion analysis threats to validity",
    "limitations weaknesses constraints",
    "future work future directions further research",
    "conclusion summary closing remarks",
]


def _keyword_match(heading: str) -> str | None:
    normalized = re.sub(r"^[\d.\s()\-:]+", "", heading.lower()).strip()
    for phrase, label in _KEYWORD_MAP:
        if phrase in normalized:
            return label
    return None


def _cosine_similarity(left: Vector, right: Vector) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


@lru_cache(maxsize=1)
def get_embedder() -> EmbedFn:
    """Lazily construct one mean-pooled SciBERT embedder per process."""
    from sentence_transformers import SentenceTransformer

    # sentence-transformers moved these in 6.x. Phase 4 pinned the library back
    # to 5.x (gliner2[local] caps transformers<5, which 6.x forbids), so accept
    # both layouts rather than coupling this module to one release line.
    try:
        from sentence_transformers.sentence_transformer.modules import (
            Pooling,
            Transformer,
        )
    except ImportError:  # sentence-transformers < 6
        from sentence_transformers.models import Pooling, Transformer

    transformer = Transformer("allenai/scibert_scivocab_uncased")
    pooling = Pooling(transformer.get_embedding_dimension(), pooling_mode="mean")
    model = SentenceTransformer(modules=[transformer, pooling])

    def embed(texts: list[str]):
        return model.encode(texts, convert_to_numpy=True)

    return embed


def classify_heading(heading: str, embed_fn: EmbedFn | None = None) -> str:
    """Classify a heading cheaply by keyword, then semantically by SciBERT."""
    keyword_label = _keyword_match(heading)
    if keyword_label:
        return keyword_label

    vectors = (embed_fn or get_embedder())([heading, *_EXEMPLARS])
    heading_vector = vectors[0]
    similarities = [
        _cosine_similarity(heading_vector, exemplar_vector)
        for exemplar_vector in vectors[1:]
    ]
    best_index = max(range(len(CANONICAL_LABELS)), key=similarities.__getitem__)
    return CANONICAL_LABELS[best_index]


def classify_section(heading: str | None) -> str:
    """Public section classifier; untitled leading material defaults to Introduction."""
    if not heading or not heading.strip():
        return "Introduction"
    return classify_heading(heading)

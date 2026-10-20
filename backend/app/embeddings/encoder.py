"""SPECTER2 paragraph/statement encoder (Stage 5).

README §7 fixes the model: **SPECTER2**, the citation-graph-aware scientific
document encoder. The official release is the `allenai/specter2_base`
transformer plus the `allenai/specter2` *proximity* adapter, which is what the
model card prescribes for retrieval and clustering — the two things Phases 5,
6, 9 and 10 do with these vectors. AdapterHub's `adapters` package is the only
way to attach that adapter (PROGRESS.md D-05-01); it was checked against the
Phase 4 pin before being added and needs no change to it.

`torch`, `transformers` and `adapters` are imported **inside** `load_specter2`,
never at module import, for the same reason Phases 2 and 4 do it: importing
this module must stay cheap, and `adapters` patches transformers model classes
when it loads — that must not happen in a process that only wanted GLiNER2.

Everything the model returns is checked before it leaves this module. A
checkpoint that returns the wrong count, the wrong width, or a NaN would
otherwise misalign every payload after it or poison cosine scoring for the
life of the collection.
"""

import logging
import math
from collections.abc import Callable, Sequence
from functools import lru_cache

from app.core.config import Settings, get_settings
from app.core.model_revision import resolved_revision, split_revision

logger = logging.getLogger(__name__)

Vector = Sequence[float]
EmbedFn = Callable[[list[str]], Sequence[Vector]]

# A hard ceiling on how many texts may sit in one forward pass, independent of
# what `.env` asks for. Batch size is the single knob that turns corpus size
# into peak memory (README §13.6).
MAX_BATCH_SIZE = 256

# Characters, not tokens: a cheap pre-tokenizer guard so a pathological
# paragraph out of a malformed PDF cannot size a tensor allocation. Well above
# what 512 word-piece tokens can hold, so it never truncates real input that
# the tokenizer would have kept.
MAX_CHARS = 4000


class Specter2Encoder:
    """Batches text through SPECTER2 and validates what comes back.

    `EvidenceIndex` depends on exactly two members: `.dimension` (it sizes the
    Qdrant collection from it) and `.encode(texts, batch_size)`, which must
    return **one vector per input text, in input order** — `upsert()` zips the
    two together with `strict=True`.
    """

    def __init__(
        self,
        embed: EmbedFn,
        dimension: int,
        *,
        default_batch_size: int = 16,
        max_batch_size: int = MAX_BATCH_SIZE,
        max_chars: int = MAX_CHARS,
    ) -> None:
        if dimension <= 0:
            raise ValueError(f"dimension must be positive, got {dimension}")
        if default_batch_size <= 0 or max_batch_size <= 0 or max_chars <= 0:
            raise ValueError("batch and length bounds must be positive")
        self._embed = embed
        self.dimension = dimension
        self._default_batch_size = default_batch_size
        self._max_batch_size = max_batch_size
        self._max_chars = max_chars

    def encode(
        self, texts: Sequence[str], batch_size: int | None = None
    ) -> list[list[float]]:
        """Embed `texts`, preserving order and returning one vector each."""
        if batch_size is None:
            batch_size = self._default_batch_size
        if not isinstance(batch_size, int) or isinstance(batch_size, bool):
            raise TypeError(f"batch_size must be an int, got {batch_size!r}")
        if batch_size <= 0:
            raise ValueError(f"batch_size must be positive, got {batch_size}")
        batch_size = min(batch_size, self._max_batch_size)

        prepared = [self._prepare(text) for text in texts]
        if not prepared:
            return []

        vectors: list[list[float]] = []
        for start in range(0, len(prepared), batch_size):
            batch = prepared[start : start + batch_size]
            vectors.extend(self._embed_batch(batch))
        return vectors

    def _prepare(self, text: object) -> str:
        if not isinstance(text, str):
            raise TypeError(f"encode() expects str, got {type(text).__name__}")
        return text[: self._max_chars]

    def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        raw = self._embed(batch)
        vectors = [[float(value) for value in vector] for vector in raw]
        if len(vectors) != len(batch):
            raise ValueError(
                f"encoder returned {len(vectors)} vectors for {len(batch)} texts"
            )
        for vector in vectors:
            if len(vector) != self.dimension:
                raise ValueError(
                    f"encoder returned a {len(vector)}-d vector, "
                    f"expected {self.dimension}-d"
                )
            if not all(math.isfinite(value) for value in vector):
                raise ValueError("encoder returned a non-finite vector")
        return vectors


def load_specter2(settings: Settings) -> tuple[EmbedFn, int]:
    """Load SPECTER2 and return `(embed_fn, dimension)`.

    Heavy imports live here so importing this module stays free. The returned
    closure runs under `torch.inference_mode()` on a model in eval mode, so the
    same text always yields the same vector — Stage 5 has to be reproducible
    for Phase 6's clusters and Phase 13's evaluation to mean anything.
    """
    import torch
    from adapters import AutoAdapterModel
    from transformers import AutoTokenizer

    revision = getattr(settings, "embedding_model_revision", None)
    tokenizer = AutoTokenizer.from_pretrained(
        settings.embedding_model, revision=revision
    )
    model = AutoAdapterModel.from_pretrained(
        settings.embedding_model, revision=revision
    )
    # The adapter is a different repository with its own commits, so the base
    # model's revision must never be sent to it (D-S5-REV-01). It is pinned
    # with an `EMBEDDING_ADAPTER=org/repo@<revision>` suffix instead, which
    # changes `embed_version` exactly as a pinned base revision does.
    adapter, adapter_revision = split_revision(settings.embedding_adapter)
    model.load_adapter(
        adapter,
        source="hf",
        load_as="specter2",
        set_active=True,
        revision=adapter_revision,
    )
    model.eval()

    dimension = int(model.config.hidden_size)
    max_tokens = settings.embedding_max_tokens
    logger.info(
        "Loaded SPECTER2 %s@%s + adapter %s@%s (%d-d)",
        settings.embedding_model,
        resolved_revision(settings.embedding_model, revision) or revision,
        adapter,
        resolved_revision(adapter, adapter_revision) or adapter_revision,
        dimension,
    )

    def embed(texts: list[str]) -> list[list[float]]:
        inputs = tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=max_tokens,
            return_token_type_ids=False,
            return_tensors="pt",
        )
        with torch.inference_mode():
            output = model(**inputs)
        # SPECTER2 represents a passage by its [CLS] token, per the model card.
        return output.last_hidden_state[:, 0, :].tolist()

    return embed, dimension


@lru_cache(maxsize=1)
def get_encoder() -> Specter2Encoder:
    """Process-wide encoder so the checkpoint is loaded at most once.

    Deliberately lazy: `EvidenceIndex` resolves it through this function only
    when it actually has vectors to produce, so an empty index answers a query
    without paying for a model load.
    """
    settings = get_settings()
    embed, dimension = load_specter2(settings)
    return Specter2Encoder(
        embed,
        dimension,
        default_batch_size=settings.embedding_batch_size,
        max_chars=settings.embedding_max_chars,
    )

"""Phase 5 — SPECTER2 encoder contract.

`app/embeddings/indexer.py` (surviving code) depends on exactly two things
here: `Specter2Encoder` with a `.dimension` attribute and an
`.encode(texts, batch_size)` method returning **one vector per input text in
input order**, and a lazy module-level `get_encoder()`.

The unit tests drive a recording stand-in for the transformer so batching,
ordering, bounds and determinism are asserted against the real
`Specter2Encoder` logic rather than a mocked-out pipeline. The single
`integration`-marked test loads the actual SPECTER2 checkpoint.
"""

import math
from types import SimpleNamespace
from typing import ClassVar

import pytest

from app.embeddings.encoder import Specter2Encoder, get_encoder


class RecordingEmbedder:
    """A deterministic stand-in that records the batches it was handed."""

    def __init__(self, dimension: int = 4) -> None:
        self.dimension = dimension
        self.batches: list[list[str]] = []

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return [
            [float(len(text) + offset) for offset in range(self.dimension)]
            for text in texts
        ]


def _encoder(embedder: RecordingEmbedder, **kwargs) -> Specter2Encoder:
    return Specter2Encoder(embedder, embedder.dimension, **kwargs)


def test_dimension_is_exposed_for_collection_creation() -> None:
    """`EvidenceIndex.ensure_collection` sizes the Qdrant vectors from this."""
    assert _encoder(RecordingEmbedder(768)).dimension == 768


def test_returns_one_vector_per_text_in_input_order() -> None:
    """`upsert` zips records with vectors using strict=True."""
    embedder = RecordingEmbedder()
    texts = ["a", "bb", "ccc", "dddd"]

    vectors = _encoder(embedder).encode(texts, 2)

    assert len(vectors) == len(texts)
    assert [vector[0] for vector in vectors] == [1.0, 2.0, 3.0, 4.0]


def test_texts_are_embedded_in_batches_of_the_requested_size() -> None:
    embedder = RecordingEmbedder()

    _encoder(embedder).encode(["a", "b", "c", "d", "e"], 2)

    assert embedder.batches == [["a", "b"], ["c", "d"], ["e"]]


def test_a_batch_larger_than_the_input_makes_a_single_call() -> None:
    embedder = RecordingEmbedder()

    _encoder(embedder).encode(["a", "b"], 64)

    assert embedder.batches == [["a", "b"]]


def test_batching_does_not_change_the_result() -> None:
    """Reproducibility: the batch size is a memory knob, not a semantic one."""
    encoder = _encoder(RecordingEmbedder())
    texts = ["alpha", "beta", "gamma", "delta", "epsilon"]

    assert encoder.encode(texts, 1) == encoder.encode(texts, 5)


def test_encoding_the_same_text_twice_gives_the_same_vector() -> None:
    encoder = _encoder(RecordingEmbedder())

    assert encoder.encode(["a limitation"], 8) == encoder.encode(["a limitation"], 8)


def test_default_batch_size_is_used_when_none_is_given() -> None:
    embedder = RecordingEmbedder()

    _encoder(embedder, default_batch_size=2).encode(["a", "b", "c"])

    assert embedder.batches == [["a", "b"], ["c"]]


def test_empty_input_returns_empty_without_touching_the_model() -> None:
    """An empty corpus slice must not pay for a forward pass."""
    embedder = RecordingEmbedder()

    assert _encoder(embedder).encode([], 8) == []
    assert embedder.batches == []


@pytest.mark.parametrize("batch_size", [0, -1])
def test_non_positive_batch_size_is_rejected(batch_size: int) -> None:
    """A misconfigured EMBEDDING_BATCH_SIZE must fail loudly, not spin."""
    with pytest.raises(ValueError):
        _encoder(RecordingEmbedder()).encode(["a"], batch_size)


def test_batch_size_is_capped_so_one_call_cannot_exhaust_memory() -> None:
    embedder = RecordingEmbedder()

    _encoder(embedder, max_batch_size=2).encode(["a", "b", "c", "d"], 1000)

    assert embedder.batches == [["a", "b"], ["c", "d"]]


def test_overlong_text_is_truncated_before_it_reaches_the_model() -> None:
    """Untrusted corpus text must not size a tensor allocation."""
    embedder = RecordingEmbedder()

    _encoder(embedder, max_chars=10).encode(["x" * 5000], 4)

    assert embedder.batches == [["x" * 10]]


def test_truncation_does_not_alter_texts_within_the_limit() -> None:
    embedder = RecordingEmbedder()

    _encoder(embedder, max_chars=64).encode(["a short limitation"], 4)

    assert embedder.batches == [["a short limitation"]]


def test_non_string_input_is_rejected() -> None:
    with pytest.raises(TypeError):
        _encoder(RecordingEmbedder()).encode(["fine", 5], 4)  # type: ignore[list-item]


def test_a_model_returning_the_wrong_number_of_vectors_is_rejected() -> None:
    """A silent length mismatch would misalign every payload after it."""

    class ShortEmbedder(RecordingEmbedder):
        def __call__(self, texts):
            super().__call__(texts)
            return [[0.0] * self.dimension]

    with pytest.raises(ValueError):
        _encoder(ShortEmbedder()).encode(["a", "b"], 8)


def test_a_model_returning_the_wrong_dimension_is_rejected() -> None:
    """Qdrant would reject the point anyway; fail with a readable message."""

    class WideEmbedder(RecordingEmbedder):
        def __call__(self, texts):
            super().__call__(texts)
            return [[0.0] * (self.dimension + 1) for _ in texts]

    with pytest.raises(ValueError):
        _encoder(WideEmbedder()).encode(["a"], 8)


def test_a_non_finite_vector_is_rejected() -> None:
    """NaNs would poison cosine scoring for every later query."""

    class NanEmbedder(RecordingEmbedder):
        def __call__(self, texts):
            super().__call__(texts)
            return [[float("nan")] * self.dimension for _ in texts]

    with pytest.raises(ValueError):
        _encoder(NanEmbedder()).encode(["a"], 8)


def test_vectors_are_plain_json_serializable_floats() -> None:
    """qdrant-client serializes the point; numpy scalars must not leak through."""
    numpy = pytest.importorskip("numpy")

    class NumpyEmbedder(RecordingEmbedder):
        def __call__(self, texts):
            super().__call__(texts)
            return numpy.zeros((len(texts), self.dimension), dtype=numpy.float32)

    vectors = _encoder(NumpyEmbedder()).encode(["a", "b"], 8)

    assert isinstance(vectors, list)
    assert all(isinstance(vector, list) for vector in vectors)
    assert all(type(value) is float for vector in vectors for value in vector)


def test_a_non_positive_dimension_is_rejected() -> None:
    with pytest.raises(ValueError):
        Specter2Encoder(RecordingEmbedder(), 0)


def test_get_encoder_is_lazy_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """The checkpoint is loaded at most once per process, on first use."""
    calls: list[str] = []

    def fake_load(settings):
        calls.append(settings.embedding_model)
        return RecordingEmbedder(8), 8

    fake_settings = SimpleNamespace(
        embedding_model="allenai/specter2_base",
        embedding_adapter="allenai/specter2",
        embedding_batch_size=4,
        embedding_max_chars=4000,
        embedding_max_tokens=512,
    )

    get_encoder.cache_clear()
    monkeypatch.setattr("app.embeddings.encoder.load_specter2", fake_load)
    monkeypatch.setattr("app.embeddings.encoder.get_settings", lambda: fake_settings)
    try:
        first = get_encoder()
        second = get_encoder()
    finally:
        get_encoder.cache_clear()

    assert first is second
    assert calls == ["allenai/specter2_base"]
    assert first.dimension == 8


class _FakeAdapterModel:
    calls: ClassVar[list] = []

    def __init__(self):
        self.config = SimpleNamespace(hidden_size=8)

    @classmethod
    def from_pretrained(cls, name, revision=None):
        cls.calls.append(("model", name, revision))
        return cls()

    def load_adapter(self, name, source, load_as, set_active, revision=None):
        type(self).calls.append(("adapter", name, revision))

    def eval(self):
        return self


def _load_with_fakes(monkeypatch, **settings):
    import sys
    import types

    import transformers

    _FakeAdapterModel.calls = []
    monkeypatch.setitem(
        sys.modules,
        "adapters",
        types.SimpleNamespace(AutoAdapterModel=_FakeAdapterModel),
    )
    monkeypatch.setattr(
        transformers.AutoTokenizer,
        "from_pretrained",
        lambda name, revision=None: _FakeAdapterModel.calls.append(
            ("tokenizer", name, revision)
        ),
    )
    from app.embeddings.encoder import load_specter2

    defaults = {
        "embedding_model": "allenai/specter2_base",
        "embedding_adapter": "allenai/specter2",
        "embedding_model_revision": None,
        "embedding_max_tokens": 512,
    }
    _, dimension = load_specter2(SimpleNamespace(**{**defaults, **settings}))
    assert dimension == 8
    return _FakeAdapterModel.calls


def test_the_base_model_revision_is_never_sent_to_the_adapter_repository(monkeypatch):
    """The two repos have unrelated commit histories (D-S5-REV-01).

    Passing `EMBEDDING_MODEL_REVISION` to `load_adapter` as well meant that
    pinning the base model to a commit made the adapter impossible to load,
    so the deployment could only ever follow both repos' moving `main`.
    """
    calls = _load_with_fakes(monkeypatch, embedding_model_revision="base-sha")
    assert ("tokenizer", "allenai/specter2_base", "base-sha") in calls
    assert ("model", "allenai/specter2_base", "base-sha") in calls
    assert ("adapter", "allenai/specter2", None) in calls


def test_the_adapter_can_be_pinned_with_a_repo_at_revision_suffix(monkeypatch):
    calls = _load_with_fakes(
        monkeypatch, embedding_adapter="allenai/specter2@adapter-sha"
    )
    assert ("adapter", "allenai/specter2", "adapter-sha") in calls
    assert ("model", "allenai/specter2_base", None) in calls


@pytest.mark.integration
def test_real_specter2_checkpoint_embeds_scientific_text() -> None:
    """Loads the actual `allenai/specter2_base` + `allenai/specter2` adapter."""
    from app.core.config import Settings
    from app.embeddings.encoder import load_specter2

    # Only the embedding fields matter here; the rest of Settings needs a
    # populated .env this test has no reason to depend on.
    defaults = Settings.model_fields
    embed, dimension = load_specter2(
        SimpleNamespace(
            embedding_model=defaults["embedding_model"].default,
            embedding_adapter=defaults["embedding_adapter"].default,
            embedding_max_tokens=defaults["embedding_max_tokens"].default,
        )
    )
    encoder = Specter2Encoder(embed, dimension)

    assert encoder.dimension == 768

    texts = [
        "Performance decreases significantly for multilingual datasets.",
        "Existing benchmarks focus predominantly on English prompts.",
        "We implement a distributed key-value store for cache coherence.",
    ]
    vectors = encoder.encode(texts, 2)

    assert len(vectors) == 3
    assert all(len(vector) == 768 for vector in vectors)
    assert all(math.isfinite(value) for vector in vectors for value in vector)

    def cosine(left, right):
        dot = sum(a * b for a, b in zip(left, right, strict=True))
        norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(
            sum(b * b for b in right)
        )
        return dot / norm

    # The two multilingual-coverage limitations must sit closer together than
    # either does to an unrelated systems sentence — this is the whole premise
    # of README §3 (the same gap phrased five different ways).
    assert cosine(vectors[0], vectors[1]) > cosine(vectors[0], vectors[2])

    # Reproducibility: identical text, and a different batch size, agree.
    again = encoder.encode(texts, 8)
    for left, right in zip(vectors, again, strict=True):
        assert all(abs(a - b) < 1e-4 for a, b in zip(left, right, strict=True))

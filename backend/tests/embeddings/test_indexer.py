"""Phase 5 — `EvidenceIndex` against a real Qdrant client and a real database.

`tests/embeddings/test_indexing.py` (surviving code) covers the record-building
shape and the empty-index short circuit. This module covers the rest of Stage 5
end to end: the vector store is a genuine `qdrant_client` local instance — real
collection config, real filters, real cosine scoring, real scroll pagination —
and `index_all` runs against a real SQLAlchemy session, so nothing about the
storage boundary is mocked away.
"""

import logging
from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient, models
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.embeddings.encoder import Specter2Encoder
from app.embeddings.indexer import (
    MAX_RETRIEVAL_LIMIT,
    EvidenceIndex,
    build_index_records,
    index_all,
)
from app.embeddings.schema import PAYLOAD_FIELDS
from app.extraction.schema import ExtractedStatement, FactType
from app.extraction.storage import ExtractedFact, replace_facts
from app.ingestion.corpus import Base, Paper
from app.parsing.storage import ParsedDocument

DIMENSION = 24

VOCABULARY = [
    "multilingual",
    "english",
    "language",
    "languages",
    "prompt",
    "injection",
    "detection",
    "low-resource",
    "benchmark",
    "evaluate",
    "cache",
    "coherence",
    "distributed",
    "store",
    "latency",
    "throughput",
    "gpu",
    "kernel",
    "scheduler",
    "compiler",
    "retrieval",
    "grounding",
    "citation",
    "hallucination",
]


def lexical_embed(texts: list[str]) -> list[list[float]]:
    """A deterministic bag-of-words embedder over a fixed vocabulary.

    Not SPECTER2 — but a real vector function, so cosine ordering in these
    tests reflects genuine text overlap rather than a stubbed score. The real
    checkpoint is exercised by the `integration`-marked test in test_encoder.py.
    """
    vectors = []
    for text in texts:
        tokens = {token.strip(".,;:()").casefold() for token in text.split()}
        vector = [1.0 if word in tokens else 0.0 for word in VOCABULARY]
        if not any(vector):
            vector[0] = 1e-6
        vectors.append(vector)
    return vectors


@pytest.fixture()
def encoder() -> Specter2Encoder:
    return Specter2Encoder(lexical_embed, DIMENSION)


@pytest.fixture()
def settings() -> SimpleNamespace:
    return SimpleNamespace(
        qdrant_url="http://qdrant:6333",
        qdrant_collection="evidence",
        embedding_batch_size=4,
        retrieval_top_n=5,
    )


@pytest.fixture()
def index(settings, encoder) -> EvidenceIndex:
    return EvidenceIndex(
        settings=settings, client=QdrantClient(":memory:"), encoder=encoder
    )


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    yield db
    db.close()


class FakeNeo4jSession:
    """Answers `get_paper_node_id`'s single Cypher query."""

    def __init__(self, node_ids: dict[int, str] | None = None) -> None:
        self.node_ids = {} if node_ids is None else node_ids

    def run(self, _query: str, paper_id: int):
        node_id = self.node_ids.get(paper_id)
        return SimpleNamespace(single=lambda: {"node_id": node_id} if node_id else None)


def _paper(**overrides) -> SimpleNamespace:
    values = {"id": 7, "year": 2024, "venue": "ACL"}
    values.update(overrides)
    return SimpleNamespace(**values)


def _document(sections) -> SimpleNamespace:
    return SimpleNamespace(sections=sections)


def _fact(**overrides) -> SimpleNamespace:
    values = {
        "id": 1,
        "fact_type": "limitation",
        "text": "Performance drops for multilingual prompt injection detection.",
        "section_type": "Limitations",
        "section_index": 0,
        "paragraph_index": 0,
        "sentence_index": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# --------------------------------------------------------------------------
# build_index_records — EMBED-01 input selection
# --------------------------------------------------------------------------


def test_only_limitation_and_future_work_facts_are_indexed() -> None:
    """Contributions/datasets/metrics are graph material, not gap evidence."""
    facts = [
        _fact(id=1, fact_type="limitation"),
        _fact(id=2, fact_type="future_work"),
        _fact(id=3, fact_type="contribution"),
        _fact(id=4, fact_type="dataset"),
    ]

    records = build_index_records(_paper(), _document([]), facts, "n1")

    assert [record.source_type for record in records] == ["limitation", "future_work"]


def test_blank_fact_text_is_skipped_rather_than_embedded() -> None:
    """A whitespace-only statement would burn a forward pass and pollute search."""
    facts = [_fact(id=1, text="   "), _fact(id=2, text="\n\t"), _fact(id=3)]

    records = build_index_records(_paper(), _document([]), facts, "n1")

    assert [record.fact_id for record in records] == [3]


def test_fact_text_whitespace_is_normalized() -> None:
    facts = [_fact(text="Poor   multilingual\n  coverage.")]

    records = build_index_records(_paper(), _document([]), facts, "n1")

    assert records[0].text == "Poor multilingual coverage."


def test_malformed_sections_do_not_abort_a_paper() -> None:
    """Parsed JSON is data; one bad row must not take the batch down."""
    document = _document(
        [
            "not-a-section",
            {"type": "Limitations"},
            {"type": "Results", "paragraphs": ["not-a-paragraph", {"text": None}]},
            {"type": "Discussion", "paragraphs": [{"text": "A real paragraph."}]},
        ]
    )

    records = build_index_records(_paper(), document, [], "n1")

    assert [record.text for record in records] == ["A real paragraph."]


def test_record_ids_are_deterministic_and_position_unique() -> None:
    """Re-indexing must overwrite the same point, never duplicate it."""
    document = _document(
        [{"type": "Limitations", "paragraphs": [{"text": "One."}, {"text": "Two."}]}]
    )

    first = build_index_records(_paper(), document, [_fact()], "n1")
    second = build_index_records(_paper(), document, [_fact()], "n1")

    ids = [record.record_id for record in first]
    assert ids == [record.record_id for record in second]
    assert len(set(ids)) == 3


def test_records_carry_every_metadata_field(index) -> None:
    document = _document(
        [{"type": "Limitations", "paragraphs": [{"text": "English-only benchmark."}]}]
    )

    record = build_index_records(_paper(), document, [], "4:abc:17")[0]

    assert record.payload() == {
        "paper_id": 7,
        "text": "English-only benchmark.",
        "source_type": "paragraph",
        "fact_id": None,
        "year": 2024,
        "venue": "ACL",
        "section_type": "Limitations",
        "graph_node_id": "4:abc:17",
        "text_sha256": record.payload()["text_sha256"],
        "embed_version": None,
    }
    assert set(record.payload()) == set(PAYLOAD_FIELDS)


# --------------------------------------------------------------------------
# Collection configuration — EMBED-02
# --------------------------------------------------------------------------


def test_collection_is_created_with_the_encoder_dimension_and_cosine(index) -> None:
    index.ensure_collection()

    params = index.client.get_collection("evidence").config.params.vectors
    assert params.size == DIMENSION
    assert params.distance == models.Distance.COSINE


def test_ensure_collection_never_recreates_an_existing_collection(index) -> None:
    """Recreating would silently destroy every indexed vector."""
    index.upsert(build_index_records(_paper(), _document([]), [_fact()], "n1"))
    before = index.client.count("evidence", exact=True).count

    index.ensure_collection()

    assert index.client.count("evidence", exact=True).count == before == 1


def test_upsert_refuses_a_collection_with_a_different_vector_size(
    settings, encoder
) -> None:
    """A model swap must fail loudly instead of writing unsearchable points."""
    client = QdrantClient(":memory:")
    client.create_collection(
        "evidence",
        vectors_config=models.VectorParams(size=8, distance=models.Distance.COSINE),
    )
    index = EvidenceIndex(settings=settings, client=client, encoder=encoder)

    with pytest.raises(ValueError, match="dimension"):
        index.upsert(build_index_records(_paper(), _document([]), [_fact()], "n1"))


def test_payload_indexes_cover_every_filterable_key(settings, encoder) -> None:
    """Local Qdrant no-ops payload indexes, so assert the calls themselves."""

    class RecordingClient(QdrantClient):
        def __init__(self):
            super().__init__(":memory:")
            self.indexed: list[str] = []

        def create_payload_index(self, *args, **kwargs):
            self.indexed.append(kwargs["field_name"])
            return super().create_payload_index(*args, **kwargs)

    client = RecordingClient()
    index = EvidenceIndex(settings=settings, client=client, encoder=encoder)

    index.ensure_payload_indexes()

    assert set(client.indexed) == {
        "paper_id",
        "year",
        "venue",
        "section_type",
        "source_type",
    }


def test_payload_index_creation_survives_a_concurrent_conflict(
    settings, encoder
) -> None:
    import httpx
    from qdrant_client.http.exceptions import UnexpectedResponse

    class ConflictingClient(QdrantClient):
        def create_payload_index(self, *args, **kwargs):
            raise UnexpectedResponse(409, "Conflict", b"exists", httpx.Headers())

    index = EvidenceIndex(
        settings=settings, client=ConflictingClient(":memory:"), encoder=encoder
    )

    index.ensure_payload_indexes()


# --------------------------------------------------------------------------
# Upsert — EMBED-01/02/03
# --------------------------------------------------------------------------


def test_upsert_stores_vectors_and_payloads(index) -> None:
    records = build_index_records(
        _paper(),
        _document(
            [{"type": "Limitations", "paragraphs": [{"text": "English-only data."}]}]
        ),
        [_fact()],
        "4:abc:17",
    )

    assert index.upsert(records) == 2

    page, _ = index.client.scroll(
        "evidence", limit=10, with_payload=True, with_vectors=True
    )
    assert len(page) == 2
    for point in page:
        assert len(point.vector) == DIMENSION
        assert point.payload["graph_node_id"] == "4:abc:17"
        assert point.payload["paper_id"] == 7


def test_upsert_is_idempotent(index) -> None:
    """Re-running the batch job must not duplicate evidence."""
    records = build_index_records(_paper(), _document([]), [_fact()], "n1")

    index.upsert(records)
    index.upsert(records)

    assert index.client.count("evidence", exact=True).count == 1


def test_upsert_of_nothing_creates_no_collection(index) -> None:
    assert index.upsert([]) == 0
    assert index.client.collection_exists("evidence") is False


def test_upsert_batches_at_the_configured_size(settings, index) -> None:
    calls: list[int] = []
    inner = index.encoder.encode

    def counting_encode(texts, batch_size=None):
        calls.append(batch_size)
        return inner(texts, batch_size)

    index.encoder.encode = counting_encode  # type: ignore[method-assign]
    index.upsert(build_index_records(_paper(), _document([]), [_fact()], "n1"))

    assert calls == [settings.embedding_batch_size]


# --------------------------------------------------------------------------
# Retrieval — EMBED-04
# --------------------------------------------------------------------------


@pytest.fixture()
def populated(index) -> EvidenceIndex:
    corpus = [
        (
            _paper(id=1, year=2023, venue="ACL"),
            "Multilingual prompt injection detection remains underexplored.",
            "limitation",
        ),
        (
            _paper(id=2, year=2024, venue="EMNLP"),
            "English benchmark coverage dominates evaluation.",
            "future_work",
        ),
        (
            _paper(id=3, year=2024, venue="OSDI"),
            "Distributed cache coherence adds latency.",
            "limitation",
        ),
    ]
    for position, (paper, text, fact_type) in enumerate(corpus):
        index.upsert(
            build_index_records(
                paper,
                _document(
                    [{"type": "Results", "paragraphs": [{"text": "GPU kernel."}]}]
                ),
                [_fact(id=position + 1, fact_type=fact_type, text=text)],
                f"4:node:{paper.id}",
            )
        )
    return index


def test_semantic_query_returns_the_relevant_paragraph_first(populated) -> None:
    results = populated.retrieve("multilingual prompt injection detection")

    assert results
    assert results[0]["paper_id"] == 1
    assert results[0]["source_type"] == "limitation"


def test_results_carry_metadata_score_and_graph_linkage(populated) -> None:
    top = populated.retrieve("multilingual prompt injection detection")[0]

    assert top["id"]
    assert isinstance(top["score"], float)
    assert top["year"] == 2023
    assert top["venue"] == "ACL"
    assert top["section_type"] == "Limitations"
    assert top["graph_node_id"] == "4:node:1"
    assert top["text"].startswith("Multilingual prompt injection")


def test_metadata_filters_restrict_the_result_set(populated) -> None:
    """EMBED-02: filtered search on year, venue, section type and paper id."""
    assert {
        row["paper_id"]
        for row in populated.retrieve("languages", filters={"year": 2024})
    } == {
        2,
        3,
    }
    assert all(
        row["venue"] == "ACL"
        for row in populated.retrieve("languages", filters={"venue": "ACL"})
    )
    assert all(
        row["source_type"] == "future_work"
        for row in populated.retrieve(
            "languages", filters={"source_type": "future_work"}
        )
    )
    assert all(
        row["paper_id"] == 3
        for row in populated.retrieve("latency", filters={"paper_id": 3})
    )


def test_combined_filters_are_conjunctive(populated) -> None:
    results = populated.retrieve("languages", filters={"year": 2024, "venue": "OSDI"})

    assert {row["paper_id"] for row in results} == {3}


def test_a_filter_matching_nothing_returns_nothing(populated) -> None:
    assert populated.retrieve("languages", filters={"year": 1999}) == []


def test_limit_caps_the_result_count(populated) -> None:
    assert len(populated.retrieve("languages", limit=2)) == 2


def test_limit_defaults_to_the_configured_top_n(populated, settings) -> None:
    settings.retrieval_top_n = 1

    assert len(populated.retrieve("languages")) == 1


def test_retrieve_on_a_missing_collection_returns_empty(index) -> None:
    assert index.retrieve("anything") == []


# --------------------------------------------------------------------------
# gap_points — the Phase 6 hand-off
# --------------------------------------------------------------------------


def test_gap_points_returns_only_gap_statements_with_their_vectors(
    populated,
) -> None:
    points = populated.gap_points()

    assert {point["source_type"] for point in points} == {"limitation", "future_work"}
    assert all(len(point["vector"]) == DIMENSION for point in points)
    assert all(point["graph_node_id"] for point in points)


def test_gap_points_pages_past_the_scroll_window(index) -> None:
    """The scroll loop uses a 256-point page; 300 statements must all return."""
    facts = [
        _fact(id=position, text=f"Limitation number {position} on languages.")
        for position in range(300)
    ]
    index.upsert(build_index_records(_paper(), _document([]), facts, "n1"))

    assert len(index.gap_points()) == 300


def test_gap_points_on_an_empty_index_returns_empty(index) -> None:
    assert index.gap_points() == []


# --------------------------------------------------------------------------
# index_all — corpus-wide batch job
# --------------------------------------------------------------------------


def _store_paper(session, paper_id: int, **overrides) -> Paper:
    values = {
        "id": paper_id,
        "title": f"Paper {paper_id}",
        "year": 2024,
        "venue": "ACL",
        "source": "arxiv",
    }
    values.update(overrides)
    paper = Paper(**values)
    session.add(paper)
    session.flush()
    return paper


def _store_document(session, paper_id: int, text: str) -> None:
    session.add(
        ParsedDocument(
            paper_id=paper_id,
            sections=[{"type": "Limitations", "paragraphs": [{"text": text}]}],
            parser_used="grobid",
        )
    )
    session.flush()


def _store_fact(session, paper_id: int, text: str) -> None:
    session.add(
        ExtractedFact(
            paper_id=paper_id,
            fact_type="limitation",
            text=text,
            section_type="Limitations",
            section_heading="Limitations",
            section_index=0,
            paragraph_index=0,
            sentence_index=0,
            confidence=0.9,
            extraction_method="rule",
        )
    )
    session.flush()


def test_index_all_indexes_every_linked_paper(session, index) -> None:
    for paper_id in (1, 2):
        _store_paper(session, paper_id)
        _store_document(session, paper_id, f"Paragraph for paper {paper_id}.")
        _store_fact(session, paper_id, f"Limitation for paper {paper_id}.")
    neo4j = FakeNeo4jSession({1: "4:n:1", 2: "4:n:2"})

    assert index_all(session, neo4j, index) == 4
    assert index.client.count("evidence", exact=True).count == 4


def test_index_all_skips_papers_that_were_never_parsed(session, index) -> None:
    _store_paper(session, 1)
    _store_paper(session, 2)
    _store_document(session, 2, "Only paper two was parsed.")

    assert index_all(session, FakeNeo4jSession({1: "4:n:1", 2: "4:n:2"}), index) == 1


def test_index_all_skips_papers_missing_from_the_graph(session, index) -> None:
    """EMBED-03: a vector with no Neo4j node to link back to is not written."""
    _store_paper(session, 1)
    _store_document(session, 1, "Parsed but never graphed.")

    assert index_all(session, FakeNeo4jSession({}), index) == 0
    assert index.client.collection_exists("evidence") is False


def test_index_all_is_source_agnostic(session, index) -> None:
    """Hard constraint: Stage 2 onward must never branch on `Paper.source`."""
    for paper_id, source in ((1, "arxiv"), (2, "manual_upload"), (3, "acl_anthology")):
        _store_paper(session, paper_id, source=source)
        _store_document(session, paper_id, f"Paragraph {paper_id}.")
    neo4j = FakeNeo4jSession({1: "4:n:1", 2: "4:n:2", 3: "4:n:3"})

    index_all(session, neo4j, index)

    page, _ = index.client.scroll("evidence", limit=10, with_payload=True)
    assert sorted(point.payload["paper_id"] for point in page) == [1, 2, 3]


def test_index_all_creates_payload_indexes_once(session, settings, encoder) -> None:
    calls: list[str] = []

    class RecordingClient(QdrantClient):
        def create_payload_index(self, *args, **kwargs):
            calls.append(kwargs["field_name"])
            return super().create_payload_index(*args, **kwargs)

    index = EvidenceIndex(
        settings=settings, client=RecordingClient(":memory:"), encoder=encoder
    )
    for paper_id in (1, 2):
        _store_paper(session, paper_id)
        _store_document(session, paper_id, f"Paragraph {paper_id}.")

    index_all(session, FakeNeo4jSession({1: "4:n:1", 2: "4:n:2"}), index)

    assert len(calls) == len(set(calls))


def test_index_all_on_an_empty_corpus_touches_nothing(session, index) -> None:
    assert index_all(session, FakeNeo4jSession({}), index) == 0
    assert index.client.collection_exists("evidence") is False


# --------------------------------------------------------------------------
# Review follow-ups — input validation and resource bounds
# --------------------------------------------------------------------------


def test_an_unknown_filter_key_is_rejected(populated) -> None:
    """Mode B passes user-shaped filters; a typo must not silently return []."""
    with pytest.raises(ValueError, match="cannot filter on"):
        populated.retrieve("languages", filters={"yr": 2024})


def test_every_indexed_field_is_filterable(populated) -> None:
    """No payload field is accidentally excluded from the allow-list."""
    probes = {
        "text_sha256": "hash",
        "embed_version": "version",
        "paper_id": 1,
        "text": "unused",
        "source_type": "limitation",
        "fact_id": 1,
        "year": 2024,
        "venue": "ACL",
        "section_type": "Limitations",
        "graph_node_id": "4:node:1",
    }
    assert set(probes) == set(PAYLOAD_FIELDS)
    for field, value in probes.items():
        populated.retrieve("languages", filters={field: value})


@pytest.mark.parametrize("limit", [0, -5])
def test_a_non_positive_limit_is_rejected(populated, limit: int) -> None:
    with pytest.raises(ValueError, match="limit must be positive"):
        populated.retrieve("languages", limit=limit)


def test_a_huge_limit_is_capped_before_it_reaches_qdrant(populated) -> None:
    """An unbounded limit would pull an arbitrary slice of the corpus."""
    seen: list[int] = []
    inner = populated.client.query_points

    def recording_query_points(**kwargs):
        seen.append(kwargs["limit"])
        return inner(**kwargs)

    populated.client.query_points = recording_query_points  # type: ignore[method-assign]
    populated.retrieve("languages", limit=10_000_000)

    assert seen == [MAX_RETRIEVAL_LIMIT]


def test_an_invalid_limit_is_rejected_even_on_an_empty_index(index) -> None:
    """Validation must not depend on whether the collection happens to exist."""
    with pytest.raises(ValueError):
        index.retrieve("languages", limit=0)


def test_upsert_chunks_the_qdrant_write_at_the_batch_size(index, settings) -> None:
    """A paper with hundreds of paragraphs must not go in one HTTP body."""
    settings.embedding_batch_size = 3
    sizes: list[int] = []
    inner = index.client.upsert

    def recording_upsert(**kwargs):
        sizes.append(len(kwargs["points"]))
        return inner(**kwargs)

    index.client.upsert = recording_upsert  # type: ignore[method-assign]
    facts = [
        _fact(id=position, text=f"Limitation {position}.") for position in range(7)
    ]

    assert index.upsert(build_index_records(_paper(), _document([]), facts, "n1")) == 7
    assert sizes == [3, 3, 1]
    assert index.client.count("evidence", exact=True).count == 7


def test_stored_text_is_capped_at_the_encoder_budget() -> None:
    """The payload must not claim more text than the vector represents."""
    from app.embeddings.encoder import MAX_CHARS

    records = build_index_records(
        _paper(), _document([]), [_fact(text="word " * 5000)], "n1"
    )

    assert len(records[0].text) == MAX_CHARS


def test_index_all_skips_a_failing_paper_and_finishes_the_rest(
    session, index, caplog
) -> None:
    """One bad paper must not abandon a corpus-wide run."""
    for paper_id in (1, 2, 3):
        _store_paper(session, paper_id)
        _store_document(session, paper_id, f"Paragraph {paper_id}.")
    neo4j = FakeNeo4jSession({1: "4:n:1", 2: "4:n:2", 3: "4:n:3"})

    inner = index.upsert
    calls: list[int] = []

    def flaky_upsert(records):
        calls.append(records[0].paper_id)
        if records[0].paper_id == 2:
            raise RuntimeError("qdrant hiccup")
        return inner(records)

    index.upsert = flaky_upsert  # type: ignore[method-assign]

    assert index_all(session, neo4j, index) == 2
    assert calls == [1, 2, 3]
    assert index.client.count("evidence", exact=True).count == 2


# --------------------------------------------------------------------------
# Repeated pipeline runs (P5) — a second POST /pipeline/run must not duplicate
# or orphan a paper's vectors. See .planning/phases/08-gap-ranking-stage-8/.
# --------------------------------------------------------------------------


def _statement(**overrides) -> ExtractedStatement:
    values = {
        "fact_type": FactType.LIMITATION,
        "text": "Performance drops for multilingual prompt injection detection.",
        "section_type": "Limitations",
        "section_heading": "Limitations",
        "section_index": 0,
        "paragraph_index": 0,
        "sentence_index": 0,
        "confidence": 0.9,
        "extraction_method": "rule",
    }
    values.update(overrides)
    return ExtractedStatement(**values)


def test_fact_point_id_survives_a_re_extraction(session) -> None:
    """`replace_facts` mints fresh autoincrement ids; the point id must not move.

    Two papers, so the surrogate ids genuinely differ between runs (with a
    single row SQLite happens to reuse rowid 1 and hides the defect).
    """
    for paper_id in (1, 2):
        _store_paper(session, paper_id)
    statements = [_statement()]

    first = replace_facts(session, 1, statements)
    replace_facts(session, 2, statements)
    second = replace_facts(session, 1, statements)

    assert first[0].id != second[0].id, "precondition: the surrogate key moved"
    paper = _paper(id=1)
    assert (
        build_index_records(paper, _document([]), first, "n1")[0].record_id
        == build_index_records(paper, _document([]), second, "n1")[0].record_id
    )


def test_fact_point_ids_separate_distinct_statements() -> None:
    """Stability must not come from collapsing genuinely different evidence."""
    facts = [
        _fact(id=1),
        _fact(id=2, sentence_index=1),
        _fact(id=3, paragraph_index=1),
        _fact(id=4, section_index=1),
        _fact(id=5, fact_type="future_work"),
        _fact(id=6, text="A different limitation entirely."),
    ]

    records = build_index_records(_paper(), _document([]), facts, "n1")

    assert len({record.record_id for record in records}) == len(facts)


def test_a_second_pipeline_run_leaves_one_point_per_statement(session, index) -> None:
    """The whole defect, end to end: re-index twice, count the gap vectors."""
    neo4j = FakeNeo4jSession({1: "4:n:1", 2: "4:n:2"})
    for paper_id in (1, 2):
        _store_paper(session, paper_id)
        _store_document(session, paper_id, f"Paragraph for paper {paper_id}.")

    def extract_and_index() -> None:
        for paper_id in (1, 2):
            replace_facts(
                session,
                paper_id,
                [
                    _statement(
                        text=f"Multilingual coverage is poor in paper {paper_id}."
                    )
                ],
            )
        index_all(session, neo4j, index)

    extract_and_index()
    first = index.client.count("evidence", exact=True).count
    extract_and_index()

    assert index.client.count("evidence", exact=True).count == first
    assert len(index.gap_points()) == 2


def test_re_indexing_prunes_statements_that_no_longer_exist(session, index) -> None:
    """Evidence that vanished from the corpus must not linger in the index."""
    neo4j = FakeNeo4jSession({1: "4:n:1"})
    _store_paper(session, 1)
    _store_document(session, 1, "A paragraph.")
    replace_facts(
        session,
        1,
        [_statement(), _statement(sentence_index=1, text="English-only benchmark.")],
    )
    index_all(session, neo4j, index)
    assert len(index.gap_points()) == 2

    replace_facts(session, 1, [_statement()])
    index_all(session, neo4j, index)

    assert [point["text"] for point in index.gap_points()] == [_statement().text]


def test_pruning_never_touches_another_paper(session, index) -> None:
    _store_paper(session, 1)
    _store_paper(session, 2)
    for paper_id in (1, 2):
        _store_document(session, paper_id, f"Paragraph {paper_id}.")
        replace_facts(
            session, paper_id, [_statement(text=f"Limitation from paper {paper_id}.")]
        )
    neo4j = FakeNeo4jSession({1: "4:n:1", 2: "4:n:2"})
    index_all(session, neo4j, index)

    replace_facts(session, 1, [_statement(text="A replacement limitation.")])
    index_all(session, neo4j, index)

    assert sorted(point["text"] for point in index.gap_points()) == [
        "A replacement limitation.",
        "Limitation from paper 2.",
    ]


def test_failed_prune_does_not_count_paper_as_complete(session, index, caplog) -> None:
    """Durable upserts alone do not establish a consistent paper snapshot."""
    _store_paper(session, 1)
    _store_document(session, 1, "A paragraph.")
    replace_facts(session, 1, [_statement()])
    neo4j = FakeNeo4jSession({1: "4:n:1"})

    def exploding_delete(*_args, **_kwargs):
        raise RuntimeError("qdrant hiccup")

    index.client.delete = exploding_delete  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING):
        assert index_all(session, neo4j, index) == 0
    assert "Indexing failed for paper 1" in caplog.text

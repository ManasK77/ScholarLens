"""Tests for the admin corpus-reset operation (app.core.corpus_reset).

`reset_corpus` is the single implementation shared by `DELETE /admin/corpus`
and `python -m app.core.corpus_reset`. Neo4j is a `Mock` (no live Neo4j in
CI); Qdrant is a real `qdrant_client` `:memory:` instance, matching the
pattern `tests/embeddings/test_indexer.py` already established.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from qdrant_client import QdrantClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.clustering.schema import GapStatement, StatementCluster
from app.clustering.storage import Cluster, ClusterMember, replace_clusters
from app.core import corpus_reset
from app.core.corpus_reset import reset_corpus
from app.core.pipeline import DerivedState, PaperStageState, PipelineRun, pipeline_run
from app.embeddings.encoder import Specter2Encoder
from app.embeddings.indexer import EvidenceIndex
from app.embeddings.schema import IndexRecord
from app.explanation.schema import GapReportRecord
from app.explanation.storage import GapReport, ReportSnapshot, replace_reports
from app.extraction.schema import ExtractedStatement, FactType
from app.extraction.storage import ExtractedFact, replace_facts
from app.ingestion.corpus import Base, Paper
from app.parsing.storage import ParsedDocument, upsert_parsed_document
from app.ranking.signals import GapSignals
from app.ranking.storage import ClusterRanking, replace_rankings
from app.trends.analysis import TrendSeries
from app.trends.storage import ClusterTrend, replace_trends

DIMENSION = 4


def _lexical_embed(texts):
    return [[float(len(t)), 1.0, 0.0, 0.0] for t in texts]


@pytest.fixture()
def encoder():
    return Specter2Encoder(_lexical_embed, DIMENSION)


@pytest.fixture()
def qdrant_settings():
    return SimpleNamespace(
        qdrant_url="http://qdrant:6333",
        qdrant_collection="scholarlens_evidence_test",
        embedding_batch_size=4,
        retrieval_top_n=5,
        embedding_model_revision=None,
    )


@pytest.fixture()
def index(qdrant_settings, encoder):
    return EvidenceIndex(
        settings=qdrant_settings, client=QdrantClient(":memory:"), encoder=encoder
    )


@pytest.fixture()
def neo4j():
    fake = Mock()

    def execute_write(fn):
        tx = Mock()
        tx.run.return_value.consume.return_value = SimpleNamespace(
            counters=SimpleNamespace(nodes_deleted=3, relationships_deleted=2)
        )
        result = fn(tx)
        fake.last_tx = tx
        return result

    fake.execute_write.side_effect = execute_write
    fake.run.return_value.single.return_value = {"c": 0}
    return fake


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _seed_full_corpus(session, index):
    """One paper with a parsed doc, a fact, a cluster, a ranking, a trend, and a
    report — every table `reset_corpus` is responsible for clearing."""
    session.add(Paper(id=1, source="arxiv", pdf_path=None))
    session.flush()
    upsert_parsed_document(session, 1, [{"type": "body", "paragraphs": []}], "grobid")
    replace_facts(
        session,
        1,
        [
            ExtractedStatement(
                fact_type=FactType.LIMITATION,
                text="Only English is evaluated.",
                section_type="body",
                section_heading=None,
                section_index=0,
                paragraph_index=0,
                sentence_index=0,
                confidence=0.9,
                extraction_method="rule",
            )
        ],
    )
    session.merge(
        PaperStageState(paper_id=1, stage="s3", input_hash="h", status="done")
    )
    [cluster_id] = replace_clusters(
        session,
        [
            StatementCluster(
                label="multilingual",
                members=[
                    GapStatement.from_point(
                        {
                            "id": "11111111-1111-1111-1111-111111111111",
                            "paper_id": 1,
                            "text": "Only English is evaluated.",
                            "year": 2023,
                            "source_type": "limitation",
                            "vector": [0.1, 0.2],
                        }
                    )
                ],
            )
        ],
    )
    replace_rankings(
        session,
        [(cluster_id, 4.0, GapSignals(1, 0.5, 0.0, 10.0, 0.0))],
    )
    replace_trends(session, [(cluster_id, TrendSeries({"2023": 1}, 0.0, 0))])
    replace_reports(
        session,
        [
            GapReportRecord(
                cluster_id=cluster_id,
                gap="Only English is evaluated.",
                evidence=[{"id": "e1"}],
                confidence=0.8,
                suggested_directions=["Evaluate more languages"],
                provider="groq",
                model="test",
            )
        ],
    )
    session.commit()
    index.upsert(
        [
            IndexRecord(
                "11111111-1111-1111-1111-111111111111",
                1,
                "Only English is evaluated.",
                "limitation",
                None,
                2023,
                "ACL",
                "body",
                "node-1",
            )
        ]
    )
    return cluster_id


def test_empty_corpus_reset_succeeds(session, neo4j, index):
    with pipeline_run(session, {}):
        result = reset_corpus(session, neo4j, index)

    assert result["deleted"] is True
    assert result["papers"] == 0
    assert result["pdfs"] == 0
    assert result["qdrant_points"] == 0


def test_populated_reset_removes_all_postgres_data(session, neo4j, index):
    _seed_full_corpus(session, index)

    with pipeline_run(session, {}):
        result = reset_corpus(session, neo4j, index)

    assert result["papers"] == 1
    assert session.scalar(select(Paper)) is None
    assert session.scalar(select(ParsedDocument)) is None
    assert session.scalar(select(ExtractedFact)) is None
    assert session.scalar(select(PaperStageState)) is None
    assert session.scalar(select(Cluster)) is None
    assert session.scalar(select(ClusterMember)) is None
    assert session.scalar(select(ClusterRanking)) is None
    assert session.scalar(select(ClusterTrend)) is None
    assert session.scalar(select(GapReport)) is None
    assert session.scalar(select(ReportSnapshot)) is None
    assert session.scalar(select(DerivedState)) is None
    assert result["postgres_records"]["papers"] == 1
    assert result["postgres_records"]["clusters"] == 1


def test_neo4j_corpus_data_is_cleared(session, neo4j, index):
    _seed_full_corpus(session, index)

    with pipeline_run(session, {}):
        result = reset_corpus(session, neo4j, index)

    assert neo4j.execute_write.call_count == 1
    cypher = neo4j.last_tx.run.call_args[0][0]
    assert "DETACH DELETE" in cypher
    assert "'Paper' IN labels" in cypher
    assert "'Entity' IN labels" in cypher
    assert result["neo4j_nodes"] == 3
    assert result["neo4j_relationships"] == 2


def test_qdrant_corpus_points_are_removed(session, neo4j, index):
    _seed_full_corpus(session, index)
    assert index.has_evidence()

    with pipeline_run(session, {}):
        result = reset_corpus(session, neo4j, index)

    assert result["qdrant_points"] == 1
    assert not index.client.collection_exists(index.collection)


def test_managed_pdf_files_are_removed(session, neo4j, index, tmp_path):
    pdf = tmp_path / "1.pdf"
    pdf.write_bytes(b"%PDF-managed")
    session.add(Paper(id=1, source="arxiv", pdf_path=str(pdf)))
    session.commit()

    with pipeline_run(session, {}):
        result = reset_corpus(session, neo4j, index, str(tmp_path))

    assert result["pdfs"] == 1
    assert not pdf.exists()


def test_pdf_path_outside_storage_root_is_not_deleted(session, neo4j, index, tmp_path):
    """Defense in depth: a corrupted `pdf_path` pointing outside the
    configured PDF root must not let reset_corpus delete an arbitrary file
    elsewhere on disk."""
    storage_root = tmp_path / "pdfs"
    storage_root.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"%PDF-not-managed")
    session.add(Paper(id=1, source="arxiv", pdf_path=str(outside)))
    session.commit()

    with pipeline_run(session, {}):
        result = reset_corpus(session, neo4j, index, str(storage_root))

    assert result["pdfs"] == 0
    assert outside.exists()


def test_unrelated_files_in_pdf_directory_are_preserved(
    session, neo4j, index, tmp_path
):
    managed = tmp_path / "1.pdf"
    managed.write_bytes(b"%PDF-managed")
    stray = tmp_path / "notes.txt"
    stray.write_text("not part of the corpus")
    session.add(Paper(id=1, source="arxiv", pdf_path=str(managed)))
    session.commit()

    with pipeline_run(session, {}):
        reset_corpus(session, neo4j, index, str(tmp_path))

    assert not managed.exists()
    assert stray.exists()


def test_reset_twice_is_idempotent(session, neo4j, index, tmp_path):
    pdf = tmp_path / "1.pdf"
    pdf.write_bytes(b"%PDF-managed")
    session.add(Paper(id=1, source="arxiv", pdf_path=str(pdf)))
    session.commit()

    with pipeline_run(session, {}):
        first = reset_corpus(session, neo4j, index, str(tmp_path))
    with pipeline_run(session, {}):
        second = reset_corpus(session, neo4j, index, str(tmp_path))

    assert first["deleted"] is True
    assert second == {
        "deleted": True,
        "papers": 0,
        "pdfs": 0,
        "postgres_records": second["postgres_records"],
        # An already-empty corpus still rewinds its (already-unused) sequences:
        # every step here is a no-op, not an error.
        "sequences_restarted": second["sequences_restarted"],
        "neo4j_nodes": 3,
        "neo4j_relationships": 2,
        "qdrant_points": 0,
    }
    assert all(count == 0 for count in second["postgres_records"].values())


def test_reset_requires_pipeline_writer_lock(session, neo4j, index):
    with pytest.raises(RuntimeError, match="writer lock"):
        reset_corpus(session, neo4j, index)


def test_neo4j_failure_surfaces_after_postgres_cleared_and_preserves_pdfs(
    session, index, tmp_path
):
    """PostgreSQL is cleared before Neo4j runs (so a paper row can never end
    up naming a `pdf_path` this function has already deleted). If Neo4j then
    fails, the failure must surface -- and because PDF deletion happens last,
    behind the empty-store verification, the PDF file itself must still be
    sitting on disk, not silently destroyed ahead of a failed reset."""
    pdf = tmp_path / "1.pdf"
    pdf.write_bytes(b"%PDF-managed")
    session.add(Paper(id=1, source="arxiv", pdf_path=str(pdf)))
    session.commit()
    failing_neo4j = Mock()
    failing_neo4j.execute_write.side_effect = RuntimeError("neo4j offline")

    with pytest.raises(RuntimeError, match="neo4j offline"), pipeline_run(session, {}):
        reset_corpus(session, failing_neo4j, index, str(tmp_path))

    assert session.scalar(select(Paper)) is None
    assert session.scalar(select(PipelineRun)).status == "failed"
    assert pdf.exists()


def test_qdrant_failure_surfaces_after_postgres_and_neo4j_cleared_and_preserves_pdfs(
    session, neo4j, index, tmp_path
):
    """Same guarantee one store later: Postgres and Neo4j have already
    converged by the time Qdrant is touched, so a Qdrant failure must not
    destroy a PDF the corpus's own bookkeeping no longer references."""
    pdf = tmp_path / "1.pdf"
    pdf.write_bytes(b"%PDF-managed")
    session.add(Paper(id=1, source="arxiv", pdf_path=str(pdf)))
    session.commit()
    index.client.collection_exists = Mock(return_value=True)
    index.client.count = Mock(return_value=SimpleNamespace(count=1))
    index.client.delete_collection = Mock(side_effect=RuntimeError("qdrant offline"))

    with pytest.raises(RuntimeError, match="qdrant offline"), pipeline_run(session, {}):
        reset_corpus(session, neo4j, index, str(tmp_path))

    assert session.scalar(select(Paper)) is None
    assert neo4j.execute_write.call_count == 1
    assert pdf.exists()


def test_verification_failure_is_raised_not_swallowed_and_preserves_pdfs(
    session, neo4j, index, tmp_path
):
    pdf = tmp_path / "2.pdf"
    pdf.write_bytes(b"%PDF-managed")
    session.add(Paper(id=2, source="arxiv", pdf_path=str(pdf)))
    session.commit()
    _seed_full_corpus(session, index)
    # Qdrant reports a stale point still present after the collection was
    # supposedly dropped -- reset_corpus must not report success anyway.
    index.client.collection_exists = Mock(side_effect=[True, True])
    index.client.count = Mock(return_value=SimpleNamespace(count=1))
    index.client.delete_collection = Mock()

    with pytest.raises(RuntimeError, match="did not converge"), pipeline_run(
        session, {}
    ):
        reset_corpus(session, neo4j, index, str(tmp_path))

    # Verification runs before any PDF is touched, so a store that fails to
    # converge must leave every PDF exactly where it was.
    assert pdf.exists()


# ---------------------------------------------------------------------------
# Identity sequences (D-RESET-02)
#
# Deleting every row leaves PostgreSQL's sequence where it was, so the paper
# after a full reset came back as id 21 rather than id 1. A reset is meant to
# return the corpus to its initial state, and the sequence is part of that
# state. It is restarted only for tables this function has just emptied, in
# the same transaction as their DELETE — never while a row that could collide
# still exists.
# ---------------------------------------------------------------------------


def test_the_first_paper_after_a_reset_is_paper_one_again(session, neo4j, index):
    session.add_all([Paper(source="arxiv") for _ in range(3)])
    session.commit()
    assert sorted(p.id for p in session.scalars(select(Paper))) == [1, 2, 3]

    with pipeline_run(session, {}):
        reset_corpus(session, neo4j, index)

    fresh = Paper(source="arxiv")
    session.add(fresh)
    session.flush()
    assert fresh.id == 1


def test_ids_keep_climbing_while_the_papers_they_belong_to_are_still_stored(session):
    """The sequence restarts on a full reset, never on an ordinary collection —
    a second search appends new ids rather than colliding with the first."""
    session.add_all([Paper(source="arxiv") for _ in range(3)])
    session.commit()

    later = Paper(source="semantic_scholar")
    session.add(later)
    session.flush()

    assert later.id == 4


def test_postgres_sequences_are_restarted_through_the_servers_own_lookup():
    """On PostgreSQL the sequence behind each emptied table is resolved with
    `pg_get_serial_sequence` and rewound with `setval(..., 1, false)`, so the
    next `nextval` returns 1. Nothing interpolates an identifier by hand, and a
    table with no serial id is skipped rather than guessed at."""
    executed = []

    class FakeResult:
        def scalar(self):
            return "public.papers_id_seq"

    class FakeSession:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def execute(self, statement, *args, **kwargs):
            compiled = statement.compile()
            executed.append((str(compiled), list(compiled.params.values())))
            return FakeResult()

    restarted = corpus_reset._restart_identity(FakeSession(), [Paper.__table__])

    assert restarted == ["papers"]
    lookup_sql, lookup_params = executed[0]
    assert "pg_get_serial_sequence" in lookup_sql
    assert lookup_params == ["papers", "id"]
    rewind_sql, rewind_params = executed[1]
    assert "setval" in rewind_sql
    # `false` keeps the sequence *unused*, so the next nextval() returns 1
    # rather than 2.
    assert rewind_params == ["public.papers_id_seq", 1, False]


def test_a_table_without_a_single_integer_identity_is_left_alone():
    """`paper_stage_state` is keyed by (paper_id, stage) — there is no sequence
    to rewind, and `paper_id` is a foreign key, not an identity of its own."""
    assert corpus_reset._identity_column(PaperStageState.__table__) is None


def test_the_reset_result_names_the_sequences_it_restarted(session, neo4j, index):
    session.add(Paper(source="arxiv"))
    session.commit()

    with pipeline_run(session, {}):
        result = reset_corpus(session, neo4j, index)

    assert "papers" in result["sequences_restarted"]

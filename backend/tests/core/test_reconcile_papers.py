"""Per-paper, per-stage reconciliation (read-only): `reconcile --papers`."""

from types import SimpleNamespace

from qdrant_client import QdrantClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core import reconcile
from app.core.pipeline import PaperStageState
from app.embeddings.encoder import Specter2Encoder
from app.embeddings.indexer import EvidenceIndex, build_index_records
from app.extraction.schema import ExtractedStatement, FactType
from app.extraction.storage import replace_facts
from app.ingestion.corpus import Base, Paper
from app.ner_re.service import SourceUnit
from app.parsing.storage import upsert_parsed_document

SECTIONS = [
    {
        "heading": "Limitations",
        "type": "Limitations",
        "paragraphs": [
            {
                "text": "Our model fails on Tamil. It does not generalize.",
                "sentences": ["Our model fails on Tamil.", "It does  not generalize."],
            }
        ],
    }
]


def _fact(text, section=0, paragraph=0, sentence=0, fact_type=FactType.LIMITATION):
    return ExtractedStatement(
        fact_type=fact_type,
        text=text,
        section_type="Limitations",
        section_heading="Limitations",
        section_index=section,
        paragraph_index=paragraph,
        sentence_index=sentence,
        confidence=1.0,
        extraction_method="rule",
    )


def test_every_statement_must_be_the_sentence_at_its_recorded_location():
    facts = [
        SimpleNamespace(
            text="Our model fails on Tamil.",
            section_index=0,
            paragraph_index=0,
            sentence_index=0,
        ),
        # Whitespace-normalized, exactly as Stage 3 stores it.
        SimpleNamespace(
            text="It does not generalize.",
            section_index=0,
            paragraph_index=0,
            sentence_index=1,
        ),
        SimpleNamespace(
            text="Something else.", section_index=0, paragraph_index=0, sentence_index=0
        ),
        SimpleNamespace(
            text="Out of range.", section_index=3, paragraph_index=0, sentence_index=0
        ),
    ]
    assert reconcile.statement_location_mismatches(SECTIONS, facts) == [
        "0.0.0",
        "3.0.0",
    ]


def test_graph_mentions_must_occur_in_the_units_they_cite():
    units = [
        SourceUnit("abstract", "We fine-tune RoBERTa."),
        SourceUnit("statement:0.0.0", "BERT fails on Tamil."),
    ]
    mentions = [
        ("bert", ["statement:0.0.0"]),  # fine
        ("roberta", ["abstract"]),  # fine
        ("gpt-4", ["abstract"]),  # in no unit at all
        ("bert", ["abstract"]),  # cites a unit that holds only "RoBERTa"
        ("tamil", None),  # written before provenance: no evidence recorded
    ]
    assert reconcile.mention_problems(units, mentions) == {
        "not_in_source": 1,
        "bad_evidence": 1,
        "no_evidence": 1,
    }


def test_stage_status_separates_not_applicable_from_not_started():
    assert reconcile.stage_status(None, applicable=False) == "not_applicable"
    assert reconcile.stage_status(None, applicable=True) == "not_started"
    assert reconcile.stage_status("done", applicable=True) == "completed"
    assert reconcile.stage_status("failed", applicable=True) == "failed"
    assert reconcile.stage_status("pending", applicable=True) == "pending"


class _Graph:
    """Answers the per-paper reads with one projected paper (id 1)."""

    def run(self, query, **params):
        if "RETURN p.id AS id" in query:
            return [{"id": 1, "node_id": "n1"}]
        if "properties(p) AS props" in query:
            return (
                [
                    {
                        "props": {
                            "nerre_provenance": "1",
                            "nerre_input_basis": "abstract_and_statements",
                            "nerre_model_revision": "sha",
                        },
                        "relations": 0,
                    }
                ]
                if params["id"] == 1
                else []
            )
        if "MENTIONS]->(e)" in query:
            return (
                [{"name": "tamil", "evidence": ["statement:0.0.0"]}]
                if params["id"] == 1
                else []
            )
        raise AssertionError(query)


def test_paper_report_reconciles_every_stage_for_full_text_and_metadata_only_papers(
    tmp_path,
):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    pdf = tmp_path / "1.pdf"
    pdf.write_bytes(b"%PDF-1.7 body")
    with Session(engine) as session:
        session.add_all(
            [
                Paper(
                    id=1,
                    title="Full",
                    year=2024,
                    source="arxiv",
                    pdf_path=str(pdf),
                    has_full_text=True,
                    abstract="We study Tamil.",
                ),
                Paper(
                    id=2,
                    title="Meta",
                    year=2025,
                    source="semantic_scholar",
                    has_full_text=False,
                    abstract="An abstract.",
                ),
            ]
        )
        session.flush()
        document = upsert_parsed_document(session, 1, SECTIONS, "grobid")
        facts = replace_facts(
            session,
            1,
            [
                _fact("Our model fails on Tamil."),
                _fact("It does not generalize.", sentence=1),
            ],
        )
        for paper_id, stage in [
            (1, "s2"),
            (1, "s3"),
            (1, "s4"),
            (1, "s5"),
            (2, "s4"),
            (2, "s5"),
        ]:
            session.add(
                PaperStageState(
                    paper_id=paper_id, stage=stage, input_hash="h", status="done"
                )
            )
        session.commit()

        settings = SimpleNamespace(
            qdrant_collection="t", embedding_model="m", embedding_batch_size=8
        )
        index = EvidenceIndex(
            settings,
            QdrantClient(":memory:"),
            Specter2Encoder(lambda t: [[1.0, 0.0] for _ in t], 2),
        )
        index.upsert(build_index_records(session.get(Paper, 1), document, facts, "n1"))

        report = reconcile.paper_reconciliation(session, _Graph(), index)

    full, meta = report["papers"]
    assert full["pdf"] == "valid_pdf"
    assert full["input_basis"] == "full_text"
    assert full["s2"] == {
        "status": "completed",
        "parser": "grobid",
        "sections": 1,
        "paragraphs": 1,
        "text_chars": 49,
    }
    assert full["s3"]["status"] == "completed"
    assert full["s3"]["statements"] == {"limitation": 2}
    assert full["s3"]["location_mismatches"] == 0
    assert full["s4"]["mentions"] == 1 and full["s4"]["provenance"] == "1"
    assert full["s5"]["points"] == {"paragraph": 1, "limitation": 2}
    assert full["s5"]["missing"] == full["s5"]["extra"] == full["s5"]["stale"] == 0
    assert full["warnings"] == []

    assert meta["pdf"] == "none"
    assert meta["input_basis"] == "abstract_only"
    assert meta["s2"]["status"] == meta["s3"]["status"] == "not_applicable"
    assert meta["s4"]["status"] == "completed"
    # Checkpoint says done, but the fake graph holds no node for paper 2.
    assert "s4_not_projected" in meta["warnings"]
    assert report["stages"]["s2"] == {"completed": 1, "not_applicable": 1}
    assert report["stages"]["s5"] == {"completed": 2}
    assert report["totals"]["points"] == 3

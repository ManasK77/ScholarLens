"""S4 provenance: every graph fact traces back to the text it was read from.

Stage 4's input is the paper's abstract plus its Stage 3 statements. Before
D-S4-PROV-01 the graph kept none of that: a MENTIONS edge carried only a
confidence, a relation only `paper_id`, and an entity the model returned was
written whether or not its text occurred in the input at all.
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.extraction.schema import ExtractedStatement, FactType
from app.extraction.storage import replace_facts
from app.ingestion.corpus import Base, Paper
from app.ner_re import graph, service
from app.ner_re.extractor import JointEntityRelationExtractor
from app.ner_re.schema import EntityMention, JointExtractionResult, RelationMention
from tests.ner_re.test_extractor import FakeJointIE, FakeJointResult
from tests.ner_re.test_graph import FakeGraphSession
from tests.ner_re.test_service import REQUIRED_ENV, FakeNeo4jSession


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


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


def _paper(session, abstract=None):
    paper = Paper(title="P", year=2024, abstract=abstract, source="arxiv")
    session.add(paper)
    session.flush()
    return paper


def _facts(session, paper_id, *located_texts):
    """`located_texts` are ((section, paragraph, sentence), text) pairs."""
    replace_facts(
        session,
        paper_id,
        [
            ExtractedStatement(
                fact_type=FactType.MODEL,
                text=text,
                section_type="Methodology",
                section_heading="Method",
                section_index=section,
                paragraph_index=paragraph,
                sentence_index=sentence,
                confidence=1.0,
                extraction_method="rule",
            )
            for (section, paragraph, sentence), text in located_texts
        ],
    )


class FixedExtractor:
    def __init__(self, result):
        self.result = result

    def extract_many(self, chunks):
        return self.result


def _written(neo4j):
    """The rows each Cypher write received, keyed by a fragment of its query."""
    mentions = next(
        p["rows"] for q, p in neo4j.queries if "MERGE (p)-[m:MENTIONS]" in q
    )
    relations = [p["rows"] for q, p in neo4j.queries if "MERGE (h)-[r:" in q]
    paper = next(p for q, p in neo4j.queries if q.startswith("MERGE (p:Paper"))
    return mentions, [row for rows in relations for row in rows], paper


def test_source_units_name_the_abstract_and_each_statement_location(session):
    paper = _paper(session, abstract="We introduce BERT.")
    _facts(session, paper.id, ((2, 1, 0), "BERT is trained on SQuAD."))

    units = service.source_units(session, paper)

    assert [(u.ref, u.text) for u in units] == [
        ("abstract", "We introduce BERT."),
        ("statement:2.1.0", "BERT is trained on SQuAD."),
    ]
    # The chunks the model sees are unchanged by carrying references.
    assert service.collect_paper_text(session, paper) == [
        "We introduce BERT. BERT is trained on SQuAD."
    ]


def test_mentions_and_relations_carry_the_units_they_were_read_from(session):
    paper = _paper(session, abstract="We introduce BERT.")
    _facts(
        session,
        paper.id,
        ((1, 0, 0), "BERT is trained on SQuAD."),
        ((1, 0, 1), "SQuAD is small."),
    )
    result = JointExtractionResult(
        [EntityMention("BERT", "model", 0.9), EntityMention("SQuAD", "dataset", 0.8)],
        [RelationMention("BERT", "trained_on", "SQuAD", 0.7)],
    )
    neo4j = FakeNeo4jSession()

    service.extract_all(session, neo4j, extractor=FixedExtractor(result))

    mentions, relations, _ = _written(neo4j)
    by_name = {row["name"]: row for row in mentions}
    assert by_name["BERT"]["evidence"] == ["abstract", "statement:1.0.0"]
    assert by_name["BERT"]["evidence_count"] == 2
    assert by_name["SQuAD"]["evidence"] == ["statement:1.0.0", "statement:1.0.1"]
    # Both endpoints occur in one statement: that statement is the evidence.
    assert relations == [
        {
            "head": "bert",
            "tail": "squad",
            "confidence": 0.7,
            "evidence": ["statement:1.0.0"],
            "cross_sentence": False,
        }
    ]


def test_a_relation_whose_endpoints_never_share_a_unit_is_marked_cross_sentence(
    session,
):
    paper = _paper(session, abstract="BERT is a model. SQuAD is a dataset.")
    result = JointExtractionResult(
        [EntityMention("BERT", "model", 0.9), EntityMention("SQuAD", "dataset", 0.8)],
        [RelationMention("BERT", "trained_on", "SQuAD", 0.7)],
    )
    _facts(session, paper.id, ((0, 0, 0), "BERT works."), ((0, 0, 1), "SQuAD helps."))
    neo4j = FakeNeo4jSession()

    service.extract_all(session, neo4j, extractor=FixedExtractor(result))

    _, relations, _ = _written(neo4j)
    # The abstract holds both names, so it is same-unit evidence; the two
    # statements each hold one and are not.
    assert relations[0]["evidence"] == ["abstract"]
    assert relations[0]["cross_sentence"] is False

    paper2 = _paper(session, abstract=None)
    _facts(session, paper2.id, ((0, 0, 0), "BERT works."), ((0, 0, 1), "SQuAD helps."))
    neo4j2 = FakeNeo4jSession()
    session.query(Paper).filter(Paper.id == paper.id).delete()
    service.extract_all(session, neo4j2, extractor=FixedExtractor(result))
    _, relations2, _ = _written(neo4j2)
    assert relations2[0]["cross_sentence"] is True
    assert relations2[0]["evidence"] == ["statement:0.0.0", "statement:0.0.1"]


def test_entities_whose_text_is_not_in_the_input_are_never_written(session):
    """The model must not put words in a paper's mouth.

    Matching is on word boundaries: "BERT" inside "RoBERTa" is not a mention.
    """
    paper = _paper(session, abstract="We fine-tune RoBERTa on SQuAD.")
    result = JointExtractionResult(
        [
            EntityMention("BERT", "model", 0.9),
            EntityMention("GPT-4", "model", 0.9),
            EntityMention("SQuAD", "dataset", 0.8),
        ],
        [RelationMention("BERT", "trained_on", "SQuAD", 0.7)],
    )
    neo4j = FakeNeo4jSession()
    stats = {}

    service.extract_paper(
        session, neo4j, paper, FixedExtractor(result), 1200, 12, stats
    )

    mentions, relations, _ = _written(neo4j)
    assert [row["name"] for row in mentions] == ["SQuAD"]
    # Its head was never in the text, so the relation goes with it.
    assert relations == []
    assert stats["entities_not_in_source"] == 2
    assert stats["relations_unsupported"] == 1


def test_the_paper_node_records_model_input_basis_and_provenance_version(session):
    _paper(session, abstract="We introduce BERT.")
    extractor = FixedExtractor(
        JointExtractionResult([EntityMention("BERT", "model", 1.0)], [])
    )
    extractor.model_revision = "abc123"
    neo4j = FakeNeo4jSession()

    service.extract_all(session, neo4j, extractor=extractor)

    _, _, paper_params = _written(neo4j)
    assert paper_params["provenance"] == {
        "nerre_provenance": graph.NERRE_PROVENANCE_VERSION,
        "nerre_model": "fastino/gliner2.5-base-v1",
        "nerre_model_revision": "abc123",
        "nerre_input_basis": "abstract_only",
        "nerre_units_used": 1,
        "nerre_units_total": 1,
    }


def test_input_basis_and_unit_counts_expose_the_chunk_cap(session):
    paper = _paper(session, abstract=None)
    _facts(
        session,
        paper.id,
        *[
            ((0, 0, index), f"Statement number {index} names BERT.")
            for index in range(5)
        ],
    )
    neo4j = FakeNeo4jSession()
    extractor = FixedExtractor(
        JointExtractionResult([EntityMention("BERT", "model", 1.0)], [])
    )

    service.extract_all(
        session, neo4j, extractor=extractor, max_chars_per_chunk=40, max_chunks=2
    )

    mentions, _, paper_params = _written(neo4j)
    assert paper_params["provenance"]["nerre_input_basis"] == "statements_only"
    assert paper_params["provenance"]["nerre_units_used"] == 2
    assert paper_params["provenance"]["nerre_units_total"] == 5
    # Evidence never cites a unit the model was not shown.
    assert mentions[0]["evidence"] == ["statement:0.0.0", "statement:0.0.1"]


def test_write_paper_graph_stores_evidence_and_paper_provenance():
    session = FakeGraphSession()
    paper = SimpleNamespace(id=4, title="P", year=2024, venue=None, doi=None)
    result = JointExtractionResult(
        [EntityMention("BERT", "model", 0.9, ("abstract",))],
        [],
    )

    graph.write_paper_graph(
        session, paper, result, input_hash="h", provenance={"nerre_provenance": "1"}
    )

    paper_query, paper_params = session.calls[0]
    assert "SET p += $provenance" in paper_query
    assert paper_params["provenance"] == {"nerre_provenance": "1"}
    mention_query, mention_params = next(
        (q, p) for q, p in session.calls if "MENTIONS]->(e)" in q
    )
    assert "m.evidence=row.evidence" in mention_query
    assert mention_params["rows"][0]["evidence"] == ["abstract"]


class _CurrentGraph:
    def __init__(self, props):
        self.props = props

    def run(self, query, **parameters):
        props = self.props

        class _R:
            def single(self_inner):
                return None if props is None else {"props": props}

        return _R()


@pytest.mark.parametrize(
    ("props", "current"),
    [
        (None, False),
        ({"nerre_input_hash": "h"}, False),  # written before provenance existed
        ({"nerre_input_hash": "h", "nerre_provenance": "0"}, False),
        (
            {
                "nerre_input_hash": "x",
                "nerre_provenance": graph.NERRE_PROVENANCE_VERSION,
            },
            False,
        ),
        (
            {
                "nerre_input_hash": "h",
                "nerre_provenance": graph.NERRE_PROVENANCE_VERSION,
            },
            True,
        ),
    ],
)
def test_a_projection_without_current_provenance_is_not_current(props, current):
    """So the checkpoint rewrites it once, under the unchanged input hash."""
    assert graph.paper_graph_current(_CurrentGraph(props), 1, "h") is current


def test_a_failing_chunk_fails_the_extraction_instead_of_a_partial_graph():
    """Outside a checkpointed run a chunk error used to be skipped, and the
    paper's projection was replaced by whatever the other chunks produced."""

    class Failing(FakeJointIE):
        def extract(self, text, schema, config=None):
            if text == "second":
                raise RuntimeError("model failure")
            return super().extract(text, schema, config)

    with pytest.raises(RuntimeError):
        JointEntityRelationExtractor(Failing()).extract_many(["first", "second"])


def test_entity_mentions_without_evidence_still_construct_positionally():
    mention = EntityMention("BERT", "model", 0.5)
    relation = RelationMention("BERT", "trained_on", "SQuAD", 0.5)
    assert mention.evidence == () and relation.evidence == ()
    assert relation.cross_sentence is False
    assert FakeJointResult({}).feasible


def _load_gliner_with_fake(monkeypatch, nerre_model):
    import sys
    import types

    calls = []

    class FakeJointIE:
        @classmethod
        def from_pretrained(cls, repo, **kwargs):
            calls.append((repo, kwargs))
            return cls()

    monkeypatch.setitem(
        sys.modules, "gliner2.joint_ie", types.SimpleNamespace(JointIE=FakeJointIE)
    )
    from app.ner_re.extractor import load_joint_model

    load_joint_model(SimpleNamespace(nerre_model=nerre_model, nerre_batch_size=1))
    return calls


def test_the_gliner_checkpoint_can_be_pinned_to_an_exact_commit(monkeypatch):
    """`NERRE_MODEL` named a branch, and that branch has moved (D-S4-REV-01)."""
    assert _load_gliner_with_fake(
        monkeypatch,
        "fastino/gliner2.5-base-v1@54785d51df8d86a0d8f2eb212fec15871aad2c4d",
    ) == [
        (
            "fastino/gliner2.5-base-v1",
            {"revision": "54785d51df8d86a0d8f2eb212fec15871aad2c4d"},
        )
    ]


def test_an_unpinned_gliner_name_loads_exactly_as_before(monkeypatch):
    assert _load_gliner_with_fake(monkeypatch, "fastino/gliner2.5-base-v1") == [
        ("fastino/gliner2.5-base-v1", {})
    ]

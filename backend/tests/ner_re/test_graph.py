from types import SimpleNamespace

import pytest

from app.ner_re.graph import (
    expand_paper_neighborhood,
    graph_snapshot,
    query_graph,
    write_paper_graph,
)
from app.ner_re.schema import EntityMention, JointExtractionResult, RelationMention


class FakeResult:
    def __init__(self, row=None):
        self.row = row

    def single(self):
        return self.row


class FakeGraphSession:
    def __init__(self):
        self.calls = []

    def run(self, query, **parameters):
        self.calls.append((query, parameters))
        if "RETURN elementId(p)" in query:
            return FakeResult({"node_id": "neo4j-paper-4"})
        return FakeResult()


def test_write_paper_graph_uses_allowlisted_relationship_and_normalized_nodes():
    session = FakeGraphSession()
    paper = SimpleNamespace(
        id=4, title="Paper", year=2024, venue="ACL", doi="10.1/paper"
    )
    result = JointExtractionResult(
        entities=[
            EntityMention("BERT", "model", 0.9),
            EntityMention("SQuAD", "dataset", 0.8),
        ],
        relations=[RelationMention("BERT", "trained_on", "SQuAD", 0.7)],
    )

    node_id = write_paper_graph(session, paper, result)

    assert node_id == "neo4j-paper-4"
    relation_query, parameters = session.calls[-1]
    assert "[r:TRAINED_ON" in relation_query
    assert parameters["head"] == "bert"
    assert parameters["tail"] == "squad"


class EmptyGraphSession:
    def __init__(self):
        self.calls = []

    def run(self, query, **parameters):
        self.calls.append((query, parameters))
        return []


def test_graph_snapshot_reads_optional_labels_from_property_map():
    session = EmptyGraphSession()

    assert graph_snapshot(session) == {"nodes": [], "edges": []}

    node_query = session.calls[0][0]
    assert "properties(n)" in node_query
    assert "n.name" not in node_query
    assert "n.title" not in node_query


def test_graph_query_uses_dynamic_labels_relationships_and_properties():
    session = EmptyGraphSession()

    assert query_graph(session, "multilingual safety") == []

    query = session.calls[0][0]
    assert "'Paper' IN labels(p)" in query
    assert "'Entity' IN labels(e)" in query
    assert "type(pe) IN ['MENTIONS', 'SUPPORTS']" in query
    assert "properties(e)['name']" in query
    assert "(p:Paper)" not in query
    assert "e.name" not in query


def test_graph_expansion_is_seeded_by_vector_hit_papers_and_bounded():
    session = EmptyGraphSession()

    assert expand_paper_neighborhood(session, [9, 4, 9], limit=7) == []

    query, parameters = session.calls[0]
    assert "UNWIND $paper_ids AS seed_id" in query
    assert "properties(seed)['id']" in query
    assert "properties(entity)['name']" in query
    assert "properties(related)['name']" in query
    assert "ORDER BY paper_id" in query
    assert parameters == {"paper_ids": [9, 4], "limit": 7}


@pytest.mark.parametrize("paper_ids", [[], [True], [1, "2"], [-1]])
def test_graph_expansion_rejects_missing_or_invalid_seed_paper_ids(paper_ids):
    session = EmptyGraphSession()

    if not paper_ids:
        assert expand_paper_neighborhood(session, paper_ids) == []
        assert session.calls == []
    else:
        with pytest.raises((TypeError, ValueError)):
            expand_paper_neighborhood(session, paper_ids)

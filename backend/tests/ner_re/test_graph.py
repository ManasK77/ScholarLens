from types import SimpleNamespace

from app.ner_re.graph import write_paper_graph
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

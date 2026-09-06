from types import SimpleNamespace

import httpx
from qdrant_client.http.exceptions import UnexpectedResponse

from app.embeddings.indexer import EvidenceIndex, build_index_records


def test_build_index_records_includes_paragraphs_and_gap_statements():
    paper = SimpleNamespace(id=7, year=2024, venue="ACL")
    document = SimpleNamespace(
        sections=[
            {
                "type": "limitations",
                "paragraphs": [
                    {"text": "Performance drops in low-resource languages."}
                ],
            }
        ]
    )
    facts = [
        SimpleNamespace(
            id=12,
            fact_type="future_work",
            text="Evaluate regional languages.",
            section_type="future work",
        )
    ]

    records = build_index_records(paper, document, facts, graph_node_id="paper:7")

    assert [item.source_type for item in records] == ["paragraph", "future_work"]
    assert all(item.paper_id == 7 for item in records)
    assert all(item.graph_node_id == "paper:7" for item in records)


def test_ensure_collection_tolerates_concurrent_create_conflict():
    class RacingClient:
        def collection_exists(self, name):
            return False

        def create_collection(self, **kwargs):
            raise UnexpectedResponse(
                409, "Conflict", b"already exists", httpx.Headers()
            )

    settings = SimpleNamespace(qdrant_collection="evidence", qdrant_url="http://qdrant")
    encoder = SimpleNamespace(dimension=768)

    EvidenceIndex(
        settings=settings, client=RacingClient(), encoder=encoder
    ).ensure_collection()


def test_empty_index_returns_without_loading_encoder(monkeypatch):
    class EmptyClient:
        def collection_exists(self, name):
            return True

        def count(self, **kwargs):
            return SimpleNamespace(count=0)

    def fail_if_loaded():
        raise AssertionError("encoder should stay unloaded for an empty index")

    monkeypatch.setattr("app.embeddings.indexer.get_encoder", fail_if_loaded)
    settings = SimpleNamespace(
        qdrant_collection="evidence",
        qdrant_url="http://qdrant",
        retrieval_top_n=12,
    )

    index = EvidenceIndex(settings=settings, client=EmptyClient())

    assert index.has_evidence() is False
    assert index.retrieve("question") == []

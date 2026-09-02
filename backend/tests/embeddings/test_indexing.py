from types import SimpleNamespace

from app.embeddings.indexer import build_index_records


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

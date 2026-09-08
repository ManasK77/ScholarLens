"""Phase 5 — `IndexRecord`, the record contract `indexer.py` was written against.

`app/embeddings/indexer.py` survived the incomplete upstream push and
constructs `IndexRecord` **positionally** with nine fields, then hands
`payload()` straight to Qdrant. These tests pin that shape.
"""

import pytest

from app.embeddings.schema import (
    GAP_SOURCE_TYPES,
    PAYLOAD_FIELDS,
    SOURCE_TYPES,
    IndexRecord,
)


def _record(**overrides) -> IndexRecord:
    values = {
        "record_id": "3f2b0a6e-0000-5000-8000-000000000001",
        "paper_id": 7,
        "text": "Performance drops in low-resource languages.",
        "source_type": "limitation",
        "fact_id": 12,
        "year": 2024,
        "venue": "ACL",
        "section_type": "Limitations",
        "graph_node_id": "4:abc:17",
    }
    values.update(overrides)
    return IndexRecord(**values)


def test_fields_are_positional_in_the_order_indexer_uses() -> None:
    """indexer.build_index_records passes all nine fields positionally."""
    record = IndexRecord(
        "3f2b0a6e-0000-5000-8000-000000000001",
        7,
        "Performance drops in low-resource languages.",
        "paragraph",
        None,
        2024,
        "ACL",
        "Limitations",
        "4:abc:17",
    )

    assert record.record_id == "3f2b0a6e-0000-5000-8000-000000000001"
    assert record.paper_id == 7
    assert record.text == "Performance drops in low-resource languages."
    assert record.source_type == "paragraph"
    assert record.fact_id is None
    assert record.year == 2024
    assert record.venue == "ACL"
    assert record.section_type == "Limitations"
    assert record.graph_node_id == "4:abc:17"


def test_payload_carries_every_filterable_metadata_key() -> None:
    """EMBED-02: paper id, year, venue and section type must be filterable."""
    payload = _record().payload()

    assert payload["paper_id"] == 7
    assert payload["year"] == 2024
    assert payload["venue"] == "ACL"
    assert payload["section_type"] == "Limitations"
    assert payload["source_type"] == "limitation"
    assert payload["fact_id"] == 12
    assert payload["text"] == "Performance drops in low-resource languages."


def test_payload_carries_the_neo4j_node_link() -> None:
    """EMBED-03: every vector links back to its Neo4j node."""
    assert _record().payload()["graph_node_id"] == "4:abc:17"


def test_payload_keys_match_the_declared_field_set() -> None:
    assert set(_record().payload()) == set(PAYLOAD_FIELDS)


def test_payload_never_collides_with_qdrant_result_keys() -> None:
    """`EvidenceIndex.retrieve` splats the payload beside `id` and `score`."""
    assert {"id", "score"}.isdisjoint(_record().payload())


def test_payload_is_a_fresh_mapping_each_call() -> None:
    """A caller mutating one point's payload must not corrupt the record."""
    record = _record()
    first = record.payload()
    first["venue"] = "mutated"

    assert record.payload()["venue"] == "ACL"


def test_record_is_hashable_and_frozen() -> None:
    record = _record()
    with pytest.raises(AttributeError):
        record.text = "reassigned"  # type: ignore[misc]
    assert hash(record) == hash(_record())


def test_optional_metadata_may_be_missing() -> None:
    """A manual upload can reach Phase 5 with no year, venue or section."""
    record = _record(fact_id=None, year=None, venue=None, section_type=None)

    payload = record.payload()
    assert payload["year"] is None
    assert payload["venue"] is None
    assert payload["section_type"] is None
    assert payload["fact_id"] is None


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_blank_text_is_rejected(text: str) -> None:
    """A blank record would burn an embedding call and pollute retrieval."""
    with pytest.raises(ValueError):
        _record(text=text)


def test_non_string_text_is_rejected() -> None:
    with pytest.raises(ValueError):
        _record(text=None)


def test_unknown_source_type_is_rejected() -> None:
    """`gap_points()` filters on source_type; a typo would silently hide data."""
    with pytest.raises(ValueError):
        _record(source_type="limitations")


def test_blank_graph_node_id_is_rejected() -> None:
    """EMBED-03 is not satisfied by an empty link."""
    with pytest.raises(ValueError):
        _record(graph_node_id="")


def test_blank_record_id_is_rejected() -> None:
    with pytest.raises(ValueError):
        _record(record_id="  ")


def test_negative_paper_id_is_rejected() -> None:
    with pytest.raises(ValueError):
        _record(paper_id=-1)


def test_source_type_vocabulary_matches_the_indexer() -> None:
    """The three types `build_index_records` can emit, and the gap subset."""
    assert SOURCE_TYPES == frozenset({"paragraph", "limitation", "future_work"})
    assert GAP_SOURCE_TYPES == frozenset({"limitation", "future_work"})
    assert GAP_SOURCE_TYPES < SOURCE_TYPES

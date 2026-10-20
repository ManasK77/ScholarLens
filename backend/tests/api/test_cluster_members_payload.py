"""`GET /clusters` lists each sentence once, with every label it carries.

Stage 6 stores one row per label (D-06-14) so no label is lost, but the
dashboard rendered a doubly-typed sentence twice next to a count that said
otherwise. Additive: `source_type` and `point_id` keep their meaning (the
first row's); `source_types`, `point_ids` and `citation_markers` are new.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.routes_backend import _cluster_payload, _evidence_with_markers
from app.clustering.schema import GapStatement, StatementCluster
from app.clustering.storage import list_clusters, replace_clusters
from app.ingestion import corpus

SENTENCE = "Prior detectors [12] fail to generalize across languages."


def _row(point_id, paper_id, text, source_type):
    return GapStatement(point_id, paper_id, text, 2024, source_type, (0.1, 0.2))


def test_a_sentence_with_two_labels_is_one_member_carrying_both():
    engine = create_engine("sqlite://")
    corpus.Base.metadata.create_all(engine)
    with Session(engine) as session:
        replace_clusters(
            session,
            [
                StatementCluster(
                    label="detectors generalize languages",
                    members=[
                        _row("p1", 1, SENTENCE, "future_work"),
                        _row("p2", 1, SENTENCE, "limitation"),
                        _row(
                            "p3", 2, "Our filter fails on Hindi prompts.", "limitation"
                        ),
                    ],
                )
            ],
        )
        payload = _cluster_payload(session, list_clusters(session)[0])

    assert payload["member_count"] == 2
    assert len(payload["members"]) == 2
    first = payload["members"][0]
    assert first["point_id"] == "p1"
    assert first["point_ids"] == ["p1", "p2"]
    assert first["source_types"] == ["future_work", "limitation"]
    assert first["citation_markers"] == ["[12]"]
    assert payload["members"][1]["citation_markers"] == []


def test_report_evidence_carries_its_citation_markers():
    stored = [
        {"id": "e1", "paper_id": 1, "text": SENTENCE, "source_type": "limitation"}
    ]
    [item] = _evidence_with_markers(stored)
    assert item["citation_markers"] == ["[12]"]
    assert item["text"] == SENTENCE
    assert "citation_markers" not in stored[0]  # derived, never written back


def test_report_evidence_says_when_a_quote_stops_at_the_length_cap():
    """Stage 9 stores at most MAX_EVIDENCE_CHARS of a passage. A quote cut there
    must not read as the whole passage under "quoted verbatim" (4 of the live
    corpus's 19 report passages are)."""
    from app.explanation.schema import MAX_EVIDENCE_CHARS

    capped = {"id": "e1", "paper_id": 1, "text": "x" * MAX_EVIDENCE_CHARS}
    whole = {"id": "e2", "paper_id": 1, "text": "x" * (MAX_EVIDENCE_CHARS - 14)}
    first, second = _evidence_with_markers([capped, whole])
    assert first["truncated"] is True
    assert second["truncated"] is False
    assert "truncated" not in capped  # derived, never written back

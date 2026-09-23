"""`GET /clusters` serves the statements behind a contradiction flag.

Added so the dashboard's "Conflicting evidence" pill can eventually be made
explainable. Before this, the flag told a reader that two papers disagreed but
never which two or about what, which is how four pairs resting on an ethics
disclaimer sat at the top of the gap ranking unnoticed.

Additive only: every key `/clusters` served before is still served.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.routes_backend import _cluster_payload
from app.clustering.schema import ContradictionPair, GapStatement, StatementCluster
from app.clustering.storage import list_clusters, replace_clusters
from app.ingestion import corpus


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    corpus.Base.metadata.create_all(engine)
    with Session(engine) as active:
        yield active


def _statement(point_id: str, paper_id: int, text: str) -> GapStatement:
    return GapStatement.from_point(
        {
            "id": point_id,
            "paper_id": paper_id,
            "text": text,
            "year": 2024,
            "source_type": "limitation",
            "vector": [0.1, 0.2],
        }
    )


FAILS = "Current prompt injection defenses fail under adaptive attacks."
HOLDS = "Our prompt injection defense holds under adaptive attacks."


def _seed(session, contradictions=()):
    members = [_statement("s1", 101, FAILS), _statement("s2", 102, HOLDS)]
    replace_clusters(
        session,
        [
            StatementCluster(
                label="prompt injection defenses",
                members=members,
                contradictions=contradictions,
            )
        ],
    )
    return list_clusters(session)[0]


def test_a_flagged_cluster_serves_the_disagreeing_statements(session):
    pair = ContradictionPair("s1", 101, "s2", 102, 0.93)
    cluster = _seed(session, (pair,))

    payload = _cluster_payload(session, cluster)

    assert payload["contradiction_flag"] is True
    [served] = payload["contradictions"]
    assert served == {
        "left_point_id": "s1",
        "left_paper_id": 101,
        "left_text": FAILS,
        "right_point_id": "s2",
        "right_paper_id": 102,
        "right_text": HOLDS,
        "score": pytest.approx(0.93),
    }


def test_an_unflagged_cluster_serves_an_empty_list(session):
    cluster = _seed(session)

    payload = _cluster_payload(session, cluster)

    assert payload["contradiction_flag"] is False
    assert payload["contradictions"] == []


def test_every_pre_existing_key_is_still_served(session):
    cluster = _seed(session, (ContradictionPair("s1", 101, "s2", 102, 0.93),))

    payload = _cluster_payload(session, cluster)

    assert {
        "id",
        "label",
        "member_count",
        "contradiction_flag",
        "contradiction_score",
        "members",
    } <= set(payload)
    assert [member["point_id"] for member in payload["members"]] == ["s1", "s2"]

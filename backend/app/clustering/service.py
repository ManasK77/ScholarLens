"""Phase 6 orchestration over the Qdrant evidence collection."""

from collections import defaultdict

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.clustering.analysis import (
    NLIPredictor,
    cluster_vectors,
    find_contradictions,
    get_nli_predictor,
)
from app.clustering.storage import ClusterMember, ContradictionEvidence, SemanticCluster
from app.core.config import Settings, get_settings
from app.embeddings.indexer import EvidenceIndex


def rebuild_clusters(
    session: Session,
    index: EvidenceIndex | None = None,
    predictor: NLIPredictor | None = None,
    settings: Settings | None = None,
) -> list[int]:
    settings = settings or get_settings()
    index = index or EvidenceIndex(settings)
    points = index.gap_points()
    labels = cluster_vectors(
        [point["vector"] for point in points], settings.cluster_min_size
    )
    grouped: dict[int, list[dict]] = defaultdict(list)
    for label, point in zip(labels, points, strict=True):
        grouped[label].append(point)

    session.execute(delete(ContradictionEvidence))
    session.execute(delete(ClusterMember))
    session.execute(delete(SemanticCluster))
    created = []
    nli = predictor or (get_nli_predictor() if points else None)
    for label in sorted(grouped):
        members = grouped[label]
        contradictions = (
            find_contradictions(
                [member["text"] for member in members],
                nli,
                settings.contradiction_threshold,
                settings.max_nli_pairs,
            )
            if nli
            else []
        )
        cluster = SemanticCluster(
            label=members[0]["text"][:240],
            member_count=len(members),
            contradiction_flag=bool(contradictions),
            contradiction_score=max(
                (item.score for item in contradictions), default=0.0
            ),
        )
        session.add(cluster)
        session.flush()
        created.append(cluster.id)
        for member in members:
            session.add(
                ClusterMember(
                    cluster_id=cluster.id,
                    point_id=member["id"],
                    paper_id=member["paper_id"],
                    source_id=member.get("source_id"),
                    source_type=member["source_type"],
                    text=member["text"],
                    year=member.get("year"),
                )
            )
        session.flush()
        for contradiction in contradictions:
            session.add(
                ContradictionEvidence(
                    cluster_id=cluster.id,
                    left_point_id=members[contradiction.left]["id"],
                    right_point_id=members[contradiction.right]["id"],
                    score=contradiction.score,
                )
            )
    session.flush()
    return created

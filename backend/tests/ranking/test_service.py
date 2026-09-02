from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.clustering.storage import ClusterMember, SemanticCluster
from app.ingestion.corpus import Base, Paper
from app.ranking.service import rebuild_rankings
from app.ranking.storage import GapRanking
from app.trends.storage import ClusterTrend


def test_rebuild_rankings_persists_signal_breakdown():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        paper = Paper(source="arxiv", citation_count=99, has_full_text=True)
        session.add(paper)
        session.flush()
        cluster = SemanticCluster(
            label="multilingual gap",
            member_count=1,
            contradiction_flag=True,
            contradiction_score=0.8,
        )
        session.add(cluster)
        session.flush()
        session.add(
            ClusterMember(
                cluster_id=cluster.id,
                point_id="point-1",
                paper_id=paper.id,
                source_type="limitation",
                text="Poor multilingual performance",
                year=2024,
            )
        )
        session.add(
            ClusterTrend(cluster_id=cluster.id, yearly_counts={2024: 1}, slope=0.5)
        )
        settings = SimpleNamespace(
            gap_weights={
                "mention_frequency": 1.0,
                "trend_slope": 1.0,
                "solution_density": 1.0,
                "citation_impact": 1.0,
                "contradiction_flag": 1.0,
            }
        )

        assert rebuild_rankings(session, settings) == [cluster.id]
        ranking = session.get(GapRanking, cluster.id)

        assert ranking.mention_frequency == 1.0
        assert ranking.trend_slope == 0.5
        assert ranking.citation_impact > 0
        assert ranking.contradiction_flag == 1.0

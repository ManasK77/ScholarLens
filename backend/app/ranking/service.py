"""Transparent five-signal gap ranking."""

import math
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clustering.storage import ClusterMember, SemanticCluster
from app.core.config import Settings, get_settings
from app.extraction.storage import ExtractedFact
from app.ingestion.corpus import Paper
from app.ranking.scoring import GapSignals, score_gap
from app.ranking.storage import GapRanking
from app.trends.storage import ClusterTrend


def _keywords(text: str) -> set[str]:
    return {
        token.casefold()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", text)
        if token.casefold() not in {"this", "that", "with", "from", "using", "paper"}
    }


def _solution_density(session: Session, members: list[ClusterMember]) -> float:
    if not members:
        return 0.0
    cluster_terms = _keywords(" ".join(member.text for member in members))
    paper_ids = {member.paper_id for member in members}
    contributions = session.scalars(
        select(ExtractedFact).where(
            ExtractedFact.paper_id.in_(paper_ids),
            ExtractedFact.fact_type == "contribution",
        )
    )
    matching = sum(bool(cluster_terms & _keywords(fact.text)) for fact in contributions)
    return matching / max(len(members), 1)


def rebuild_rankings(session: Session, settings: Settings | None = None) -> list[int]:
    settings = settings or get_settings()
    processed = []
    for cluster in session.scalars(
        select(SemanticCluster).order_by(SemanticCluster.id)
    ):
        members = list(
            session.scalars(
                select(ClusterMember).where(ClusterMember.cluster_id == cluster.id)
            )
        )
        paper_ids = {member.paper_id for member in members}
        citations = (
            list(
                session.scalars(
                    select(Paper.citation_count).where(
                        Paper.id.in_(paper_ids), Paper.citation_count.is_not(None)
                    )
                )
            )
            if paper_ids
            else []
        )
        trend = session.get(ClusterTrend, cluster.id)
        signals = GapSignals(
            mention_frequency=float(len(paper_ids)),
            trend_slope=trend.slope if trend else 0.0,
            solution_density=_solution_density(session, members),
            citation_impact=(
                sum(math.log1p(value) for value in citations) / len(citations)
                if citations
                else 0.0
            ),
            contradiction_flag=1.0 if cluster.contradiction_flag else 0.0,
        )
        scored = score_gap(signals, settings.gap_weights)
        row = session.get(GapRanking, cluster.id)
        if row is None:
            row = GapRanking(cluster_id=cluster.id)
            session.add(row)
        row.score = scored.score
        row.mention_frequency = signals.mention_frequency
        row.trend_slope = signals.trend_slope
        row.solution_density = signals.solution_density
        row.citation_impact = signals.citation_impact
        row.contradiction_flag = signals.contradiction_flag
        processed.append(cluster.id)
    session.flush()
    return processed

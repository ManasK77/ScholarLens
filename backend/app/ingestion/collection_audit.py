"""The S1 audit trail (D-S1-GATE-01): one run per collection, one decision per
candidate.

`collection_runs` stores what a collection was asked for -- the topic, the
facets it was split into, the gate and lexicon versions and the settings that
judged it. `candidate_decisions` stores every candidate a source returned and
what happened to it: ``admitted`` (written to, or merged into, `papers`),
``rejected`` (with the gate's reason) or ``capped`` (relevant, but the
source's admission cap was already full). Nothing a source returns is dropped
without a row here.

Manual uploads record an ``owner_upload`` decision under an ``upload`` run:
the owner vouches for them, so discovery relevance gating does not apply, but
their provenance is kept in the same place.
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.ingestion.corpus import Base

# Longest client-supplied filename kept as upload provenance.
MAX_FILENAME_CHARS = 255


class CollectionRun(Base):
    __tablename__ = "collection_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)  # collect | upload
    topic: Mapped[str | None] = mapped_column(String, nullable=True)
    gate_version: Mapped[str | None] = mapped_column(String, nullable=True)
    lexicon_version: Mapped[str | None] = mapped_column(String, nullable=True)
    facets: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    settings: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    source_status: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    counts: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class CandidateDecision(Base):
    __tablename__ = "candidate_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("collection_runs.id"), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String, nullable=False)
    # arXiv id, ACL Anthology id or DOI -- whichever the source supplied first;
    # for an upload, the client's filename.
    source_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decision: Mapped[str] = mapped_column(String, nullable=False)
    reason_code: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    tier: Mapped[str | None] = mapped_column(String, nullable=True)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # Set for admitted candidates. SET NULL so removing a paper through the
    # maintenance tool leaves its admission history readable.
    paper_id: Mapped[int | None] = mapped_column(
        ForeignKey("papers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


def _topic_key(topic: str | None) -> str:
    return " ".join((topic or "").lower().split())


def start_collection(
    session: Session, topic: str, gate_description: dict, settings: dict
) -> CollectionRun:
    run = CollectionRun(
        kind="collect",
        topic=topic,
        gate_version=gate_description.get("gate_version"),
        lexicon_version=gate_description.get("lexicon_version"),
        facets=gate_description.get("facets", []),
        settings=settings,
        source_status={},
        counts={},
    )
    session.add(run)
    session.flush()
    return run


def source_ref(candidate: dict) -> str | None:
    return candidate.get("arxiv_id") or candidate.get("acl_id") or candidate.get("doi")


def record(
    session: Session,
    run: CollectionRun,
    source: str,
    candidate: dict,
    decision,
    *,
    paper_id: int | None = None,
    outcome: str | None = None,
) -> CandidateDecision:
    """One candidate's decision. `outcome` overrides admitted/rejected wording."""
    row = CandidateDecision(
        run_id=run.id,
        source=source,
        source_ref=source_ref(candidate),
        title=candidate.get("title"),
        year=candidate.get("year"),
        decision=outcome or ("admitted" if decision.admitted else "rejected"),
        reason_code=decision.reason_code,
        reason=decision.reason,
        score=decision.score,
        tier=decision.tier,
        evidence=decision.evidence,
        paper_id=paper_id,
    )
    session.add(row)
    session.flush()
    return row


def admitted_for_topic(session: Session, paper_id: int, topic: str | None) -> bool:
    """Whether `paper_id` was admitted by an earlier decision for the same topic."""
    key = _topic_key(topic)
    rows = session.execute(
        select(CollectionRun.topic)
        .join(CandidateDecision, CandidateDecision.run_id == CollectionRun.id)
        .where(
            CandidateDecision.paper_id == paper_id,
            CandidateDecision.decision == "admitted",
            CollectionRun.kind == "collect",
        )
    ).all()
    return any(_topic_key(row_topic) == key for (row_topic,) in rows)


def start_upload(session: Session) -> CollectionRun:
    run = CollectionRun(
        kind="upload", topic=None, facets=[], settings={}, source_status={}, counts={}
    )
    session.add(run)
    session.flush()
    return run


def record_upload(
    session: Session, run: CollectionRun, paper_id: int, filename: str | None
) -> CandidateDecision:
    """An owner-supplied PDF: admitted by the owner, not by the discovery gate."""
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1][:MAX_FILENAME_CHARS]
    row = CandidateDecision(
        run_id=run.id,
        source="manual_upload",
        source_ref=name or None,
        title=None,
        year=None,
        decision="admitted",
        reason_code="owner_upload",
        reason="Uploaded by the owner; discovery relevance gating does not apply.",
        score=None,
        tier=None,
        evidence={},
        paper_id=paper_id,
    )
    session.add(row)
    session.flush()
    return row


def serialize_run(run: CollectionRun) -> dict:
    return {
        "id": run.id,
        "kind": run.kind,
        "topic": run.topic,
        "gate_version": run.gate_version,
        "lexicon_version": run.lexicon_version,
        "facets": run.facets,
        "settings": run.settings,
        "source_status": run.source_status,
        "counts": run.counts,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


def serialize_decision(row: CandidateDecision) -> dict:
    return {
        "id": row.id,
        "source": row.source,
        "source_ref": row.source_ref,
        "title": row.title,
        "year": row.year,
        "decision": row.decision,
        "reason_code": row.reason_code,
        "reason": row.reason,
        "score": row.score,
        "tier": row.tier,
        "evidence": row.evidence,
        "paper_id": row.paper_id,
    }


def decisions_for_run(session: Session, run_id: int) -> list[CandidateDecision]:
    return list(
        session.scalars(
            select(CandidateDecision)
            .where(CandidateDecision.run_id == run_id)
            .order_by(CandidateDecision.id)
        )
    )

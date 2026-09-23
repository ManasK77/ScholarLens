"""PostgreSQL/SQLAlchemy persistence for Stage 9 gap reports (GAPEXP-02).

Like Stages 6, 7 and 8 this table is **derived**: everything in it is
regenerated from `cluster_rankings` + `clusters` + the vector store by
`service.generate_top_reports`, so a run replaces the previous one wholesale
rather than merging into it.

`app/api/routes_backend.py` is the frozen consumer contract: `list_reports`
rows expose `.cluster_id`, `.gap`, `.evidence`, `.confidence`,
`.suggested_directions`, `.provider` and `.model`, and `GET /reports`
serializes exactly those seven.

`provider` and `model` are stored beside the text on purpose. A report is a
generated artefact, and six months later "which model wrote this, and was it the
one we evaluated?" is not answerable from the prose.
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    String,
    Text,
    delete,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.core.pipeline import current_run, fingerprint, now
from app.explanation.schema import GapReportRecord
from app.ingestion.corpus import Base


class ReportSnapshot(Base):
    """Historical report data has no FK to replaceable cluster identifiers."""

    __tablename__ = "report_snapshots"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    reports: Mapped[list] = mapped_column(JSON)
    run_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


def serialize_report(row):
    return {
        key: getattr(row, key)
        for key in (
            "cluster_id",
            "gap",
            "evidence",
            "confidence",
            "suggested_directions",
            "provider",
            "model",
            "justification",
        )
    }


def archive_reports(session):
    rows = [serialize_report(row) for row in list_reports(session)]
    if rows:
        digest = fingerprint(rows)
        previous = session.get(ReportSnapshot, digest)
        if previous is None:
            run = current_run()
            session.add(
                ReportSnapshot(id=digest, reports=rows, run_id=run.id if run else None)
            )
            session.flush()
        else:
            previous.created_at = now()


def report_snapshot(session):
    rows = list_reports(session)
    if rows:
        return [serialize_report(row) for row in rows]
    previous = session.scalar(
        select(ReportSnapshot)
        .order_by(ReportSnapshot.created_at.desc(), ReportSnapshot.id.desc())
        .limit(1)
    )
    if previous is None:
        return []
    return [
        {
            **row,
            "stale": True,
            "snapshot_id": previous.id,
            "snapshot_created_at": previous.created_at.isoformat(),
        }
        for row in previous.reports
    ]


class GapReport(Base):
    """One top-ranked cluster's grounded Mode A report.

    A separate table rather than columns on `clusters`, for the same reason
    `cluster_trends` and `cluster_rankings` are: Phase 6 rewrites `clusters`
    wholesale and cluster ids are not stable across rebuilds. The `ForeignKey`
    is what makes `replace_clusters._dependent_tables()` discover this table and
    clear it, so a report can never end up attached to a recycled id describing
    a different theme.
    """

    __tablename__ = "gap_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    cluster_id: Mapped[int] = mapped_column(
        ForeignKey("clusters.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    # The gap statement itself — README §8's "Research Gap:" line.
    gap: Mapped[str] = mapped_column(Text, nullable=False)
    # The retrieved paragraphs the model actually cited, in retrieval order.
    # `frontend/src/types.ts` types this `Evidence[]`; the six keys are fixed by
    # `schema.evidence_from_point`. Stored rather than re-retrieved so the
    # report stays auditable after the next re-index moves the vectors.
    evidence: Mapped[list] = mapped_column(JSON, nullable=False)
    # A number in [0, 1], not README's illustrative "High": `App.tsx` renders
    # `Math.round(confidence * 100)`.
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    suggested_directions: Mapped[list] = mapped_column(JSON, nullable=False)
    # The grounded A/B/C justification (D-09-05): which of "the problem
    # exists", "existing solutions fall short" and "a specific gap remains"
    # the retrieved evidence actually carried, each with the ids behind it.
    # Nullable so a database written before this column can still be read; the
    # API serves `{}` for those rows.
    justification: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Which model produced this text, so a stored report can be attributed.
    provider: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


def replace_reports(session: Session, reports: list[GapReportRecord]) -> list[int]:
    """Atomically replace every stored report within the caller's transaction.

    Returns the cluster ids in the order given — `POST /reports` returns them to
    the caller under the key `clusters`, and that order is rank order.

    Flushes but never commits: `routes_backend` owns the transaction and commits
    after the call.

    The wipe is unconditional and explicit rather than relying on the foreign
    key's `ON DELETE CASCADE`, because SQLite — the documented local-dev store —
    does not enforce foreign keys by default.
    """
    archive_reports(session)
    session.execute(delete(GapReport))

    session.add_all(
        GapReport(
            cluster_id=report.cluster_id,
            gap=report.gap,
            evidence=list(report.evidence),
            confidence=report.confidence,
            suggested_directions=list(report.suggested_directions),
            provider=report.provider,
            model=report.model,
            justification=dict(report.justification),
        )
        for report in reports
    )
    session.flush()
    return [report.cluster_id for report in reports]


def list_reports(session: Session) -> list[GapReport]:
    """Every stored report, best gap first.

    Insertion order *is* rank order — `generate_top_reports` walks
    `list_rankings` highest score first — so ordering by the surrogate key
    reproduces the ranking without duplicating the score in this table. The
    dashboard falls back to this order for its rank numbers when `/gaps` is
    empty, so it must not depend on which rows PostgreSQL happens to return.
    """
    return list(session.scalars(select(GapReport).order_by(GapReport.id)))

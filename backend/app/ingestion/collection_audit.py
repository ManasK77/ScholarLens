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

**Evidence (Phase 1 closeout, 2026-10-12).** A decision's evidence keeps the
gate's own verdict in ``gate`` and every later transition in ``gate_history``
(`relevance.RelevanceDecision._then`); `gate_history` here also reconstructs
that history for records written before it existed. Rejected and capped
candidates carry ``text_audit`` (`text_audit`): a hash and length of the text
the gate judged and a bounded abstract excerpt, so a false negative can be
reviewed without storing whole abstracts.
"""

import hashlib
import re
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
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
from app.ingestion.relevance import normalize

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
    # The run's lifecycle and its complete, persisted result (2026-10-12):
    # `running` from the moment the run starts, then `completed`,
    # `completed_with_warnings`, `partial` or `failed`. `summary` is the
    # /collect response minus the id list, so a finished run can be audited
    # without its response or the logs. Nullable: older runs have neither.
    status: Mapped[str | None] = mapped_column(String, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)


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


class FulltextAttempt(Base):
    """One provider's attempt to obtain one paper's full text (D-FULLTEXT-01).

    Appended, never rewritten: a later failure never hides an earlier
    success. `location` is the URL's host and path only -- no query string.
    """

    __tablename__ = "fulltext_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("collection_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    paper_id: Mapped[int | None] = mapped_column(
        ForeignKey("papers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(String, nullable=False)
    stage: Mapped[str] = mapped_column(String, nullable=False)  # discovery | enrichment
    outcome: Mapped[str] = mapped_column(String, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retryable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    location_found: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    pdf_path: Mapped[str | None] = mapped_column(String, nullable=True)
    detail: Mapped[str | None] = mapped_column(String, nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


def serialize_attempt(row: FulltextAttempt) -> dict:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "paper_id": row.paper_id,
        "provider": row.provider,
        "stage": row.stage,
        "outcome": row.outcome,
        "http_status": row.http_status,
        "retryable": row.retryable,
        "location_found": row.location_found,
        "location": row.location,
        "stored": bool(row.pdf_path),
        "detail": row.detail,
        "attempted_at": row.attempted_at.isoformat() if row.attempted_at else None,
    }


def attempts_for_run(session: Session, run_id: int) -> list[FulltextAttempt]:
    return list(
        session.scalars(
            select(FulltextAttempt)
            .where(FulltextAttempt.run_id == run_id)
            .order_by(FulltextAttempt.id)
        )
    )


def _topic_key(topic: str | None) -> str:
    return " ".join((topic or "").lower().split())


def start_collection(
    session: Session, topic: str, gate_description: dict, settings: dict
) -> CollectionRun:
    run = CollectionRun(
        kind="collect",
        status="running",
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
        "status": run.status,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "summary": run.summary,
    }


def text_audit(candidate: dict, excerpt_chars: int) -> dict:
    """Bounded evidence of the text a rejected or capped candidate was judged on.

    The hash and length are of the gate's normalized text, so two records of
    the same candidate text can be matched without storing it; the excerpt is
    at most `excerpt_chars` characters of the abstract, none when that is 0.
    Uses only what the source already returned -- nothing is fetched.
    """
    title = " ".join((candidate.get("title") or "").split())
    abstract = " ".join((candidate.get("abstract") or "").split())
    normalized = normalize(f"{title} {abstract}")
    excerpt = abstract[:excerpt_chars] if abstract and excerpt_chars > 0 else None
    return {
        "normalization": "relevance.normalize(title + ' ' + abstract)",
        "sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "normalized_length": len(normalized),
        "abstract_available": bool(abstract),
        "abstract_length": len(abstract),
        "abstract_excerpt": excerpt,
        "excerpt_truncated": None if excerpt is None else len(abstract) > len(excerpt),
        "excerpt_max_chars": excerpt_chars,
    }


# The promotion reason gate v1/v2 wrote, which is the only place a capped
# promotion's original verdict survived before `gate_history` existed.
_PROMOTED_FROM = re.compile(r"the gate's verdict on this copy alone was '([^']+)'")
_STEP_FOR = {
    "duplicate_of_admitted": "duplicate_promotion",
    "source_cap_reached": "source_cap",
    "owner_upload": "owner_upload",
}


def _entry(step: str, admitted: bool, reason_code: str, reason: str) -> dict:
    return {
        "step": step,
        "admitted": admitted,
        "reason_code": reason_code,
        "reason": reason,
    }


def gate_history(row: CandidateDecision) -> tuple[list[dict], str]:
    """Every verdict this candidate went through, oldest first, and its source.

    The source says how much of that history was actually stored:

    * ``recorded`` -- the row stored its own `gate_history` (a promotion or a
      cap since 2026-10-12);
    * ``no_transition`` -- the row is a single verdict (the gate's, or an
      owner upload) and nothing happened after it; the history is that one
      verdict, and nothing is implied about a history that was not kept;
    * ``reconstructed`` -- an older row that *did* go through a transition but
      stored only `evidence["gate"]`. Before 2026-10-12, a promoted copy that
      was then capped stored the *promotion* there; its original verdict is
      recovered from the promotion's reason text and marked ``recovered_from``.
    """
    evidence = row.evidence or {}
    if evidence.get("gate_history"):
        return list(evidence["gate_history"]), "recorded"
    history: list[dict] = []
    earlier = evidence.get("gate")
    if not earlier:
        step = _STEP_FOR.get(row.reason_code, "gate")
        return [
            _entry(step, row.decision == "admitted", row.reason_code, row.reason)
        ], "no_transition"
    if earlier:
        if earlier.get("reason_code") == "duplicate_of_admitted":
            match = _PROMOTED_FROM.search(earlier.get("reason") or "")
            if match:
                history.append(
                    {
                        **_entry("gate", False, match.group(1), ""),
                        "recovered_from": "duplicate_promotion_reason",
                    }
                )
            history.append(
                _entry(
                    "duplicate_promotion",
                    True,
                    "duplicate_of_admitted",
                    earlier.get("reason") or "",
                )
            )
        else:
            history.append(
                _entry(
                    "gate",
                    bool(earlier.get("admitted")),
                    earlier.get("reason_code") or "",
                    earlier.get("reason") or "",
                )
            )
    history.append(
        _entry(
            _STEP_FOR.get(row.reason_code, "gate"),
            row.decision == "admitted",
            row.reason_code,
            row.reason,
        )
    )
    return history, "reconstructed"


def serialize_decision(row: CandidateDecision) -> dict:
    history, history_source = gate_history(row)
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
        # Additive (2026-10-12): the decision's verdicts in order; see
        # `gate_history` for how older records are reconstructed.
        "gate_history": history,
        "gate_history_source": history_source,
    }


def decisions_for_run(session: Session, run_id: int) -> list[CandidateDecision]:
    return list(
        session.scalars(
            select(CandidateDecision)
            .where(CandidateDecision.run_id == run_id)
            .order_by(CandidateDecision.id)
        )
    )

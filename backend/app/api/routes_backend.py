"""Complete Phase 11 REST surface for Phases 1–10."""

import logging
from datetime import UTC, date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.security import require_api_key
from app.api.write_guard import exclusive_write
from app.clustering import service as clustering_service
from app.clustering.storage import (
    contradictions_for_cluster,
    list_clusters,
    members_for_cluster,
)
from app.core.config import get_settings
from app.core.corpus_reset import reset_corpus
from app.core.maintenance import resume_deletions
from app.core.pipeline import (
    PaperStageState,
    PipelineBusy,
    fingerprint,
    pipeline_run,
    settings_fingerprint,
)
from app.embeddings.indexer import EvidenceIndex, index_all
from app.explanation import service as report_service
from app.explanation.llm import (
    LLMError,
    LLMRateLimitError,
    LLMUnavailableError,
)
from app.explanation.schema import MAX_EVIDENCE_CHARS
from app.explanation.selection import evidence_summary
from app.explanation.storage import list_reports, report_snapshot
from app.extraction.citations import citation_markers
from app.extraction.service import extract_all_parsed
from app.extraction.storage import ExtractedFact
from app.ingestion import corpus
from app.ingestion.corpus import Paper
from app.interactive.deadline import (
    Deadline,
    QueryDeadlineExceeded,
    bound_services,
    call_with_deadline,
)
from app.interactive.workflow import default_services, run_query
from app.ner_re import service as graph_service
from app.ner_re.graph import graph_session, graph_snapshot
from app.parsing.service import parse_all_pending
from app.parsing.storage import ParsedDocument
from app.ranking import service as ranking_service
from app.ranking.signals import evidence_scope
from app.ranking.storage import ClusterRanking, list_rankings
from app.trends import service as trend_service
from app.trends.analysis import trend_qualifiers
from app.trends.storage import list_trends

router = APIRouter(dependencies=[Depends(require_api_key)])
logger = logging.getLogger(__name__)


class QueryRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)

    @field_validator("question")
    @classmethod
    def _strip_non_blank_question(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError(
                "question must contain at least 3 non-whitespace characters"
            )
        return value


class QueryCitation(BaseModel):
    id: str
    paper_id: int
    title: str | None
    text: str
    year: int | None
    source_type: str | None


class QueryClaim(BaseModel):
    text: str
    evidence_ids: list[str]


class QueryResponse(BaseModel):
    """`POST /query`. Citations are listed only for a grounded answer; every
    refusal outcome returns no claims and no citations."""

    outcome: Literal[
        "grounded_answer",
        "no_indexed_evidence",
        "insufficient_evidence",
        "grounding_failed",
    ]
    answer: str
    claims: list[QueryClaim]
    citations: list[QueryCitation]
    grounded: bool
    retrieval_rounds: int
    retry_count: int
    regeneration_count: int
    # Claims of a grounded answer's draft that failed a check and are not
    # shown; non-zero means the answer may be incomplete. 0 for every refusal.
    removed_claim_count: int = 0
    trace: list[str]
    provider: str
    model: str


class PaperOut(BaseModel):
    """`GET /papers` item. Field set unchanged from the untyped dict it replaced."""

    id: int
    title: str | None
    authors: list | None
    year: int | None
    venue: str | None
    doi: str | None
    abstract: str | None
    source: str
    has_full_text: bool
    citation_count: int | None


class TrendOut(BaseModel):
    """`GET /trends` item. `yearly_counts` maps a year to distinct papers."""

    cluster_id: int
    yearly_counts: dict
    slope: float
    qualifiers: dict


class GapSignals(BaseModel):
    mention_frequency: int
    trend_slope: float
    solution_density: float
    citation_impact: float
    contradiction_flag: float


class GapOut(BaseModel):
    """`GET /gaps` item, highest score first, with its signal breakdown."""

    cluster_id: int
    score: float
    paper_count: int
    evidence_scope: Literal["corroborated", "single_paper"]
    exploratory: bool
    signals: GapSignals


class ClusterMemberOut(BaseModel):
    """One sentence in a cluster, with every label it was stored under."""

    point_id: str
    point_ids: list[str]
    paper_id: int
    text: str
    year: int | None
    source_type: str
    source_types: list[str]
    citation_markers: list[str]


class ContradictionPairOut(BaseModel):
    """A verified cross-paper disagreement. A side's text is null when that
    point is no longer a member of the cluster."""

    left_point_id: str
    left_paper_id: int
    left_text: str | None
    right_point_id: str
    right_paper_id: int
    right_text: str | None
    score: float


class ClusterOut(BaseModel):
    """`GET /clusters` item, largest cluster first."""

    id: int
    label: str | None
    member_count: int
    paper_count: int
    evidence_scope: Literal["corroborated", "single_paper"]
    contradiction_flag: bool
    contradiction_score: float
    contradictions: list[ContradictionPairOut]
    members: list[ClusterMemberOut]


class ReportEvidenceOut(BaseModel):
    """One cited passage (`schema.evidence_from_point`'s six keys).

    `citation_markers` and `truncated` are derived for current reports only;
    rows served from an archived snapshot omit them.
    """

    id: str
    paper_id: int
    # Absent from the oldest archived snapshots, which stored only id, paper
    # and text; omitted from the response then, never sent as null.
    title: str | None = None
    text: str
    year: int | None = None
    source_type: str | None = None
    citation_markers: list[str] | None = None
    truncated: bool | None = None


class ConflictVerification(BaseModel):
    """`explanation.verification`'s NLI check of a claimed conflict."""

    method: str
    threshold: float
    pairs_checked: int
    max_score: float | None
    verified: bool


class JustificationClaim(BaseModel):
    """One part of a report's justification (D-09-05).

    `basis` is absent on claims stored before it existed (the oldest
    snapshot); `llm_supported` and `verification` exist on the conflict claim
    only, the latter only when the model asserted a conflict.
    """

    supported: bool
    statement: str
    evidence_ids: list[str]
    basis: str | None = None
    llm_supported: bool | None = None
    verification: ConflictVerification | None = None


class ReportJustification(BaseModel):
    """`{}` for a report stored before justifications existed."""

    problem_evidence: JustificationClaim | None = None
    solution_insufficiency_evidence: JustificationClaim | None = None
    research_gap_evidence: JustificationClaim | None = None
    conflicting_evidence: JustificationClaim | None = None


class EvidenceSummaryOut(BaseModel):
    passage_count: int
    unique_paper_count: int
    max_passages_from_one_paper: int


class ReportOut(BaseModel):
    """`GET /reports` item, best gap first.

    Two sources, one shape: current reports carry `evidence_summary` and a
    non-null `justification`; when none exist the latest archived snapshot is
    served instead, its rows marked `stale` with `snapshot_id` and
    `snapshot_created_at`, stored as they were (justification may be null, or
    absent from a snapshot archived before justifications existed).
    Keys a row does not have are omitted, never sent as null.
    """

    cluster_id: int
    gap: str
    evidence: list[ReportEvidenceOut]
    confidence: float
    suggested_directions: list[str]
    provider: str
    model: str
    justification: ReportJustification | None = None
    evidence_summary: EvidenceSummaryOut | None = None
    stale: bool | None = None
    snapshot_id: str | None = None
    snapshot_created_at: str | None = None


class GraphNodeOut(BaseModel):
    id: str
    labels: list[str]
    label: str | None


class GraphEdgeOut(BaseModel):
    source: str
    target: str
    type: str


class GraphOut(BaseModel):
    """`GET /graph`: a bounded, self-consistent part of the graph, with the
    whole graph's size so a caller can tell when it is `truncated`."""

    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]
    total_nodes: int
    total_edges: int
    truncated: bool


class PipelineRequest(BaseModel):
    generate_reports: bool = True


# What a caller is told when S2–S8 succeeded and only Stage 9 was refused. It
# names `POST /reports` on purpose: the evidence base is intact and report
# generation is resumable against it, so re-running the whole pipeline — let
# alone rebuilding the corpus — would repeat hours of work to retry one call.
_S9_PREFIX = (
    "The evidence base was built and is intact, but reports were not "
    "generated (retry with POST /reports — S2-S8 do not need to run again): "
)


@router.post("/parse")
def parse_papers(session: Session = Depends(corpus.get_session)):  # noqa: B008
    settings = get_settings()
    try:
        with pipeline_run(session, {"operation": "parse"}) as run, run.stage("s2"):
            result = parse_all_pending(
                session,
                settings.grobid_url,
                settings.unpaywall_email,
                settings.pdf_storage_path,
            )
            session.commit()
            return {
                "processed": result.processed,
                "missing_pdfs": result.missing_pdfs,
                **_parse_outcomes(result),
                "run_id": run.id,
            }
    except PipelineBusy as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/extract")
def extract_papers(session: Session = Depends(corpus.get_session)):  # noqa: B008
    try:
        with pipeline_run(session, {"operation": "extract"}) as run, run.stage("s3"):
            ids = extract_all_parsed(session)
            return {"processed": ids, "count": len(ids), "run_id": run.id}
    except PipelineBusy as exc:
        raise HTTPException(409, str(exc)) from exc


def _today() -> date:
    """Today's date, for partial-year trend caveats. Patched in tests."""
    return datetime.now(UTC).date()


def _parse_outcomes(batch) -> dict:
    """S2 results that are not clean parses (D-S2-STATUS-01). Additive keys.

    `processed` alone cannot tell a GROBID parse from a PDF no parser read, or
    from a fallback parse made while GROBID was down (retried on a later run).
    """
    return {
        "no_text_pdfs": list(getattr(batch, "no_text", [])),
        "grobid_unavailable": list(getattr(batch, "grobid_unavailable", [])),
    }


def _paper_dict(paper: Paper) -> dict:
    return {
        "id": paper.id,
        "title": paper.title,
        "authors": paper.authors,
        "year": paper.year,
        "venue": paper.venue,
        "doi": paper.doi,
        "abstract": paper.abstract,
        "source": paper.source,
        "has_full_text": paper.has_full_text,
        "citation_count": paper.citation_count,
    }


@router.get("/papers", response_model=list[PaperOut])
def papers(
    year: int | None = None,
    venue: str | None = None,
    has_full_text: bool | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    session: Session = Depends(corpus.get_session),  # noqa: B008
):
    statement = select(Paper).order_by(Paper.id).offset(offset).limit(limit)
    if year is not None:
        statement = statement.where(Paper.year == year)
    if venue is not None:
        statement = statement.where(Paper.venue == venue)
    if has_full_text is not None:
        statement = statement.where(Paper.has_full_text.is_(has_full_text))
    return [_paper_dict(paper) for paper in session.scalars(statement)]


@router.get("/graph", response_model=GraphOut)
def get_graph(limit: int = Query(500, ge=1, le=2000)):
    with graph_session() as neo4j:
        return graph_snapshot(neo4j, limit)


@router.post("/graph", dependencies=[Depends(exclusive_write)])
def build_graph(session: Session = Depends(corpus.get_session)):  # noqa: B008
    with graph_session() as neo4j:
        ids = graph_service.extract_all(session, neo4j)
    session.commit()
    return {"processed": ids, "count": len(ids)}


@router.post("/index", dependencies=[Depends(exclusive_write)])
def build_index(session: Session = Depends(corpus.get_session)):  # noqa: B008
    with graph_session() as neo4j:
        count = index_all(session, neo4j)
    return {"indexed": count}


def _contradiction_payload(session, cluster_id: int, members) -> list[dict]:
    """The actual disagreeing statement pairs behind a contradiction flag.

    Added so "Conflicting evidence" can be explained rather than asserted: the
    flag alone told a reader that two papers disagreed but never which two or
    about what, which is how four pairs resting on an ethics disclaimer went
    unnoticed in the corpus's top-ranked gap.

    Additive and backward compatible -- every existing key of `/clusters` is
    untouched, and `frontend/src/types.ts` types this as optional.
    """
    text_by_point = {member.point_id: member.text for member in members}
    return [
        {
            "left_point_id": pair.left_point_id,
            "left_paper_id": pair.left_paper_id,
            "left_text": text_by_point.get(pair.left_point_id),
            "right_point_id": pair.right_point_id,
            "right_paper_id": pair.right_paper_id,
            "right_text": text_by_point.get(pair.right_point_id),
            "score": pair.score,
        }
        for pair in contradictions_for_cluster(session, cluster_id)
    ]


def _cluster_payload(session, cluster) -> dict:
    """One cluster as `/clusters` serves it.

    `members` is read once and used twice -- for the member list and to give
    each contradiction pair its statement text -- so it is bound here rather
    than queried twice per cluster.
    """
    members = members_for_cluster(session, cluster.id)
    paper_count = len({member.paper_id for member in members})
    return {
        "id": cluster.id,
        "label": cluster.label,
        "member_count": cluster.member_count,
        # Additive (S8 scope): a theme one paper states is exploratory.
        "paper_count": paper_count,
        "evidence_scope": evidence_scope(paper_count),
        "contradiction_flag": cluster.contradiction_flag,
        "contradiction_score": cluster.contradiction_score,
        "contradictions": _contradiction_payload(session, cluster.id, members),
        "members": _member_payload(members),
    }


def _member_payload(members) -> list[dict]:
    """One entry per sentence, with every label it was stored under (D-06-14).

    Stage 6 keeps a row per label so none is lost; listing the rows made a
    doubly-typed sentence appear twice beside a count of one. `point_id` and
    `source_type` keep their meaning (the first row's); `point_ids`,
    `source_types` and `citation_markers` are additive.
    """
    grouped: dict[tuple[int, str], dict] = {}
    for member in members:
        entry = grouped.get((member.paper_id, member.text))
        if entry is None:
            grouped[(member.paper_id, member.text)] = {
                "point_id": member.point_id,
                "point_ids": [member.point_id],
                "paper_id": member.paper_id,
                "text": member.text,
                "year": member.year,
                "source_type": member.source_type,
                "source_types": [member.source_type],
                "citation_markers": citation_markers(member.text),
            }
        else:
            entry["point_ids"].append(member.point_id)
            if member.source_type not in entry["source_types"]:
                entry["source_types"].append(member.source_type)
    for entry in grouped.values():
        entry["source_types"].sort()
    return list(grouped.values())


@router.get("/clusters", response_model=list[ClusterOut])
def clusters(session: Session = Depends(corpus.get_session)):  # noqa: B008
    return [_cluster_payload(session, cluster) for cluster in list_clusters(session)]


@router.post("/clusters", dependencies=[Depends(exclusive_write)])
def rebuild_clusters(session: Session = Depends(corpus.get_session)):  # noqa: B008
    ids = clustering_service.rebuild_clusters(session)
    session.commit()
    return {"clusters": ids, "count": len(ids)}


@router.get("/trends", response_model=list[TrendOut])
def trends(session: Session = Depends(corpus.get_session)):  # noqa: B008
    return [
        {
            "cluster_id": row.cluster_id,
            "yearly_counts": row.yearly_counts,
            "slope": row.slope,
            # Additive (D-07-08): sample size and partial-year caveats.
            "qualifiers": trend_qualifiers(row.yearly_counts, _today()),
        }
        for row in list_trends(session)
    ]


@router.post("/trends", dependencies=[Depends(exclusive_write)])
def rebuild_trends(session: Session = Depends(corpus.get_session)):  # noqa: B008
    ids = trend_service.rebuild_trends(session)
    session.commit()
    return {"clusters": ids, "count": len(ids)}


@router.get("/gaps", response_model=list[GapOut])
def gaps(
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(corpus.get_session),  # noqa: B008
):
    return [
        {
            "cluster_id": row.cluster_id,
            "score": row.score,
            # Additive: distinct papers stating the theme, and whether that is
            # one paper's view (exploratory) or corroborated. Ranking unchanged.
            "paper_count": row.mention_frequency,
            "evidence_scope": evidence_scope(row.mention_frequency),
            "exploratory": evidence_scope(row.mention_frequency) == "single_paper",
            "signals": {
                "mention_frequency": row.mention_frequency,
                "trend_slope": row.trend_slope,
                "solution_density": row.solution_density,
                "citation_impact": row.citation_impact,
                "contradiction_flag": row.contradiction_flag,
            },
        }
        for row in list_rankings(session, limit)
    ]


@router.post("/gaps", dependencies=[Depends(exclusive_write)])
def rebuild_gaps(session: Session = Depends(corpus.get_session)):  # noqa: B008
    ids = ranking_service.rebuild_rankings(session)
    session.commit()
    return {"clusters": ids, "count": len(ids)}


@router.get(
    "/reports",
    response_model=list[ReportOut],
    # A current row and an archived row have different key sets; omit, not null.
    response_model_exclude_unset=True,
)
def reports(session: Session = Depends(corpus.get_session)):  # noqa: B008
    return [
        {
            "cluster_id": row.cluster_id,
            "gap": row.gap,
            "evidence": _evidence_with_markers(row.evidence),
            "confidence": row.confidence,
            "suggested_directions": row.suggested_directions,
            "provider": row.provider,
            "model": row.model,
            # Which parts of the gap statement the evidence actually carried
            # (D-09-05), and since D-09-13 the `basis` of each claim; `{}` for
            # a report stored before justifications existed.
            "justification": row.justification or {},
            # Passages and papers are not the same number, and a reader cannot
            # tell eight passages from one study apart from eight papers without
            # being told (D-09-04). Derived, so no stored row has to be migrated.
            "evidence_summary": evidence_summary(row.evidence or []),
        }
        for row in list_reports(session)
    ] or report_snapshot(session)


def _evidence_with_markers(evidence) -> list[dict]:
    """Each passage plus the citation markers in its text (D-S3-CITE-01).

    So a reader can see when a quoted finding is attributed to other work.
    Derived; the stored evidence is unchanged.

    `truncated` (additive): the quote reached `MAX_EVIDENCE_CHARS`, the cap
    Stage 9 stores, so the source passage may continue past it. A stored
    quote is never longer than the cap.
    """
    return [
        {
            **item,
            "citation_markers": citation_markers(item.get("text")),
            "truncated": len(item.get("text") or "") >= MAX_EVIDENCE_CHARS,
        }
        for item in evidence or []
    ]


def _llm_http_error(exc: LLMError, prefix: str = "") -> HTTPException:
    """An LLM failure as the explicit error README §12 promises.

    `LLMError` messages are this project's own strings — a missing or rejected
    key, an outage, an exhausted quota — optionally carrying the provider's own
    bounded diagnostic (which ceiling was hit, which model was refused). Neither
    part contains a credential, so unlike an arbitrary exception they are safe
    to return; `/query` already returns them the same way.

    The status code is what tells the caller *what to do*, so the three cases
    are kept apart rather than collapsed into one "unavailable":

    * **429** — Groq is metering us. Nothing is wrong with the deployment and
      the evidence base is intact; come back later. `Retry-After` carries the
      provider's own wait when it named one.
    * **503** — a genuine outage or transport failure. Retrying is reasonable.
    * **502** — a rejected key or an unusable model. Waiting will not help;
      something has to be configured.
    """
    if isinstance(exc, LLMRateLimitError):
        headers = (
            {"Retry-After": str(int(exc.retry_after))}
            if exc.retry_after is not None
            else None
        )
        return HTTPException(429, detail=f"{prefix}{exc}", headers=headers)
    status_code = 503 if isinstance(exc, LLMUnavailableError) else 502
    return HTTPException(status_code=status_code, detail=f"{prefix}{exc}")


@router.post("/reports", dependencies=[Depends(exclusive_write)])
def generate_reports(session: Session = Depends(corpus.get_session)):  # noqa: B008
    try:
        outcome = report_service.generate_reports(session)
    except LLMError as exc:
        logger.warning("Mode A report generation stopped: %s", exc)
        raise _llm_http_error(exc) from exc
    except Exception as exc:
        logger.exception("Mode A report generation failed")
        raise HTTPException(status_code=502, detail="Report generation failed") from exc
    session.commit()
    return {
        "clusters": outcome.stored,
        "count": len(outcome.stored),
        # Additive (D-09-08): which of the requested reports failed, and why.
        "report_generation": outcome.as_dict(),
    }


@router.post(
    "/query",
    response_model=QueryResponse,
    responses={504: {"description": "The overall query deadline passed; no answer."}},
)
def query(body: QueryRequest):
    settings = get_settings()
    deadline = Deadline(settings.query_deadline_seconds)

    def answer() -> dict:
        return run_query(
            body.question,
            bound_services(default_services(settings), deadline),
            settings.retrieval_round_cap,
            settings.retrieval_top_n,
            settings.retrieval_min_chunks,
            settings.retrieval_min_papers,
            settings.retrieval_min_score,
            settings.grounding_regeneration_cap,
            settings.retrieval_min_papers_paper_specific,
            settings.retrieval_pool_factor,
        )

    try:
        return call_with_deadline(answer, deadline)
    except QueryDeadlineExceeded as exc:
        # Never a partial or "grounded" answer: whatever the worker produced
        # after this point is discarded. The question is not logged.
        logger.warning(
            "Interactive GraphRAG query exceeded its %gs deadline at step=%s",
            deadline.seconds,
            exc,
        )
        raise HTTPException(
            status_code=504,
            detail=(
                f"The question was not answered within the "
                f"{deadline.seconds:g}-second limit. No answer was produced; "
                "try a narrower question or retry later."
            ),
        ) from exc
    except LLMError as exc:
        # Same mapping as /reports: 429 (+ Retry-After) when Groq is metering
        # us, 503 for an outage, 502 for a rejected key or unusable model.
        logger.warning("Interactive GraphRAG LLM call failed: %s", exc)
        raise _llm_http_error(exc) from exc
    except Exception as exc:
        logger.exception("Interactive GraphRAG query failed")
        raise HTTPException(status_code=502, detail="Query generation failed") from exc


@router.post("/pipeline/run")
def run_pipeline(
    body: PipelineRequest,
    session: Session = Depends(corpus.get_session),  # noqa: B008
):
    settings = get_settings()
    try:
        with pipeline_run(
            session,
            settings_fingerprint(
                settings,
                (
                    "nerre_",
                    "embedding_",
                    "clustering_",
                    "nli_",
                    "gap_",
                    "report_",
                    "groq_model",
                ),
            ),
        ) as run:
            return _execute_pipeline(body, session, settings, run)
    except PipelineBusy as exc:
        raise HTTPException(409, str(exc)) from exc


# Bumped when Stages 6-8 produce something different from unchanged inputs.
# 2: each sentence is clustered once, however many labels it has (D-06-14).
_DERIVED_VERSION = 2


def _derived_fingerprint(session, settings):
    return fingerprint(
        {
            "version": _DERIVED_VERSION,
            "stages": [
                (s.paper_id, s.stage, s.input_hash, s.status)
                for s in session.scalars(
                    select(PaperStageState)
                    .where(PaperStageState.stage.in_(["s3", "s4", "s5"]))
                    .order_by(PaperStageState.paper_id, PaperStageState.stage)
                )
            ],
            "papers": [
                (p.id, p.title, p.year, p.venue, p.citation_count)
                for p in session.scalars(select(Paper).order_by(Paper.id))
            ],
            "contributions": [
                (f.paper_id, f.text)
                for f in session.scalars(
                    select(ExtractedFact)
                    .where(ExtractedFact.fact_type == "contribution")
                    .order_by(ExtractedFact.paper_id, ExtractedFact.id)
                )
            ],
            "settings": settings_fingerprint(
                settings,
                (
                    "clustering_",
                    "nli_",
                    "contradiction_",
                    "trend_",
                    "gap_",
                    "embedding_",
                ),
            ),
        }
    )


# Bumped when what an `s9` marker *means* changes. "s9-outcome-v2": a marker is
# written only for a complete pass (D-09-08). Markers from before that were
# also written after partial passes, so they must not count as current.
_S9_MARKER_VERSION = "s9-outcome-v2"


def _report_digest(digest: str, settings) -> str:
    """What S9's stored reports were produced from: evidence + report settings."""
    return fingerprint(
        [
            digest,
            settings_fingerprint(settings, ("report_", "groq_model")),
            "prompt-v1",
            _S9_MARKER_VERSION,
        ]
    )


def _count(result) -> int:
    """How many items a stage produced. Stages return either ids or a count."""
    return len(result) if hasattr(result, "__len__") else int(result)


def _execute_pipeline(body, session, settings, run):
    # §4.2: the run's own header line. The run id reaches the record through
    # the logging filter, so it is not repeated here.
    logger.info(
        "pipeline start generate_reports=%s papers=%d full_text=%d parsed=%d",
        body.generate_reports,
        session.scalar(select(func.count()).select_from(Paper)) or 0,
        session.scalar(
            select(func.count()).select_from(Paper).where(Paper.has_full_text.is_(True))
        )
        or 0,
        session.scalar(select(func.count()).select_from(ParsedDocument)) or 0,
    )
    # A killed explicit deletion is completed before any stage can reindex it.
    if session.scalar(
        select(PaperStageState).where(PaperStageState.stage == "delete").limit(1)
    ):
        with graph_session(settings) as neo4j, run.stage("delete"):
            resume_deletions(session, neo4j, EvidenceIndex(settings))
    with run.stage("s2"):
        parsing = parse_all_pending(
            session,
            settings.grobid_url,
            settings.unpaywall_email,
            settings.pdf_storage_path,
        )
        session.commit()
    parsed = parsing.processed
    with run.stage("s3"):
        extracted = extract_all_parsed(session)
        session.commit()
    with graph_session(settings) as neo4j:
        with run.stage("s4"):
            graphed = graph_service.extract_all(session, neo4j)
        with run.stage("s5"):
            indexed = index_all(session, neo4j, EvidenceIndex(settings))
    digest = _derived_fingerprint(session, settings)
    with run.stage("s6-s8"):
        if run.derived_current("s6-s8", digest):
            clusters = [r.id for r in list_clusters(session)]
            trends = [r.cluster_id for r in list_trends(session)]
            gaps = list(
                session.scalars(
                    select(ClusterRanking.cluster_id).order_by(
                        ClusterRanking.score.desc()
                    )
                )
            )
        else:
            clusters = clustering_service.rebuild_clusters(session, settings=settings)
            trends = trend_service.rebuild_trends(session)
            gaps = ranking_service.rebuild_rankings(session, settings)
            run.mark_derived("s6-s8", digest)
        session.commit()
    reports: list[int] = []
    report_generation = report_service.not_requested()
    if body.generate_reports:
        report_digest = _report_digest(digest, settings)
        with run.stage("s9"):
            try:
                if run.derived_current("s9", report_digest):
                    reports = [r.cluster_id for r in list_reports(session)]
                    report_generation = report_service.ReportOutcome(
                        requested=len(reports), stored=reports, reused=reports
                    ).as_dict()
                else:
                    # D-09-08: only a pass with no failed generation is
                    # complete. A partial one stores what succeeded and is
                    # resumed next run -- only its missing reports are
                    # generated -- instead of being served as finished.
                    outcome = report_service.generate_reports(
                        session,
                        settings,
                        resume=run.derived_current("s9-partial", report_digest),
                    )
                    reports = outcome.stored
                    report_generation = outcome.as_dict()
                    if outcome.status == "complete":
                        run.mark_derived("s9", report_digest)
                    elif outcome.status == "partial":
                        # Every stored report now belongs to this digest, so
                        # the next run may reuse them.
                        run.mark_derived("s9-partial", report_digest)
            except LLMError as exc:
                session.rollback()
                raise _llm_http_error(exc, _S9_PREFIX) from exc
            session.commit()
    logger.info(
        "pipeline complete parsed=%d missing_pdfs=%d extracted=%d graphed=%d "
        "indexed=%d clusters=%d trends=%d gaps=%d reports=%d report_status=%s "
        "checkpoints=%s",
        _count(parsed),
        _count(parsing.missing_pdfs),
        _count(extracted),
        _count(graphed),
        _count(indexed),
        _count(clusters),
        _count(trends),
        _count(gaps),
        _count(reports),
        report_generation["status"],
        run.counts,
    )
    return {
        "parsed": parsed,
        "missing_pdfs": parsing.missing_pdfs,
        **_parse_outcomes(parsing),
        "extracted": extracted,
        "graphed": graphed,
        "indexed": indexed,
        "clusters": clusters,
        "trends": trends,
        "gaps": gaps,
        "reports": reports,
        # Additive (D-09-08): `reports` alone cannot tell 2 of 2 from 2 of 5.
        "report_generation": report_generation,
        "run_id": run.id,
        "checkpoints": run.counts,
    }


@router.delete("/admin/corpus")
def reset_corpus_endpoint(session: Session = Depends(corpus.get_session)):  # noqa: B008
    """Dev/admin operation: wipe every paper and everything derived from it.

    Leaves PostgreSQL/Neo4j/Qdrant themselves, PDF storage, GROBID, model
    caches, and configuration untouched — see app.core.corpus_reset for what
    is and is not removed. Destructive and irreversible; gated by the same
    X-API-Key every other route on this router requires.
    """
    settings = get_settings()
    try:
        with (
            pipeline_run(session, {"operation": "reset-corpus"}) as run,
            graph_session(settings) as neo4j,
            run.stage("reset"),
        ):
            result = reset_corpus(
                session, neo4j, EvidenceIndex(settings), settings.pdf_storage_path
            )
    except PipelineBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        # The exception is logged, never returned: it can carry an internal
        # detail (a driver's connection string, a stray host/port) that a
        # caller of an admin endpoint should not receive, matching how
        # /reports handles an unclassified failure.
        logger.exception("Corpus reset failed")
        raise HTTPException(status_code=502, detail="Corpus reset failed") from exc
    logger.warning("Corpus reset executed via API run_id=%s", run.id)
    result["run_id"] = run.id
    return result

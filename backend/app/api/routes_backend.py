"""Complete Phase 11 REST surface for Phases 1–10."""

import logging

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
from app.explanation.selection import evidence_summary
from app.explanation.storage import list_reports, report_snapshot
from app.extraction.service import extract_all_parsed
from app.extraction.storage import ExtractedFact
from app.ingestion import corpus
from app.ingestion.corpus import Paper
from app.interactive.workflow import default_services, run_query
from app.ner_re import service as graph_service
from app.ner_re.graph import graph_session, graph_snapshot
from app.parsing.service import parse_all_pending
from app.parsing.storage import ParsedDocument
from app.ranking import service as ranking_service
from app.ranking.storage import ClusterRanking, list_rankings
from app.trends import service as trend_service
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


@router.get("/papers")
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


@router.get("/graph")
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
    return {
        "id": cluster.id,
        "label": cluster.label,
        "member_count": cluster.member_count,
        "contradiction_flag": cluster.contradiction_flag,
        "contradiction_score": cluster.contradiction_score,
        "contradictions": _contradiction_payload(session, cluster.id, members),
        "members": [
            {
                "point_id": member.point_id,
                "paper_id": member.paper_id,
                "text": member.text,
                "year": member.year,
            }
            for member in members
        ],
    }


@router.get("/clusters")
def clusters(session: Session = Depends(corpus.get_session)):  # noqa: B008
    return [_cluster_payload(session, cluster) for cluster in list_clusters(session)]


@router.post("/clusters", dependencies=[Depends(exclusive_write)])
def rebuild_clusters(session: Session = Depends(corpus.get_session)):  # noqa: B008
    ids = clustering_service.rebuild_clusters(session)
    session.commit()
    return {"clusters": ids, "count": len(ids)}


@router.get("/trends")
def trends(session: Session = Depends(corpus.get_session)):  # noqa: B008
    return [
        {
            "cluster_id": row.cluster_id,
            "yearly_counts": row.yearly_counts,
            "slope": row.slope,
        }
        for row in list_trends(session)
    ]


@router.post("/trends", dependencies=[Depends(exclusive_write)])
def rebuild_trends(session: Session = Depends(corpus.get_session)):  # noqa: B008
    ids = trend_service.rebuild_trends(session)
    session.commit()
    return {"clusters": ids, "count": len(ids)}


@router.get("/gaps")
def gaps(
    limit: int = Query(100, ge=1, le=500),
    session: Session = Depends(corpus.get_session),  # noqa: B008
):
    return [
        {
            "cluster_id": row.cluster_id,
            "score": row.score,
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


@router.get("/reports")
def reports(session: Session = Depends(corpus.get_session)):  # noqa: B008
    return [
        {
            "cluster_id": row.cluster_id,
            "gap": row.gap,
            "evidence": row.evidence,
            "confidence": row.confidence,
            "suggested_directions": row.suggested_directions,
            "provider": row.provider,
            "model": row.model,
            # Which parts of the gap statement the evidence actually carried
            # (D-09-05); `{}` for a report stored before this existed.
            "justification": row.justification or {},
            # Passages and papers are not the same number, and a reader cannot
            # tell eight passages from one study apart from eight papers without
            # being told (D-09-04). Derived, so no stored row has to be migrated.
            "evidence_summary": evidence_summary(row.evidence or []),
        }
        for row in list_reports(session)
    ] or report_snapshot(session)


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
        ids = report_service.generate_top_reports(session)
    except LLMError as exc:
        logger.warning("Mode A report generation stopped: %s", exc)
        raise _llm_http_error(exc) from exc
    except Exception as exc:
        logger.exception("Mode A report generation failed")
        raise HTTPException(status_code=502, detail="Report generation failed") from exc
    session.commit()
    return {"clusters": ids, "count": len(ids)}


@router.post("/query")
def query(body: QueryRequest):
    settings = get_settings()
    try:
        return run_query(
            body.question,
            default_services(settings),
            settings.retrieval_round_cap,
            settings.retrieval_top_n,
            settings.retrieval_min_chunks,
            settings.retrieval_min_papers,
            settings.retrieval_min_score,
            settings.grounding_regeneration_cap,
        )
    except LLMUnavailableError as exc:
        logger.warning("Interactive GraphRAG LLM provider unavailable: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LLMError as exc:
        logger.warning("Interactive GraphRAG LLM configuration failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
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


def _derived_fingerprint(session, settings):
    return fingerprint(
        {
            "version": 1,
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
    if body.generate_reports:
        report_digest = fingerprint(
            [
                digest,
                settings_fingerprint(
                    settings,
                    (
                        "report_",
                        "groq_model",
                    ),
                ),
                "prompt-v1",
            ]
        )
        with run.stage("s9"):
            try:
                if run.derived_current("s9", report_digest):
                    reports = [r.cluster_id for r in list_reports(session)]
                else:
                    reports = report_service.generate_top_reports(session, settings)
                    # A run that generated no valid reports should retry later.
                    if reports or not gaps:
                        run.mark_derived("s9", report_digest)
            except LLMError as exc:
                session.rollback()
                raise _llm_http_error(exc, _S9_PREFIX) from exc
            session.commit()
    logger.info(
        "pipeline complete parsed=%d missing_pdfs=%d extracted=%d graphed=%d "
        "indexed=%d clusters=%d trends=%d gaps=%d reports=%d checkpoints=%s",
        _count(parsed),
        _count(parsing.missing_pdfs),
        _count(extracted),
        _count(graphed),
        _count(indexed),
        _count(clusters),
        _count(trends),
        _count(gaps),
        _count(reports),
        run.counts,
    )
    return {
        "parsed": parsed,
        "missing_pdfs": parsing.missing_pdfs,
        "extracted": extracted,
        "graphed": graphed,
        "indexed": indexed,
        "clusters": clusters,
        "trends": trends,
        "gaps": gaps,
        "reports": reports,
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
        with pipeline_run(session, {"operation": "reset-corpus"}) as run, graph_session(
            settings
        ) as neo4j, run.stage("reset"):
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

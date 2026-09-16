"""Complete Phase 11 REST surface for Phases 1–10."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.security import require_api_key
from app.api.write_guard import exclusive_write
from app.core.pipeline import pipeline_run, PipelineBusy, settings_fingerprint, fingerprint, PaperStageState
from app.core.maintenance import resume_deletions
from app.core.pipeline import require_complete_projections
from app.extraction.storage import ExtractedFact
from app.clustering import service as clustering_service
from app.clustering.storage import list_clusters, members_for_cluster
from app.core.config import get_settings
from app.embeddings.indexer import EvidenceIndex, index_all
from app.explanation import service as report_service
from app.explanation.llm import LLMError, LLMUnavailableError
from app.explanation.storage import list_reports, report_snapshot
from app.extraction.service import extract_all_parsed
from app.ingestion import corpus
from app.ingestion.corpus import Paper
from app.interactive.workflow import default_services, run_query
from app.ner_re import service as graph_service
from app.ner_re.graph import graph_session, graph_snapshot
from app.parsing.service import parse_all_pending
from app.ranking import service as ranking_service
from app.ranking.storage import list_rankings, ClusterRanking
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


@router.post('/parse')
def parse_papers(session: Session = Depends(corpus.get_session)):  # noqa: B008
    settings = get_settings()
    try:
        with pipeline_run(session, {'operation': 'parse'}) as run, run.stage('s2'):
            result = parse_all_pending(session, settings.grobid_url, settings.unpaywall_email, settings.pdf_storage_path)
            session.commit()
            return {'processed': result.processed, 'missing_pdfs': result.missing_pdfs, 'run_id': run.id}
    except PipelineBusy as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post('/extract')
def extract_papers(session: Session = Depends(corpus.get_session)):  # noqa: B008
    try:
        with pipeline_run(session, {'operation': 'extract'}) as run, run.stage('s3'):
            ids = extract_all_parsed(session)
            return {'processed': ids, 'count': len(ids), 'run_id': run.id}
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


@router.get("/clusters")
def clusters(session: Session = Depends(corpus.get_session)):  # noqa: B008
    return [
        {
            "id": cluster.id,
            "label": cluster.label,
            "member_count": cluster.member_count,
            "contradiction_flag": cluster.contradiction_flag,
            "contradiction_score": cluster.contradiction_score,
            "members": [
                {
                    "point_id": member.point_id,
                    "paper_id": member.paper_id,
                    "text": member.text,
                    "year": member.year,
                }
                for member in members_for_cluster(session, cluster.id)
            ],
        }
        for cluster in list_clusters(session)
    ]


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
        dict(cluster_id=row.cluster_id, gap=row.gap, evidence=row.evidence,
             confidence=row.confidence, suggested_directions=row.suggested_directions,
             provider=row.provider, model=row.model)
        for row in list_reports(session)
    ] or report_snapshot(session)


def _llm_http_error(exc: LLMError, prefix: str = "") -> HTTPException:
    """An LLM failure as the explicit error README §12 promises.

    `LLMError` messages are this project's own fixed strings (a missing or
    rejected key, an outage, a rate limit), so unlike an arbitrary exception
    they are safe to return; `/query` already returns them the same way.
    """
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
        with pipeline_run(session, settings_fingerprint(settings, ('nerre_', 'embedding_', 'clustering_', 'nli_', 'gap_', 'report_', 'groq_model'))) as run:
            return _execute_pipeline(body, session, settings, run)
    except PipelineBusy as exc:
        raise HTTPException(409, str(exc)) from exc


def _derived_fingerprint(session, settings):
    return fingerprint({
        'version': 1,
        'stages': [(s.paper_id, s.stage, s.input_hash, s.status) for s in session.scalars(select(PaperStageState).where(PaperStageState.stage.in_(['s3', 's4', 's5'])).order_by(PaperStageState.paper_id, PaperStageState.stage))],
        'papers': [(p.id, p.title, p.year, p.venue, p.citation_count) for p in session.scalars(select(Paper).order_by(Paper.id))],
        'contributions': [(f.paper_id, f.text) for f in session.scalars(select(ExtractedFact).where(ExtractedFact.fact_type == 'contribution').order_by(ExtractedFact.paper_id, ExtractedFact.id))],
        'settings': settings_fingerprint(settings, ('clustering_', 'nli_', 'contradiction_', 'trend_', 'gap_', 'embedding_')),
    })


def _execute_pipeline(body, session, settings, run):
    # A killed explicit deletion is completed before any stage can reindex it.
    if session.scalar(select(PaperStageState).where(PaperStageState.stage == 'delete').limit(1)):
        with graph_session(settings) as neo4j, run.stage('delete'):
            resume_deletions(session, neo4j, EvidenceIndex(settings))
    with run.stage('s2'):
        parsing = parse_all_pending(session, settings.grobid_url, settings.unpaywall_email, settings.pdf_storage_path)
        session.commit()
    parsed = parsing.processed
    with run.stage('s3'):
        extracted = extract_all_parsed(session)
        session.commit()
    with graph_session(settings) as neo4j:
        with run.stage('s4'):
            graphed = graph_service.extract_all(session, neo4j)
        with run.stage('s5'):
            indexed = index_all(session, neo4j, EvidenceIndex(settings))
    digest = _derived_fingerprint(session, settings)
    with run.stage('s6-s8'):
        if run.derived_current('s6-s8', digest):
            clusters = [r.id for r in list_clusters(session)]
            trends = [r.cluster_id for r in list_trends(session)]
            gaps = list(session.scalars(select(ClusterRanking.cluster_id).order_by(ClusterRanking.score.desc())))
        else:
            clusters = clustering_service.rebuild_clusters(session, settings=settings)
            trends = trend_service.rebuild_trends(session)
            gaps = ranking_service.rebuild_rankings(session, settings)
            run.mark_derived('s6-s8', digest)
        session.commit()
    reports: list[int] = []
    if body.generate_reports:
        report_digest = fingerprint([digest, settings_fingerprint(settings, ('report_', 'groq_model',)), 'prompt-v1'])
        with run.stage('s9'):
            try:
                if run.derived_current('s9', report_digest):
                    reports = [r.cluster_id for r in list_reports(session)]
                else:
                    reports = report_service.generate_top_reports(session, settings)
                    # A run that generated no valid reports should retry later.
                    if reports or not gaps:
                        run.mark_derived('s9', report_digest)
            except LLMError as exc:
                session.rollback()
                raise _llm_http_error(exc, 'The evidence base was built, but reports were not generated: ') from exc
            session.commit()
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

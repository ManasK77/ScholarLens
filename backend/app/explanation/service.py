"""Mode A: single-pass, evidence-grounded report generation."""

from sqlalchemy.orm import Session

from app.clustering.storage import members_for_cluster
from app.core.config import Settings, get_settings
from app.embeddings.indexer import EvidenceIndex
from app.explanation.llm import StructuredLLM, get_llm_client
from app.explanation.schema import GAP_REPORT_SCHEMA, GapReportDraft
from app.explanation.storage import GapReport
from app.ranking.storage import list_rankings


class UnsupportedReport(RuntimeError):
    pass


def _report_prompt(cluster_id: int, evidence: list[dict]) -> str:
    rendered = "\n".join(
        f"[{item['id']}] paper_id={item.get('paper_id')} | {item.get('text', '')}"
        for item in evidence
    )
    return (
        "You are explaining a research gap already discovered by ScholarLens. "
        "Do not invent evidence or discover a different gap. Use only the supplied "
        "evidence. Return evidence_ids exactly as shown.\n\n"
        f"Cluster: {cluster_id}\nEvidence:\n{rendered}"
    )


def generate_report(
    session: Session,
    cluster_id: int,
    index: EvidenceIndex,
    llm: StructuredLLM,
    retrieval_limit: int,
) -> GapReport:
    members = members_for_cluster(session, cluster_id)
    if not members:
        raise UnsupportedReport(f"Cluster {cluster_id} has no evidence members")
    query = " ".join(member.text for member in members[:5])
    evidence = index.retrieve(query, retrieval_limit)
    if not evidence:
        raise UnsupportedReport(f"Cluster {cluster_id} retrieved no evidence")
    raw = llm.generate_json(_report_prompt(cluster_id, evidence), GAP_REPORT_SCHEMA)
    draft = GapReportDraft.model_validate(raw)
    by_id = {str(item["id"]): item for item in evidence}
    selected = [by_id[item_id] for item_id in draft.evidence_ids if item_id in by_id]
    if not selected:
        raise UnsupportedReport("Generated report cites no retrieved evidence IDs")
    report = session.get(GapReport, cluster_id)
    if report is None:
        report = GapReport(cluster_id=cluster_id)
        session.add(report)
    report.gap = draft.gap
    report.evidence = selected
    report.confidence = draft.confidence
    report.suggested_directions = draft.suggested_directions
    report.provider = llm.provider
    report.model = llm.model
    session.flush()
    return report


def generate_top_reports(
    session: Session,
    settings: Settings | None = None,
    index: EvidenceIndex | None = None,
    llm: StructuredLLM | None = None,
) -> list[int]:
    settings = settings or get_settings()
    index = index or EvidenceIndex(settings)
    llm = llm or get_llm_client(settings)
    generated = []
    for ranking in list_rankings(session, settings.report_top_k):
        generate_report(
            session,
            ranking.cluster_id,
            index,
            llm,
            settings.retrieval_top_n,
        )
        generated.append(ranking.cluster_id)
    return generated

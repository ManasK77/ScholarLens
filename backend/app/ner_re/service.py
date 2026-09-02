"""Phase 4 orchestration from extracted facts into Neo4j."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.extraction.storage import ExtractedFact
from app.ingestion.corpus import Paper
from app.ner_re.extractor import JointScientificExtractor, get_joint_extractor
from app.ner_re.graph import ensure_graph_schema, write_paper_graph
from app.ner_re.schema import JointExtractionResult


def extract_paper(
    session: Session,
    graph_session,
    paper: Paper,
    extractor: JointScientificExtractor | None = None,
) -> JointExtractionResult:
    facts = session.scalars(
        select(ExtractedFact)
        .where(ExtractedFact.paper_id == paper.id)
        .order_by(ExtractedFact.id)
    )
    text = "\n".join(dict.fromkeys(fact.text for fact in facts))
    result = (
        (extractor or get_joint_extractor()).extract(text)
        if text
        else JointExtractionResult()
    )
    write_paper_graph(graph_session, paper, result)
    return result


def extract_all(session: Session, graph_session, extractor=None) -> list[int]:
    ensure_graph_schema(graph_session)
    processed = []
    for paper in session.scalars(select(Paper).order_by(Paper.id)):
        extract_paper(session, graph_session, paper, extractor)
        processed.append(paper.id)
    return processed

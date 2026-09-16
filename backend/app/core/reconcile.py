"""Read-only store audit: python -m app.core.reconcile (run with writes idle)."""

import json
from pathlib import Path

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.pipeline import PaperStageState
from app.embeddings.indexer import EvidenceIndex, build_index_records
from app.extraction.storage import ExtractedFact
from app.ingestion.corpus import Paper, get_engine
from app.ner_re.graph import graph_session
from app.parsing.storage import get_parsed_document


def compare_projections(expected, actual, paper_ids, graph_nodes):
    missing = sorted(expected.keys() - actual.keys())
    extra = sorted(actual.keys() - expected.keys())
    stale = sorted(key for key in expected.keys() & actual.keys() if expected[key] != actual[key])
    absent_graph = sorted(paper_ids - graph_nodes.keys())
    orphan_graph = sorted(graph_nodes.keys() - paper_ids)
    return dict(consistent=not any((missing, extra, stale, absent_graph, orphan_graph)),
                missing_points=missing, extra_points=extra, stale_points=stale,
                missing_graph_papers=absent_graph, orphan_graph_papers=orphan_graph)


def inspect_stores(session, neo4j, index, pdf_directory=None):
    graph_nodes = {row['id']: row['node_id'] for row in neo4j.run(
        'MATCH (p:Paper) RETURN p.id AS id, elementId(p) AS node_id'
    )}
    actual, offset = {}, None
    if index.client.collection_exists(index.collection):
        while True:
            page, offset = index.client.scroll(collection_name=index.collection, with_payload=True, with_vectors=False, limit=256, offset=offset)
            actual.update((str(point.id), dict(point.payload or {})) for point in page)
            if offset is None:
                break
    papers = list(session.scalars(select(Paper).order_by(Paper.id)))
    expected = {}
    missing_files, referenced_files = [], set()
    for paper in papers:
        if paper.pdf_path:
            referenced_files.add(Path(paper.pdf_path).resolve())
            if not Path(paper.pdf_path).is_file():
                missing_files.append(paper.id)
        doc = get_parsed_document(session, paper.id)
        if doc is None:
            continue
        facts = list(session.scalars(select(ExtractedFact).where(ExtractedFact.paper_id == paper.id)))
        for record in build_index_records(paper, doc, facts, graph_nodes.get(paper.id, 'missing-graph-node')):
            expected[record.record_id] = index.record_payload(record)
    report = compare_projections(expected, actual, {p.id for p in papers}, graph_nodes)
    report['counts'] = dict(papers=len(papers), expected_points=len(expected), actual_points=len(actual), graph_papers=len(graph_nodes))
    report['missing_pdf_papers'] = missing_files
    report['unreferenced_pdf_count'] = sum(path.resolve() not in referenced_files for path in Path(pdf_directory).glob('*.pdf')) if pdf_directory else 0
    report['incomplete_checkpoints'] = []
    if inspect(session.get_bind()).has_table(PaperStageState.__tablename__):
        report['incomplete_checkpoints'] = [{'paper_id': s.paper_id, 'stage': s.stage, 'status': s.status} for s in session.scalars(select(PaperStageState).where(PaperStageState.status != 'done'))]
    report['consistent'] = report['consistent'] and not report['incomplete_checkpoints']
    return report


def main():
    settings = get_settings()
    with Session(get_engine()) as session, graph_session(settings) as neo4j:
        report = inspect_stores(session, neo4j, EvidenceIndex(settings), settings.pdf_storage_path)
    print(json.dumps(report, indent=2))
    return 0 if report['consistent'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

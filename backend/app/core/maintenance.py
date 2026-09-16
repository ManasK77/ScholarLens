"""Resumable, explicitly requested paper removal; PDF originals are retained."""

import argparse
import json

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.pipeline import DerivedState, PaperStageState, current_run, pipeline_run
from app.extraction.storage import ExtractedFact
from app.ingestion.corpus import Paper, get_engine
from app.parsing.storage import ParsedDocument


def delete_paper(session, neo4j, index, paper_id):
    run = current_run()
    if run is None:
        raise RuntimeError('Paper removal requires the pipeline writer lock')
    if session.get(Paper, paper_id) is None:
        return False
    session.merge(PaperStageState(paper_id=paper_id, stage='delete', input_hash='delete-v1', status='tombstone', run_id=run.id))
    session.commit()
    index.prune_paper(paper_id, [])
    def remove_graph(tx):
        tx.run('MATCH ()-[r]->() WHERE r.paper_id=$id DELETE r', id=paper_id).consume()
        tx.run('MATCH (p:Paper {id: $id}) DETACH DELETE p', id=paper_id).consume()
        # Shared Entity nodes are intentionally retained; orphan cleanup is
        # optional maintenance and must never delete another paper's evidence.
    neo4j.execute_write(remove_graph)
    from app.clustering.storage import replace_clusters
    # Invalidate derived findings atomically with the corpus deletion, archiving
    # reports before removing the cluster IDs they used. No stale ranking leaks.
    replace_clusters(session, [])
    for model in (ExtractedFact, ParsedDocument, PaperStageState):
        session.execute(delete(model).where(model.paper_id == paper_id))
    session.execute(delete(Paper).where(Paper.id == paper_id))
    session.execute(delete(DerivedState))
    session.commit()
    return True


def resume_deletions(session, neo4j, index):
    ids = list(session.scalars(select(PaperStageState.paper_id).where(PaperStageState.stage == 'delete', PaperStageState.status == 'tombstone').order_by(PaperStageState.paper_id)))
    for paper_id in ids:
        delete_paper(session, neo4j, index, paper_id)
    return ids


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paper_id', type=int)
    parser.add_argument('--confirm', action='store_true', help='Remove this paper and its projections, keeping its PDF')
    args = parser.parse_args()
    if not args.confirm:
        parser.error('--confirm is required; run reconciliation and take backups first')
    from app.core.config import get_settings
    from app.embeddings.indexer import EvidenceIndex
    from app.ner_re.graph import graph_session
    from app.clustering.service import rebuild_clusters
    from app.trends.service import rebuild_trends
    from app.ranking.service import rebuild_rankings
    settings = get_settings()
    with Session(get_engine()) as session, pipeline_run(session, {'operation': 'delete-paper'}) as run, graph_session(settings) as graph:
        index = EvidenceIndex(settings)
        with run.stage('delete'):
            removed = delete_paper(session, graph, index, args.paper_id)
        with run.stage('s6-s8'):
            rebuild_clusters(session, settings, index)
            rebuild_trends(session)
            rebuild_rankings(session, settings)
            session.commit()
    print(json.dumps({'paper_id': args.paper_id, 'removed': removed, 'pdf_retained': True}))


if __name__ == '__main__':
    main()

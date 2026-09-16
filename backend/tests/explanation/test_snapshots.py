from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.ingestion.corpus import Base
from app.clustering.storage import replace_clusters
from app.explanation.storage import ReportSnapshot, replace_reports, report_snapshot
from app.explanation.schema import GapReportRecord


def report():
    return GapReportRecord(cluster_id=1, gap='A grounded gap', evidence=[{'id': 'evidence'}], confidence=.8, suggested_directions=['Investigate'], provider='groq', model='test')


def test_rebuild_archives_reports_and_new_reports_replace_fallback():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        replace_reports(session, [report()])
        session.commit()
        replace_clusters(session, [])
        session.commit()
        rows = report_snapshot(session)
        assert rows[0]['stale'] is True
        assert rows[0]['gap'] == 'A grounded gap'
        assert rows[0]['snapshot_id']
        assert list(session.scalars(select(ReportSnapshot)))
        replace_reports(session, [report()])
        session.commit()
        assert not report_snapshot(session)[0].get('stale', False)


def test_archiving_rolls_back_with_cluster_rebuild():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        replace_reports(session, [report()])
        session.commit()
        replace_clusters(session, [])
        session.rollback()
        assert not report_snapshot(session)[0].get('stale', False)
        assert not list(session.scalars(select(ReportSnapshot)))

import logging

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.pipeline import (
    PaperStageState, PipelineRun, PipelineBusy, fingerprint, pipeline_run,
    writer_lock, current_run,
)
from app.ingestion.corpus import Base, Paper


@pytest.fixture
def session():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Paper(id=1, source='manual_upload'))
        session.commit()
        yield session


def test_fingerprint_is_order_stable_and_sensitive():
    assert fingerprint({'a': 1, 'b': 2}) == fingerprint({'b': 2, 'a': 1})
    assert fingerprint([1, 2]) != fingerprint([2, 1])


def test_checkpoint_skips_unchanged_and_retries_failed_input(session):
    calls = []
    with pipeline_run(session, {}) as run:
        assert current_run() is run
        assert run.paper(1, 's3', 'first', lambda: calls.append(1))
        assert not run.paper(1, 's3', 'first', lambda: calls.append(2))
    with pytest.raises(ValueError):
        with pipeline_run(session, {}) as run:
            run.paper(1, 's3', 'second', lambda: (_ for _ in ()).throw(ValueError('secret')))
    assert session.get(PaperStageState, (1, 's3')).status == 'failed'
    with pipeline_run(session, {}) as run:
        run.paper(1, 's3', 'second', lambda: calls.append(3))
    assert calls == [1, 3]
    assert current_run() is None
    assert [r.status for r in session.scalars(select(PipelineRun).order_by(PipelineRun.started_at))] == ['succeeded', 'failed', 'succeeded']
    assert 'secret' not in str([r.error for r in session.scalars(select(PipelineRun))])


def test_failed_checkpoint_rolls_back_data_but_preserves_previous_paper(session):
    with pytest.raises(RuntimeError):
        with pipeline_run(session, {}) as run:
            run.paper(1, 's2', 'good', lambda: setattr(session.get(Paper, 1), 'title', 'committed'))
            def fail():
                session.get(Paper, 1).title = 'uncommitted'
                session.flush()
                raise RuntimeError()
            run.paper(1, 's3', 'bad', fail)
    assert session.get(Paper, 1).title == 'committed'


def test_stale_running_run_becomes_aborted_and_lock_releases(session):
    session.add(PipelineRun(id='killed', status='running', config_fingerprint='x'))
    session.commit()
    with pipeline_run(session, {}):
        with pytest.raises(PipelineBusy):
            with writer_lock(session):
                pass
    assert session.get(PipelineRun, 'killed').status == 'aborted'
    with writer_lock(session):
        pass


def test_external_verifier_prevents_false_skip(session):
    calls = []
    with pipeline_run(session, {}) as run:
        run.paper(1, 's5', 'hash', lambda: calls.append(1))
        run.paper(1, 's5', 'hash', lambda: calls.append(2), verify=lambda: False)
    assert calls == [1, 2]


def test_stage_logging_includes_run_id_and_no_exception_content(session, caplog):
    from app.core.logging import RunFilter, stage_timer
    caplog.handler.addFilter(RunFilter())
    with caplog.at_level(logging.INFO):
        with pytest.raises(ValueError):
            with pipeline_run(session, {}) as run:
                with stage_timer(logging.getLogger('app.test'), 's2'):
                    raise ValueError('private paragraph')
    assert 'private paragraph' not in caplog.text
    assert any(r.run_id == run.id and 'failed' in r.message for r in caplog.records)

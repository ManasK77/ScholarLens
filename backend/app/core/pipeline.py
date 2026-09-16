"""Durable checkpoints for the existing synchronous pipeline.

PostgreSQL is the desired-state ledger, not a distributed transaction manager.
External writes precede the done marker and must be safely repeatable.
"""

import hashlib
import json
import logging
import sqlite3
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, String, select, text, update
from sqlalchemy.orm import Mapped, mapped_column

from app.core.logging import run_id, stage_timer
from app.ingestion.corpus import Base

logger = logging.getLogger(__name__)
_active: ContextVar = ContextVar('pipeline', default=None)
LOCK_KEY = 7363686


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def settings_fingerprint(settings, prefixes=()):
    values = settings.model_dump() if hasattr(settings, 'model_dump') else vars(settings)
    # Credentials and infrastructure addresses are never persisted or hashed.
    return {k: v for k, v in values.items() if any(k.startswith(p) for p in prefixes) and not any(s in k for s in ('key', 'password', 'url'))}


class PipelineRun(Base):
    __tablename__ = 'pipeline_runs'
    id: Mapped[str] = mapped_column(String, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String)
    current_stage: Mapped[str | None] = mapped_column(String)
    config_fingerprint: Mapped[str] = mapped_column(String)
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(String)


class PaperStageState(Base):
    __tablename__ = 'paper_stage_state'
    paper_id: Mapped[int] = mapped_column(ForeignKey('papers.id'), primary_key=True)
    stage: Mapped[str] = mapped_column(String, primary_key=True)
    input_hash: Mapped[str] = mapped_column(String)
    output_hash: Mapped[str | None] = mapped_column(String)
    stage_version: Mapped[str] = mapped_column(String, default='1')
    status: Mapped[str] = mapped_column(String)
    run_id: Mapped[str | None] = mapped_column(String)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)
    error: Mapped[str | None] = mapped_column(String)


class DerivedState(Base):
    __tablename__ = 'derived_state'
    name: Mapped[str] = mapped_column(String, primary_key=True)
    input_fingerprint: Mapped[str] = mapped_column(String)
    run_id: Mapped[str | None] = mapped_column(String)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class PipelineBusy(RuntimeError):
    pass


@contextmanager
def writer_lock(session):
    """Session-level PG lock survives commits on a dedicated connection.

SQLite local development uses a separate SQLite lock file; its exclusive
transaction is also released by process death, without locking the corpus.
"""
    bind = session.get_bind()
    if bind.dialect.name == 'postgresql':
        engine = getattr(bind, 'engine', bind)
        with engine.connect() as connection:
            acquired = connection.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': LOCK_KEY})
            connection.commit()
            if not acquired:
                raise PipelineBusy('Another corpus write is already running')
            try:
                yield
            finally:
                try:
                    connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': LOCK_KEY})
                    connection.commit()
                except BaseException:
                    connection.invalidate()
                    raise
    else:
        engine = getattr(bind, 'engine', bind)
        database = engine.url.database
        identity = str(Path(database).resolve()) if database and database != ':memory:' else str(id(engine))
        path = Path(tempfile.gettempdir()) / ('scholarlens-' + fingerprint(identity) + '.lock.sqlite')
        connection = sqlite3.connect(path, timeout=0)
        try:
            try:
                connection.execute('BEGIN EXCLUSIVE')
            except sqlite3.OperationalError as exc:
                raise PipelineBusy('Another corpus write is already running') from exc
            yield
        finally:
            connection.close()


def current_run():
    return _active.get()


class RunContext:
    def __init__(self, session, identifier):
        self.session, self.id = session, identifier
        self.counts = {}

    @contextmanager
    def stage(self, name):
        row = self.session.get(PipelineRun, self.id)
        row.current_stage = name
        self.session.commit()
        with stage_timer(logger, name):
            yield

    def paper(self, paper_id, stage, input_hash, operation, verify=None, output=None):
        session = self.session
        key = (paper_id, stage)
        state = session.get(PaperStageState, key)
        if state and state.status == 'done' and state.input_hash == input_hash and (verify is None or verify()) and (output is None or state.output_hash == fingerprint(output())):
            self.counts[stage + '_skipped'] = self.counts.get(stage + '_skipped', 0) + 1
            return False
        if state is None:
            state = PaperStageState(paper_id=paper_id, stage=stage, input_hash=input_hash, status='pending')
            session.add(state)
        downstream = {'s2': ('s3', 's4', 's5'), 's3': ('s4', 's5'), 's4': ('s5',)}.get(stage, ())
        for next_stage in downstream:
            child = session.get(PaperStageState, (paper_id, next_stage))
            if child is None:
                session.add(PaperStageState(paper_id=paper_id, stage=next_stage, input_hash='', status='pending', run_id=self.id))
            else:
                child.status = 'pending'
        state.input_hash, state.status, state.run_id, state.error = input_hash, 'pending', self.id, None
        session.commit()
        try:
            operation()
            session.flush()
            if output is not None:
                state.output_hash = fingerprint(output())
            state.status = 'done'
            session.commit()
        except BaseException as exc:
            session.rollback()
            state = session.get(PaperStageState, key)
            state.status, state.error = 'failed', type(exc).__name__
            session.commit()
            raise
        self.counts[stage + '_processed'] = self.counts.get(stage + '_processed', 0) + 1
        return True

    def derived_current(self, name, input_hash):
        state = self.session.get(DerivedState, name)
        return state is not None and state.input_fingerprint == input_hash

    def mark_derived(self, name, input_hash):
        self.session.merge(DerivedState(name=name, input_fingerprint=input_hash, run_id=self.id))


def require_complete_projections(session):
    """Never build new findings from a known interrupted evidence build."""
    incomplete = session.scalar(select(PaperStageState).where(PaperStageState.status != 'done').limit(1))
    if incomplete:
        raise RuntimeError('Incomplete paper checkpoints; resume /pipeline/run first')
    unfinished = session.scalar(select(PipelineRun).where(PipelineRun.status.in_(['running', 'failed', 'aborted']), PipelineRun.current_stage.in_(['s2', 's3', 's4', 's5', 'delete'])).order_by(PipelineRun.started_at.desc()).limit(1))
    latest = session.scalar(select(PipelineRun).order_by(PipelineRun.started_at.desc()).limit(1))
    if unfinished and latest and unfinished.id == latest.id and current_run() is None:
        raise RuntimeError('Interrupted evidence build; resume /pipeline/run first')

@contextmanager
def pipeline_run(session, config):
    with writer_lock(session):
        session.execute(update(PipelineRun).where(PipelineRun.status == 'running').values(status='aborted', finished_at=now()))
        identifier = str(uuid4())
        row = PipelineRun(id=identifier, status='running', config_fingerprint=fingerprint(config))
        session.add(row)
        session.commit()
        run = RunContext(session, identifier)
        token, log_token = _active.set(run), run_id.set(identifier)
        try:
            yield run
            row.status = 'succeeded'
        except BaseException as exc:
            session.rollback()
            row = session.get(PipelineRun, identifier)
            row.status, row.error = 'failed', type(exc).__name__
            raise
        finally:
            row.finished_at, row.counts = now(), run.counts
            try:
                session.commit()
            finally:
                _active.reset(token)
                run_id.reset(log_token)

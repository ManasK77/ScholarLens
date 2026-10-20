"""Serialize corpus/derived writes across all synchronous API entry points."""

from fastapi import Depends, HTTPException
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.pipeline import DerivedState, PipelineBusy, writer_lock
from app.ingestion.corpus import get_session


def exclusive_lock(session: Session = Depends(get_session)):  # noqa: B008
    """Hold the corpus writer lock for the request, without invalidating.

    For routes with a request body: FastAPI runs dependencies *before* it
    validates the body, so invalidating here would let a request answered 422
    clear cached derived results. Those handlers call `invalidate_derived`
    themselves, once validation has passed.
    """
    try:
        with writer_lock(session):
            yield
    except PipelineBusy as exc:
        raise HTTPException(409, str(exc)) from exc


def invalidate_derived(session: Session) -> None:
    """Standalone writes invalidate cached downstream results; committing this
    small invalidation first also covers a failed external write."""
    session.execute(delete(DerivedState))
    session.commit()


def exclusive_write(session: Session = Depends(get_session)):  # noqa: B008
    try:
        with writer_lock(session):
            invalidate_derived(session)
            yield
    except PipelineBusy as exc:
        raise HTTPException(409, str(exc)) from exc

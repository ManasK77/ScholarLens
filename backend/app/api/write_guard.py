"""Serialize corpus/derived writes across all synchronous API entry points."""

from fastapi import Depends, HTTPException
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.pipeline import DerivedState, PipelineBusy, writer_lock
from app.ingestion.corpus import get_session


def exclusive_write(session: Session = Depends(get_session)):  # noqa: B008
    try:
        with writer_lock(session):
            # Standalone writes invalidate cached downstream results; committing
            # this small invalidation first also covers a failed external write.
            session.execute(delete(DerivedState))
            session.commit()
            yield
    except PipelineBusy as exc:
        raise HTTPException(409, str(exc)) from exc

"""ScholarLens FastAPI application entrypoint."""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import routes_backend, routes_health, routes_ingestion
from app.api.body_limit import RequestBodyLimitMiddleware
from app.clustering import (
    storage as clustering_storage,  # noqa: F401 -- registers Cluster/ClusterMember
)
from app.core import pipeline as pipeline_storage  # noqa: F401
from app.core.config import get_settings
from app.core.logging import configure_logging, install_access_log_redaction
from app.core.schema import ensure_schema_compatibility
from app.explanation import (
    storage as explanation_storage,  # noqa: F401 -- registers GapReport
)
from app.extraction import (
    storage as extraction_storage,  # noqa: F401 -- registers ExtractedFact
)
from app.ingestion.corpus import Base, get_engine
from app.parsing import (
    storage,  # noqa: F401 -- registers ParsedDocument on Base.metadata
)
from app.ranking import (
    storage as ranking_storage,  # noqa: F401 -- registers ClusterRanking
)
from app.trends import (
    storage as trends_storage,  # noqa: F401 -- registers ClusterTrend
)

logger = logging.getLogger(__name__)
# At import, not only at startup: uvicorn writes access lines from its first
# request, and this module is imported after uvicorn configured its loggers.
install_access_log_redaction()

app = FastAPI(
    title="ScholarLens",
    description=(
        "Evidence-backed research gap discovery. Every operation except "
        "`GET /health` requires the shared secret in an `X-API-Key` header."
    ),
)
app.add_middleware(RequestBodyLimitMiddleware)


@app.exception_handler(Exception)
async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    """Any exception no route mapped: one generic JSON 500, details logged.

    The exception's text can carry a connection string or host, so neither
    the caller nor the log gets it: the log records the method, the path
    (never the query string) and the exception type. JSON (not Starlette's
    plain-text page) keeps every error in the `{"detail": ...}` shape the
    dashboard reads.
    """
    logger.error(
        "Unhandled error on %s %s error=%s",
        request.method,
        request.url.path,
        type(exc).__name__,
    )
    return JSONResponse({"detail": "Internal server error"}, status_code=500)


app.include_router(routes_health.router)
app.include_router(routes_ingestion.router)
app.include_router(routes_ingestion.audit_router)
app.include_router(routes_backend.router)


@app.on_event("startup")
def _create_tables() -> None:
    configure_logging(get_settings().log_level)
    engine = get_engine()
    Base.metadata.create_all(engine)
    ensure_schema_compatibility(engine)

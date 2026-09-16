"""ScholarLens FastAPI application entrypoint."""

from fastapi import FastAPI

from app.api import routes_backend, routes_health, routes_ingestion
from app.clustering import (
    storage as clustering_storage,  # noqa: F401 -- registers Cluster/ClusterMember
)
from app.core.schema import ensure_schema_compatibility
from app.core.logging import configure_logging
from app.core.config import get_settings
from app.core import pipeline as pipeline_storage  # noqa: F401
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

app = FastAPI(title="ScholarLens")

app.include_router(routes_health.router)
app.include_router(routes_ingestion.router)
app.include_router(routes_backend.router)


@app.on_event("startup")
def _create_tables() -> None:
    configure_logging(get_settings().log_level)
    engine = get_engine()
    Base.metadata.create_all(engine)
    ensure_schema_compatibility(engine)

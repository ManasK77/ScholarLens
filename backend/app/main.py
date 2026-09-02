"""ScholarLens FastAPI application entrypoint."""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from sqlalchemy import inspect, text

from app.api import routes_backend, routes_health, routes_ingestion
from app.api.security import require_api_key
from app.clustering import storage as clustering_storage  # noqa: F401
from app.explanation import storage as explanation_storage  # noqa: F401
from app.extraction import (
    storage as extraction_storage,  # noqa: F401 -- registers ExtractedFact
)
from app.ingestion.corpus import Base, get_engine
from app.parsing import (
    storage,  # noqa: F401 -- registers ParsedDocument on Base.metadata
)
from app.ranking import storage as ranking_storage  # noqa: F401
from app.trends import storage as trends_storage  # noqa: F401


@asynccontextmanager
async def lifespan(_app: FastAPI):
    engine = get_engine()
    Base.metadata.create_all(engine)
    # The project intentionally does not use Alembic. Keep the one additive
    # column introduced by Phase 8 compatible with databases created earlier.
    if engine.dialect.name == "postgresql":
        columns = {column["name"] for column in inspect(engine).get_columns("papers")}
        if "citation_count" not in columns:
            with engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE papers ADD COLUMN citation_count INTEGER")
                )
    yield


app = FastAPI(
    title="ScholarLens",
    description="Evidence-backed scientific research-gap discovery API",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(routes_health.router)
app.include_router(routes_ingestion.router, dependencies=[Depends(require_api_key)])
app.include_router(routes_backend.router)

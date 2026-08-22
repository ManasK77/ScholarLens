"""ScholarLens FastAPI application entrypoint."""

from fastapi import FastAPI

from app.api import routes_health, routes_ingestion
from app.ingestion.corpus import Base, get_engine

app = FastAPI(title="ScholarLens")

app.include_router(routes_health.router)
app.include_router(routes_ingestion.router)


@app.on_event("startup")
def _create_tables() -> None:
    Base.metadata.create_all(get_engine())

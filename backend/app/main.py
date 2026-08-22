"""ScholarLens FastAPI application entrypoint."""

from fastapi import FastAPI

from app.api import routes_health

app = FastAPI(title="ScholarLens")

app.include_router(routes_health.router)

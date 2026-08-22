"""GET /health — composes the three service checks into the D-06 contract."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.health import check_neo4j, check_postgres, check_qdrant

router = APIRouter()


@router.get("/health")
def health() -> JSONResponse:
    settings = get_settings()

    services = {
        "neo4j": check_neo4j(settings),
        "qdrant": check_qdrant(settings),
        "postgres": check_postgres(settings),
    }
    all_up = all(services.values())

    return JSONResponse(
        content={"status": "ok" if all_up else "degraded", "services": services},
        status_code=200 if all_up else 503,
    )

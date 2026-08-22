"""Per-service round-trip health checks (D-06).

Each function opens a real connection to its service and returns a plain
bool — connection/driver exceptions are caught here so a single unreachable
service degrades the `/health` response instead of raising through the API.
"""

from neo4j import GraphDatabase
from qdrant_client import QdrantClient
from sqlalchemy import create_engine, text

from app.core.config import Settings

# Bounds each round-trip so an unreachable-but-not-refusing service (e.g.
# still starting up, or silently dropped packets) fails fast instead of
# hanging past a caller's readiness-probe timeout.
_CHECK_TIMEOUT_SECONDS = 5


def check_neo4j(settings: Settings) -> bool:
    """Round-trip a `RETURN 1` Cypher query against Neo4j."""
    try:
        driver = GraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
            connection_timeout=_CHECK_TIMEOUT_SECONDS,
        )
        with driver as d, d.session() as session:
            session.run("RETURN 1")
        return True
    except Exception:  # noqa: BLE001 — any driver failure means "unreachable"
        return False


def check_qdrant(settings: Settings) -> bool:
    """Round-trip a `GET /collections` call against Qdrant."""
    try:
        QdrantClient(
            url=settings.qdrant_url, timeout=_CHECK_TIMEOUT_SECONDS
        ).get_collections()
        return True
    except Exception:  # noqa: BLE001 — any client failure means "unreachable"
        return False


def check_postgres(settings: Settings) -> bool:
    """Round-trip a `SELECT 1` query against Postgres."""
    try:
        engine = create_engine(
            settings.postgres_url,
            connect_args={"connect_timeout": _CHECK_TIMEOUT_SECONDS},
        )
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001 — any engine failure means "unreachable"
        return False

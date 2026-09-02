"""Neo4j persistence and retrieval for the scientific knowledge graph."""

from collections.abc import Iterable
from contextlib import contextmanager
from typing import Any

from neo4j import GraphDatabase

from app.core.config import Settings, get_settings
from app.ingestion.corpus import Paper
from app.ner_re.schema import RELATION_ENDPOINTS, JointExtractionResult

RELATION_LABELS = {name: name.upper() for name in RELATION_ENDPOINTS}


@contextmanager
def graph_session(settings: Settings | None = None):
    settings = settings or get_settings()
    driver = GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
        connection_timeout=5,
    )
    try:
        with driver.session() as session:
            yield session
    finally:
        driver.close()


def ensure_graph_schema(session) -> None:
    session.run(
        "CREATE CONSTRAINT paper_id IF NOT EXISTS FOR (p:Paper) REQUIRE p.id IS UNIQUE"
    )
    session.run(
        "CREATE CONSTRAINT entity_key IF NOT EXISTS FOR (e:Entity) "
        "REQUIRE (e.normalized_name, e.entity_type) IS UNIQUE"
    )


def write_paper_graph(session, paper: Paper, result: JointExtractionResult) -> str:
    """Idempotently merge a paper, its entities, and typed relationships."""
    paper_row = session.run(
        "MERGE (p:Paper {id: $id}) "
        "SET p.title=$title, p.year=$year, p.venue=$venue, p.doi=$doi "
        "RETURN elementId(p) AS node_id",
        id=paper.id,
        title=paper.title,
        year=paper.year,
        venue=paper.venue,
        doi=paper.doi,
    ).single()
    paper_node_id = paper_row["node_id"]

    type_by_name: dict[str, str] = {}
    for entity in result.entities:
        normalized = " ".join(entity.text.casefold().split())
        type_by_name[entity.text.casefold()] = entity.entity_type
        session.run(
            "MATCH (p:Paper {id: $paper_id}) "
            "MERGE (e:Entity {normalized_name: $normalized, entity_type: $type}) "
            "ON CREATE SET e.name=$name "
            "MERGE (p)-[m:MENTIONS]->(e) "
            "SET m.confidence=$confidence",
            paper_id=paper.id,
            normalized=normalized,
            type=entity.entity_type,
            name=entity.text,
            confidence=entity.confidence,
        )

    for relation in result.relations:
        relationship = RELATION_LABELS[relation.relation_type]
        head_type, tail_type = RELATION_ENDPOINTS[relation.relation_type]
        query = (
            "MATCH (p:Paper {id: $paper_id}) "
            "MATCH (h:Entity {normalized_name: $head, entity_type: $head_type}) "
            "MATCH (t:Entity {normalized_name: $tail, entity_type: $tail_type}) "
            f"MERGE (h)-[r:{relationship} {{paper_id: $paper_id}}]->(t) "
            "SET r.confidence=$confidence "
            "MERGE (p)-[:SUPPORTS]->(h) MERGE (p)-[:SUPPORTS]->(t)"
        )
        session.run(
            query,
            paper_id=paper.id,
            head=" ".join(relation.head.casefold().split()),
            tail=" ".join(relation.tail.casefold().split()),
            head_type=head_type,
            tail_type=tail_type,
            confidence=relation.confidence,
        )
    return paper_node_id


def get_paper_node_id(session, paper_id: int) -> str | None:
    row = session.run(
        "MATCH (p:Paper {id: $paper_id}) RETURN elementId(p) AS node_id",
        paper_id=paper_id,
    ).single()
    return row["node_id"] if row else None


def query_graph(session, query: str, limit: int = 20) -> list[dict[str, Any]]:
    tokens = [token.casefold() for token in query.split() if len(token) >= 3][:12]
    if not tokens:
        return []
    rows: Iterable = session.run(
        "MATCH (p:Paper)-[:MENTIONS|SUPPORTS]->(e:Entity) "
        "WHERE any(token IN $tokens WHERE toLower(e.name) CONTAINS token) "
        "OPTIONAL MATCH (e)-[r]->(other:Entity) "
        "RETURN DISTINCT p.id AS paper_id, p.title AS title, "
        "e.name AS entity, e.entity_type AS entity_type, "
        "type(r) AS relation, other.name AS related_entity "
        "LIMIT $limit",
        tokens=tokens,
        limit=limit,
    )
    return [dict(row) for row in rows]


def graph_snapshot(session, limit: int = 500) -> dict[str, list[dict]]:
    nodes = [
        dict(row)
        for row in session.run(
            "MATCH (n) RETURN elementId(n) AS id, labels(n) AS labels, "
            "coalesce(n.name, n.title) AS label LIMIT $limit",
            limit=limit,
        )
    ]
    edges = [
        dict(row)
        for row in session.run(
            "MATCH (a)-[r]->(b) RETURN elementId(a) AS source, "
            "elementId(b) AS target, type(r) AS type LIMIT $limit",
            limit=limit,
        )
    ]
    return {"nodes": nodes, "edges": edges}

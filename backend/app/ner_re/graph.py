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


def write_paper_graph(session, paper: Paper, result: JointExtractionResult, input_hash: str | None = None) -> str:
    """Replace one paper's projection atomically, preserving shared entities."""
    return session.execute_write(lambda tx: _replace_paper(tx, paper, result, input_hash))


def _replace_paper(session, paper, result, input_hash):
    paper_row = session.run(
        "MERGE (p:Paper {id: $id}) "
        "SET p.title=$title, p.year=$year, p.venue=$venue, p.doi=$doi, "
        "p.nerre_input_hash=$input_hash, p.nerre_version='1' "
        "RETURN elementId(p) AS node_id",
        id=paper.id,
        title=paper.title,
        year=paper.year,
        venue=paper.venue,
        doi=paper.doi,
        input_hash=input_hash,
    ).single()
    paper_node_id = paper_row["node_id"]

    session.run(
        "MATCH (p:Paper {id: $paper_id})-[r:MENTIONS|SUPPORTS]->() DELETE r",
        paper_id=paper.id,
    ).consume()
    session.run(
        "MATCH ()-[r]->() WHERE r.paper_id=$paper_id DELETE r", paper_id=paper.id,
    ).consume()
    if result.entities:
        session.run(
            "UNWIND $rows AS row "
            "MATCH (p:Paper {id: $paper_id}) "
            "MERGE (e:Entity {normalized_name: row.normalized, entity_type: row.type}) "
            "ON CREATE SET e.name=row.name "
            "MERGE (p)-[m:MENTIONS]->(e) "
            "SET m.confidence=row.confidence",
            paper_id=paper.id,
            rows=[dict(normalized=' '.join(e.text.casefold().split()), type=e.entity_type, name=e.text, confidence=e.confidence) for e in result.entities],
        ).consume()

    grouped = {}
    for relation in result.relations:
        # Validate against the closed relation vocabulary before interpolation.
        RELATION_LABELS[relation.relation_type]
        grouped.setdefault(relation.relation_type, []).append(dict(head=' '.join(relation.head.casefold().split()), tail=' '.join(relation.tail.casefold().split()), confidence=relation.confidence))
    for kind, rows in grouped.items():
        relationship = RELATION_LABELS[kind]
        head_type, tail_type = RELATION_ENDPOINTS[kind]
        query = (
            "UNWIND $rows AS row "
            "MATCH (p:Paper {id: $paper_id}) "
            "MATCH (h:Entity {normalized_name: row.head, entity_type: $head_type}) "
            "MATCH (t:Entity {normalized_name: row.tail, entity_type: $tail_type}) "
            f"MERGE (h)-[r:{relationship} {{paper_id: $paper_id}}]->(t) "
            "SET r.confidence=row.confidence "
            "MERGE (p)-[:SUPPORTS]->(h) MERGE (p)-[:SUPPORTS]->(t)"
        )
        session.run(
            query,
            paper_id=paper.id,
            rows=rows,
            head_type=head_type,
            tail_type=tail_type,
        ).consume()
    return paper_node_id


def paper_graph_current(session, paper_id, input_hash):
    row = session.run('MATCH (p:Paper {id: $id}) RETURN properties(p)["nerre_input_hash"] AS input_hash', id=paper_id).single()
    return row is not None and row['input_hash'] == input_hash


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
        "MATCH (p)-[pe]->(e) "
        "WHERE 'Paper' IN labels(p) AND 'Entity' IN labels(e) "
        "AND type(pe) IN ['MENTIONS', 'SUPPORTS'] "
        "AND any(token IN $tokens WHERE "
        "toLower(toString(properties(e)['name'])) CONTAINS token) "
        "OPTIONAL MATCH (e)-[r]->(other) "
        "WHERE 'Entity' IN labels(other) "
        "RETURN DISTINCT properties(p)['id'] AS paper_id, "
        "properties(p)['title'] AS title, properties(e)['name'] AS entity, "
        "properties(e)['entity_type'] AS entity_type, type(r) AS relation, "
        "properties(other)['name'] AS related_entity "
        "LIMIT $limit",
        tokens=tokens,
        limit=limit,
    )
    return [dict(row) for row in rows]


def expand_paper_neighborhood(
    session, paper_ids: list[int], limit: int = 20
) -> list[dict[str, Any]]:
    """Return a bounded one-hop neighborhood rooted at vector-hit papers.

    Property and label access is dynamic so an empty/partially populated Neo4j
    database does not emit ``UnknownPropertyKeyWarning`` notifications.
    """
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise TypeError("limit must be an integer")
    if limit < 1:
        raise ValueError("limit must be positive")

    unique_ids: list[int] = []
    for paper_id in paper_ids:
        if not isinstance(paper_id, int) or isinstance(paper_id, bool) or paper_id < 0:
            raise ValueError("paper IDs must be non-negative integers")
        if paper_id not in unique_ids:
            unique_ids.append(paper_id)
    if not unique_ids:
        return []

    rows: Iterable = session.run(
        "UNWIND $paper_ids AS seed_id "
        "MATCH (seed) "
        "WHERE 'Paper' IN labels(seed) "
        "AND properties(seed)['id'] = seed_id "
        "OPTIONAL MATCH (seed)-[seed_edge]->(entity) "
        "WHERE 'Entity' IN labels(entity) "
        "AND type(seed_edge) IN ['MENTIONS', 'SUPPORTS'] "
        "OPTIONAL MATCH (neighbour)-[neighbour_edge]->(entity) "
        "WHERE 'Paper' IN labels(neighbour) "
        "AND type(neighbour_edge) IN ['MENTIONS', 'SUPPORTS'] "
        "OPTIONAL MATCH (entity)-[relation]->(related) "
        "WHERE 'Entity' IN labels(related) "
        "WITH seed, entity, relation, related, "
        "CASE WHEN neighbour IS NULL THEN seed ELSE neighbour END AS paper "
        "RETURN DISTINCT properties(paper)['id'] AS paper_id, "
        "properties(paper)['title'] AS title, "
        "properties(seed)['id'] AS seed_paper_id, "
        "properties(entity)['name'] AS entity, "
        "properties(entity)['entity_type'] AS entity_type, "
        "type(relation) AS relation, "
        "properties(related)['name'] AS related_entity "
        "ORDER BY paper_id, entity, relation, related_entity "
        "LIMIT $limit",
        paper_ids=unique_ids,
        limit=min(limit, 100),
    )
    return [dict(row) for row in rows]


def graph_snapshot(session, limit: int = 500) -> dict[str, list[dict]]:
    nodes = [
        dict(row)
        for row in session.run(
            "MATCH (n) WITH n, properties(n) AS props "
            "RETURN elementId(n) AS id, labels(n) AS labels, "
            "coalesce(props['name'], props['title'], labels(n)[0]) AS label "
            "LIMIT $limit",
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

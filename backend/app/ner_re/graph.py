"""Neo4j persistence and retrieval for the scientific knowledge graph."""

from collections.abc import Iterable
from contextlib import contextmanager
from typing import Any

from neo4j import GraphDatabase

from app.core.config import Settings, get_settings
from app.ingestion.corpus import Paper
from app.ner_re.schema import RELATION_ENDPOINTS, JointExtractionResult

RELATION_LABELS = {name: name.upper() for name in RELATION_ENDPOINTS}

# Bumped when what a projection *records about its own origin* changes, not
# what it extracts (that is the S4 input hash). A projection written before
# D-S4-PROV-01 has no evidence on its edges and no model on its Paper node, so
# `paper_graph_current` refuses it and the checkpoint rewrites it once -- with
# the same input hash, which leaves every derived stage's fingerprint alone.
NERRE_PROVENANCE_VERSION = "1"

# A MENTIONS edge names at most this many units; `evidence_count` is exact.
MAX_EVIDENCE_REFS = 25


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


def write_paper_graph(
    session,
    paper: Paper,
    result: JointExtractionResult,
    input_hash: str | None = None,
    provenance: dict | None = None,
) -> str:
    """Replace one paper's projection atomically, preserving shared entities.

    `provenance` is stored on the Paper node (model, its resolved revision, the
    input basis, `nerre_provenance`); see `ner_re.service._provenance`.
    """
    return session.execute_write(
        lambda tx: _replace_paper(tx, paper, result, input_hash, provenance or {})
    )


def _replace_paper(session, paper, result, input_hash, provenance):
    paper_row = session.run(
        "MERGE (p:Paper {id: $id}) "
        "SET p.title=$title, p.year=$year, p.venue=$venue, p.doi=$doi, "
        "p.nerre_input_hash=$input_hash, p.nerre_version='1' "
        "SET p += $provenance "
        "RETURN elementId(p) AS node_id",
        id=paper.id,
        title=paper.title,
        year=paper.year,
        venue=paper.venue,
        doi=paper.doi,
        input_hash=input_hash,
        provenance=provenance,
    ).single()
    paper_node_id = paper_row["node_id"]

    session.run(
        "MATCH (p:Paper {id: $paper_id})-[r:MENTIONS|SUPPORTS]->() DELETE r",
        paper_id=paper.id,
    ).consume()
    session.run(
        "MATCH ()-[r]->() WHERE r.paper_id=$paper_id DELETE r",
        paper_id=paper.id,
    ).consume()
    if result.entities:
        session.run(
            "UNWIND $rows AS row "
            "MATCH (p:Paper {id: $paper_id}) "
            "MERGE (e:Entity {normalized_name: row.normalized, entity_type: row.type}) "
            "ON CREATE SET e.name=row.name "
            "MERGE (p)-[m:MENTIONS]->(e) "
            "SET m.confidence=row.confidence, m.evidence=row.evidence, "
            "m.evidence_count=row.evidence_count",
            paper_id=paper.id,
            rows=[
                {
                    "normalized": " ".join(e.text.casefold().split()),
                    "type": e.entity_type,
                    "name": e.text,
                    "confidence": e.confidence,
                    "evidence": list(e.evidence[:MAX_EVIDENCE_REFS]),
                    "evidence_count": len(e.evidence),
                }
                for e in result.entities
            ],
        ).consume()

    grouped = {}
    for relation in result.relations:
        # Validate against the closed relation vocabulary before interpolation.
        RELATION_LABELS[relation.relation_type]
        grouped.setdefault(relation.relation_type, []).append(
            {
                "head": " ".join(relation.head.casefold().split()),
                "tail": " ".join(relation.tail.casefold().split()),
                "confidence": relation.confidence,
                "evidence": list(relation.evidence[:MAX_EVIDENCE_REFS]),
                "cross_sentence": relation.cross_sentence,
            }
        )
    for kind, rows in grouped.items():
        relationship = RELATION_LABELS[kind]
        head_type, tail_type = RELATION_ENDPOINTS[kind]
        query = (
            "UNWIND $rows AS row "
            "MATCH (p:Paper {id: $paper_id}) "
            "MATCH (h:Entity {normalized_name: row.head, entity_type: $head_type}) "
            "MATCH (t:Entity {normalized_name: row.tail, entity_type: $tail_type}) "
            f"MERGE (h)-[r:{relationship} {{paper_id: $paper_id}}]->(t) "
            "SET r.confidence=row.confidence, r.evidence=row.evidence, "
            "r.cross_sentence=row.cross_sentence "
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


def remove_orphan_entity(session, normalized_name: str, entity_type: str) -> bool:
    """Delete one Entity node by exact key, only if nothing references it.

    Shared entities are kept on purpose when a paper's projection is replaced,
    so a node can outlive every mention. This removes a specific one that was
    confirmed invalid, and never detaches: a node with any relationship -- for
    example one another paper mentions -- is left untouched. Returns whether a
    node was removed.
    """

    def remove(tx):
        return tx.run(
            "MATCH (e:Entity {normalized_name: $name, entity_type: $entity_type}) "
            "WHERE NOT (e)--() "
            "WITH collect(e) AS orphans "
            "FOREACH (e IN orphans | DELETE e) "
            "RETURN size(orphans) AS removed",
            name=normalized_name,
            entity_type=entity_type,
        ).single()["removed"]

    return bool(session.execute_write(remove))


def paper_graph_current(session, paper_id, input_hash):
    row = session.run(
        "MATCH (p:Paper {id: $id}) RETURN properties(p) AS props",
        id=paper_id,
    ).single()
    if row is None:
        return False
    props = row["props"] or {}
    return (
        props.get("nerre_input_hash") == input_hash
        and props.get("nerre_provenance") == NERRE_PROVENANCE_VERSION
    )


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


def graph_snapshot(session, limit: int = 500) -> dict:
    """A bounded, self-consistent view of the graph for the dashboard.

    Every paper comes first, then entities by how connected they are, and only
    edges between returned nodes are returned. Nodes and edges used to be two
    independent unordered `LIMIT`s, so the view showed an arbitrary few papers,
    entities whose edges were cut, and edges to nodes it never sent. The totals
    let a caller say how much of the graph it is showing (additive keys).
    """
    nodes = [
        dict(row)
        for row in session.run(
            "MATCH (n) WITH n, properties(n) AS props, COUNT { (n)--() } AS degree "
            "RETURN elementId(n) AS id, labels(n) AS labels, "
            "coalesce(props['name'], props['title'], labels(n)[0]) AS label "
            "ORDER BY CASE WHEN 'Paper' IN labels(n) THEN 0 ELSE 1 END, "
            "degree DESC, id "
            "LIMIT $limit",
            limit=limit,
        )
    ]
    ids = [node["id"] for node in nodes]
    edges = (
        [
            dict(row)
            for row in session.run(
                "MATCH (a)-[r]->(b) WHERE elementId(a) IN $ids AND elementId(b) IN $ids "
                "RETURN elementId(a) AS source, elementId(b) AS target, type(r) AS type "
                "LIMIT $edge_limit",
                ids=ids,
                edge_limit=limit * 4,
            )
        ]
        if ids
        else []
    )
    totals = next(
        iter(
            session.run(
                "MATCH (n) WITH count(n) AS nodes "
                "OPTIONAL MATCH ()-[r]->() RETURN nodes, count(r) AS edges"
            )
        ),
        None,
    )
    total_nodes = totals["nodes"] if totals else len(nodes)
    total_edges = totals["edges"] if totals else len(edges)
    return {
        "nodes": nodes,
        "edges": edges,
        "total_nodes": total_nodes,
        "total_edges": total_edges,
        "truncated": total_nodes > len(nodes) or total_edges > len(edges),
    }

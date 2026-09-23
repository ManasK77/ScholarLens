"""Admin/dev operation: wipe the accumulated corpus, keep the infrastructure.

`reset_corpus` removes every paper and everything derived from it —
PostgreSQL rows, the Neo4j research graph, Qdrant evidence vectors, and the
PDF files the corpus itself downloaded or accepted — without touching the
databases, containers, GROBID, model caches, or `.env` themselves. It is the
single implementation `DELETE /admin/corpus` (routes_backend.py) and
`python -m app.core.corpus_reset` both call; neither may duplicate this logic.

Once every `papers` row is gone, the identity sequences of the tables this
function emptied are rewound too (D-RESET-02), so the next collection's first
paper is id 1 again rather than continuing from the deleted corpus's high-water
mark. That happens in the same transaction as the deletes and only for tables
emptied here, so an id is never reissued while a row still holds it; ordinary
collections keep appending new ids as before.

Deletion order: PostgreSQL, then Neo4j, then Qdrant — verified empty — and
only then the PDF files, last. PDF deletion cannot be undone, so nothing here
removes a PDF before every store that could still fail has already succeeded;
the Postgres delete instead runs first, so a paper's row is never left
pointing at a `pdf_path` this function has already deleted (the opposite,
in-between ordering — deleting some PDFs, then failing before the row that
names them is gone — would leave `Paper.has_full_text=True` for a file that
no longer exists, an actively wrong record, not just an incomplete reset).
Every step is otherwise idempotent — a store already emptied by an earlier,
failed attempt is a no-op, not an error. The one residual risk this ordering
accepts: if Neo4j or Qdrant fails *after* Postgres has already committed, the
PDFs survive un-deleted, and a retry's fresh `pdf_path` scan (Postgres has
already forgotten them) will not find them either. That is the same harmless,
already-instrumented orphan `app.core.reconcile`'s `unreferenced_pdf_count`
reports — never a corpus record that lies about what it still has.
"""

import argparse
import json
import logging
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import Column, Integer, Table, delete, func, select, text
from sqlalchemy.orm import Session

from app.clustering.storage import Cluster, _dependent_tables
from app.core.pipeline import DerivedState, PaperStageState, current_run, pipeline_run
from app.explanation.storage import ReportSnapshot
from app.extraction.storage import ExtractedFact
from app.ingestion.corpus import Paper, get_engine
from app.parsing.storage import ParsedDocument

logger = logging.getLogger(__name__)

# The only two node labels app.ner_re.graph ever writes (ensure_graph_schema's
# constraints are on Paper.id and Entity(normalized_name, entity_type)).
# DETACH DELETE on either label also removes every MENTIONS/SUPPORTS/typed
# relation edge between them, so no separate relationship query is needed —
# and nothing outside these two labels is ever touched.
_CLEAR_GRAPH_QUERY = (
    "MATCH (n) WHERE 'Paper' IN labels(n) OR 'Entity' IN labels(n) " "DETACH DELETE n"
)
_COUNT_GRAPH_QUERY = (
    "MATCH (n) WHERE 'Paper' IN labels(n) OR 'Entity' IN labels(n) "
    "RETURN count(n) AS c"
)


def _clear_graph(tx):
    summary = tx.run(_CLEAR_GRAPH_QUERY).consume()
    return summary.counters.nodes_deleted, summary.counters.relationships_deleted


_SQLITE_SEQUENCE_EXISTS = text(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
)
_SQLITE_SEQUENCE_CLEAR = text("DELETE FROM sqlite_sequence WHERE name = :table")


def _identity_column(table: Table) -> Column | None:
    """The table's own generated id column, or None if it has none.

    A single-column integer primary key that is not itself a foreign key is a
    generated identity (`papers.id`, `clusters.id`). A composite key
    (`paper_stage_state`), a text key, or a key borrowed from a parent table
    has no sequence of its own to rewind.
    """
    columns = list(table.primary_key.columns)
    if len(columns) != 1:
        return None
    column = columns[0]
    if column.foreign_keys or not isinstance(column.type, Integer):
        return None
    return column


def _restart_identity(session: Session, tables: Iterable[Table]) -> list[str]:
    """Rewind each emptied table's id sequence so the next row is row 1 again.

    Called only for tables whose every row this reset has just deleted, inside
    the same transaction as those deletes — so an id can never be handed out
    while a row already holding it survives. A table that is not emptied here
    (`pipeline_runs`, which records the reset itself) is never passed in.

    PostgreSQL: the sequence is resolved by the server via
    `pg_get_serial_sequence` — covering both `SERIAL` and `GENERATED … AS
    IDENTITY` without this code assuming either naming — then rewound with
    `setval(seq, 1, false)`, which makes the next `nextval` return 1. The
    sequence name is bound as a value, never interpolated into SQL text.

    SQLite (the documented local-dev store): rowids are reused from 1 once the
    table is empty, so there is nothing to do unless an `AUTOINCREMENT` column
    has left a high-water mark in `sqlite_sequence`; that row is cleared when
    the table exists. Any other dialect is left untouched rather than guessed
    at.
    """
    dialect = session.get_bind().dialect.name
    restarted: list[str] = []
    sqlite_sequence_exists = (
        dialect == "sqlite"
        and session.execute(_SQLITE_SEQUENCE_EXISTS).first() is not None
    )

    for table in tables:
        column = _identity_column(table)
        if column is None:
            continue
        if dialect == "postgresql":
            sequence = session.execute(
                select(func.pg_get_serial_sequence(table.name, column.name))
            ).scalar()
            if not sequence:
                continue
            session.execute(select(func.setval(sequence, 1, False)))
        elif dialect == "sqlite":
            if sqlite_sequence_exists:
                session.execute(_SQLITE_SEQUENCE_CLEAR, {"table": table.name})
        else:
            continue
        restarted.append(table.name)
    return restarted


def _qdrant_point_count(index) -> int:
    if not index.client.collection_exists(index.collection):
        return 0
    return index.client.count(collection_name=index.collection, exact=True).count


def reset_corpus(
    session: Session, neo4j, index, pdf_storage_path: str | None = None
) -> dict:
    """Remove every paper and all data derived from it.

    Requires the pipeline writer lock (call from inside `pipeline_run`), the
    same discipline `app.core.maintenance.delete_paper` follows, so a reset
    can never race a pipeline run or another reset.

    Idempotent: safe to call on an already-empty corpus, and safe to re-run
    after a partial failure — every step either targets rows/nodes/points
    that may already be gone, or a file that may already be deleted.
    """
    if current_run() is None:
        raise RuntimeError("Corpus reset requires the pipeline writer lock")

    # Step 1 — identifiers, captured before anything destructive happens.
    # PDF paths in particular: once the `papers` rows are gone, there is no
    # other record of which files on disk were ever ours.
    pdf_paths = [
        path
        for (path,) in session.execute(
            select(Paper.pdf_path).where(Paper.pdf_path.is_not(None))
        ).all()
    ]

    # Step 2 — PostgreSQL, children before parents. Explicit statements
    # rather than relying on ON DELETE CASCADE, because SQLite (the
    # documented local-dev store) does not enforce foreign keys by default.
    # The cluster family is discovered off its foreign keys, the same
    # reflection `clustering.storage.replace_clusters` uses, so a future
    # table FK'd to `clusters.id` is covered automatically. This runs before
    # Neo4j/Qdrant/PDFs specifically so that once it commits, no `Paper` row
    # can ever again be caught naming a `pdf_path` this function deleted.
    postgres_records: dict[str, int] = {}
    for table in _dependent_tables():
        postgres_records[table.name] = session.execute(delete(table)).rowcount
    postgres_records[Cluster.__tablename__] = session.execute(delete(Cluster)).rowcount
    postgres_records[ReportSnapshot.__tablename__] = session.execute(
        delete(ReportSnapshot)
    ).rowcount
    postgres_records[ExtractedFact.__tablename__] = session.execute(
        delete(ExtractedFact)
    ).rowcount
    postgres_records[ParsedDocument.__tablename__] = session.execute(
        delete(ParsedDocument)
    ).rowcount
    postgres_records[PaperStageState.__tablename__] = session.execute(
        delete(PaperStageState)
    ).rowcount
    postgres_records[Paper.__tablename__] = session.execute(delete(Paper)).rowcount
    postgres_records[DerivedState.__tablename__] = session.execute(
        delete(DerivedState)
    ).rowcount

    # Step 2b — every table above is now empty, so their id sequences rewind in
    # the same transaction as the deletes: the next collection's first paper is
    # paper 1 again, as it was on a fresh corpus. Ordinary collections never
    # touch this — ids only ever restart once nothing is left to collide with.
    sequences_restarted = _restart_identity(
        session,
        (
            *_dependent_tables(),
            Cluster.__table__,
            ReportSnapshot.__table__,
            ExtractedFact.__table__,
            ParsedDocument.__table__,
            PaperStageState.__table__,
            Paper.__table__,
            DerivedState.__table__,
        ),
    )
    session.commit()

    # Step 3 — Neo4j.
    neo4j_nodes, neo4j_relationships = neo4j.execute_write(_clear_graph)

    # Step 4 — Qdrant. One collection, exclusively ScholarLens evidence
    # (Settings.qdrant_collection); dropping it is simpler and safer than
    # scrolling every point, and EvidenceIndex.ensure_collection() recreates
    # it lazily the next time anything is indexed.
    qdrant_points = _qdrant_point_count(index)
    if qdrant_points or index.client.collection_exists(index.collection):
        index.client.delete_collection(collection_name=index.collection)

    # Step 5 — verify Postgres/Neo4j/Qdrant are actually empty *before*
    # deleting a single PDF. Removing a PDF cannot be undone, so it must never
    # happen ahead of knowing every other store already converged.
    remaining_papers = session.scalar(select(func.count()).select_from(Paper)) or 0
    remaining_graph_nodes = neo4j.run(_COUNT_GRAPH_QUERY).single()["c"]
    remaining_qdrant_points = _qdrant_point_count(index)
    if remaining_papers or remaining_graph_nodes or remaining_qdrant_points:
        raise RuntimeError(
            "Corpus reset did not converge: "
            f"{remaining_papers} papers, {remaining_graph_nodes} graph nodes, "
            f"{remaining_qdrant_points} vector points remain"
        )

    # Step 6 — ScholarLens-managed PDFs only: exactly the paths the corpus
    # itself recorded (the same convention app.core.reconcile uses to tell
    # managed files from strays), never a directory-wide sweep. When the
    # storage root is known, a path a corrupted row pointed outside it is
    # skipped rather than trusted — defense in depth, since every writer
    # (manual_upload, arxiv/acl fetch_pdf) only ever stores paths under it.
    # Reached only after Step 5's verification passed, so a paper row naming
    # one of these paths is already guaranteed gone.
    pdf_root = Path(pdf_storage_path).resolve() if pdf_storage_path else None
    pdfs_removed = 0
    for path in pdf_paths:
        file_path = Path(path)
        if pdf_root is not None:
            try:
                file_path = file_path.resolve()
            except OSError:
                continue
            if not file_path.is_relative_to(pdf_root):
                logger.warning(
                    "Skipping a pdf_path outside pdf_storage_path during corpus reset"
                )
                continue
        if file_path.is_file():
            file_path.unlink()
            pdfs_removed += 1

    logger.warning(
        "corpus reset complete papers=%d pdfs=%d neo4j_nodes=%d "
        "neo4j_relationships=%d qdrant_points=%d",
        postgres_records[Paper.__tablename__],
        pdfs_removed,
        neo4j_nodes,
        neo4j_relationships,
        qdrant_points,
    )
    return {
        "deleted": True,
        "papers": postgres_records[Paper.__tablename__],
        "pdfs": pdfs_removed,
        "postgres_records": postgres_records,
        "sequences_restarted": sequences_restarted,
        "neo4j_nodes": neo4j_nodes,
        "neo4j_relationships": neo4j_relationships,
        "qdrant_points": qdrant_points,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Permanently remove all corpus data, keeping infrastructure intact",
    )
    args = parser.parse_args()
    if not args.confirm:
        parser.error("--confirm is required; this permanently deletes all corpus data")
    from app.core.config import get_settings
    from app.embeddings.indexer import EvidenceIndex
    from app.ner_re.graph import graph_session

    settings = get_settings()
    with Session(get_engine()) as session, pipeline_run(
        session, {"operation": "reset-corpus"}
    ) as run, graph_session(settings) as neo4j:
        index = EvidenceIndex(settings)
        with run.stage("reset"):
            result = reset_corpus(session, neo4j, index, settings.pdf_storage_path)
    result["run_id"] = run.id
    print(json.dumps(result))


if __name__ == "__main__":
    main()

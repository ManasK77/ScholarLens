"""Stage 6's tables must exist by the time `POST /clusters` runs.

`app/main.py` creates the schema with a single `Base.metadata.create_all()` at
startup, and a table only reaches `Base.metadata` if its module has been
imported. Phases 2 and 3 each added an explicit `noqa: F401` import to
`main.py` for exactly this reason; Stage 6 needs the same one, or
`rebuild_clusters` fails against Postgres with "relation does not exist" the
first time it is called.
"""

from app.ingestion.corpus import Base

CLUSTERING_TABLES = ("clusters", "cluster_members", "cluster_contradictions")


def test_importing_the_app_registers_the_clustering_tables() -> None:
    import app.main  # noqa: F401 -- the import under test

    for table in CLUSTERING_TABLES:
        assert table in Base.metadata.tables, (
            f"{table!r} is not on Base.metadata after importing app.main; "
            "app/main.py needs to import app.clustering.storage"
        )


def test_the_member_and_contradiction_tables_point_at_clusters() -> None:
    import app.main  # noqa: F401

    for table in ("cluster_members", "cluster_contradictions"):
        foreign_keys = {
            key.column.table.name for key in Base.metadata.tables[table].foreign_keys
        }
        assert foreign_keys == {"clusters"}

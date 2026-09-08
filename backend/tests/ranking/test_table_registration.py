"""Stage 8's table must exist by the time `POST /gaps` runs.

`app/main.py` creates the schema with a single `Base.metadata.create_all()` at
startup, and a table only reaches `Base.metadata` if its module has been
imported. Phases 2, 3, 6 and 7 each carry an explicit `noqa: F401` import in
`main.py` for exactly this reason — the Phase 6 code review caught the missing
one — so Stage 8 needs the same, or `rebuild_rankings` fails against PostgreSQL
with "relation does not exist" the first time it is called.
"""

from app.clustering.storage import _dependent_tables
from app.ingestion.corpus import Base


def test_importing_the_app_registers_the_ranking_table() -> None:
    import app.main  # noqa: F401 -- the import under test

    assert "cluster_rankings" in Base.metadata.tables, (
        "'cluster_rankings' is not on Base.metadata after importing app.main; "
        "app/main.py needs to import app.ranking.storage"
    )


def test_the_ranking_table_points_at_clusters() -> None:
    import app.main  # noqa: F401

    foreign_keys = {
        key.column.table.name
        for key in Base.metadata.tables["cluster_rankings"].foreign_keys
    }

    assert foreign_keys == {"clusters"}


def test_a_cluster_rebuild_is_known_to_clear_the_rankings() -> None:
    """The Phase 7 review's fix has to actually cover this table."""
    import app.main  # noqa: F401

    assert "cluster_rankings" in {table.name for table in _dependent_tables()}

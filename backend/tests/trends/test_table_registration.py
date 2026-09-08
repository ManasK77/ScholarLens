"""Stage 7's table must exist by the time `POST /trends` runs.

`app/main.py` creates the schema with a single `Base.metadata.create_all()` at
startup, and a table only reaches `Base.metadata` if its module has been
imported. Phases 2, 3 and 6 each carry an explicit `noqa: F401` import in
`main.py` for exactly this reason — the Phase 6 code review caught the missing
one — so Stage 7 needs the same, or `rebuild_trends` fails against PostgreSQL
with "relation does not exist" the first time it is called.
"""

from app.ingestion.corpus import Base


def test_importing_the_app_registers_the_trend_table() -> None:
    import app.main  # noqa: F401 -- the import under test

    assert "cluster_trends" in Base.metadata.tables, (
        "'cluster_trends' is not on Base.metadata after importing app.main; "
        "app/main.py needs to import app.trends.storage"
    )


def test_the_trend_table_points_at_clusters() -> None:
    import app.main  # noqa: F401

    foreign_keys = {
        key.column.table.name
        for key in Base.metadata.tables["cluster_trends"].foreign_keys
    }

    assert foreign_keys == {"clusters"}

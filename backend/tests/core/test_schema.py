from unittest.mock import MagicMock

from app.core.schema import ensure_schema_compatibility


def test_postgres_compatibility_updates_are_idempotent_additions() -> None:
    engine = MagicMock()
    engine.dialect.name = "postgresql"
    connection = engine.begin.return_value.__enter__.return_value

    ensure_schema_compatibility(engine)

    statements = [str(call.args[0]) for call in connection.execute.call_args_list]
    assert statements == [
        "ALTER TABLE cluster_trends ADD COLUMN IF NOT EXISTS id SERIAL",
        "ALTER TABLE cluster_trends ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT now()",
        "ALTER TABLE gap_reports ADD COLUMN IF NOT EXISTS id SERIAL",
    ]


def test_non_postgres_database_needs_no_compatibility_updates() -> None:
    engine = MagicMock()
    engine.dialect.name = "sqlite"

    ensure_schema_compatibility(engine)

    engine.begin.assert_not_called()

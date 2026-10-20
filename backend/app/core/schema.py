"""Small, idempotent compatibility updates for existing local databases."""

from sqlalchemy import text
from sqlalchemy.engine import Engine

_POSTGRES_COMPATIBILITY_DDL = (
    "ALTER TABLE cluster_trends ADD COLUMN IF NOT EXISTS id SERIAL",
    "ALTER TABLE cluster_trends ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT now()",
    "ALTER TABLE gap_reports ADD COLUMN IF NOT EXISTS id SERIAL",
    # The A/B/C justification (D-09-05). Nullable: rows written before it
    # existed have no justification, and `GET /reports` serves `{}` there.
    "ALTER TABLE gap_reports ADD COLUMN IF NOT EXISTS justification JSON",
    # S1 hardening (2026-10-12): full-text provenance on papers, and the
    # collection run's lifecycle and persisted summary. All nullable; older
    # rows simply have none. `fulltext_attempts` is a new table, which
    # `create_all` creates.
    "ALTER TABLE papers ADD COLUMN IF NOT EXISTS openaccess_pdf_url VARCHAR",
    "ALTER TABLE papers ADD COLUMN IF NOT EXISTS source_url VARCHAR",
    "ALTER TABLE collection_runs ADD COLUMN IF NOT EXISTS status VARCHAR",
    "ALTER TABLE collection_runs ADD COLUMN IF NOT EXISTS finished_at TIMESTAMP",
    "ALTER TABLE collection_runs ADD COLUMN IF NOT EXISTS summary JSON",
)


def ensure_schema_compatibility(engine: Engine) -> None:
    """Bring pre-Phase-14 PostgreSQL volumes up to the current ORM shape.

    ``create_all`` intentionally leaves existing tables untouched. These
    derived-result tables gained surrogate IDs later, so older local volumes
    need a narrow additive update before their dashboard endpoints can read
    them. Fresh databases already have the columns and make these statements
    no-ops.
    """
    if engine.dialect.name != "postgresql":
        return

    with engine.begin() as connection:
        for statement in _POSTGRES_COMPATIBILITY_DDL:
            connection.execute(text(statement))

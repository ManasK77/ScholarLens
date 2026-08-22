"""Centralized application settings (D-07).

Every module that needs a connection URI, credential, or config value reads
it from here via `get_settings()` — never via a scattered `os.environ` /
`os.getenv` call elsewhere in the codebase (CLAUDE.md config-centralization
rule).

Values are loaded from the process environment, falling back to a `.env`
file (D-09 documents the full variable contract in `.env.example`).
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings sourced from environment variables / `.env`."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Neo4j (graph store) ---
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str

    # --- Qdrant (vector store) ---
    qdrant_url: str

    # --- PostgreSQL (relational store) ---
    postgres_host: str
    postgres_port: int
    postgres_user: str
    postgres_password: str
    postgres_db: str

    # --- PDF ingestion / upload ---
    pdf_storage_path: str
    max_upload_size_mb: int

    # --- Ingestion sources ---
    unpaywall_email: str
    semantic_scholar_api_key: str | None = None

    # --- LLM ---
    llm_api_key: str | None = None

    @property
    def postgres_url(self) -> str:
        """SQLAlchemy connection string built from the individual postgres_* fields."""
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


def get_settings() -> Settings:
    """Module-level accessor other modules should import instead of instantiating Settings() directly."""
    return Settings()

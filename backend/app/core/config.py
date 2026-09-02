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
    max_upload_files: int = 20

    # --- Ingestion sources ---
    unpaywall_email: str
    semantic_scholar_api_key: str | None = None
    acl_anthology_bib_path: str = "data/acl_anthology_export.bib"

    # --- GROBID (Stage 2 parsing service) ---
    grobid_url: str

    # --- LLM ---
    llm_api_key: str | None = None
    llm_provider: str = "ollama"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    gemini_api_url: str = "https://generativelanguage.googleapis.com/v1beta"
    ollama_url: str = "http://ollama:11434"
    ollama_model: str = "qwen3:8b"
    llm_timeout_seconds: float = 120.0

    # --- Backend models / analysis ---
    joint_ie_model: str = "fastino/gliner2.5-base-v1"
    specter2_model: str = "allenai/specter2_base"
    nli_model: str = "MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli"
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    qdrant_collection: str = "scholarlens-evidence"
    embedding_batch_size: int = 16
    cluster_min_size: int = 3
    contradiction_threshold: float = 0.75
    max_nli_pairs: int = 1000
    retrieval_round_cap: int = 3
    retrieval_top_n: int = 12
    report_top_k: int = 10

    # Transparent Phase 8 score weights.
    gap_weight_mention_frequency: float = 1.0
    gap_weight_trend_slope: float = 1.0
    gap_weight_solution_density: float = 1.0
    gap_weight_citation_impact: float = 0.2
    gap_weight_contradiction_flag: float = 0.5

    # Required for every endpoint except health/OpenAPI.
    api_key: str

    @property
    def postgres_url(self) -> str:
        """SQLAlchemy connection string built from the individual postgres_* fields."""
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def gap_weights(self) -> dict[str, float]:
        return {
            "mention_frequency": self.gap_weight_mention_frequency,
            "trend_slope": self.gap_weight_trend_slope,
            "solution_density": self.gap_weight_solution_density,
            "citation_impact": self.gap_weight_citation_impact,
            "contradiction_flag": self.gap_weight_contradiction_flag,
        }


def get_settings() -> Settings:
    """Module-level accessor other modules should import instead of instantiating Settings() directly."""
    return Settings()

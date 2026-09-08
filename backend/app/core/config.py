"""Centralized application settings (D-07).

Every module that needs a connection URI, credential, or config value reads
it from here via `get_settings()` — never via a scattered `os.environ` /
`os.getenv` call elsewhere in the codebase (CLAUDE.md config-centralization
rule).

Values are loaded from the process environment, falling back to a `.env`
file (D-09 documents the full variable contract in `.env.example`).
"""

from pydantic import Field, field_validator
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
    acl_anthology_bib_path: str = "data/acl_anthology_export.bib"

    # --- GROBID (Stage 2 parsing service) ---
    grobid_url: str

    # --- Joint NER + Relation Extraction (Phase 4) ---
    # GLiNER2.5 boundary checkpoint built with enable_relations=True — the
    # JointIE typed-graph API needs one (PROGRESS.md D-04-01).
    nerre_model: str = "fastino/gliner2.5-base-v1"
    # Calibrated against fastino/gliner2.5-base-v1 over a sample of scientific
    # sentences. The `confidence` field is a *typing* score, not an is-this-an-
    # entity score — JointIE's own candidate_threshold already filters those —
    # and it is strongly bimodal: one correct entity scored 0.248 ("BERT"), the
    # next lowest 0.482, with zero false positives anywhere in the sample. 0.15
    # therefore sits safely below the true-positive floor while keeping a guard.
    # Missing a `model` entity is especially costly: every relation has model as
    # its head, so one dropped entity silently removes all four edge types.
    nerre_entity_threshold: float = Field(default=0.15, ge=0.0, le=1.0)
    nerre_relation_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    # Caps the transformer work per paper (README 13.6 resource safety).
    # gt=0: a zero/negative cap would silently extract nothing corpus-wide.
    nerre_max_chars_per_chunk: int = Field(default=1200, gt=0)
    nerre_max_chunks_per_paper: int = Field(default=12, gt=0)

    # --- Embeddings & vector indexing (Phase 5) ---
    # SPECTER2 = the `allenai/specter2_base` transformer plus the
    # `allenai/specter2` *proximity* adapter, which is the variant the model
    # card prescribes for retrieval and clustering (PROGRESS.md D-05-01).
    embedding_model: str = "allenai/specter2_base"
    embedding_adapter: str = "allenai/specter2"
    # Optional Hugging Face revision (commit SHA or tag) to pin both of the
    # above to. Unset tracks the repo's default branch, so a silent upstream
    # re-upload would change every vector the corpus was indexed with; set it
    # to make a run reproducible across machines and time.
    embedding_model_revision: str | None = None
    # Qdrant collection holding every indexed paragraph/limitation/future-work
    # vector. One name, centrally defined: Phases 6, 9 and 10 all read it.
    qdrant_collection: str = "scholarlens_evidence"
    # Batch size is the one knob that turns corpus size into peak memory
    # (README 13.6). Bounded on both ends: 0 would index nothing, and an
    # unbounded value would try to hold the whole corpus in one forward pass.
    embedding_batch_size: int = Field(default=16, gt=0, le=256)
    # SPECTER2 is a BERT-family encoder: 512 word-piece positions.
    embedding_max_tokens: int = Field(default=512, gt=0, le=512)
    # Cheap pre-tokenizer character guard against a pathological paragraph out
    # of a malformed PDF. Far above what 512 tokens can hold, so it never cuts
    # text the tokenizer would have kept.
    embedding_max_chars: int = Field(default=4000, gt=0, le=100_000)
    # Default `limit` for a semantic query when a caller does not pass one.
    retrieval_top_n: int = Field(default=12, gt=0, le=1000)

    # --- Semantic clustering + contradiction detection (Phase 6) ---
    # HDBSCAN discovers the number of themes from the density of the SPECTER2
    # space; there is deliberately no cluster-count setting (CLUSTER-01).
    # `min_cluster_size` is the smallest group that counts as a theme — 2 is
    # HDBSCAN's floor, and a "theme" one paper mentions twice is not a gap.
    clustering_min_cluster_size: int = Field(default=3, ge=2, le=1000)
    # How conservative HDBSCAN is about calling a point noise. `None` makes it
    # follow `min_cluster_size`, which is the documented default.
    clustering_min_samples: int | None = Field(default=None, ge=1, le=1000)
    # Hard resource guard: HDBSCAN's mutual-reachability step is O(n^2) in
    # memory and the corpus decides how many gap vectors arrive (README 13.6).
    clustering_max_statements: int = Field(default=20_000, gt=0, le=200_000)

    # DeBERTa-MNLI checkpoint for pairwise contradiction detection. The base
    # release (~139M params) rather than deberta-large-mnli (~400M): README
    # 13.6 prefers the smaller representative model, and swapping to the large
    # variant is a `.env` change. Label indices are always read from the
    # checkpoint's own config, never hardcoded (PROGRESS.md D-06-03).
    nli_model: str = "microsoft/deberta-base-mnli"
    # Optional Hugging Face revision pin, for the same reproducibility reason
    # `embedding_model_revision` exists.
    nli_model_revision: str | None = None
    nli_batch_size: int = Field(default=8, gt=0, le=64)
    # DeBERTa is a BERT-family encoder; a premise+hypothesis pair shares the
    # 512-position budget.
    nli_max_tokens: int = Field(default=256, gt=0, le=512)
    # Cheap pre-tokenizer character guard on each side of a pair.
    nli_max_chars: int = Field(default=1000, gt=0, le=10_000)
    # Pairs grow as O(n^2) in cluster size and each is a forward pass, so this
    # is what stops one large cluster stalling a corpus run.
    nli_max_pairs_per_cluster: int = Field(default=50, gt=0, le=2000)
    # Minimum P(contradiction) before a pair is flagged as gap evidence.
    contradiction_threshold: float = Field(default=0.7, ge=0.0, le=1.0)

    # --- Trend analysis (Phase 7) ---
    # Sanity window on publication years. Broken metadata reaches Stage 7 as
    # `year = 12` or `year = 99999`; because the series zero-fills the gaps
    # inside a cluster's observed range, one such value would turn a five-year
    # series into a several-thousand-entry dict and flatten the slope toward
    # nothing (PROGRESS.md D-07-04). Members outside the window are dropped
    # exactly like undated ones. Both bounds are static rather than derived
    # from "now": a slope whose value moved with the wall clock would not be
    # reproducible, which is the whole point of this phase.
    trend_min_year: int = Field(default=1900, ge=1, le=3000)
    trend_max_year: int = Field(default=2100, ge=1, le=3000)

    # --- Gap ranking (Phase 8) ---
    # `gap_score = w1*mention_frequency + w2*trend_slope - w3*solution_density
    #            + w4*citation_impact + w5*contradiction_flag` (README §8).
    #
    # Every weight is `ge=0`: the formula's own signs carry each term's
    # direction, so a negative value would invert a signal's meaning behind the
    # operator's back — `solution_density` is *subtracted*, and a negative w3
    # would reward a theme for already being solved. Zero is allowed; that is
    # how a signal is switched off.
    #
    # The signals are raw and in different units (papers, papers/year, a ratio
    # in [0,1], mean citations, a 0/1 flag), so the defaults below are what put
    # them on a common scale — see PROGRESS.md D-08-07.
    #
    # One point per mentioning paper: the reference unit for the other four.
    gap_weight_mention_frequency: float = Field(default=1.0, ge=0.0)
    # A theme growing by one paper per year is worth three mentioning papers.
    gap_weight_trend_slope: float = Field(default=3.0, ge=0.0)
    # Large on purpose: solution density is a ratio in [0, 1] while mention
    # frequency is an unbounded count, so a small weight would make the one
    # signal that says "this is already being addressed" decorative. A fully
    # addressed theme loses 25 points.
    gap_weight_solution_density: float = Field(default=25.0, ge=0.0)
    # 50 mean citations is worth about one mentioning paper.
    gap_weight_citation_impact: float = Field(default=0.02, ge=0.0)
    # A flagged cross-paper disagreement is worth five mentioning papers.
    gap_weight_contradiction: float = Field(default=5.0, ge=0.0)

    # --- Mode A RAG explanation (Phase 9) ---
    # How many of Stage 8's ranked gaps get explained. Each one is a separate
    # LLM call, so this is the knob that turns corpus size into generation
    # cost; README §15 asks for the *top-ranked* gaps, not all of them.
    report_top_k: int = Field(default=5, gt=0, le=100)
    # Supporting passages retrieved per gap. This is the report's entire
    # evidence budget and its entire prompt budget: too few and the model has
    # nothing to ground on, too many and the passages crowd each other out of
    # the context window (README §15 flags context hygiene for this phase).
    report_evidence_top_n: int = Field(default=8, gt=0, le=100)

    # --- LLM ---
    # Groq is the only provider `app/explanation/llm.py` builds a client for.
    # The field is kept so a deployment that names anything else fails loudly
    # at call time with a clear message instead of silently calling Groq.
    llm_provider: str = "groq"
    llm_api_key: str | None = None
    # Bounded on both ends: 0 would abort every generation, and an unbounded
    # value lets one hung request hold a pipeline run open indefinitely.
    llm_timeout_seconds: float = Field(default=120.0, gt=0.0, le=600.0)

    # Groq. `groq_api_key` falls back to `llm_api_key` in `get_llm_client`, so
    # an existing single-key `.env` keeps working. The default model is one of
    # the Groq models that supports `response_format: json_schema`, which is
    # what lets `REPORT_SCHEMA` be enforced by the provider rather than only
    # requested in the prompt; a model without that support would degrade
    # Stage 9 to best-effort JSON.
    groq_api_key: str | None = None
    groq_model: str = "openai/gpt-oss-20b"
    groq_api_url: str = "https://api.groq.com/openai/v1"

    # --- Backend API authentication (Phase 11) ---
    # The shared secret callers present as `X-API-Key`. Distinct from
    # `llm_api_key`, which authenticates *us* to an LLM provider.
    # Optional at load time so an unconfigured deployment still starts (and
    # `/health` still answers); `app.api.security.require_api_key` then fails
    # closed and rejects every authenticated request.
    api_key: str | None = None

    @field_validator("trend_max_year")
    @classmethod
    def _reject_inverted_year_window(cls, value: int, info) -> int:
        """An inverted window silently empties every cluster's time series."""
        minimum = info.data.get("trend_min_year")
        if minimum is not None and value < minimum:
            raise ValueError(
                f"trend_max_year {value} must not be below trend_min_year {minimum}"
            )
        return value

    @field_validator("qdrant_collection")
    @classmethod
    def _reject_blank_collection(cls, value: str) -> str:
        """A blank name makes every Qdrant call fail with an opaque 4xx."""
        if not value.strip():
            raise ValueError("qdrant_collection must not be blank")
        return value.strip()

    def gap_weights(self) -> tuple[float, float, float, float, float]:
        """`(w1, w2, w3, w4, w5)` in README §8's order.

        One accessor so the ranking code cannot pair a weight with the wrong
        signal, and so the five stay together if a sixth is ever added.
        """
        return (
            self.gap_weight_mention_frequency,
            self.gap_weight_trend_slope,
            self.gap_weight_solution_density,
            self.gap_weight_citation_impact,
            self.gap_weight_contradiction,
        )

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

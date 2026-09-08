"""Tests for backend/app/core/config.py — centralized pydantic-settings Settings.

Covers D-07 (config centralization) and D-09 (the full 14-variable .env contract).
"""

import pytest
from pydantic import ValidationError

ALL_REQUIRED_ENV = {
    "NEO4J_URI": "bolt://neo4j:7687",
    "NEO4J_USER": "neo4j",
    "NEO4J_PASSWORD": "test_neo4j_pw",
    "QDRANT_URL": "http://qdrant:6333",
    "POSTGRES_HOST": "postgres",
    "POSTGRES_PORT": "5432",
    "POSTGRES_USER": "scholarlens",
    "POSTGRES_PASSWORD": "test_pg_pw",
    "POSTGRES_DB": "scholarlens",
    "PDF_STORAGE_PATH": "/data/pdfs",
    "MAX_UPLOAD_SIZE_MB": "50",
    "UNPAYWALL_EMAIL": "you@example.com",
    "GROBID_URL": "http://grobid:8070",
}


def _set_all_required(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in ALL_REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


def test_settings_loads_all_d09_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings exposes all D-09 fields matching the env values exactly."""
    _set_all_required(monkeypatch)
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "s2-test-key")
    monkeypatch.setenv("LLM_API_KEY", "llm-test-key")

    from app.core.config import Settings

    settings = Settings(_env_file=None)

    assert settings.neo4j_uri == "bolt://neo4j:7687"
    assert settings.neo4j_user == "neo4j"
    assert settings.neo4j_password == "test_neo4j_pw"
    assert settings.qdrant_url == "http://qdrant:6333"
    assert settings.postgres_host == "postgres"
    assert settings.postgres_port == 5432
    assert isinstance(settings.postgres_port, int)
    assert settings.postgres_user == "scholarlens"
    assert settings.postgres_password == "test_pg_pw"
    assert settings.postgres_db == "scholarlens"
    assert settings.pdf_storage_path == "/data/pdfs"
    assert settings.max_upload_size_mb == 50
    assert isinstance(settings.max_upload_size_mb, int)
    assert settings.unpaywall_email == "you@example.com"
    assert settings.semantic_scholar_api_key == "s2-test-key"
    assert settings.llm_api_key == "llm-test-key"
    assert settings.grobid_url == "http://grobid:8070"


def test_optional_keys_default_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """semantic_scholar_api_key and llm_api_key default to None when unset."""
    _set_all_required(monkeypatch)
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    from app.core.config import Settings

    settings = Settings(_env_file=None)

    assert settings.semantic_scholar_api_key is None
    assert settings.llm_api_key is None


def test_postgres_url_computed_property(monkeypatch: pytest.MonkeyPatch) -> None:
    """postgres_url builds a valid SQLAlchemy psycopg2 connection string."""
    _set_all_required(monkeypatch)

    from app.core.config import Settings

    settings = Settings(_env_file=None)

    assert (
        settings.postgres_url
        == "postgresql+psycopg2://scholarlens:test_pg_pw@postgres:5432/scholarlens"
    )


def test_get_settings_returns_settings_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_settings() is the module-level accessor other modules should import."""
    _set_all_required(monkeypatch)

    from app.core.config import Settings, get_settings

    result = get_settings()

    assert isinstance(result, Settings)


def test_api_key_loads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """API_KEY is the Phase 11 backend authentication credential."""
    _set_all_required(monkeypatch)
    monkeypatch.setenv("API_KEY", "backend-api-key")

    from app.core.config import Settings

    settings = Settings(_env_file=None)

    assert settings.api_key == "backend-api-key"


def test_api_key_defaults_to_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset API_KEY must not block startup — the dependency fails closed instead."""
    _set_all_required(monkeypatch)
    monkeypatch.delenv("API_KEY", raising=False)

    from app.core.config import Settings

    settings = Settings(_env_file=None)

    assert settings.api_key is None


def test_api_key_is_distinct_from_the_llm_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API_KEY authenticates callers; LLM_API_KEY authenticates us to a provider."""
    _set_all_required(monkeypatch)
    monkeypatch.setenv("API_KEY", "backend-api-key")
    monkeypatch.setenv("LLM_API_KEY", "provider-key")

    from app.core.config import Settings

    settings = Settings(_env_file=None)

    assert settings.api_key == "backend-api-key"
    assert settings.llm_api_key == "provider-key"


def test_acl_anthology_bib_path_defaults_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """acl_anthology_bib_path is optional — existing .env files need no change."""
    _set_all_required(monkeypatch)
    monkeypatch.delenv("ACL_ANTHOLOGY_BIB_PATH", raising=False)

    from app.core.config import Settings

    settings = Settings(_env_file=None)

    assert settings.acl_anthology_bib_path == "data/acl_anthology_export.bib"


def test_nerre_settings_have_sane_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 4 joint NER+RE knobs live in the central settings module."""
    _set_all_required(monkeypatch)

    from app.core.config import Settings

    settings = Settings(_env_file=None)

    assert settings.nerre_model
    assert 0.0 <= settings.nerre_entity_threshold <= 1.0
    assert 0.0 <= settings.nerre_relation_threshold <= 1.0
    assert settings.nerre_max_chars_per_chunk > 0
    assert settings.nerre_max_chunks_per_paper > 0


def test_nerre_chunk_caps_reject_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zero cap would silently extract nothing for the whole corpus."""
    _set_all_required(monkeypatch)
    monkeypatch.setenv("NERRE_MAX_CHUNKS_PER_PAPER", "0")

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_nerre_thresholds_reject_out_of_range_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A negative threshold would accept every hallucinated entity."""
    _set_all_required(monkeypatch)
    monkeypatch.setenv("NERRE_ENTITY_THRESHOLD", "-1")

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_embedding_settings_have_sane_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Phase 5 SPECTER2 / Qdrant knobs live in the central settings module."""
    _set_all_required(monkeypatch)

    from app.core.config import Settings

    settings = Settings(_env_file=None)

    assert settings.qdrant_collection
    assert settings.embedding_model == "allenai/specter2_base"
    assert settings.embedding_adapter == "allenai/specter2"
    assert settings.embedding_batch_size > 0
    assert settings.embedding_max_tokens > 0
    assert settings.embedding_max_chars > 0
    assert settings.retrieval_top_n > 0


def test_embedding_settings_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_all_required(monkeypatch)
    monkeypatch.setenv("QDRANT_COLLECTION", "scholarlens_evidence")
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "8")
    monkeypatch.setenv("RETRIEVAL_TOP_N", "20")

    from app.core.config import Settings

    settings = Settings(_env_file=None)

    assert settings.qdrant_collection == "scholarlens_evidence"
    assert settings.embedding_batch_size == 8
    assert settings.retrieval_top_n == 20


def test_phase_10_settings_have_bounded_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_all_required(monkeypatch)

    from app.core.config import Settings

    settings = Settings(_env_file=None)

    assert settings.retrieval_round_cap == 3
    assert settings.retrieval_min_chunks >= 1
    assert settings.retrieval_min_papers >= 1
    assert 0.0 <= settings.retrieval_min_score <= 1.0
    assert settings.reranker_model == "cross-encoder/ms-marco-MiniLM-L-6-v2"
    assert settings.reranker_batch_size >= 1
    assert settings.grounding_regeneration_cap == 1


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("RETRIEVAL_ROUND_CAP", "0"),
        ("RETRIEVAL_ROUND_CAP", "4"),
        ("RETRIEVAL_MIN_CHUNKS", "0"),
        ("RETRIEVAL_MIN_PAPERS", "0"),
        ("RETRIEVAL_MIN_SCORE", "-0.1"),
        ("RETRIEVAL_MIN_SCORE", "1.1"),
        ("RERANKER_BATCH_SIZE", "0"),
        ("GROUNDING_REGENERATION_CAP", "2"),
    ],
)
def test_phase_10_bounds_reject_runaway_or_empty_values(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    _set_all_required(monkeypatch)
    monkeypatch.setenv(variable, value)

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("EMBEDDING_BATCH_SIZE", "0"),
        ("EMBEDDING_BATCH_SIZE", "-4"),
        ("EMBEDDING_MAX_TOKENS", "0"),
        ("EMBEDDING_MAX_CHARS", "0"),
        ("RETRIEVAL_TOP_N", "0"),
    ],
)
def test_embedding_caps_reject_non_positive_values(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    """A zero batch size or top-N would index or return nothing, silently."""
    _set_all_required(monkeypatch)
    monkeypatch.setenv(variable, value)

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("EMBEDDING_BATCH_SIZE", "100000"),
        ("EMBEDDING_MAX_TOKENS", "100000"),
        ("RETRIEVAL_TOP_N", "100000"),
    ],
)
def test_embedding_caps_reject_unbounded_values(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    """An unbounded batch or token budget is an OOM waiting to happen."""
    _set_all_required(monkeypatch)
    monkeypatch.setenv(variable, value)

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_blank_qdrant_collection_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty collection name would make every Qdrant call fail obscurely."""
    _set_all_required(monkeypatch)
    monkeypatch.setenv("QDRANT_COLLECTION", "   ")

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_embedding_model_revision_is_unpinned_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opt-in reproducibility pin; unset tracks the model repo's main branch."""
    _set_all_required(monkeypatch)

    from app.core.config import Settings

    assert Settings(_env_file=None).embedding_model_revision is None

    monkeypatch.setenv(
        "EMBEDDING_MODEL_REVISION", "3447645e1def9117997203454fa4495937bfbd83"
    )
    assert (
        Settings(_env_file=None).embedding_model_revision
        == "3447645e1def9117997203454fa4495937bfbd83"
    )


def test_clustering_settings_have_sane_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Phase 6 HDBSCAN / DeBERTa-MNLI knobs live in the central settings module."""
    _set_all_required(monkeypatch)

    from app.core.config import Settings

    settings = Settings(_env_file=None)

    assert settings.clustering_min_cluster_size >= 2
    # CLUSTER-01: there is deliberately no cluster-count setting — HDBSCAN
    # discovers the number of themes from the embedding space.
    assert not hasattr(settings, "clustering_n_clusters")
    assert settings.clustering_min_samples is None
    assert settings.clustering_max_statements > 0
    assert settings.nli_model == "microsoft/deberta-base-mnli"
    assert settings.nli_model_revision is None
    assert settings.nli_batch_size > 0
    assert settings.nli_max_tokens > 0
    assert settings.nli_max_chars > 0
    assert settings.nli_max_pairs_per_cluster > 0
    assert 0.0 <= settings.contradiction_threshold <= 1.0


def test_clustering_settings_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_all_required(monkeypatch)
    monkeypatch.setenv("CLUSTERING_MIN_CLUSTER_SIZE", "5")
    monkeypatch.setenv("CLUSTERING_MIN_SAMPLES", "2")
    monkeypatch.setenv("NLI_MODEL", "microsoft/deberta-large-mnli")
    monkeypatch.setenv("NLI_MAX_PAIRS_PER_CLUSTER", "120")
    monkeypatch.setenv("CONTRADICTION_THRESHOLD", "0.85")

    from app.core.config import Settings

    settings = Settings(_env_file=None)

    assert settings.clustering_min_cluster_size == 5
    assert settings.clustering_min_samples == 2
    assert settings.nli_model == "microsoft/deberta-large-mnli"
    assert settings.nli_max_pairs_per_cluster == 120
    assert settings.contradiction_threshold == 0.85


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        # 1 would make every point its own "cluster"; HDBSCAN's floor is 2.
        ("CLUSTERING_MIN_CLUSTER_SIZE", "1"),
        ("CLUSTERING_MIN_CLUSTER_SIZE", "0"),
        ("CLUSTERING_MIN_SAMPLES", "0"),
        ("CLUSTERING_MAX_STATEMENTS", "0"),
        ("NLI_BATCH_SIZE", "0"),
        ("NLI_MAX_TOKENS", "0"),
        ("NLI_MAX_CHARS", "0"),
        ("NLI_MAX_PAIRS_PER_CLUSTER", "0"),
        ("CONTRADICTION_THRESHOLD", "-0.1"),
    ],
)
def test_clustering_caps_reject_non_positive_values(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    _set_all_required(monkeypatch)
    monkeypatch.setenv(variable, value)

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("CLUSTERING_MAX_STATEMENTS", "10000000"),
        ("NLI_BATCH_SIZE", "100000"),
        ("NLI_MAX_TOKENS", "100000"),
        ("NLI_MAX_PAIRS_PER_CLUSTER", "1000000"),
        # A threshold above 1 can never be met, silently disabling CLUSTER-02.
        ("CONTRADICTION_THRESHOLD", "1.5"),
    ],
)
def test_clustering_caps_reject_unbounded_values(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    """Pairwise NLI is O(n^2) forward passes; every knob is bounded above."""
    _set_all_required(monkeypatch)
    monkeypatch.setenv(variable, value)

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_trend_year_window_has_bounded_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stage 7 zero-fills interior years, so the window bounds the series size."""
    _set_all_required(monkeypatch)

    from app.core.config import Settings

    settings = Settings(_env_file=None)

    assert settings.trend_min_year == 1900
    assert settings.trend_max_year == 2100
    assert settings.trend_min_year < settings.trend_max_year


def test_trend_year_window_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_all_required(monkeypatch)
    monkeypatch.setenv("TREND_MIN_YEAR", "1990")
    monkeypatch.setenv("TREND_MAX_YEAR", "2050")

    from app.core.config import Settings

    settings = Settings(_env_file=None)

    assert settings.trend_min_year == 1990
    assert settings.trend_max_year == 2050


def test_trend_window_rejects_an_inverted_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inverted window would silently empty every cluster's time series."""
    _set_all_required(monkeypatch)
    monkeypatch.setenv("TREND_MIN_YEAR", "2030")
    monkeypatch.setenv("TREND_MAX_YEAR", "2020")

    from app.core.config import Settings

    with pytest.raises(ValidationError, match="trend_max_year"):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("TREND_MIN_YEAR", "0"),
        ("TREND_MIN_YEAR", "9999"),
        ("TREND_MAX_YEAR", "0"),
        ("TREND_MAX_YEAR", "9999"),
    ],
)
def test_trend_window_rejects_implausible_years(
    monkeypatch: pytest.MonkeyPatch, variable: str, value: str
) -> None:
    _set_all_required(monkeypatch)
    monkeypatch.setenv(variable, value)

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


# ---------------------------------------------------------------------------
# Gap-ranking weights (Phase 8, GAPRANK-01)
# ---------------------------------------------------------------------------

GAP_WEIGHT_VARIABLES = (
    "GAP_WEIGHT_MENTION_FREQUENCY",
    "GAP_WEIGHT_TREND_SLOPE",
    "GAP_WEIGHT_SOLUTION_DENSITY",
    "GAP_WEIGHT_CITATION_IMPACT",
    "GAP_WEIGHT_CONTRADICTION",
)


def test_gap_weights_have_documented_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """README §8 fixes the formula; the weights are the only knob."""
    _set_all_required(monkeypatch)

    from app.core.config import Settings

    settings = Settings(_env_file=None)

    assert settings.gap_weight_mention_frequency == 1.0
    assert settings.gap_weight_trend_slope == 3.0
    assert settings.gap_weight_solution_density == 25.0
    assert settings.gap_weight_citation_impact == 0.02
    assert settings.gap_weight_contradiction == 5.0


def test_gap_weights_are_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_all_required(monkeypatch)
    for variable in GAP_WEIGHT_VARIABLES:
        monkeypatch.setenv(variable, "2.5")

    from app.core.config import Settings

    settings = Settings(_env_file=None)

    assert settings.gap_weights() == (2.5, 2.5, 2.5, 2.5, 2.5)


@pytest.mark.parametrize("variable", GAP_WEIGHT_VARIABLES)
def test_a_negative_gap_weight_is_rejected(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """The formula's own signs carry each term's direction.

    `solution_density` is subtracted; a negative weight would silently turn the
    "this is already being solved" term into a bonus.
    """
    _set_all_required(monkeypatch)
    monkeypatch.setenv(variable, "-1")

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_zero_weights_are_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switching a signal off is a legitimate configuration, not an error."""
    _set_all_required(monkeypatch)
    for variable in GAP_WEIGHT_VARIABLES:
        monkeypatch.setenv(variable, "0")

    from app.core.config import Settings

    assert Settings(_env_file=None).gap_weights() == (0.0, 0.0, 0.0, 0.0, 0.0)


REPORT_CAP_VARIABLES = ("REPORT_TOP_K", "REPORT_EVIDENCE_TOP_N")


def test_llm_provider_settings_have_sane_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`app/explanation/llm.py` reads every one of these off `Settings`."""
    _set_all_required(monkeypatch)

    from app.core.config import Settings

    settings = Settings(_env_file=None)

    # Groq is the only supported provider. The key stays unset by default so an
    # unconfigured deployment still starts and fails loudly at call time.
    assert settings.llm_provider == "groq"
    assert settings.groq_model
    assert settings.groq_api_url.startswith("https://")
    assert settings.groq_api_key is None
    assert settings.llm_timeout_seconds == 120.0


def test_llm_provider_settings_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_all_required(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "secret")
    monkeypatch.setenv("GROQ_MODEL", "groq-test")
    monkeypatch.setenv("GROQ_API_URL", "https://api.groq.example/openai/v1")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "30")

    from app.core.config import Settings

    settings = Settings(_env_file=None)

    assert settings.llm_provider == "groq"
    assert settings.groq_api_key == "secret"
    assert settings.groq_model == "groq-test"
    assert settings.groq_api_url == "https://api.groq.example/openai/v1"
    assert settings.llm_timeout_seconds == 30.0


def test_report_settings_have_sane_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_all_required(monkeypatch)

    from app.core.config import Settings

    settings = Settings(_env_file=None)

    assert settings.report_top_k == 5
    assert settings.report_evidence_top_n == 8


@pytest.mark.parametrize("variable", REPORT_CAP_VARIABLES)
def test_report_caps_reject_non_positive_values(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """Zero would explain nothing / ground nothing, silently."""
    _set_all_required(monkeypatch)
    monkeypatch.setenv(variable, "0")

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize("variable", REPORT_CAP_VARIABLES)
def test_report_caps_reject_unbounded_values(
    monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    """Each report is one LLM call; the evidence is its whole prompt budget."""
    _set_all_required(monkeypatch)
    monkeypatch.setenv(variable, "100000")

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_llm_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_all_required(monkeypatch)
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "0")

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None)

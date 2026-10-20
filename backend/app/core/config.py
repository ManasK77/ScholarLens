"""Centralized application settings (D-07).

Every module that needs a connection URI, credential, or config value reads
it from here via `get_settings()` — never via a scattered `os.environ` /
`os.getenv` call elsewhere in the codebase (CLAUDE.md config-centralization
rule).

Values are loaded from the process environment, falling back to a `.env`
file (D-09 documents the full variable contract in `.env.example`).
"""

from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings sourced from environment variables / `.env`."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

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
    # Per-source caps: each automatic discovery source adds at most this many
    # distinct papers to the corpus per /collect run. 10 is a hard ceiling —
    # config may lower a cap, never raise it. Manual uploads are not capped.
    #
    # Papers requested from arXiv per /collect. The `arxiv` client's page size
    # follows this, so the request asks for what is kept rather than the
    # library's default page of 100.
    arxiv_max_results: int = Field(default=10, gt=0, le=10)
    # Retries of a 429/5xx arXiv answer, made by ScholarLens itself: the arxiv
    # library's own retry layer is switched off so no 429 is retried twice.
    # A 429 carrying no Retry-After is not retried at all (D-429-03) — arXiv
    # sends none, and retrying a block it never dated only adds requests — so
    # this budget applies to 5xx, transport failures, and dated 429s.
    arxiv_num_retries: int = Field(default=2, ge=0, le=3)
    # Minimum spacing between any two arXiv API requests this process makes.
    # arXiv's API Terms of Use ask for at most one request every three seconds,
    # so that is the floor; the default leaves headroom.
    arxiv_min_interval_seconds: float = Field(default=5.0, ge=3.0, le=60.0)
    # The wait before retry n is max(Retry-After, base * 2**n). A wait above
    # the max ends retrying rather than being cut short.
    arxiv_backoff_base_seconds: float = Field(default=10.0, gt=0.0, le=120.0)
    arxiv_backoff_max_seconds: float = Field(default=60.0, gt=0.0, le=600.0)
    # Operator contact (an email or URL) sent in the arXiv User-Agent, as arXiv
    # asks of automated clients. Optional.
    arxiv_contact: str | None = None
    # Sent as Semantic Scholar's search `limit`, which otherwise defaults to 100.
    semantic_scholar_max_results: int = Field(default=10, gt=0, le=10)
    # Semantic Scholar's candidate pool (D-S1-RETRIEVAL-01, 2026-10-12): the
    # search is paged until this many research records are kept (records
    # dropped as non-research by `publicationTypes` are backfilled from later
    # pages), the result set is exhausted, or the page budget below runs out.
    # Separate from the admission cap above: the pool is what the relevance
    # gate judges, the cap what may become new papers. 1000 is the API's
    # ceiling for relevance search (offset + limit <= 1000). The 2026-10-12
    # audit found every gate-passing record within the first 24 of 16,451
    # matches and none in ranks 41-100, so 100 is ample for this topic.
    semantic_scholar_request_limit: int = Field(default=100, gt=0, le=1000)
    # Records per Semantic Scholar search page; 100 is the API's maximum.
    semantic_scholar_page_size: int = Field(default=100, gt=0, le=100)
    # Hard stop on pages per run, whatever the pool: the pool's own pages plus
    # one page of backfill by default.
    semantic_scholar_max_pages: int = Field(default=3, gt=0, le=10)
    # ACL Anthology is searched in the local export, which can match hundreds
    # of records; at most this many distinct papers (and their PDFs) are
    # admitted, highest relevance-gate score first.
    acl_anthology_max_results: int = Field(default=10, gt=0, le=10)

    # --- S1 relevance gate (D-S1-GATE-01) ---
    # Candidates fetched per source before the gate judges them. The admitted
    # cap per source stays `*_max_results` (10); the pool is larger so that
    # rejected candidates do not cost recall.
    #
    # arXiv (D-S1-RETRIEVAL-01): the retrieval ceiling. The search is paged,
    # `arxiv_page_size` results per request, until arXiv's reported total is
    # exhausted or this many results were read; a run that stops at the
    # ceiling is recorded as incomplete. The 2026-10-12 query matched 28
    # papers, so one request covers it.
    arxiv_candidate_pool: int = Field(default=200, gt=0, le=1000)
    arxiv_page_size: int = Field(default=100, gt=0, le=500)
    # ACL's bounds how many local matches are judged.
    acl_anthology_candidate_pool: int = Field(default=100, gt=0, le=1000)
    # The ACL Anthology export cache (D-ACL-CACHE-01). Older than this many
    # days, `/collect` tries to refresh it first and keeps the last known-good
    # file when the refresh fails (the run is then marked stale). 0 refreshes
    # on every run. A refresh is rejected unless it parses to at least
    # `acl_anthology_min_records` records and to no fewer than 90% of the
    # cached file's.
    acl_anthology_max_age_days: int = Field(default=7, ge=0, le=365)
    acl_anthology_min_records: int = Field(default=50_000, ge=1)
    acl_anthology_export_url: str = (
        "https://aclanthology.org/anthology+abstracts.bib.gz"
    )
    # The cross-encoder scoring the topic against a candidate's title and
    # abstract. Its own setting, so a Mode B reranker change cannot move S1's
    # calibrated floor.
    relevance_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    relevance_batch_size: int = Field(default=16, ge=1, le=256)
    # Raw-logit score below which a candidate with every facet evidenced is
    # still rejected (`low_semantic_relevance`). CALIBRATED ON ONE TOPIC ONLY:
    # "multilingual prompt injection", a 50-document hand-labelled pool,
    # 2026-09-24 -- every on-topic document scored >= -0.27 there, and the one
    # facet-complete off-topic document -6.58. Not a validated constant;
    # re-check it for a new topic.
    relevance_semantic_floor: float = -1.0
    # Admitted candidates at or above this score are tier "strong", below it
    # "conceptual". A label for auditing only; it admits and rejects nothing.
    # Calibrated on the same single topic as the floor.
    relevance_strong_threshold: float = 3.5
    # Paper-language check (D-S1-LANG-01): a candidate's abstract must be in
    # the Latin alphabet and in English. Judged on the abstract only; text
    # shorter than the minimums is recorded `unverified`, never rejected.
    # Measured on the 26-paper corpus and the regression set (2026-10-12):
    # every English abstract was 100% Latin, with an English-marker ratio of
    # 0.18-0.29; the French/Turkish test abstracts are below 0.05.
    paper_language_min_latin_fraction: float = Field(default=0.5, ge=0.0, le=1.0)
    paper_language_min_english_ratio: float = Field(default=0.10, ge=0.0, le=1.0)
    paper_language_min_words: int = Field(default=20, ge=1, le=1000)
    paper_language_min_letters: int = Field(default=40, ge=1, le=10_000)
    # Rejected and capped candidates keep at most this many characters of
    # their abstract in `candidate_decisions.evidence["text_audit"]`, for
    # false-negative review; 0 stores none (the hash and length are kept).
    relevance_audit_excerpt_chars: int = Field(default=400, ge=0, le=4000)

    # --- S1 full-text acquisition (D-FULLTEXT-01) ---
    # Every PDF download: per-request timeout, size limit and retries of a
    # timeout / 429 / 5xx answer. A body is stored only when it is a PDF.
    fulltext_timeout_seconds: float = Field(default=30.0, gt=0.0, le=120.0)
    fulltext_max_pdf_mb: int = Field(default=50, gt=0, le=500)
    fulltext_max_retries: int = Field(default=1, ge=0, le=3)
    # Wall-clock budget for the post-discovery full-text step of one
    # /collect; papers it does not reach are recorded `not_attempted`.
    fulltext_step_budget_seconds: float = Field(default=600.0, gt=0.0, le=3600.0)
    # Semantic Scholar's introductory keyed rate is one request per second;
    # the default leaves headroom.
    semantic_scholar_min_interval_seconds: float = Field(default=1.5, ge=1.0, le=60.0)
    # Bounded retry on 429/5xx, the same policy as arXiv's above.
    semantic_scholar_max_retries: int = Field(default=3, ge=0, le=5)
    semantic_scholar_backoff_base_seconds: float = Field(default=2.0, gt=0.0, le=30.0)
    semantic_scholar_backoff_max_seconds: float = Field(default=30.0, gt=0.0, le=300.0)

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
    # Opt in only after the offline parity benchmark passes on your model/CPU.
    nerre_batch_size: int = Field(default=1, ge=1, le=32)

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
    embedding_sort_by_length: bool = False
    # SPECTER2 is a BERT-family encoder: 512 word-piece positions.
    embedding_max_tokens: int = Field(default=512, gt=0, le=512)
    # Cheap pre-tokenizer character guard against a pathological paragraph out
    # of a malformed PDF. Far above what 512 tokens can hold, so it never cuts
    # text the tokenizer would have kept.
    embedding_max_chars: int = Field(default=4000, gt=0, le=100_000)
    # Default `limit` for a semantic query when a caller does not pass one.
    retrieval_top_n: int = Field(default=12, gt=0, le=1000)

    # --- Interactive GraphRAG (Phase 10) ---
    # Hard workflow bound from README §15: initial retrieval plus at most two
    # reformulated retries. It cannot be raised through environment config.
    retrieval_round_cap: int = Field(default=3, ge=1, le=3)
    # Evidence is sufficient only when enough relevant chunks come from enough
    # distinct papers. This prevents one prolific paper from appearing broad.
    retrieval_min_chunks: int = Field(default=2, ge=1, le=100)
    retrieval_min_papers: int = Field(default=2, ge=1, le=100)
    # A question about one paper (app/interactive/intent.py) can be answered by
    # that paper alone; synthesis, comparison and unclassifiable questions keep
    # `retrieval_min_papers`.
    retrieval_min_papers_paper_specific: int = Field(default=1, ge=1, le=100)
    retrieval_min_score: float = Field(default=0.2, ge=0.0, le=1.0)
    # The cross-encoder picks the `retrieval_top_n` passages shown to the model
    # from this many times as many cosine hits; the sufficiency gate and the
    # graph expansion still see only the first `retrieval_top_n`. 1 disables it.
    retrieval_pool_factor: int = Field(default=3, ge=1, le=10)
    # Cross-encoder is instantiated lazily on the first Mode B request because
    # loading it during API startup would make even /health model-dependent.
    reranker_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    reranker_batch_size: int = Field(default=16, ge=1, le=256)
    # One stricter generation attempt is permitted after grounding fails.
    grounding_regeneration_cap: int = Field(default=1, ge=0, le=1)

    # --- Semantic clustering + contradiction detection (Phase 6) ---
    # HDBSCAN discovers the number of themes from the density of the SPECTER2
    # space; there is deliberately no cluster-count setting (CLUSTER-01).
    # `min_cluster_size` is the smallest group that counts as a theme — 2 is
    # HDBSCAN's floor, and a "theme" one paper mentions twice is not a gap.
    clustering_min_cluster_size: int = Field(default=3, ge=2, le=1000)
    # Shortest statement Stage 6 will cluster. Below this a "limitation" out
    # of a PDF is a heading, a caption or a citation fragment ("2023.",
    # "DISCUSSION AND LIMITATIONS", "(Ghosal et al., CVPR 2025)"). Lower than
    # `contradiction_min_claim_words` on purpose: clustering needs enough words
    # to carry a theme, contradiction needs a full assertable claim. Carries
    # the `clustering_` prefix so the pipeline's derived-state fingerprint
    # invalidates S6 when it changes (D-06-13).
    clustering_min_statement_words: int = Field(default=6, gt=0, le=100)
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

    # --- What has to hold before a pair counts as a disagreement -------------
    #
    # Detection used to be "the NLI head said contradiction", which on the
    # 20-paper LLM-security corpus flagged four pairs above P=0.999 whose
    # right-hand side was an ethics disclaimer ("no attack tooling is
    # released") and whose left-hand sides were a recommendation bullet, a
    # related-work description, a future-work proposal and an unrelated
    # limitation. None was a disagreement. These four settings are the gates
    # that make README S8's sentence -- "one paper claims a method works where
    # another says it fails under the same setup" -- something the pipeline
    # actually tests. See app/clustering/claims.py for the calibration.
    #
    # Shorter than this a statement is a heading, a caption or a fragment.
    contradiction_min_claim_words: int = Field(default=8, gt=0, le=100)
    # Shared stemmed content terms before two statements are "about the same
    # setup". At 3 (with the ratio below) this admits 3.8% of the corpus's
    # cross-paper pairs and none of the four false positives, which shared 0-1.
    contradiction_min_shared_terms: int = Field(default=3, gt=0, le=100)
    # ...and that overlap must also be a real share of what either says. Both
    # bounds are needed: long sentences from one field clear the count on
    # shared jargon, and two fragments clear the ratio on a single word.
    contradiction_min_topic_jaccard: float = Field(default=0.12, ge=0.0, le=1.0)
    # NLI is asymmetric. With this on, a pair counts only when *both*
    # directions clear `contradiction_threshold`, which is what separates a
    # disagreement from an ordering artefact. The stored score is still the
    # stronger direction (D-06-05 is unchanged for scoring).
    contradiction_require_both_directions: bool = True
    # Distinct *paper* pairs -- not statement pairs -- that must disagree
    # before a cluster is flagged at all. Cluster 1's four "pairs" were four
    # statements against one, from two papers. Set to 1 to flag a single
    # well-corroborated two-paper disagreement, at the cost of that being the
    # commonest shape a false positive takes.
    contradiction_min_paper_pairs: int = Field(default=2, gt=0, le=100)
    # Distinct paper pairs at which the Stage 8 contradiction signal reaches
    # its full weight. Below this it is graded pro rata, so one borderline
    # disagreement can no longer contribute the whole of `w5` (D-08-10).
    contradiction_full_strength_paper_pairs: int = Field(default=3, gt=0, le=100)

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
    # How many of a cluster label's stemmed terms a paper's contribution must
    # share before that paper counts as working on this theme. Two rather than
    # all of them: requiring every term made the signal 0.00 for every cluster
    # in the corpus, because a label like "prompt attacks injection" appears
    # verbatim in no contribution sentence anywhere (D-08-11).
    gap_solution_min_label_overlap: int = Field(default=2, gt=0, le=20)
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
    # How many passages one paper may contribute to a single report. Stage 9
    # used to take whatever Qdrant ranked highest, which for the corpus's top
    # gap meant 7 of 8 passages from one paper -- a report resting on one
    # study's experiments while five of the six mentioning papers went unquoted
    # (D-09-04).
    report_evidence_per_paper_cap: int = Field(default=3, gt=0, le=100)
    # How much wider than `report_evidence_top_n` the retrieval reaches before
    # the per-paper cap and the cluster-paper preference select from it. With
    # no headroom a cap can only ever *remove* evidence; with it, a crowded-out
    # paper has something to be promoted from.
    report_evidence_oversample: int = Field(default=4, ge=1, le=20)

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

    # --- Groq rate limiting ---
    # Groq meters *tokens* per minute as well as requests per day, and Stage 9
    # sends `report_top_k` generations back to back. A 429 mid-run is therefore
    # routine rather than an outage, and these five knobs are what let a run
    # ride one out instead of failing. They feed `app.ingestion.rate_limit`,
    # the same bounded retry loop the discovery sources use.
    #
    # Retries of a 429/5xx answer, on top of the first attempt. Four covers a
    # per-minute ceiling comfortably; more only lengthens a request that is
    # already going to fail on a daily quota.
    groq_max_retries: int = Field(default=4, ge=0, le=10)
    # First backoff step; each subsequent retry doubles it. Overridden upward
    # whenever Groq sends a longer Retry-After.
    groq_backoff_base_seconds: float = Field(default=2.0, gt=0.0, le=60.0)
    # The longest wait a single request will absorb. Above 60 on purpose: a
    # tokens-per-minute ceiling clears within the minute, so a lower cap would
    # abandon the one refusal most worth waiting out. A daily quota asks for
    # hours, exceeds this, and is reported instead of slept on.
    groq_backoff_max_seconds: float = Field(default=90.0, gt=0.0, le=600.0)
    # Proactive spacing between Groq requests, process-wide. 0 means "send as
    # fast as the work arrives and rely on retries", which is right for a paid
    # tier. On a free tier whose TPM a burst of `report_top_k` reports exceeds,
    # setting this to roughly 60 / (TPM ÷ tokens-per-report) turns 429s into
    # waiting rather than into retries.
    groq_min_interval_seconds: float = Field(default=0.0, ge=0.0, le=300.0)
    # Ceiling on the tokens one generation may produce. The default model
    # reasons before it answers and those hidden tokens are billed against
    # this same cap, so uncapped, a single report's spend against the
    # per-minute ceiling is unbounded. `None` sends no cap.
    #
    # Sized from the baseline (D-09-07), not guessed: the report JSON measured
    # ~500-700 tokens and is ~1.2K worst case (8 cited UUID evidence ids
    # repeated across five id lists); 2048 left the reasoning under ~900, and
    # one of five reports ran out. 4000 leaves ~2.8K for reasoning, while the
    # worst-case prompt (~4K: 8 passages at the 1200-char cap, plus the
    # schema) plus this cap stays within an 8K tokens-per-minute request --
    # Groq sizes a request as prompt + cap and answers 413 when it cannot fit.
    groq_max_completion_tokens: int | None = Field(default=4000, gt=0, le=32768)

    # --- Backend API authentication (Phase 11) ---
    # The shared secret callers present as `X-API-Key`. Distinct from
    # `llm_api_key`, which authenticates *us* to an LLM provider.
    # Optional at load time so an unconfigured deployment still starts (and
    # `/health` still answers); `app.api.security.require_api_key` then fails
    # closed and rejects every authenticated request.
    api_key: str | None = None
    # FastAPI reads a request body before any dependency runs, so these bounds
    # are enforced by `app.api.body_limit` ahead of `require_api_key`: an
    # anonymous caller cannot make the server buffer an unbounded body before
    # being told 401. JSON bodies (`/query`, `/collect`, `/pipeline/run`) are
    # tiny; `/upload` is bounded by `max_upload_files * max_upload_size_mb`.
    max_request_body_kb: int = Field(default=256, gt=0)
    max_upload_files: int = Field(default=20, gt=0)
    # Failed API-key attempts per client address before `429` (sliding
    # window; `app.api.security.AuthFailureThrottle`). Process-local: each
    # worker counts separately. A lockout ends one window after the failures
    # that caused it, so a legitimate user is never locked out for longer.
    auth_failure_limit: int = Field(default=20, gt=0, le=10_000)
    auth_failure_window_seconds: float = Field(default=60.0, gt=0.0, le=3600.0)

    # --- Mode B overall deadline ---
    # Wall-clock bound on one `POST /query`, across retrieval, reranking and
    # every Groq call (generation, grounding check, one regeneration). The
    # live grounded run took 74 s with two Groq calls; the worst path makes
    # four, each allowed `LLM_TIMEOUT_SECONDS` (120 s) plus retries, so
    # without this a request could run for many minutes. 180 s leaves room
    # for the normal path. On expiry the caller gets 504 and no further
    # model call starts; one already in flight is not interrupted.
    query_deadline_seconds: float = Field(default=180.0, gt=0.0, le=1800.0)

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

    @field_validator("reranker_model")
    @classmethod
    def _reject_blank_reranker_model(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reranker_model must not be blank")
        return value.strip()

    @field_validator("arxiv_contact")
    @classmethod
    def _clean_arxiv_contact(cls, value: str | None) -> str | None:
        """Blank means unset; a control character would break the User-Agent header."""
        if value is None or not value.strip():
            return None
        value = value.strip()
        if len(value) > 200 or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError("arxiv_contact must be at most 200 printable characters")
        return value

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

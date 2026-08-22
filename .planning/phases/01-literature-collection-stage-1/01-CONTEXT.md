# Phase 1: Literature Collection (Stage 1) - Context

**Gathered:** 2026-08-22
**Status:** Ready for planning

<domain>
## Phase Boundary

Given a topic string, gather candidate papers from arXiv, Semantic Scholar, and ACL Anthology; enrich DOI-bearing-but-full-text-missing records via Unpaywall; accept manually uploaded PDFs; deduplicate everything into one normalized corpus (PostgreSQL). Expose `/collect` (topic → discovery + enrichment) and `/upload` (manual ingestion). No parsing, extraction, or downstream NLP yet — that's Phase 2 onward. Per README §15 Phase 1 and REQUIREMENTS.md INGEST-01..09.

</domain>

<decisions>
## Implementation Decisions

**Owner directive:** the owner authorized Phase 1 directly ("proceed with Phase 1 now since Phase 0 is complete") under the project's autonomous-execution operating model (CLAUDE.md / README §13.7) — README §13.5's Phase 1 row does not list a discuss step (only "GSD plans — one plan per ingestion source"), so gray areas below are decided by Claude from README/REQUIREMENTS.md/CLAUDE.md, mirroring how Phase 0's CONTEXT.md was produced. No scope creep beyond INGEST-01..09.

### Normalized corpus & persistence
- **D-10:** The `papers` table lives in PostgreSQL via SQLAlchemy (already a Phase-0 runtime dependency), with a plain declarative model. Table columns match README §8 Stage 1 exactly: `id`, `title`, `authors` (JSON array), `year`, `venue`, `doi`, `abstract`, `source`, `pdf_path`, `has_full_text`, plus per-source identifier columns needed for dedup (`arxiv_id`, `acl_id`) and `created_at`/`updated_at`.
- **D-11:** No migration framework (Alembic, etc.) in this phase — `Base.metadata.create_all(engine)` run at app startup is sufficient for a single evolving table at this stage of the project; introducing a migration tool now would be scope creep beyond INGEST-01..09. Revisit if a later phase's schema changes make hand-rolled migration risky.
- **D-12:** PDFs are stored on the local filesystem under `settings.pdf_storage_path` (already defined in Phase 0's `.env` contract), named by a stable identifier (e.g. `{paper_id}.pdf`) — no S3/blob storage, since README's tech stack never introduces one.

### Ingestion sources
- **D-13 (corrected post-research):** arXiv via the official `arxiv` PyPI package v4.0.1 (already named in README §7 tech stack) — query by topic string via `arxiv.Client`/`arxiv.Search`, pull metadata (title, authors, year via `published`, `summary` as abstract, arXiv ID from `entry_id`). **The installed v4.0.1 package has no `download_pdf`/`download_source` helper** (removed in a prior major version) — the PDF is fetched directly via `httpx` against `Result.pdf_url`, the same download pattern used for ACL Anthology and Unpaywall PDFs. See `01-RESEARCH.md`'s Critical Finding.
- **D-14:** Semantic Scholar via `httpx` against the public Graph API (`https://api.semanticscholar.org/graph/v1/paper/search`), requesting `title,authors,year,venue,externalIds,abstract,openAccessPdf` fields. Metadata-only per README — if `openAccessPdf.url` is present, store the URL but do not download through this source (that's what Unpaywall enrichment / a future fetch step is for; keeping this source's job strictly to "metadata + optional OA link" per its README role). `SEMANTIC_SCHOLAR_API_KEY` is sent as the `x-api-key` header when set; unauthenticated (lower rate limit) when absent, exactly as `.env.example` documents.
- **D-15 (resolved post-research):** ACL Anthology via direct parsing of the Anthology's published `anthology+abstracts.bib.gz` export (confirmed live, 42MB gzipped, updated daily) using `bibtexparser` — every corpus field (title/authors/year/venue/DOI/abstract) is directly present in the BibTeX record, and the Anthology ID + PDF URL are derived from the record's `url` field (`{url}.pdf`). This is primary, not fallback: the official `acl-anthology` PyPI package was evaluated and rejected as primary because it pulls ~30 heavy transitive dependencies and downloads ~120MB on first use for what is a simple topic-filter job here; it remains the documented fallback if direct parsing proves insufficient (e.g. needs richer venue metadata than BibTeX carries). Both approaches satisfy README's tech-stack line.
- **D-16:** `httpx` moves from Phase 0's `dev` extras to the main `[project] dependencies` in `backend/pyproject.toml` — Phase 1 needs it at runtime (Semantic Scholar, Unpaywall), not just for FastAPI's TestClient. Its dev-only use from Phase 0 continues to work identically once it's a runtime dependency.
- **D-17:** Unpaywall enrichment runs over corpus rows with a `doi` set and `has_full_text=false`, calling `https://api.unpaywall.org/v2/{doi}?email={UNPAYWALL_EMAIL}`; on a `best_oa_location.url_for_pdf`, download and attach to the existing row (never create a new row) and flip `has_full_text=true`.
- **D-18:** Manual upload (`POST /upload`, multipart form, one or more files) writes each PDF to `pdf_storage_path`, creates a corpus row with `source='manual_upload'`, `has_full_text=true`, and metadata fields left `None` (backfilled by GROBID in Phase 2, per README §8). Enforces `MAX_UPLOAD_SIZE_MB` from Settings; rejects non-PDF content types with 415.

### Deduplication
- **D-19:** `dedup.py` checks, in order, DOI → arXiv ID → ACL Anthology ID → normalized-title+year, against existing corpus rows before inserting a new one. On a match, the existing row is enriched (fields merged, preferring a value that's present over `None`, and preferring a record that already carries a full-text PDF) rather than duplicated. Normalization for the title+year fallback is lowercase + strip punctuation/whitespace.

### API surface
- **D-20:** `/collect` (`POST`, JSON body `{"topic": str}`) runs arXiv + Semantic Scholar + ACL Anthology discovery for the topic, then Unpaywall enrichment over the resulting DOI-bearing gaps, returning the count and IDs of papers now in the corpus for that run (new + enriched).
- **D-21:** `/upload` (`POST`, multipart) is the manual-ingestion path described in D-18.
- **D-22:** Both endpoints live in `app/api/routes_ingestion.py`, included from `app/main.py` alongside the existing health router — no new top-level app changes beyond that.

### Testing strategy for external APIs
- **D-23:** Per Superpowers TDD, all four source clients and `corpus.py`/`dedup.py` are unit-tested against mocked HTTP responses (`httpx`'s `MockTransport` / `respx`-style local fixtures, or `unittest.mock` for the `arxiv` package's client) — no tests make real network calls to arXiv/Semantic Scholar/ACL Anthology/Unpaywall. `respx` is added as a `dev`-only test dependency for this if `httpx` mocking needs it (evaluated during Task 1; skip if plain `unittest.mock` suffices).

### Claude's Discretion
- Exact response-field mapping per source (e.g. which Semantic Scholar JSON keys map to which corpus columns) — implementation detail, not a scope decision.
- Whether ACL Anthology integration uses the `acl-anthology` package or direct file parsing — whichever the phase's research/implementation finds more reliable; either satisfies README's tech-stack line.
- Retry/backoff behavior on transient HTTP errors — standard, conservative defaults (a couple of retries with backoff), not a designed feature.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project spec
- `README.md` §7 (Complete Tech Stack) — arxiv/httpx/ACL Anthology/Unpaywall tech choices
- `README.md` §8 Stage 1 (End-to-End Pipeline — Literature Collection) — full source table, enrichment step, normalized-corpus schema, dedup order, manual-upload metadata backfill note
- `README.md` §11 (Repository Structure) — `app/ingestion/` module layout (`sources/`, `unpaywall.py`, `manual_upload.py`, `dedup.py`, `corpus.py`)
- `README.md` §12 (Environment & Configuration) — `PDF_STORAGE_PATH`, `MAX_UPLOAD_SIZE_MB`, `UNPAYWALL_EMAIL`, `SEMANTIC_SCHOLAR_API_KEY`
- `README.md` §15 Phase 1 entry — Build/Done-when criteria for this phase
- `.planning/REQUIREMENTS.md` §"Ingestion (Phase 1)" — INGEST-01 through INGEST-09, the acceptance bar for this phase

### Operating rules
- `CLAUDE.md` — hard constraint 4 (fixed ingestion sources, IEEE/Springer/ACM prohibited), hard constraint 5 (source-agnostic corpus from Phase 2 on — this phase is what *produces* that source-agnostic corpus), config centralization, TDD requirement

### Prior phase artifacts
- `backend/app/core/config.py` — `Settings`/`get_settings()` this phase's sources read connection/credential values from
- `backend/app/main.py` — FastAPI app instance this phase's new router attaches to

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `backend/app/core/config.py` (Phase 0) — `Settings` already exposes `pdf_storage_path`, `max_upload_size_mb`, `unpaywall_email`, `semantic_scholar_api_key`, plus Postgres connection fields (`postgres_url` computed property, ready for SQLAlchemy engine creation).
- `backend/app/core/health.py`'s `check_postgres` pattern (SQLAlchemy `create_engine` + connection) is the established pattern for this phase's own Postgres access.
- `backend/app/ingestion/` and `backend/app/ingestion/sources/` package directories already exist (empty, scaffolded in Phase 0) per README §11 — this phase fills them in.

### Established Patterns
- Config centralization via `get_settings()` (Phase 0) — every new source client and the corpus writer must read credentials/paths this way, never inline `os.environ`.
- TDD red-green-refactor per task, mirroring Phase 0's `tests/core/test_config.py` / `tests/core/test_health.py` style (arrange env/mocks, assert behavior, one behavior per test).
- `ruff check`/`ruff format` clean before a task is considered done (Phase 0 precedent).

### Integration Points
- New SQLAlchemy models/engine must reuse `Settings.postgres_url` rather than re-deriving a connection string.
- New router(s) attach to the existing `app.main.app` via `app.include_router(...)`, following `routes_health.py`'s pattern exactly.

</code_context>

<specifics>
## Specific Ideas

No UI/UX surface in this phase (backend-only, per README §15 Phase 1 — no frontend work listed). Four external HTTP/package integrations are the specific technical surface; a research pass on their exact request/response mechanics precedes planning to avoid guessing at library APIs mid-implementation.

</specifics>

<deferred>
## Deferred Ideas

- Downloading Semantic Scholar's `openAccessPdf` link directly at discovery time — deferred; Unpaywall enrichment (D-17) is the designated full-text-recovery step per README, and Semantic Scholar's own role is explicitly "metadata-only" (README §8).
- A learned/fuzzy dedup matcher beyond normalized title+year — out of scope; README's dedup order is a fixed, simple priority chain, not a scoring model (that kind of complexity belongs with Phase 8's explicit "no learned ranker in the MVP" precedent).
- Rate-limiting/backoff tuning beyond conservative defaults — revisit only if real-world testing against the actual APIs (not just mocked tests) surfaces throttling problems.

### Reviewed Todos (not folded)
None — no pending todos existed for Phase 1.

</deferred>

---

*Phase: 01-literature-collection-stage-1*
*Context gathered: 2026-08-22*

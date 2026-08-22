---
phase: 01-literature-collection-stage-1
plan: 01
subsystem: backend
tags: [sqlalchemy, corpus, dedup, tdd]

requires: []
provides:
  - "backend/app/ingestion/corpus.py — Paper model (D-10 schema), insert_paper/enrich_paper/rows_missing_fulltext/download_pdf/get_engine/get_session"
  - "backend/app/ingestion/dedup.py — find_existing (DOI->arxiv_id->acl_id->normalized title+year) + merge + normalize_title"
  - "backend/pyproject.toml — httpx promoted to main deps; arxiv, bibtexparser added to main deps; respx added to dev"
  - "backend/app/main.py — startup hook creates tables via Base.metadata.create_all(), no Alembic"
affects: ["01-02 through 01-06 — every remaining Phase 1 plan depends on this contract"]

tech-stack:
  added: ["arxiv>=4.0,<5", "bibtexparser>=1.4,<2", "respx>=0.23,<1 (dev)", "httpx>=0.28,<1 (promoted from dev to main)"]
  patterns:
    - "Source modules return plain dicts; only corpus.py touches SQLAlchemy sessions (source-agnostic contract, CLAUDE.md hard constraint 5)"
    - "download_pdf is a single shared httpx-streaming helper reused by arXiv/ACL Anthology/Unpaywall (no per-source download logic)"
    - "No migration framework — Base.metadata.create_all() at FastAPI startup (D-11)"

key-files:
  created:
    - backend/app/ingestion/corpus.py
    - backend/app/ingestion/dedup.py
    - backend/tests/ingestion/__init__.py
    - backend/tests/ingestion/test_corpus.py
    - backend/tests/ingestion/test_dedup.py
  modified:
    - backend/pyproject.toml
    - backend/app/main.py

key-decisions:
  - "Package-legitimacy checkpoint (arxiv, bibtexparser, httpx main-dep promotion, respx) resolved without an interactive pause per the standing pre-authorization — all four already vetted [OK]/Approved in 01-RESEARCH.md's Package Legitimacy Audit table."
  - "test_insert_paper_requires_source narrowed to pytest.raises(IntegrityError) instead of a blind Exception, per ruff's B017 finding during the lint pass."
  - "get_session() is a plain generator (no lru_cache/connection-pool tuning) — sufficient for this phase; FastAPI's Depends() handles per-request lifecycle."

requirements-completed: [INGEST-01, INGEST-02]

duration: ~35min
completed: 2026-08-22
---

# Phase 1 Plan 01: Corpus + Dedup Shared Contract Summary

**Built the SQLAlchemy-backed `papers` corpus (D-10 schema) and the DOI→arXiv-ID→ACL-ID→normalized-title+year dedup priority chain (D-19) that every Phase 1 ingestion source and Unpaywall enrichment plugs into.**

## Accomplishments

- Wrote `tests/ingestion/test_corpus.py` (8 behaviors) and `tests/ingestion/test_dedup.py` (9 behaviors) first; confirmed RED (`ModuleNotFoundError`) before either module existed.
- Implemented `corpus.py`: `Paper` declarative model with all D-10 columns, `insert_paper`/`enrich_paper`/`rows_missing_fulltext`, a shared `download_pdf` httpx-streaming helper, and `get_engine`/`get_session` (mirrors Phase 0's `connect_timeout=5` pattern from `health.py`).
- Implemented `dedup.py`: `find_existing` (priority-chain matcher, each identifier lookup guarded by a not-None check before querying) and `merge` (prefers present-over-None, prefers `has_full_text=True`).
- Moved `httpx` to main dependencies, added `arxiv`/`bibtexparser` (main) and `respx` (dev) to `pyproject.toml`; package-legitimacy checkpoint auto-resolved (all four pre-audited in RESEARCH.md).
- Extended `app/main.py` with a startup hook calling `Base.metadata.create_all(get_engine())` — no Alembic.
- Full backend suite green: 27/27 (Phase 0's 10 + this plan's 17), `ruff check`/`ruff format --check` clean, config-centralization and no-Alembic verification checks all pass.

## Files Created/Modified

- `backend/app/ingestion/corpus.py`, `backend/app/ingestion/dedup.py` (new)
- `backend/tests/ingestion/__init__.py`, `test_corpus.py`, `test_dedup.py` (new)
- `backend/pyproject.toml`, `backend/app/main.py` (modified)

## Deviations from Plan

None from intent or acceptance criteria.

## Issues Encountered

None. `httpx`/`arxiv` were already present in the environment from Phase 0/research; only `bibtexparser` needed a fresh install.

## Next Phase Readiness

- `corpus.py`/`dedup.py` contract is stable and tested — Plans 01-02 (arXiv), 01-03 (Semantic Scholar), 01-04 (ACL Anthology), 01-05 (Unpaywall) can now build against it independently (Wave 2).

---
*Phase: 01-literature-collection-stage-1*
*Completed: 2026-08-22*

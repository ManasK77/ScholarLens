---
phase: 02-parsing-section-normalization-stage-2
plan: 01
subsystem: backend
tags: [grobid, docker-compose, sqlalchemy, config]

requires: []
provides:
  - "docker-compose.yml — grobid service (grobid/grobid:0.9.1-full), healthy-gated backend dependency"
  - "backend/app/core/config.py — Settings.grobid_url"
  - "backend/app/parsing/storage.py — ParsedDocument (D-28), upsert_parsed_document/get_parsed_document"
  - "backend/pyproject.toml — pymupdf/pdfplumber/transformers/torch(CPU-only)/sentence-transformers"
affects: ["02-02 through 02-05 — every remaining Phase 2 plan depends on this contract"]

tech-stack:
  added: ["pymupdf>=1.28,<2", "pdfplumber>=0.11,<1", "transformers>=4.57,<6", "torch>=2.9,<3 (CPU-only wheel)", "sentence-transformers>=6.0,<7"]
  patterns:
    - "ParsedDocument shares corpus.Base declarative metadata — Base.metadata.create_all() creates papers and parsed_documents together"
    - "torch CPU-only wheel must be installed explicitly (pip install torch --index-url .../whl/cpu) BEFORE the generic pip install -e \".[dev]\", or the default PyPI index resolves a multi-GB CUDA build"

key-files:
  created:
    - backend/app/parsing/storage.py
    - backend/tests/parsing/__init__.py
    - backend/tests/parsing/test_storage.py
  modified:
    - docker-compose.yml
    - .env.example
    - backend/app/core/config.py
    - backend/app/main.py
    - backend/pyproject.toml
    - backend/tests/core/test_config.py
    - backend/tests/core/test_health.py
    - backend/tests/api/test_health.py
    - backend/tests/api/test_routes_ingestion.py

key-decisions:
  - "Package-legitimacy checkpoint (pymupdf, pdfplumber, transformers, torch, sentence-transformers) auto-resolved per standing pre-authorization — all five pre-audited [OK]/Approved in 02-RESEARCH.md."
  - "Real dependency conflict found at install time (not caught by research): sentence-transformers>=6.0 requires transformers>=5.0, but the plan/research had pinned transformers<5. Widened to transformers>=4.57,<6."
  - "grobid_url added as a required Settings field (no default) — every test file with its own REQUIRED_ENV dict (test_health.py, test_routes_ingestion.py) needed GROBID_URL added, plus one direct-kwargs Settings() construction in tests/core/test_health.py."

requirements-completed: [PARSE-01]

duration: ~35min
completed: 2026-08-23
---

# Phase 2 Plan 01: GROBID Infra + ParsedDocument Storage Summary

**Landed the shared infrastructure and storage contract every later Phase 2 plan depends on: the `grobid` Docker Compose service, `Settings.grobid_url`, the five new Phase 2 Python dependencies, and the `ParsedDocument` SQLAlchemy table.**

## Accomplishments

- Task 1: added `grobid` service to `docker-compose.yml` (pinned `0.9.1-full`, healthcheck with a 90s `start_period` for JVM/DL-model cold start), `Settings.grobid_url`, `.env.example` entry, and all five Phase 2 dependencies to `pyproject.toml` (with the CPU-only torch install executed explicitly first, verified via `torch.version.cuda is None`).
- Found and fixed a real resolver conflict during install (not caught by research): `sentence-transformers>=6.0` requires `transformers>=5.0`; widened the constraint.
- Task 2: wrote `tests/parsing/test_storage.py` (4 behaviors) first, confirmed RED, implemented `storage.py`'s `ParsedDocument` model + `upsert_parsed_document`/`get_parsed_document`, confirmed GREEN.
- Full backend suite green: 72/72 (68 pre-existing + 4 new); `ruff check`/`ruff format --check` clean; `docker compose config --quiet` validates cleanly.

## Files Created/Modified

- `backend/app/parsing/storage.py` (new)
- `backend/tests/parsing/__init__.py`, `test_storage.py` (new)
- `docker-compose.yml`, `.env.example`, `backend/app/core/config.py`, `backend/app/main.py`, `backend/pyproject.toml` (modified)
- `backend/tests/core/test_config.py`, `test_health.py`, `backend/tests/api/test_health.py`, `test_routes_ingestion.py` (modified — GROBID_URL added to each file's own required-env fixture)

## Deviations from Plan

- `transformers` version constraint widened from the plan's `<5` to `<6` — a real dependency conflict discovered at install time, not anticipated by research.

## Issues Encountered

- `sentence-transformers`/`transformers` version conflict (see above) — resolved by widening the constraint, re-verified full install succeeds and torch stays CPU-only.

## Next Phase Readiness

- Ready for Plans 02-02 (grobid_client + tei_parser), 02-03 (section_classifier), 02-04 (fallback) to build against this contract in parallel (wave 2) — none share files with each other.

---
*Phase: 02-parsing-section-normalization-stage-2*
*Completed: 2026-08-23*

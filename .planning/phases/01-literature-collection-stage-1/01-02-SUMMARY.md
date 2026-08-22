---
phase: 01-literature-collection-stage-1
plan: 02
subsystem: backend
tags: [arxiv, ingestion, tdd]

requires: ["01-01"]
provides:
  - "backend/app/ingestion/sources/arxiv.py — search_topic(topic) -> list[dict], fetch_pdf(candidate, dest_dir) -> str | None"
affects: ["01-06 — /collect orchestration calls arxiv.search_topic + arxiv.fetch_pdf"]

tech-stack:
  patterns:
    - "PDF fetched via corpus.download_pdf against Result.pdf_url — never a package-level download helper (corrected D-13, arxiv v4.0.1 has no download_pdf/download_source method)"

key-files:
  created:
    - backend/app/ingestion/sources/arxiv.py
    - backend/tests/ingestion/sources/__init__.py
    - backend/tests/ingestion/sources/test_arxiv.py

key-decisions:
  - "search_topic maps arxiv.Result fields directly: title, authors ([a.name for a in r.authors]), year (r.published.year), doi (r.doi, nullable), abstract (r.summary), arxiv_id (r.get_short_id()), pdf_url (r.pdf_url) — venue is always None since arXiv preprints have no venue."
  - "fetch_pdf delegates entirely to corpus.download_pdf; a regression test explicitly asserts no .download_pdf()/.download_source() call is ever made on a Result object, guarding against reintroducing the removed package API."

requirements-completed: [INGEST-03]

duration: ~20min
completed: 2026-08-22
---

# Phase 1 Plan 02: arXiv Ingestion Source Summary

**Built the arXiv source: topic search via the `arxiv` package, metadata mapping to corpus-shaped dicts, and PDF fetch via the shared `corpus.download_pdf` helper (never the package's own download API, which was removed in v4.0.1).**

## Accomplishments

- Wrote `test_arxiv.py` (5 behaviors, including a regression guard against the removed `download_pdf`/`download_source` package methods) first; confirmed RED.
- Implemented `search_topic(topic, max_results=50)` and `fetch_pdf(candidate, dest_dir)` exactly per the corrected D-13.
- All 5 tests pass; full backend suite green; `ruff check`/`ruff format --check` clean.

## Files Created/Modified

- `backend/app/ingestion/sources/arxiv.py` (new)
- `backend/tests/ingestion/sources/__init__.py`, `test_arxiv.py` (new)

## Deviations from Plan

None.

## Next Phase Readiness

Ready for Plan 01-06 to call `arxiv.search_topic`/`arxiv.fetch_pdf` from `/collect`.

---
*Phase: 01-literature-collection-stage-1*
*Completed: 2026-08-22*

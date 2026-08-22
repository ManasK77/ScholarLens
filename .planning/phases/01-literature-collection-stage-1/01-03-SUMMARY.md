---
phase: 01-literature-collection-stage-1
plan: 03
subsystem: backend
tags: [semantic-scholar, ingestion, tdd]

requires: ["01-01"]
provides:
  - "backend/app/ingestion/sources/semantic_scholar.py — search_topic(topic, api_key=None) -> list[dict], metadata-only"
affects: ["01-06 — /collect orchestration calls semantic_scholar.search_topic"]

tech-stack:
  patterns:
    - "httpx.get with transport=httpx.HTTPTransport(retries=2) — conservative retry default, resolves RESEARCH.md Open Question #2 without inventing an unverified rate-limit number"
    - "Metadata-only by design — never calls corpus.download_pdf; a regression test asserts this"

key-files:
  created:
    - backend/app/ingestion/sources/semantic_scholar.py
    - backend/tests/ingestion/sources/test_semantic_scholar.py

key-decisions:
  - "authors mapped via [a['name'] for a in p.get('authors', [])] per Pitfall 4 (author objects are not plain strings in the raw API response)."
  - "x-api-key header only attached when api_key is truthy; module never calls get_settings() itself — caller (Plan 01-06) passes api_key explicitly, keeping this a pure/testable function."

requirements-completed: [INGEST-04]

duration: ~20min
completed: 2026-08-22
---

# Phase 1 Plan 03: Semantic Scholar Ingestion Source Summary

**Built the Semantic Scholar source: topic search against the public Graph API via httpx, metadata-only mapping to corpus-shaped dicts, with a conservative retry default and no PDF download.**

## Accomplishments

- Wrote `test_semantic_scholar.py` (4 behaviors) first; confirmed RED.
- Implemented `search_topic(topic, api_key=None)` per RESEARCH.md's live-verified request/response shape, including the `HTTPTransport(retries=2)` conservative retry addition from the plan-checker fix.
- All 4 tests pass; full backend suite green; `ruff check`/`ruff format --check` clean.

## Files Created/Modified

- `backend/app/ingestion/sources/semantic_scholar.py` (new)
- `backend/tests/ingestion/sources/test_semantic_scholar.py` (new)

## Deviations from Plan

None.

## Next Phase Readiness

Ready for Plan 01-06 to call `semantic_scholar.search_topic` from `/collect`, passing `get_settings().semantic_scholar_api_key`.

---
*Phase: 01-literature-collection-stage-1*
*Completed: 2026-08-22*

---
phase: 01-literature-collection-stage-1
plan: 06
subsystem: backend
tags: [fastapi, manual-upload, orchestration, tdd]

requires: ["01-01", "01-02", "01-03", "01-04", "01-05"]
provides:
  - "backend/app/ingestion/manual_upload.py — save_upload(...) -> dict, raises UploadRejected(status_code, message)"
  - "backend/app/api/routes_ingestion.py — POST /collect, POST /upload"
  - "backend/app/main.py — routes_ingestion.router included alongside routes_health.router"
  - "backend/tests/ingestion/test_no_prohibited_sources.py — permanent INGEST-09 regression gate"
affects: []

tech-stack:
  added: ["python-multipart>=0.0.12,<1 (FastAPI's own required dependency for file/form uploads)"]
  patterns:
    - "_to_paper_dict() is the single place a source's raw candidate dict (which carries non-Paper keys like pdf_url/openaccess_pdf_url) gets mapped onto exactly the Paper column set before dedup/insert — source modules never need to know the corpus schema"
    - "In-memory SQLite + FastAPI TestClient requires poolclass=StaticPool and check_same_thread=False (TestClient dispatches requests on a worker thread; a plain :memory: engine is thread-affined and breaks otherwise)"

key-files:
  created:
    - backend/app/ingestion/manual_upload.py
    - backend/app/api/routes_ingestion.py
    - backend/tests/ingestion/test_manual_upload.py
    - backend/tests/api/test_routes_ingestion.py
    - backend/tests/ingestion/test_no_prohibited_sources.py
  modified:
    - backend/app/main.py
    - backend/pyproject.toml

key-decisions:
  - "Added python-multipart as a new runtime dependency (FastAPI's own officially-documented requirement for UploadFile/Form) without an interactive pause — well-known, unambiguous, official companion package per the standing pre-authorization."
  - "manual_upload.save_upload never derives the on-disk filename from the client-supplied filename — always writes to a server-generated {paper_id}.pdf, verified by a dedicated path-traversal test."
  - "/collect's dedup+merge touches a shared _process_one() helper so the same DOI/arxiv_id/acl_id/title+year priority chain and merge-preference rules apply uniformly across all three discovery sources, not reimplemented per source."
  - "Fixed two implementation bugs found during Task 2 GREEN verification (not plan deviations): (1) source candidate dicts carry keys (pdf_url, openaccess_pdf_url) that aren't Paper columns — corpus.insert_paper would raise TypeError without the _to_paper_dict() mapping step; (2) Path(dest_dir)/paper.id.pdf must use pathlib (not f-string forward-slash concatenation) for correct cross-platform path comparison."

requirements-completed: [INGEST-07, INGEST-08, INGEST-09]

duration: ~50min
completed: 2026-08-22
---

# Phase 1 Plan 06: Manual Upload + /collect and /upload Orchestration Summary

**Wired every Phase 1 ingestion source into the two public endpoints: `/collect` (arXiv + Semantic Scholar + ACL Anthology discovery → dedup → Unpaywall enrichment) and `/upload` (validated PDF → corpus row), closing the phase's Done-when criterion, plus a permanent INGEST-09 regression gate.**

## Accomplishments

- Task 1: wrote `test_manual_upload.py` (4 behaviors) first, confirmed RED, implemented `manual_upload.save_upload` (content-type/size validation, server-generated filename, never client-supplied). Confirmed GREEN.
- Task 2: wrote `test_routes_ingestion.py` (7 behaviors) first, confirmed RED. Implemented `routes_ingestion.py`'s `/collect` (orchestrates all three sources through `dedup.find_existing`/`merge`, then `unpaywall.enrich`, returning `{"count": N, "ids": [...]}`) and `/upload` (multi-file, 415/413 mapping). Wired `app.main` to include the new router. Found and fixed two real bugs during GREEN verification (see Key Decisions) and added `python-multipart` (FastAPI's own required dependency, previously missing). Confirmed GREEN.
- Task 3: wrote `test_no_prohibited_sources.py` — a permanent test walking `app/ingestion/` for IEEE/Springer/ACM references and asserting the source-module set is exactly the four permitted files.
- Full backend suite green: 66/66 (all of Phase 0 + all of Phase 1); `ruff check`/`ruff format --check` clean (two `noqa: B008` comments for FastAPI's required `Depends()`-in-default-arg pattern); all of the plan's verification-block grep checks pass with zero matches for IEEE/Springer/ACM.

## Files Created/Modified

- `backend/app/ingestion/manual_upload.py`, `backend/app/api/routes_ingestion.py` (new)
- `backend/tests/ingestion/test_manual_upload.py`, `backend/tests/api/test_routes_ingestion.py`, `backend/tests/ingestion/test_no_prohibited_sources.py` (new)
- `backend/app/main.py` (routes_ingestion router included), `backend/pyproject.toml` (python-multipart added)

## Deviations from Plan

None from intent or acceptance criteria. Two implementation-level bugs (not plan gaps) were found and fixed during TDD GREEN verification — see Key Decisions.

## Issues Encountered

- In-memory SQLite + FastAPI's `TestClient` requires `poolclass=StaticPool` and `check_same_thread=False`, since `TestClient` dispatches requests on a worker thread and a plain `:memory:` engine is thread-affined by default. Fixed in the test fixture.
- `python-multipart` was missing (FastAPI raises a clear `RuntimeError` at import time without it) — added as a main dependency.

## Next Phase Readiness

- Phase 1 is functionally complete: `/collect` and `/upload` are reachable, tested, and wired into the app. Ready for the phase-closing `/code-review` and Phase 1 completion report.

---
*Phase: 01-literature-collection-stage-1*
*Completed: 2026-08-22*

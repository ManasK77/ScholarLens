---
phase: 01-literature-collection-stage-1
plan: 05
subsystem: backend
tags: [unpaywall, ingestion, tdd]

requires: ["01-01"]
provides:
  - "backend/app/ingestion/unpaywall.py — resolve_oa_pdf(doi, email) -> str | None, enrich(session, email, dest_dir) -> list[int]"
affects: ["01-06 — /collect orchestration calls unpaywall.enrich after discovery"]

tech-stack:
  patterns:
    - "httpx.get with transport=httpx.HTTPTransport(retries=2) — same conservative retry default as Plan 01-03"
    - "status_code != 200 guard runs before any .json() call — Unpaywall's 404 body isn't JSON (Pitfall 2)"
    - "enrich() only calls corpus.enrich_paper, never corpus.insert_paper — Pattern 3, UPDATE not INSERT"

key-files:
  created:
    - backend/app/ingestion/unpaywall.py
    - backend/tests/ingestion/test_unpaywall.py

key-decisions:
  - "resolve_oa_pdf falls back to best_oa_location.url when url_for_pdf is absent but url is present, per RESEARCH.md's live-verified response shape."
  - "enrich() skips (does not raise, does not call enrich_paper) rows where resolve_oa_pdf or download_pdf fails — a failed enrichment attempt leaves the row as-is for a future /collect retry."

requirements-completed: [INGEST-06]

duration: ~20min
completed: 2026-08-22
---

# Phase 1 Plan 05: Unpaywall Enrichment Summary

**Built Unpaywall enrichment: resolves open-access full text for DOI-bearing, full-text-missing corpus rows, correctly distinguishing "no OA copy" (200) from "DOI not found" (non-200), and updates the existing row — never inserts a new one.**

## Accomplishments

- Wrote `test_unpaywall.py` (6 behaviors) first; confirmed RED.
- Implemented `resolve_oa_pdf(doi, email)` and `enrich(session, email, dest_dir)` per D-17 and RESEARCH.md's live-verified response shapes, including the `HTTPTransport(retries=2)` conservative retry addition from the plan-checker fix.
- All 6 tests pass; full backend suite green; `ruff check`/`ruff format --check` clean.

## Files Created/Modified

- `backend/app/ingestion/unpaywall.py` (new)
- `backend/tests/ingestion/test_unpaywall.py` (new)

## Deviations from Plan

None.

## Next Phase Readiness

Ready for Plan 01-06's `/collect` orchestration to call `unpaywall.enrich` after discovery.

---
*Phase: 01-literature-collection-stage-1*
*Completed: 2026-08-22*

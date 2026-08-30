---
phase: 02-parsing-section-normalization-stage-2
plan: 05
completed: 2026-08-30
requirements-completed: [PARSE-04, PARSE-05, PARSE-06]
---

# Plan 05: Pipeline and Metadata Backfill

- Added `parse_and_store`, which never reads source provenance and applies GROBID → fallback → normalize → persist uniformly.
- Added fallback coverage for HTTP failure, malformed XML, and valid TEI with no body sections.
- Added manual-upload header backfill through `corpus.enrich_paper`; recovered DOIs reuse Phase 1's `unpaywall.enrich` path.
- Added `parse_all_pending` for corpus-wide processing of papers missing a `ParsedDocument`.

Verification: 101 default backend tests pass, 2 opt-in SciBERT tests pass, Ruff and `git diff --check` clean. Docker/GROBID live acceptance could not be rerun because Docker is unavailable on this machine; offline tests use real captured GROBID output.

---
phase: 03-scientific-information-extraction-stage-3
plan: 01
completed: 2026-08-30
requirements-completed: [EXTRACT-01, EXTRACT-02]
---

# Phase 3: Scientific Information Extraction Summary

Implemented a hybrid spaCy + SciBERT scientific-information-extraction
pipeline over Phase 2's normalized document JSON.

## Built

- Six canonical statement types: Contribution, Limitation, Future Work,
  Dataset, Model, and Metric.
- spaCy phrase rules and Limitation/Future Work section priors, supporting
  multiple fact types for one exact evidence sentence.
- An abstaining SciBERT semantic fallback for rule-unmatched sentences, with
  cached exemplar vectors and lazy model loading.
- `extracted_facts` SQLAlchemy storage linked to `papers.id`, including exact
  section/paragraph/sentence coordinates, confidence, and extraction method.
- Idempotent per-paper replacement, duplicate suppression, query-by-type, and
  corpus-wide extraction of every `ParsedDocument`.
- Startup registration of the new table through the existing shared metadata.

## TDD and review

- 24 Phase 3 fast tests cover every fact type, representative negatives,
  multi-label statements, semantic acceptance/abstention, provenance,
  replacement, and orchestration.
- 126 default backend tests pass; the explicit real-SciBERT Phase 3 smoke test
  also passes.
- Ruff, formatting, and `git diff --check` pass.
- Review findings fixed: section-index collisions, repeated exemplar embedding,
  and eager SciBERT loading.

## Environment note

The ORM contract is exercised with SQLite in tests. Live PostgreSQL verification
could not be rerun because Docker remains unavailable on this host.


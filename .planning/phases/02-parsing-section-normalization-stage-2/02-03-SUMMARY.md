---
phase: 02-parsing-section-normalization-stage-2
plan: 03
completed: 2026-08-30
requirements-completed: [PARSE-03]
---

# Plan 03: Section Normalization

- Added the seven-label heading classifier with a fast keyword cascade and deterministic cosine-similarity fallback.
- Added a lazily loaded, process-cached mean-pooled `allenai/scibert_scivocab_uncased` embedder.
- Kept real-model tests opt-in; the downloaded CPU model passed both embedding and cache checks.

Tests: fast classifier suite and two explicit SciBERT integration tests green.

---
phase: 02-parsing-section-normalization-stage-2
plan: 02
completed: 2026-08-30
requirements-completed: [PARSE-01]
---

# Plan 02: GROBID Client and TEI Parser

- Added bounded, retrying GROBID full-text/header HTTP clients; full-text requests enable `segmentSentences=1`.
- Added defensive TEI extraction for body sections, paragraphs, sentence tags, acknowledgements, and references.
- Verified against captured GROBID 0.9.1 TEI plus non-200, connection-error, and sparse-XML cases.

Tests: `test_grobid_client.py`, `test_tei_parser.py` green.

---
phase: 02-parsing-section-normalization-stage-2
plan: 04
completed: 2026-08-30
requirements-completed: [PARSE-02]
---

# Plan 04: PDF Fallback

- Added fallback-only sentence splitting.
- Added PyMuPDF-primary and pdfplumber-secondary extraction with a font-size heading heuristic.
- Ensured uniform-font documents degrade to one untitled section and corrupt documents return `None` without escaping parser errors.

Tests: synthetic-PDF fallback suite green; no binary fixture added.

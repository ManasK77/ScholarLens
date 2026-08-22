---
phase: 01-literature-collection-stage-1
plan: 04
subsystem: backend
tags: [acl-anthology, bibtexparser, ingestion, tdd]

requires: ["01-01"]
provides:
  - "backend/app/ingestion/sources/acl_anthology.py — search_topic(bib_path, topic), fetch_pdf(candidate, dest_dir), download_anthology_export(dest_path, force=False)"
  - "backend/app/core/config.py — Settings.acl_anthology_bib_path (defaulted, no required .env change)"
affects: ["01-06 — /collect orchestration calls download_anthology_export then acl_anthology.search_topic/fetch_pdf"]

tech-stack:
  added: []
  patterns:
    - "Direct bibtexparser parsing of anthology+abstracts.bib.gz (D-15 corrected) instead of the official acl-anthology package — lighter dependency footprint for this phase's simple topic-filter job"
    - "Anthology ID/PDF URL: url field's trailing-slash-stripped last path segment (primary), BibTeX cite key entry[\"ID\"] fallback for url-less old-style entries (resolves RESEARCH.md Open Question #1)"
    - "download_anthology_export is a fetch-and-cache no-op when dest_path already exists — /collect never re-downloads the 42MB export per request"

key-files:
  created:
    - backend/app/ingestion/sources/acl_anthology.py
    - backend/tests/ingestion/sources/test_acl_anthology.py
    - backend/tests/fixtures/acl_anthology_sample.bib
  modified:
    - backend/app/core/config.py
    - backend/tests/core/test_config.py
    - .env.example

key-decisions:
  - "Author field whitespace normalized (re.sub(r'\\s+', ' ', ...)) before splitting on ' and ' — bibtexparser preserves the raw newline/indentation of a BibTeX multi-line author continuation verbatim, which a plain '.split(\" and \")' misses."
  - "Fixed a test-fixture mismatch found during implementation: the fixture's martin-2019-tagging ('non-multilingual') and smith-2022-parsing ('excluding multilingual considerations') entries both legitimately contain the substring 'multilingual', so the case-insensitive substring filter correctly matches them too — the test's original expected-IDs set only listed 4 of the 6 actual matches. Corrected the test's assertion, not the implementation, since the substring-match behavior is exactly what the plan specifies (a plain filter, not semantic negation-awareness)."

requirements-completed: [INGEST-05]

duration: ~30min
completed: 2026-08-22
---

# Phase 1 Plan 04: ACL Anthology Ingestion Source Summary

**Built the ACL Anthology source: direct `bibtexparser` parsing of the Anthology's live BibTeX export (primary, not the official heavier package), topic filtering, ID/PDF-URL derivation with an old-style-entry fallback, and a fetch-and-cache export downloader wired to a new defaulted Settings field — closing the plan-checker's "no real backing data in production" gap.**

## Accomplishments

- Task 1: created `tests/fixtures/acl_anthology_sample.bib` (7 entries: LaTeX-braced title, multi-author multi-line continuation, DOI present/absent, topic match/non-match, and an old-style url-less entry `E91-1001`).
- Task 2: implemented `search_topic`/`fetch_pdf` with the `url`-present/`url`-absent branching per corrected D-15; found and fixed a real whitespace-normalization bug in multi-line author parsing during GREEN verification.
- Task 3: implemented `download_anthology_export` (fetch-and-gunzip-and-cache, no-op on existing file) and added `Settings.acl_anthology_bib_path` (defaulted — no required `.env` change) plus a commented `.env.example` entry.
- All 14 tests pass (9 acl_anthology + 5 config, including the two new ones from this plan); full backend suite green (53/53); `ruff check`/`ruff format --check` clean; all plan verification-block checks pass.

## Files Created/Modified

- `backend/app/ingestion/sources/acl_anthology.py` (new)
- `backend/tests/ingestion/sources/test_acl_anthology.py`, `backend/tests/fixtures/acl_anthology_sample.bib` (new — both pre-existed from an earlier partial run, reviewed and one test assertion corrected)
- `backend/app/core/config.py`, `backend/tests/core/test_config.py`, `.env.example` (modified)

## Deviations from Plan

- One test-fixture authoring bug (not a plan/implementation deviation): the fixture's own text unintentionally made 2 more entries match "multilingual" than the test file anticipated. Fixed by correcting the test's expected set to the full, correct 6-entry match — this is what the plan's specified substring-filter behavior actually produces.

## Issues Encountered

- Multi-line BibTeX author continuation (`"A  and\n      B"`) isn't whitespace-collapsed by `bibtexparser` by default — a straightforward `.split(" and ")` silently failed to separate authors. Fixed with a `re.sub(r"\s+", " ", ...)` normalization pass before splitting.

## Next Phase Readiness

Ready for Plan 01-06's `/collect` to call `acl_anthology.download_anthology_export(settings.acl_anthology_bib_path)` then `acl_anthology.search_topic`/`fetch_pdf`.

---
*Phase: 01-literature-collection-stage-1*
*Completed: 2026-08-22*

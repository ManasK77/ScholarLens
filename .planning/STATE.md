---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
status: executing
stopped_at: Phase 2 Plan 02-01 complete (infra/contract — grobid service, Settings.grobid_url, ParsedDocument, new deps installed); Plans 02-02..02-05 not yet executed — checkpointed here due to context budget, not a blocker
last_updated: "2026-08-23T00:45:00+05:30"
last_activity: 2026-08-23 -- Plan 02-01 executed and committed (both tasks); 76/76 backend tests pass; found and fixed a real transformers/sentence-transformers version conflict at install time
progress:
  total_phases: 15
  completed_phases: 2
  total_plans: 11
  completed_plans: 11
  percent: 13
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-08-21)

**Core value:** Every research gap ScholarLens reports is grounded in structured, retrieved evidence extracted before any LLM is involved — the LLM explains gaps already discovered by extraction, clustering, trend analysis, and ranking; it never discovers gaps on its own.
**Current focus:** Phase 2 (Parsing & Section Normalization) — Plan 02-01 done, Plans 02-02 through 02-05 remain

## Current Position

Phase: 2 (Parsing & Section Normalization, Stage 2) — IN PROGRESS
Plan: 1 of 5 executed (02-01 infra/contract DONE; 02-02 grobid_client+tei_parser, 02-03 section_classifier, 02-04 fallback, 02-05 service+backfill remain — all already planned and plan-checker PASSED, just not yet executed)
Status: Resume by running Plans 02-02/02-03/02-04 (wave 2 — independent of each other, no shared files, can run in parallel), then 02-05 (wave 3, depends on all three).
Last activity: 2026-08-23 -- Plan 02-01 executed: docker-compose grobid service (0.9.1-full, healthy-gated), Settings.grobid_url, ParsedDocument SQLAlchemy table (D-28), and all 5 new Phase 2 dependencies installed (torch CPU-only verified via torch.version.cuda is None). Found and fixed a real dependency conflict during install (sentence-transformers>=6.0 needs transformers>=5.0 — not caught by research, which had assumed <5). 76/76 backend tests pass (72 after Plan 02-01 + confirms no regressions). Stopped here on purpose — context budget was getting tight; Wave 2's three plans involve loading the real SciBERT model, PDF fallback parsing, and the GROBID HTTP client — substantial enough to warrant a fresh context window rather than risk running out mid-execution.

Progress: [███░░░░░░░] ~13% (2/15 phases fully complete + Phase 2 1/5 plans done; understates actual progress)

## Performance Metrics

**Velocity:**

- Total plans completed: 10 (4 Phase 0 + 6 Phase 1)
- Average duration: ~30 min
- Total execution time: ~5 hours

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| 0 | 4 | ~2.3h | ~35min |
| 1 | 6 | ~2.7h | ~27min |

**Recent Trend:**

- Last 6 plans: 01-01, 01-02, 01-03, 01-04, 01-05, 01-06
- Trend: steady, with two real defects (not plan gaps) caught by a live smoke test after the mocked TDD suite passed — see 01-06 decisions below

*Updated after each plan completion*

## Accumulated Context

### Decisions

Full decision log lives in PROJECT.md Key Decisions table (16 ADR-locked constraints from CLAUDE.md, all reaffirmed in README.md — no LangChain anywhere, LangGraph only in Phase 10, no loop around Stage 8→9, ingestion sources fixed, source-agnostic corpus from Phase 2 on, LLM comes last, TDD, secrets in `.env` only, config centralized, `/code-review ultra` used exactly once after Phase 14).

Recent decisions affecting current work:

- [Ingest]: README.md is the sole source of truth for phase structure, tech stack, and done-when criteria; CLAUDE.md operating instructions defer to it on any conflict.
- [Ingest]: Roadmap phases (0-14) were copied verbatim from README §15, not re-derived — this is a spec-driven project, not a granularity-driven one.
- [Workflow]: CLAUDE.md and README §13.3/13.4/13.7-13.9 redesigned for autonomous phase execution — once a phase is authorized, Claude executes it end-to-end without per-task approval, pausing only for genuine ambiguity, high-stakes decisions, or owner-only actions. Includes a standing pre-authorization for well-known-package legitimacy checks and new resource-safety/context-efficiency guardrails.
- [00-01..00-04]: Phase 0 decisions — see prior entries in git history / 00-0X-SUMMARY.md files (config centralization, no Alembic, timeout-bounded health checks, package legitimacy pre-authorization pattern established).
- [01-CONTEXT]: Phase 1 has no discuss-step requirement per README §13.5 ("GSD plans — one plan per ingestion source" only) — CONTEXT.md self-decided by Claude from README/REQUIREMENTS.md/CLAUDE.md, mirroring Phase 0's precedent.
- [01-RESEARCH]: Pre-planning research corrected two assumptions before any code was written: the `arxiv` package (v4.0.1) has no PDF-download helper (removed in a prior major version — PDFs fetched via `httpx` against `Result.pdf_url` instead), and ACL Anthology is integrated via direct `bibtexparser` parsing of the live `anthology+abstracts.bib.gz` export (primary) rather than the official `acl-anthology` package (documented fallback — the official package pulls ~30 heavy transitive dependencies and ~120MB on first use for what's a simple topic-filter job here).
- [01-planchecker]: The initial 6-plan set had 2 blockers from `gsd-plan-checker`: ACL Anthology's live export was never wired into `/collect` (fixed — added `download_anthology_export` fetch-and-cache + a defaulted `Settings.acl_anthology_bib_path`), and two RESEARCH.md open questions were silently unaddressed (fixed — old-style ACL-ID cite-key fallback implemented+tested; Semantic Scholar/Unpaywall given a conservative `httpx.HTTPTransport(retries=2)` default instead of guessing an unverified rate limit).
- [01-01]: `corpus.py`/`dedup.py` built first as the shared, source-agnostic contract (Paper model, insert/enrich/query, shared `download_pdf`) — every subsequent source plan built and tested against it in isolation.
- [01-02..01-05]: arXiv, Semantic Scholar, ACL Anthology, and Unpaywall built in parallel (independent plans, no shared files) once the corpus contract landed; each source module returns plain corpus-shaped dicts and never touches the DB directly.
- [01-06]: `routes_ingestion.py`'s `_to_paper_dict()` is the single place a source's raw candidate dict (which carries non-Paper keys like `pdf_url`) gets mapped onto exactly the Paper column set — source modules stay ignorant of the corpus schema.
- [01-06 / live smoke test]: A live smoke test against real Postgres + real external APIs (run after the mocked 66-test suite was fully green) caught two real defects the mocks missed: (1) `httpx.get()`'s module-level shortcut doesn't accept a `transport` kwarg — only `httpx.Client()` does — breaking every real request in Semantic Scholar, Unpaywall, and the ACL Anthology export fetch; fixed by switching all three to a context-managed `httpx.Client(transport=...)`. (2) `corpus.download_pdf`/`manual_upload.save_upload` never ensured their destination directory existed before writing — fixed with `mkdir(parents=True, exist_ok=True)`, matching the pattern `download_anthology_export` already used. Both fixes live-reverified: real arXiv search + real 1MB PDF download succeeded end-to-end; `POST /upload` succeeded against real Postgres.
- [01-06 / environmental note]: The live smoke test hit a real 429 (rate limit) from Semantic Scholar's public unauthenticated API during testing — an external condition, not a code defect. Not retried repeatedly per resource-safety guidance; the source's correctness is covered by its 5 mocked unit tests instead.
- [01-06 / code-review]: `/code-review` on the full Phase 1 diff found 3 more issues, all fixed: `corpus.get_engine()` now cached (`lru_cache`) instead of building a fresh Engine/connection-pool per request; `download_anthology_export` now calls `raise_for_status()` before gzip-decompressing (a non-200 response previously crashed inside `gzip.decompress` with a confusing error); `POST /upload` now reads uploads in bounded 1MB chunks instead of buffering the whole file before the size check ran. A 4th finding (`dedup.merge()`'s has_full_text/pdf_path special-casing) was evaluated and left as-is with a clarifying comment — the two fields are a genuinely coupled pair, not independently generalizable.

- [02-CONTEXT]: Phase 2 has no discuss-step requirement per README §13.5 ("GSD plans with a fresh context per batch" only). CONTEXT.md self-decided D-24 through D-36, covering the GROBID Docker service, parsing pipeline module layout, a single JSON-column `ParsedDocument` table (not 3 relational tables), and — the phase's one real design ambiguity — what "SciBERT rhetorical-role classifier" means given no training data/dataset exists anywhere in the 15-phase roadmap: resolved as keyword-pre-filter + SciBERT-embedding cosine-similarity against the 7 canonical labels, not supervised fine-tuning.
- [02-RESEARCH]: A real GROBID container (`grobid/grobid:0.9.1-crf`, lightweight, torn down after use) was run this session to capture two real TEI-XML fixtures and correct a wrong assumption: GROBID's body `<div>` elements are NOT reliably type-tagged (only back-matter categories like `references`/`acknowledgement` are) — the classifier must classify heading text in effectively all cases, not just as a fallback (corrects D-31). Also found GROBID's `segmentSentences=1` param sentence-segments the primary path itself, narrowing the custom sentence-splitter to the PyMuPDF/pdfplumber fallback path only (narrows D-29). Confirmed no pretrained rhetorical-role classifier exists on HuggingFace as a shortcut (by elimination) — the embedding-similarity design is correct. Pinned `grobid/grobid:0.9.1-full` for the real service (D-24).
- [02-planchecker]: 5-plan set PASSED with 2 non-blocking warnings, both fixed: RESEARCH.md's Open Questions marked `(RESOLVED)`, and Plan 02-01 Task 1's torch install order corrected (CPU-only wheel installed explicitly first, verified via `torch.version.cuda is None`, before the generic `pip install -e ".[dev]"` — previously inconsistent and risked an accidental multi-GB CUDA download).

### Pending Todos

None yet.

### Blockers/Concerns

None currently blocking. Phase 2 Plan 02-01 (wave 1) is done; Plans 02-02/02-03/02-04 (wave 2) and 02-05 (wave 3) remain — stopped here deliberately due to context budget (not an owner-only blocker). Resume by executing Plans 02-02/02-03/02-04.

## Deferred Items

Items acknowledged and carried forward from previous milestone close:

| Category | Item | Status | Deferred At |
|----------|------|--------|-------------|
| Future Extension | FUT-01: Cross-domain gap discovery | Deferred to v2 | Initial roadmap (README §16) |
| Future Extension | FUT-02: Learned research-opportunity ranking | Deferred to v2 | Initial roadmap (README §16) |

Phase 1-specific deferrals (per CONTEXT.md, in scope for a later revisit only if real-world usage demands it):

| Category | Item | Status | Deferred At |
|----------|------|--------|-------------|
| Hardening | Content-type spoofing via magic-byte (`%PDF-`) verification on `/upload` | Accepted risk, not built | 01-06 threat model (T-01-14) |
| Hardening | Rate-limiting/backoff tuning beyond the conservative `HTTPTransport(retries=2)` default | Deferred | 01-CONTEXT.md |
| Scope | A learned/fuzzy dedup matcher beyond normalized title+year | Out of scope | 01-CONTEXT.md |

## Session Continuity

Last session: 2026-08-23T00:45:00+05:30
Stopped at: Plan 02-01 (wave 1, infra/contract) executed and committed — grobid docker-compose service, Settings.grobid_url, ParsedDocument table, new deps installed (76/76 tests pass). Plans 02-02/02-03/02-04 (wave 2) and 02-05 (wave 3) remain, already planned and plan-checker PASSED. Stopped deliberately due to context budget, not a blocker. No owner input needed; just resume and execute.
Resume file: .planning/phases/02-parsing-section-normalization-stage-2/02-02-PLAN.md (start here — wave 2: grobid_client.py + tei_parser.py; 02-03 section_classifier.py and 02-04 fallback.py can run in parallel since none share files)

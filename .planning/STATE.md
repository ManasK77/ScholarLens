---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
status: awaiting_authorization
stopped_at: Phase 11 partially delivered (API-key auth only). Phases 4-10 implementations are absent from the repository — see PROGRESS.md (2026-09-06) for the full reconciliation.
last_updated: "2026-09-06"
last_activity: 2026-09-06 -- Project-state reconciliation after the incomplete upstream push in 478597b. Phase 11 API-key auth landed and verified; routes_backend.router remains unregistered because 12 modules it imports do not exist.
progress:
  total_phases: 15
  # Verified by implementation + passing tests, not by planning-file claims:
  # complete = Phases 0, 1, 2, 3 and 13 (13 is self-contained and was verified independently).
  # Phase 12 is built but blocked; Phase 11 is partial; Phases 4-10 are absent.
  completed_phases: 5
  total_plans: 16
  completed_plans: 16
  percent: 33
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-08-21)

**Core value:** Every research gap ScholarLens reports is grounded in structured, retrieved evidence extracted before any LLM is involved — the LLM explains gaps already discovered by extraction, clustering, trend analysis, and ranking; it never discovers gaps on its own.
**Current focus:** Phases 4-10 must be rebuilt before Phase 11 can be finished. Phase 4 is blocked on an owner model-choice decision.

## Current Position

Phase: 11 (Backend API Integration) — PARTIAL; Phases 4-10 — NOT IMPLEMENTED
Plan: no plan documents exist for Phases 4 through 11
Status: Waiting for owner authorization and for the Phase 4 model-choice decision.
Last activity: 2026-09-06 -- Reconciled the three conflicted planning files against git history and the source tree. Confirmed by exhaustive object-database search that the Phase 4-10 modules exist in no reachable commit, tree, blob, reflog entry, or unreachable object, and that there are zero unreachable commits.

Progress: [███░░░░░░░] 33% (5/15 phases verified complete)

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

- Phases 4-10: rebuild from scratch (not recoverable from git). Phase 4 first, and only after the owner locks the model choice.
- Phase 11: register `routes_backend.router` and finish `/papers`, `/graph`, `/clusters`, `/trends`, `/gaps`, `/reports`, `/query`, `/pipeline/run` once Phases 4-10 exist.
- `backend/pyproject.toml` and `backend/uv.lock` disagree: the lock records `gliner2[local]`, `langgraph`, `pandas`, `scikit-learn` and `numpy`, none of which the committed `pyproject.toml` declares. The lock is a fossil of an uncommitted `pyproject.toml`.

### Blockers/Concerns

**BLOCKER — Phases 4-10 implementations are missing and unrecoverable.** Commit 478597b (authored by Jain Kanish <jainkanish412@gmail.com>, 2026-09-06) added `routes_backend.py`, `ner_re/graph.py`, `embeddings/indexer.py`, `interactive/workflow.py`, the frontend and `evaluation/`, but not the 12 modules those files import. An exhaustive search of all 43 commits, 285 trees, 272 blobs, both refs, the 91-entry reflog and every unreachable object found none of them, and found no unreachable commits. `git stash list` is empty. They must be rebuilt.

**BLOCKER — Phase 4 model choice is unmade.** See "Owner decisions required" below.

Docker is available and healthy on this host as of 2026-09-06 (`neo4j`, `qdrant`, `postgres`, `grobid`, `backend` all reported healthy; `/health` returned 200 with all three service checks true). This supersedes the earlier "Docker unavailable" note.

### Owner decisions required (could not be resolved from repository evidence)

1. **Phase 4 joint NER+RE model.** ROADMAP Phase 4 requires a discuss phase to lock this "before any code". README §7/§15 specify a SciERC-style/DyGIE++ model "or an equivalent pretrained scientific IE model". `uv.lock` shows the lost implementation used `gliner2[local]>=2.0,<3` — a substitution the owner never recorded a decision for.
2. **Phase 6 clustering library.** README §7 and CLUSTER-01 specify HDBSCAN/BERTopic. `uv.lock` contains neither; it has `scikit-learn>=1.6,<2` instead. The lost implementation appears to have deviated here too.
3. **Whether Phases 4-13 were ever authorized.** The two sides of this file disagreed and the repository holds no evidence either way.
4. **Whether Phase 14 is "explicitly deferred by owner"** — asserted by one side of the conflict only; unverifiable here.

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

Last session: 2026-09-06
Stopped at: Project-state reconciliation. Planning-file conflicts resolved from repository evidence; four items above still need an owner decision.
Resume file: README.md §15 Phase 4 — only after explicit owner authorization AND the Phase 4 model-choice decision.

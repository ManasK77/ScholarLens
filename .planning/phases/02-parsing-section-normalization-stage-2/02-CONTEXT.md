# Phase 2: Parsing & Section Normalization (Stage 2) - Context

**Gathered:** 2026-08-22
**Status:** Ready for planning

<domain>
## Phase Boundary

Every paper in the normalized corpus (regardless of `source`) gets parsed into structured sections/paragraphs/sentences via GROBID (primary) or PyMuPDF/pdfplumber (fallback), with inconsistent venue headings normalized onto a common schema (Introduction, Methodology, Results, Discussion, Limitations, Future Work, Conclusion) via a SciBERT-based classifier. Manually uploaded papers with missing metadata get it backfilled from GROBID's header extraction, becoming eligible for the same Semantic Scholar/Unpaywall enrichment as any other paper. Per README §15 Phase 2 and REQUIREMENTS.md PARSE-01..06.

</domain>

<decisions>
## Implementation Decisions

**Owner directive:** the owner authorized Phase 2 directly ("Start Phase 2"). README §13.5's Phase 2 row does not list a discuss step (only "GSD plans with a fresh context per batch"), so gray areas below are decided by Claude from README/REQUIREMENTS.md/CLAUDE.md, mirroring how Phase 0 and Phase 1's CONTEXT.md were produced. No scope creep beyond PARSE-01..06.

### GROBID service
- **D-24 (pinned post-research):** GROBID is added to `docker-compose.yml` as a new `grobid` service using the official `grobid/grobid:0.9.1-full` Docker image (current stable per GROBID's own docs as of this research; the canonical, actively-maintained image published by the GROBID project itself — confirmed well-known per the standing package-legitimacy pre-authorization), exposing its REST API on port 8070, with a generous healthcheck `start_period` (large ML models load in-container on startup). No GPU/CRF-only lightweight variant is specified by README, so the full image is used in the real service — the lighter `-crf` variant was used only for this phase's own research/fixture-capture, per resource-safety guidance, and is not what ships.
- **D-25:** The backend talks to GROBID over HTTP via its `/api/processFulltextDocument` endpoint (the standard full-text TEI-XML extraction endpoint), using `httpx` (already a project dependency) — no dedicated GROBID Python client library, since the REST call surface needed here is narrow (one endpoint) and README doesn't name a specific client package.
- **D-26:** `GROBID_URL` is added to Settings/`.env.example` (e.g. `http://grobid:8070` in-container, following the same pattern as `QDRANT_URL`), read via `get_settings()` — never inline.

### Parsing pipeline structure
- **D-27:** `app/parsing/grobid_client.py` wraps the GROBID HTTP call (send PDF bytes, get back TEI-XML); `app/parsing/tei_parser.py` extracts sections/paragraphs/sentences from the TEI-XML into a structured schema; `app/parsing/fallback.py` wraps PyMuPDF/pdfplumber for when GROBID fails or is unreachable; `app/parsing/section_classifier.py` implements the heading-normalization step; `app/parsing/service.py` orchestrates: try GROBID → on failure/timeout, fall back → normalize sections → persist.
- **D-28:** Parsed output is stored in PostgreSQL as new tables (`sections`, `paragraphs`, `sentences`, or a single `parsed_documents` JSON-column table) linked to `papers.id` via a `paper_id` foreign key — mirroring Phase 1's SQLAlchemy declarative-model pattern in `corpus.py`. A single `ParsedDocument` table storing the normalized structure as JSON (one row per paper, `sections: list[{type, heading, paragraphs: list[{text, sentences: list[str]}]}]`) is simpler than three fully-normalized relational tables and is sufficient for what Phase 3 (extraction) needs to read — avoids premature relational modeling for structure that's read as a whole document at a time, not queried column-by-column.
- **D-29 (narrowed post-research):** GROBID's `/api/processFulltextDocument` call sets `segmentSentences=1`, which makes GROBID itself return sentence-segmented TEI (`<s>` tags inside each `<p>`) — so the GROBID-success path needs no separate sentence tokenizer. A lightweight sentence splitter (regex-based, or `nltk` punkt) is only needed for the PyMuPDF/pdfplumber **fallback** path, where no such structure exists.

### Section-heading normalization (the phase's one real design ambiguity)
- **D-30 (resolved via research):** README names "SciBERT (rhetorical-role classifier)" without specifying training data, a labeled dataset, or a training phase anywhere in the project's 15 phases — there is no "train a classifier" step in Requirements or Roadmap. Per Claude's discretion (consistent with the project's "no learned ranker in the MVP" precedent for Phase 8), this is implemented as **embedding-based semantic similarity**, not supervised fine-tuning: each paper's raw heading text is embedded with SciBERT (or a suitable sentence-embedding model) and matched via cosine similarity against embeddings of the seven canonical category labels (+ short exemplar phrases per category, e.g. "Related Work"/"Prior Work" → Introduction-adjacent, "Threats to Validity" → Discussion), assigning the closest match. A cheap exact/near-exact keyword pre-filter (e.g. "introduction" → Introduction directly) short-circuits the embedding call for the common case, keeping the heavy model path only for genuinely inconsistent headings. This is the concrete meaning of "SciBERT-based rhetorical-role classifier" for this MVP — the phase's research pass should confirm whether a pretrained rhetorical-role/discourse-tagging model already exists on HuggingFace that fits better than raw SciBERT embeddings, and use it if so; otherwise raw SciBERT (or a lighter sentence-embedding model, if research finds one better suited and still satisfying README's "SciBERT-based" naming in spirit) is the fallback.
- **D-31 (corrected post-research):** A real GROBID run (this session) shows body `<div>` elements are **not** reliably tagged with a section-semantic `type` attribute (e.g. `type="introduction"`) — only a narrow, fixed set of back-matter categories (`acknowledgement`, `references`, `annex`, `availability`, `conflict`, `contribution`, `funding`) get one. For the seven canonical body categories, the classifier has no GROBID-provided shortcut and must classify the `<head>` heading text (and optionally first-paragraph content) in effectively every case — this is not merely a fallback path, it's the primary path. The keyword-pre-filter → SciBERT-embedding-similarity cascade (D-30) does more of the actual classification work than originally assumed, but the design itself is unchanged.

### Manual-upload metadata backfill
- **D-32:** GROBID's `/api/processHeaderDocument` endpoint (or equivalently, header fields already present in the full-text TEI from D-25) extracts `title`/`authors`/`DOI` for `source='manual_upload'` records with `title IS NULL`. Backfilled fields are written via `corpus.enrich_paper` (never a new insert) — the same UPDATE-only pattern Phase 1's Unpaywall enrichment already established.
- **D-33:** After backfill, if a DOI was recovered, the record is eligible for the same Semantic Scholar/Unpaywall enrichment path Phase 1 built — Phase 2 triggers this by calling Phase 1's existing `unpaywall.enrich`-equivalent logic (or, more simply, the same `rows_missing_fulltext`/DOI-bearing-row query already used) rather than re-implementing enrichment; this satisfies PARSE-06 by reusing Phase 1's contract instead of duplicating it.

### Fallback and error handling
- **D-34:** GROBID failure modes this phase handles: connection error/timeout (service unreachable), and a non-200/malformed-TEI response (GROBID ran but couldn't parse this specific PDF cleanly) — both trigger the PyMuPDF/pdfplumber fallback path per README's explicit "PyMuPDF/pdfplumber is the fallback parser for documents GROBID can't handle cleanly." The fallback produces a best-effort section structure (e.g. treating the whole extracted text as a single "Introduction"-equivalent or heuristically splitting on all-caps/short lines as headings) — a materially lower-fidelity result than GROBID's, which is expected and acceptable per README's own framing of it as a fallback, not a co-primary parser.
- **D-35:** A conservative retry (matching Phase 1's `httpx.HTTPTransport(retries=2)` precedent) is used for the GROBID HTTP call before falling back — GROBID startup/first-request latency can be substantial (large ML models loaded in-container), so the client also uses a generous timeout (not the 5-10s used for Phase 1's lightweight metadata APIs).

### Testing strategy
- **D-36:** TDD per CLAUDE.md — all GROBID/TEI-XML parsing logic is unit-tested against small, hand-crafted or real-but-small TEI-XML fixture files (captured once from a real GROBID call against a small test PDF, per the plan's own research step, then used as an offline fixture) — no test makes a real network call to a live GROBID container (mirrors Phase 1's D-23 no-real-network-calls rule). The classifier's embedding-similarity logic is tested with mocked/small embedding vectors, not by loading the full SciBERT model in every test run (keeps the unit suite fast; a small number of tests may load the real model if a plan's task explicitly calls for an integration-level check, kept separate from the main fast suite).

### Claude's Discretion
- Exact GROBID Docker image tag/version — pin to a specific recent stable release rather than floating `latest`, consistent with README's specificity elsewhere.
- Whether the section classifier uses raw `AutoModel` SciBERT embeddings via `transformers` + `torch`, or a lighter `sentence-transformers` wrapper — whichever the phase's research finds more practical; either satisfies "SciBERT-based" in spirit since both are transformer embedding approaches, and `sentence-transformers` can still be configured to use a SciBERT checkpoint.
- Exact fallback heuristic for PyMuPDF/pdfplumber section detection — a reasonable best-effort heuristic, not a research problem in itself.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project spec
- `README.md` §7 (Complete Tech Stack) — GROBID/PyMuPDF/pdfplumber/SciBERT tech choices
- `README.md` §8 Stage 2 (End-to-End Pipeline — Parsing & Section Normalization) — merged-stage rationale, fallback role, manual-upload metadata-backfill note
- `README.md` §11 (Repository Structure) — `app/parsing/` module layout
- `README.md` §12 (Environment & Configuration) — services orchestrated via Docker Compose (grobid joins here)
- `README.md` §15 Phase 2 entry — Build/Done-when criteria for this phase
- `.planning/REQUIREMENTS.md` §"Parsing (Phase 2)" — PARSE-01 through PARSE-06, the acceptance bar for this phase

### Operating rules
- `CLAUDE.md` — hard constraint 5 (source-agnostic from Phase 2 on — this phase must never branch on `source`), config centralization, TDD requirement, resource-safety guidance (GROBID is a heavy Docker image — pull/start deliberately, don't batch-process large PDF sets without a small representative run first)

### Prior phase artifacts
- `backend/app/ingestion/corpus.py` — `Paper` model, `enrich_paper`, `get_session` this phase reads/writes through; establishes the SQLAlchemy declarative-model pattern this phase's new tables should mirror
- `backend/app/core/config.py` — `Settings`/`get_settings()` this phase's `GROBID_URL` joins
- `docker-compose.yml` — existing service definitions (neo4j/qdrant/postgres/backend) this phase's `grobid` service joins, following the same healthcheck-gated pattern

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `backend/app/ingestion/corpus.py`'s `download_pdf`-adjacent pattern (a single shared, testable I/O helper) — this phase's GROBID client should follow the same "plain function, mockable, no hidden global state" shape.
- `backend/app/core/health.py`'s timeout-bounded external-service-check pattern — GROBID's own reachability could optionally extend `/health` in a later phase, but this phase's own retry/timeout handling should follow the same spirit (bounded, not indefinite).
- `backend/app/parsing/` and `backend/app/models/` package directories already exist (empty, scaffolded in Phase 0) per README §11 — this phase fills `app/parsing/` in; `app/models/` may gain shared Pydantic schemas for the parsed-document structure if useful.

### Established Patterns
- Config centralization via `get_settings()` (Phase 0/1) — `GROBID_URL` must be read this way.
- TDD red-green-refactor per task (Phase 0/1 precedent).
- `ruff check`/`ruff format` clean before a task is considered done.
- Source-agnostic corpus reads (Phase 1 established the corpus; Phase 2 is the first stage required to never branch on `source` per CLAUDE.md hard constraint 5 — this is a hard gate, not just a style preference).

### Integration Points
- New parsed-document table(s) reuse `corpus.get_session`/`get_engine` rather than opening a separate connection path.
- Manual-upload backfill reuses Phase 1's `enrich_paper` and (per D-33) its DOI-driven enrichment trigger, rather than duplicating that logic.

</code_context>

<specifics>
## Specific Ideas

No UI/UX surface in this phase (backend-only). The one genuinely open technical question — what "SciBERT rhetorical-role classifier" concretely means for implementation, given no training data/dataset is specified anywhere in the project — is resolved above (D-30) and should be double-checked by the phase's research pass before planning locks it in.

</specifics>

<deferred>
## Deferred Ideas

- Training or fine-tuning a supervised rhetorical-role classifier on a labeled dataset — out of scope; no such dataset or training phase exists anywhere in the 15-phase roadmap, and doing so here would be scope creep beyond PARSE-03's actual requirement (normalize headings onto a common schema).
- Extending `/health` to verify GROBID connectivity — not required by PARSE-01..06; Phase 0's `/health` contract (D-06) is locked to neo4j/qdrant/postgres and isn't reopened here without a separate decision.
- A more sophisticated PDF-fallback heuristic (e.g. a learned layout model) — README explicitly frames PyMuPDF/pdfplumber as a fallback, not a co-primary parser; a best-effort heuristic is sufficient.

### Reviewed Todos (not folded)
None — no pending todos existed for Phase 2.

</deferred>

---

*Phase: 02-parsing-section-normalization-stage-2*
*Context gathered: 2026-08-22*

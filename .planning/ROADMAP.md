# Roadmap: ScholarLens

## Overview

ScholarLens is built in 15 fixed phases (0 through 14), taken verbatim from README.md §15 — the authoritative, pre-scoped phase breakdown for this project. The journey follows the pipeline itself: stand up the scaffold (Phase 0), collect and normalize literature from four fixed sources (Phase 1), parse and section-normalize it (Phase 2), extract structured scientific facts (Phase 3), build a knowledge graph via joint NER+RE (Phase 4), embed and index everything for semantic search (Phase 5), cluster similar limitations and flag contradictions (Phase 6), analyze trends (Phase 7), rank gaps with a transparent formula (Phase 8), generate grounded explanations for the top gaps in batch (Mode A, Phase 9), answer on-demand questions via LangGraph GraphRAG (Mode B, Phase 10), wire everything into a REST API (Phase 11), build the researcher-facing dashboard (Phase 12), prove the gaps are valid via an evaluation harness (Phase 13), and finally package the whole system for one-command deployment (Phase 14). The LLM is introduced only in Phases 9 and 10 — every earlier phase builds the structured evidence it will explain. Work proceeds strictly one phase at a time; the project owner authorizes each phase start explicitly.

## Phases

**Phase Numbering:**
- This roadmap uses README.md §15's fixed integer phases (0–14). No phases were added, removed, merged, or re-derived — README §15 is the authoritative source, not a granularity-driven derivation.
- Decimal phases (e.g. 4.1) may be inserted later via `/gsd:phase insert` for urgent work discovered mid-build; none exist yet.

- [x] **Phase 0: Project Scaffolding & Environment** - Empty but running Docker Compose stack (Neo4j/Qdrant/Postgres) with a verified `/health` endpoint
- [x] **Phase 1: Literature Collection (Stage 1)** - A topic yields a deduplicated, normalized corpus from arXiv/Semantic Scholar/ACL Anthology + Unpaywall, plus manual upload
- [x] **Phase 2: Parsing & Section Normalization (Stage 2)** - Every paper becomes structured JSON with normalized sections/paragraphs/sentences
- [x] **Phase 3: Scientific Information Extraction (Stage 3)** - Contribution/Limitation/Future Work/Dataset/Model/Metric facts extracted per paper
- [x] **Phase 4: Joint NER + Relation Extraction (Stage 4)** - Entities and typed relations written into a queryable Neo4j graph
- [x] **Phase 5: Embeddings & Vector Indexing (Stage 5)** - Every statement embedded and searchable in Qdrant, linked back to Neo4j
- [x] **Phase 6: Semantic Clustering + Contradiction Detection (Stage 6)** - Similar limitations clustered; contradictions flagged via NLI
- [x] **Phase 7: Trend Analysis (Stage 7)** - Per-cluster mention time series and trend score exposed via the API
- [x] **Phase 8: Gap Ranking (Stage 8)** - Transparent weighted gap-score formula ranks clusters, queryable via the API
- [x] **Phase 9: Mode A - RAG-Grounded Explanation Generation (Stage 9)** - Top-ranked gaps get complete, evidence-grounded reports
- [x] **Phase 10: Mode B - Interactive GraphRAG with LangGraph (Stage 10)** - On-demand questions get grounded, cited answers with retry behavior
- [x] **Phase 11: Backend API Integration** - Full REST surface wiring Phases 1-10 together, documented, with API-key auth
- [ ] **Phase 12: Frontend Dashboard** - Researcher-facing UI for gap reports, trends, graph exploration, and Mode B chat
- [ ] **Phase 13: Evaluation Harness** - Time-shifted validation, survey cross-check, and expert scoring produce a concrete evaluation report
- [ ] **Phase 14: Full Deployment** - Fresh clone + `docker-compose up` + `.env` brings up the entire system end-to-end

## Phase Details

### Phase 0: Project Scaffolding & Environment
**Goal**: A working scaffold exists so every subsequent phase has a running environment to build on, with no ML/NLP logic yet.
**Depends on**: Nothing (first phase)
**Requirements**: SETUP-01, SETUP-02, SETUP-03, SETUP-04, SETUP-05, SETUP-06
**Success Criteria** (what must be TRUE):
  1. Running `docker-compose up` brings up empty Neo4j, Qdrant, and Postgres containers cleanly.
  2. `/health` on the FastAPI backend returns 200 with all service connections verified.
  3. The repository structure matches the layout defined in README §11.
  4. The pytest test harness is wired up and runs (even if the suite is still small).
**Skills**: Verify Context Mode + Claude Mem are active first (`/contextmode:ctx-stats`, `/help`) — everything after this depends on them. GSD discuss + plans for repo shape; Superpowers execute + TDD; close with `/code-review`.
**Tech**: Python 3.11+, pyproject.toml, Docker & Docker Compose, pre-commit, .env handling, FastAPI skeleton, React+TS skeleton (empty shell), pytest setup.
**Build**: Repository structure as in README §11. `docker-compose.yml` bringing up empty Neo4j, Qdrant, and Postgres containers plus a `/health` endpoint on the FastAPI backend. No ML/NLP logic yet.
**Done when**: `docker-compose up` runs cleanly and `/health` returns 200 with all service connections verified.
**Plans**: 4 plans
Plans:
- [x] 00-01-PLAN.md — Repo package tree, backend/pyproject.toml (dev extras), pre-commit + .gitignore
- [x] 00-02-PLAN.md — docker-compose.yml (neo4j/qdrant/postgres/backend), backend/Dockerfile, .env.example
- [x] 00-03-PLAN.md — Frontend empty shell (Vite react-ts)
- [x] 00-04-PLAN.md — Centralized config + /health endpoint (TDD), full-stack integration proof

### Phase 1: Literature Collection (Stage 1)
**Goal**: Users can point the system at a topic and get a deduplicated, normalized corpus of papers from every permitted source, plus manual uploads.
**Depends on**: Phase 0
**Requirements**: INGEST-01, INGEST-02, INGEST-03, INGEST-04, INGEST-05, INGEST-06, INGEST-07, INGEST-08, INGEST-09
**Success Criteria** (what must be TRUE):
  1. A topic string submitted to `/collect` yields N deduplicated papers gathered from arXiv, Semantic Scholar, and ACL Anthology.
  2. Corpus records with a DOI but no full text are enriched with a legal open-access PDF via Unpaywall where one exists.
  3. A manually uploaded PDF via `/upload` lands in the same normalized corpus, indistinguishable from other sources except its `source` field.
  4. No IEEE Xplore, Springer, or ACM Digital Library API is present anywhere in the ingestion code.
**Skills**: Context Mode **heavy** (bulk arXiv / Semantic Scholar / ACL Anthology / Unpaywall API responses are exactly the raw-output case it exists for). GSD plans — **one plan per ingestion source**, so each source is built and tested in isolation against the shared corpus contract. Superpowers TDD; close with `/code-review`.
**Tech**: `arxiv` Python package; Semantic Scholar API via `httpx`; ACL Anthology bibliographic export + PDF URLs; Unpaywall REST API via `httpx`; FastAPI file upload for manual ingestion; PostgreSQL for the normalized corpus.
**Build**: Build the normalized corpus writer first (`corpus.py` + the canonical `papers` schema + `dedup.py`), then each ingestion source against it: (1) arXiv source — topic query → metadata + PDF download; (2) Semantic Scholar source — topic query → metadata (+ `openAccessPdf` link where present), metadata-only by design; (3) ACL Anthology source — topic/venue query → metadata + open-access PDF, feeding the same corpus writer, no parallel path; (4) Unpaywall enrichment — runs after discovery, over records with a DOI but no full-text PDF, attaches OA copies, creates no new records; (5) Manual upload — `POST /upload` accepts one or more PDFs, writes to the shared PDF store, creates corpus records with `source='manual_upload'` (metadata backfilled in Phase 2). Expose `/collect` (topic → discovery + enrichment) and `/upload`. Deduplicate across all sources in order DOI → arXiv ID → ACL Anthology ID → normalized title+year. **Constraint:** IEEE Xplore, Springer, and ACM Digital Library APIs are prohibited — do not add them here or anywhere else.
**Done when**: A topic string yields N deduplicated papers in the normalized corpus from all discovery sources with Unpaywall enrichment applied; a manually uploaded PDF lands in that same corpus and is indistinguishable to Phase 2 apart from its `source` field.
**Plans**: 6 plans
Plans:
- [x] 01-01-PLAN.md — corpus.py (D-10 schema) + dedup.py (D-19 priority chain) + new deps (arxiv/bibtexparser/httpx)
- [x] 01-02-PLAN.md — arXiv source (metadata + PDF via shared httpx helper)
- [x] 01-03-PLAN.md — Semantic Scholar source (metadata-only)
- [x] 01-04-PLAN.md — ACL Anthology source (bibtexparser over anthology+abstracts.bib.gz)
- [x] 01-05-PLAN.md — Unpaywall enrichment (targeted UPDATE, never INSERT)
- [x] 01-06-PLAN.md — Manual upload + /collect + /upload endpoints + INGEST-09 exclusion check

### Phase 2: Parsing & Section Normalization (Stage 2)
**Goal**: Every paper in the corpus has clean, structurally normalized text ready for downstream extraction.
**Depends on**: Phase 1
**Requirements**: PARSE-01, PARSE-02, PARSE-03, PARSE-04, PARSE-05, PARSE-06
**Success Criteria** (what must be TRUE):
  1. Every stored paper has a structured JSON representation with normalized sections (Introduction/Methodology/Results/Discussion/Limitations/Future Work/Conclusion), paragraphs, and sentences.
  2. Malformed PDFs that fail GROBID parsing are still parsed via the PyMuPDF/pdfplumber fallback.
  3. No manually uploaded paper remains without metadata that GROBID could recover.
  4. Parsing logic never branches on a paper's `source` field.
**Skills**: GSD plans with a **fresh context per batch** — GROBID TEI-XML output is large and repeats across hundreds of papers. Context Mode **heavy**; Superpowers TDD; close with `/code-review`.
**Tech**: GROBID (Dockerized service, primary parser), PyMuPDF + pdfplumber (fallback), SciBERT-based section-role classifier.
**Build**: A parsing module that sends each PDF to GROBID, extracts structured sections/paragraphs/sentences, falls back to PyMuPDF/pdfplumber on failure, and normalizes headings onto the common schema. Reads from the normalized corpus only and must not branch on a paper's `source` field — every paper is parsed identically regardless of ingestion path. Additionally, backfill metadata for manually uploaded papers via GROBID header extraction (title, authors, DOI); a DOI recovered this way becomes eligible for the same Semantic Scholar / Unpaywall enrichment as any other paper.
**Done when**: Every stored paper has a structured JSON representation with normalized sections, paragraphs, and sentences, and no manually uploaded paper remains without metadata that GROBID could recover.
**Plans**: 5 plans
Plans:
- [x] 02-01-PLAN.md — GROBID Compose/config, dependencies, and ParsedDocument storage
- [x] 02-02-PLAN.md — GROBID HTTP client and TEI structure parser
- [x] 02-03-PLAN.md — keyword + SciBERT section-role classifier
- [x] 02-04-PLAN.md — PyMuPDF/pdfplumber fallback parser
- [x] 02-05-PLAN.md — source-agnostic orchestration and manual-upload backfill

### Phase 3: Scientific Information Extraction (Stage 3)
**Goal**: Each paper's claims are turned into structured, queryable facts instead of prose.
**Depends on**: Phase 2
**Requirements**: EXTRACT-01, EXTRACT-02
**Success Criteria** (what must be TRUE):
  1. Each parsed paper has structured fact rows (Contribution/Limitation/Future Work/Dataset/Model/Metric) queryable by type in Postgres.
**Skills**: Superpowers-led (`/superpowers:brainstorm` → `write-plan` → `execute-plan`) with strict TDD — extraction rules are unit-testable sentence by sentence. Close with `/code-review`.
**Tech**: spaCy + Transformer pipeline (SciBERT/DeBERTa), structured extraction schema.
**Build**: Extracts Contribution / Limitation / Future Work / Dataset / Model / Metric statements per paper from the normalized sections, stores them in Postgres linked to `paper_id`.
**Done when**: Each parsed paper has associated structured fact rows queryable by type.
**Plans**: 1 plan
Plans:
- [x] 03-01-PLAN.md — spaCy/SciBERT extraction, structured fact storage, and orchestration

### Phase 4: Joint NER + Relation Extraction (Stage 4)
**Goal**: The system understands which entities (models, datasets, tasks, metrics, domains) relate to which papers and how.
**Depends on**: Phase 3
**Requirements**: NERRE-01, NERRE-02
**Success Criteria** (what must be TRUE):
  1. The Neo4j graph contains papers connected to extracted entities via typed relationships (`trained_on`, `evaluated_using`, `fails_on`, `achieves`, etc.).
  2. Entities and relations are queryable via Cypher.
**Skills**: Start with a **GSD discuss phase** to lock the model choice before any code — errors here cascade into the graph, clusters, and every downstream stage. Then GSD plans + Superpowers TDD, Context Mode **heavy**, and close with `/code-review` (free, unlimited). This phase's extra risk is covered later by the project's single `/code-review ultra` pass after Phase 14, not a per-phase ultra run.
**Tech**: SciERC-style joint entity+relation model (DyGIE++ architecture or an equivalent pretrained scientific IE model).
**Build**: Extracts entities (Model, Dataset, Task, Metric, Domain) and relations between them (`trained_on`, `evaluated_using`, `fails_on`, `achieves`, etc.) in a single pass; writes entities and relations into Neo4j as nodes/edges linked to the source paper.
**Done when**: The Neo4j graph contains papers connected to extracted entities via typed relationships, queryable via Cypher.
**Plans**: Complete in combined backend pass — see `.planning/BACKEND-04-11-SUMMARY.md`

### Phase 5: Embeddings & Vector Indexing (Stage 5)
**Goal**: Any paragraph/limitation/future-work statement in the corpus is semantically searchable.
**Depends on**: Phase 4
**Requirements**: EMBED-01, EMBED-02, EMBED-03, EMBED-04
**Success Criteria** (what must be TRUE):
  1. A semantic query against Qdrant returns relevant paragraphs with correct metadata (paper ID, year, venue, section type).
  2. Each returned vector links back to its corresponding Neo4j node.
**Skills**: Context Mode **heavy** (batch embedding jobs, Qdrant upserts, Neo4j link-back queries all produce bulky output). GSD plans; Superpowers TDD; close with `/code-review`.
**Tech**: SPECTER2, Qdrant.
**Build**: Batch-embeds every paragraph/limitation/future-work statement using SPECTER2; stores vectors in Qdrant with metadata (paper ID, year, venue, section type) enabling filtered search; links each vector back to its Neo4j node ID.
**Done when**: A semantic query against Qdrant returns relevant paragraphs with correct metadata and graph linkage.
**Plans**: Complete in combined backend pass — see `.planning/BACKEND-04-11-SUMMARY.md`

### Phase 6: Semantic Clustering + Contradiction Detection (Stage 6)
**Goal**: Similar limitations/future-work statements across papers are recognized as the same underlying gap, and conflicting claims are flagged.
**Depends on**: Phase 5
**Requirements**: CLUSTER-01, CLUSTER-02, CLUSTER-03
**Success Criteria** (what must be TRUE):
  1. Semantically similar limitation statements (e.g. "English bias", "poor multilingual support", "low-resource languages") are grouped into the same cluster without a pre-specified cluster count.
  2. Clusters are stored with their member statements.
  3. Contradiction flags are attached to clusters where pairwise NLI detects conflicting claims.
**Skills**: Use **GSD's validate-before-code step** first — establish a concrete test signal (a small hand-labelled set of limitation sentences that *should* cluster together) before writing clustering code. Then GSD plans + Superpowers TDD; close with `/code-review`.
**Tech**: HDBSCAN or BERTopic, DeBERTa-MNLI.
**Build**: Clusters limitation/future-work statements by SPECTER2 embedding similarity (no fixed cluster count). Within each cluster, runs pairwise NLI checks to flag contradicting statements between papers.
**Done when**: Clusters are stored with member statements, and contradiction flags are attached where applicable.
**Plans**: Complete in combined backend pass — see `.planning/BACKEND-04-11-SUMMARY.md`

### Phase 7: Trend Analysis (Stage 7)
**Goal**: Each cluster's momentum over time is visible.
**Depends on**: Phase 6
**Requirements**: TREND-01, TREND-02
**Success Criteria** (what must be TRUE):
  1. Each cluster has a mention-count time series by publication year.
  2. Each cluster exposes a computed trend score via the API.
**Skills**: Superpowers-led with TDD — deterministic aggregation logic with clear expected outputs. Close with `/code-review`.
**Tech**: Pandas, NumPy.
**Build**: Aggregates per-cluster mention counts by publication year and computes a trend slope/score per cluster.
**Done when**: Each cluster has a time series and a trend score, exposed via the API.
**Plans**: Complete in combined backend pass — see `.planning/BACKEND-04-11-SUMMARY.md`

### Phase 8: Gap Ranking (Stage 8)
**Goal**: Clusters are ranked by how likely they are to represent a genuine, unresolved research gap, with the ranking fully explainable.
**Depends on**: Phase 7
**Requirements**: GAPRANK-01, GAPRANK-02
**Success Criteria** (what must be TRUE):
  1. A ranked, queryable list of gap clusters is available via the API.
  2. Each ranked cluster exposes its individual signal breakdown (mention_frequency, trend_slope, solution_density, citation_impact, contradiction_flag).
**Skills**: Superpowers `write-plan` → `execute-plan` with TDD (test each signal term independently, then the combined score). Close with `/code-review`. This formula determines the validity of every gap the system reports, so lean on thorough TDD coverage here — the deep review pass happens once, at the end (see Phase 14).
**Tech**: Plain Python (no ML model — a transparent weighted formula).
**Build**: Implements `gap_score = w1·mention_frequency + w2·trend_slope − w3·solution_density + w4·citation_impact + w5·contradiction_flag` with configurable weights; ranks clusters and stores results in Postgres.
**Done when**: A ranked, queryable list of gap clusters with individual signal breakdowns is available via the API.
**Plans**: Complete in combined backend pass — see `.planning/BACKEND-04-11-SUMMARY.md`

### Phase 9: Mode A - RAG-Grounded Explanation Generation (Stage 9)
**Goal**: Top-ranked gaps come with a human-readable, evidence-grounded explanation, not just a score.
**Depends on**: Phase 8
**Requirements**: MODEA-01, MODEA-02, MODEA-03
**Success Criteria** (what must be TRUE):
  1. Each of the top-K ranked gaps has a complete report (Gap/Evidence/Confidence/Suggested Directions).
  2. Every report references real retrieved paragraphs/papers from Qdrant, not free-form LLM text.
  3. Reports are retrievable via `/reports`.
**Skills**: GSD plans; Superpowers TDD; Context Mode **heavy** (retrieved paragraph batches + LLM traces). Close with `/code-review`, explicitly checking that no generated claim is unsupported by retrieved evidence — this is the project's core credibility claim, and it gets a second, deeper look in the single final `/code-review ultra` pass after Phase 14.
**Tech**: Qdrant retrieval, direct LLM API call (Llama 3.x / Qwen / GPT), structured JSON output/schema validation. **No LangGraph loop here.**
**Build**: For each top-ranked gap cluster, retrieves the top-N actual supporting paragraphs from Qdrant and passes them as context to the LLM in a single pass, producing a structured report (Gap / Evidence / Confidence / Suggested Directions). Stores and exposes reports via `/reports`.
**Done when**: Top-K ranked gaps each have a complete, evidence-grounded report referencing real retrieved paragraphs/papers.
**Plans**: Complete in combined backend pass — see `.planning/BACKEND-04-11-SUMMARY.md`

### Phase 10: Mode B - Interactive GraphRAG with LangGraph (Stage 10)
**Goal**: A researcher can ask a specific question and get a grounded, cited answer on demand.
**Depends on**: Phase 9
**Requirements**: MODEB-01, MODEB-02, MODEB-03
**Success Criteria** (what must be TRUE):
  1. A test question reliably returns an answer whose claims trace back to specific retrieved papers.
  2. When initial retrieval is weak, visible retry/reformulation behavior occurs (capped at 3 rounds).
  3. `/query` returns a grounded answer with source citations for a natural-language research question.
**Skills**: The most complex phase — use **GSD discuss first**, then **one GSD plan per graph node** (retrieve → assess → reformulate → rerank → generate → grounding check), each in a fresh context with atomic commits. Superpowers TDD per node; Context Mode **heavy**; close with `/code-review` (takes live user input, so logic *and* security angles both matter — this phase gets particular attention in the single final `/code-review ultra` pass after Phase 14).
**Tech**: LangGraph, Qdrant, Neo4j, cross-encoder reranker, LLM API.
**Build**: Implements the stateful graph described in README §9: retrieve (vector + graph) → assess sufficiency → reformulate/retry (capped rounds) → rerank → generate → grounding check → return or regenerate. Exposes a `/query` endpoint accepting a natural-language research question and returning a grounded answer with source citations.
**Done when**: A test question reliably returns an answer whose claims trace back to specific retrieved papers, with visible retry behavior when initial retrieval is weak.
**Plans**: Complete in combined backend pass — see `.planning/BACKEND-04-11-SUMMARY.md`

### Phase 11: Backend API Integration
**Goal**: Every capability built in Phases 1-10 is reachable through one documented, coherent, secured API.
**Depends on**: Phase 10
**Requirements**: API-01, API-02, API-03
**Success Criteria** (what must be TRUE):
  1. `/collect`, `/upload`, `/papers`, `/graph`, `/clusters`, `/trends`, `/gaps`, `/reports`, `/query` are all live and documented via OpenAPI.
  2. API-key auth protects the endpoints.
  3. The full backend is usable end-to-end without running internal scripts manually.
**Skills**: GSD plans (one per endpoint group); Superpowers TDD. Close with `/code-review` followed by `/security-review` (both free, unlimited) — these endpoints are the system's exposed surface, so run `/security-review` thoroughly here.
**Tech**: FastAPI.
**Build**: Wires Phases 1–10 into a complete REST surface: `/collect`, `/upload`, `/papers`, `/graph`, `/clusters`, `/trends`, `/gaps`, `/reports` (Mode A), `/query` (Mode B). Adds OpenAPI docs and basic API-key auth.
**Done when**: The full backend is usable end-to-end via documented REST endpoints without needing to run internal scripts manually.
**Plans**: Complete in combined backend pass — see `.planning/BACKEND-04-11-SUMMARY.md`

### Phase 12: Frontend Dashboard
**Goal**: A researcher can explore gaps and ask questions without touching the API directly.
**Depends on**: Phase 11
**Requirements**: UI-01, UI-02, UI-03, UI-04
**Success Criteria** (what must be TRUE):
  1. A researcher can browse ranked gap reports through the UI.
  2. A researcher can view trend charts and explore the cluster/knowledge-graph visually.
  3. A researcher can ask Mode B questions through a chat-style interface and see citations displayed.
**Skills**: Superpowers-led (`brainstorm` is worthwhile here for layout/UX decisions) → `write-plan` → `execute-plan`. Close with `/code-review`.
**Tech**: React + TypeScript, Plotly/D3.js, a graph-visualization component (e.g. force-directed graph view).
**Build**: Dashboard views for: ranked gap reports (Mode A), trend charts, cluster/knowledge-graph exploration, and a chat-style interface for Mode B queries with citations displayed.
**Done when**: A researcher can browse gap reports and ask interactive questions entirely through the UI.
**Plans**: TBD
**UI hint**: yes

### Phase 13: Evaluation Harness
**Goal**: The project can demonstrate, with evidence, that its flagged gaps are actually valid.
**Depends on**: Phase 12
**Requirements**: EVAL-01, EVAL-02, EVAL-03, EVAL-04
**Success Criteria** (what must be TRUE):
  1. Running the harness against a test corpus produces a concrete evaluation report with precision/validity indicators.
  2. The report includes time-shifted validation results (gaps later addressed by post-X papers).
  3. The report includes a survey cross-check against recent survey "open problems"/"future work" sections.
  4. The report includes expert scoring (1-5 scale) on a sample of gap reports.
**Skills**: Superpowers `write-plan` → `execute-plan` with TDD — scripts with well-defined expected outputs. Close with `/code-review`.
**Tech**: Python scripts.
**Build**: Implements the three evaluation methods from README §10 — time-shifted validation, survey cross-check, and an expert-scoring rubric/collection script — and produces a summary report.
**Done when**: Running the harness against a test corpus produces a concrete evaluation report with precision/validity indicators.
**Plans**: TBD

### Phase 14: Full Deployment
**Goal**: Anyone can stand up the entire system from a fresh clone with minimal effort.
**Depends on**: Phase 13
**Requirements**: DEPLOY-01, DEPLOY-02, DEPLOY-03
**Success Criteria** (what must be TRUE):
  1. A fresh clone + `docker-compose up` + configured `.env` results in a fully working system end-to-end (backend, frontend, Neo4j, Qdrant, Postgres, GROBID all up).
  2. A documented local run procedure exists.
**Skills**: **GSD discuss phase** before touching config (secrets handling, service wiring, volumes). Superpowers execute. Close with `/code-review` + `/security-review` (both free, unlimited). **Once this phase is approved, run the project's single, one-time `/code-review ultra` pass** — see the note below.
**Tech**: Docker Compose (full stack).
**Build**: Single `docker-compose up` brings up backend, frontend, Neo4j, Qdrant, Postgres, and GROBID together, wired via environment configuration, with a documented local run procedure.
**Done when**: A fresh clone + `docker-compose up` + `.env` configured results in a fully working system end-to-end.
**Plans**: TBD

**Final review (after Phase 14, once only, not a phase of its own).** Once the project owner approves Phase 14, run exactly one `/code-review ultra` pass over the complete, integrated codebase — the only time `/code-review ultra` is used anywhere in this project (Anthropic provides just 3 free runs before billing usage credits; every other review gate throughout Phases 0–14 uses the free `/code-review`, plus `/security-review` on Phases 11/14). Address anything the ultra pass raises; once resolved, the build is complete.

## Progress

**Execution Order:**
Phases execute in numeric order: 0 → 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12 → 13 → 14

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 0. Project Scaffolding & Environment | 4/4 | Complete | 2026-08-22 |
| 1. Literature Collection | 6/6 | Complete | 2026-08-22 |
| 2. Parsing & Section Normalization | 5/5 | Complete | 2026-08-30 |
| 3. Scientific Information Extraction | 1/1 | Complete | 2026-08-30 |
| 4. Joint NER + Relation Extraction | 1/1 | Complete | 2026-09-01 |
| 5. Embeddings & Vector Indexing | 1/1 | Complete | 2026-09-01 |
| 6. Semantic Clustering + Contradiction Detection | 1/1 | Complete | 2026-09-01 |
| 7. Trend Analysis | 1/1 | Complete | 2026-09-01 |
| 8. Gap Ranking | 1/1 | Complete | 2026-09-01 |
| 9. Mode A - RAG-Grounded Explanation Generation | 1/1 | Complete | 2026-09-01 |
| 10. Mode B - Interactive GraphRAG with LangGraph | 1/1 | Complete | 2026-09-01 |
| 11. Backend API Integration | 1/1 | Complete | 2026-09-01 |
| 12. Frontend Dashboard | 0/TBD | Not started | - |
| 13. Evaluation Harness | 0/TBD | Not started | - |
| 14. Full Deployment | 0/TBD | Not started | - |

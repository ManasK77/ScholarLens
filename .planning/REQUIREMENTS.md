# Requirements: ScholarLens

**Defined:** 2026-08-21
**Core Value:** Every research gap ScholarLens reports is grounded in structured, retrieved evidence extracted before any LLM is involved — the LLM explains gaps already discovered by extraction, clustering, trend analysis, and ranking; it never discovers gaps on its own.

**Source note:** README.md was classified SPEC (not PRD) during ingest, so these requirements are derived directly from README §15's per-phase Build/Done-when criteria and supporting sections (§7, §8, §9, §10, §11, §12), not from a separate PRD. Requirement categories map 1:1 to the 15 fixed implementation phases in README §15 — see `.planning/ROADMAP.md` for the phase breakdown.

## v1 Requirements

Requirements for initial release. Each maps to exactly one roadmap phase.

### Setup (Phase 0)

- [x] **SETUP-01**: Repository structure matches the layout defined in README §11 (backend/app with api/core/ingestion/parsing/extraction/ner_re/embeddings/clustering/trends/ranking/explanation/interactive/models, frontend/, evaluation/).
- [x] **SETUP-02**: `docker-compose.yml` brings up empty Neo4j, Qdrant, and Postgres containers.
- [x] **SETUP-03**: FastAPI backend exposes a `/health` endpoint returning 200 with all service connections verified.
- [x] **SETUP-04**: React + TypeScript frontend skeleton (empty shell) exists.
- [x] **SETUP-05**: pytest test setup exists and runs.
- [x] **SETUP-06**: `.env` handling and `.env.example` are established for secrets/config.

### Literature Collection (Phase 1)

- [x] **INGEST-01**: Normalized corpus writer (`corpus.py`) implements the canonical `papers` schema (title, authors, year, venue, DOI, abstract, source, pdf_path, has_full_text) in PostgreSQL.
- [x] **INGEST-02**: `dedup.py` deduplicates across sources in priority order DOI → arXiv ID → ACL Anthology ID → normalized title+year.
- [x] **INGEST-03**: arXiv source fetches metadata + full-text PDF for a topic query into the normalized corpus.
- [x] **INGEST-04**: Semantic Scholar source fetches metadata (+ `openAccessPdf` link where present); metadata-only by design.
- [x] **INGEST-05**: ACL Anthology source fetches metadata + open-access PDF via the same corpus writer (no parallel path).
- [x] **INGEST-06**: Unpaywall enrichment resolves a legal open-access full text for corpus records that have a DOI but no full-text PDF, creating no new records.
- [x] **INGEST-07**: Manual upload endpoint (`POST /upload`) accepts one or more PDFs, writes them to the shared PDF store, and creates corpus records with `source='manual_upload'`.
- [x] **INGEST-08**: `/collect` endpoint runs discovery + enrichment for a topic string, returning N deduplicated papers in the normalized corpus.
- [x] **INGEST-09**: IEEE Xplore, Springer, and ACM Digital Library APIs are not integrated anywhere in ingestion, nor is scripted access to those platforms.

### Parsing & Section Normalization (Phase 2)

- [x] **PARSE-01**: GROBID parses each PDF into structured sections/paragraphs/sentences.
- [x] **PARSE-02**: PyMuPDF/pdfplumber fallback handles PDFs GROBID cannot parse cleanly.
- [x] **PARSE-03**: A SciBERT rhetorical-role classifier normalizes inconsistent venue headings onto the common schema (Introduction, Methodology, Results, Discussion, Limitations, Future Work, Conclusion).
- [x] **PARSE-04**: Parsing reads from the normalized corpus only and never branches on a paper's `source` field.
- [x] **PARSE-05**: Manual-upload records missing metadata are backfilled via GROBID header extraction (title, authors, DOI).
- [x] **PARSE-06**: A DOI recovered via backfill becomes eligible for the same Semantic Scholar / Unpaywall enrichment as any other paper.

### Scientific Information Extraction (Phase 3)

- [x] **EXTRACT-01**: A spaCy + Transformer pipeline extracts Contribution / Limitation / Future Work / Dataset / Model / Metric statements per paper from normalized sections.
- [x] **EXTRACT-02**: Extracted statements are stored in Postgres linked to `paper_id` and queryable by type.

### Joint NER + Relation Extraction (Phase 4)

- [x] **NERRE-01**: A SciERC-style joint model (DyGIE++ architecture or equivalent) extracts entities (Model, Dataset, Task, Metric, Domain) and relations (`trained_on`, `evaluated_using`, `fails_on`, `achieves`, etc.) between them in a single pass.
- [x] **NERRE-02**: Extracted entities and relations are written into Neo4j as nodes/edges linked to the source paper, queryable via Cypher.

### Embeddings & Vector Indexing (Phase 5)

- [x] **EMBED-01**: SPECTER2 batch-embeds every paragraph/limitation/future-work statement.
- [x] **EMBED-02**: Vectors are stored in Qdrant with metadata (paper ID, year, venue, section type) enabling filtered search.
- [x] **EMBED-03**: Each vector links back to its corresponding Neo4j node ID.
- [x] **EMBED-04**: A semantic query against Qdrant returns relevant paragraphs with correct metadata and graph linkage.

### Clustering + Contradiction Detection (Phase 6)

- [x] **CLUSTER-01**: HDBSCAN/BERTopic clusters limitation/future-work statements by SPECTER2 embedding similarity without a pre-specified cluster count.
- [x] **CLUSTER-02**: DeBERTa-MNLI runs pairwise NLI checks within each cluster to flag contradicting statements between papers.
- [x] **CLUSTER-03**: Clusters are stored with member statements and contradiction flags attached where applicable.

### Trend Analysis (Phase 7)

- [x] **TREND-01**: Pandas/NumPy aggregate per-cluster mention counts by publication year.
- [x] **TREND-02**: A trend slope/score is computed per cluster and exposed via the API.

### Gap Ranking (Phase 8)

- [x] **GAPRANK-01**: `gap_score = w1·mention_frequency + w2·trend_slope − w3·solution_density + w4·citation_impact + w5·contradiction_flag` is implemented with configurable weights stored in central settings.
- [x] **GAPRANK-02**: A ranked list of gap clusters with individual signal breakdowns is stored in Postgres and queryable via the API.

### Mode A — RAG-Grounded Explanation (Phase 9)

- [x] **MODEA-01**: The top-N actual supporting paragraphs are retrieved from Qdrant per top-ranked gap cluster.
- [x] **MODEA-02**: The LLM produces a structured Gap/Evidence/Confidence/Suggested-Directions report in a single pass (no loop) from the retrieved paragraphs.
- [x] **MODEA-03**: Reports are stored and exposed via `/reports`, referencing real retrieved paragraphs/papers.

### Mode B — Interactive GraphRAG with LangGraph (Phase 10)

- [x] **MODEB-01**: A LangGraph stateful graph implements retrieve (vector + graph) → assess sufficiency → reformulate/retry (capped at 3 rounds) → rerank (cross-encoder) → generate → grounding check → return or regenerate.
- [x] **MODEB-02**: `/query` accepts a natural-language research question and returns a grounded answer with source citations.
- [x] **MODEB-03**: Retry/reformulation behavior is visible when initial retrieval is weak, and every returned claim traces back to a specific retrieved paper.

### Backend API Integration (Phase 11)

- [x] **API-01**: A REST surface exposes `/collect`, `/upload`, `/papers`, `/graph`, `/clusters`, `/trends`, `/gaps`, `/reports`, `/query`, wiring Phases 1–10 together.
- [x] **API-02**: OpenAPI docs and API-key auth are added.
- [x] **API-03**: The full backend is usable end-to-end via documented REST endpoints without running internal scripts manually.

### Frontend Dashboard (Phase 12)

- [ ] **UI-01**: A researcher can browse ranked gap reports (Mode A) through the dashboard.
- [ ] **UI-02**: A researcher can view trend charts through the dashboard.
- [ ] **UI-03**: A researcher can explore clusters and the knowledge graph through the dashboard.
- [ ] **UI-04**: A researcher can ask Mode B questions through a chat-style interface with citations displayed.

### Evaluation Harness (Phase 13)

- [ ] **EVAL-01**: Time-shifted validation checks whether gaps flagged from a corpus snapshot (up to date X) were later addressed by papers published after X.
- [ ] **EVAL-02**: Survey cross-check compares flagged gaps against the "open problems"/"future work" sections of recent survey papers in the same field.
- [ ] **EVAL-03**: An expert-scoring rubric/collection script rates a sample of generated gap reports on novelty and validity (1–5 scale).
- [ ] **EVAL-04**: Running the harness against a test corpus produces a concrete evaluation report with precision/validity indicators.

### Full Deployment (Phase 14)

- [ ] **DEPLOY-01**: A single `docker-compose up` brings up backend, frontend, Neo4j, Qdrant, Postgres, and GROBID together via environment configuration.
- [ ] **DEPLOY-02**: A documented local run procedure exists.
- [ ] **DEPLOY-03**: A fresh clone + `docker-compose up` + configured `.env` results in a fully working system end-to-end.

## v2 Requirements

Deferred to future release (README §16 Future Extensions). Tracked but not in current roadmap.

### Future Extensions

- **FUT-01**: Cross-domain gap discovery — link concepts across research fields (e.g. applying cybersecurity techniques to healthcare NLP) to surface opportunities the single-domain pipeline can't see.
- **FUT-02**: Learned research-opportunity ranking — replace the Phase 8 weighted formula with a trained learning-to-rank model once enough labeled/validated gap data exists from the evaluation harness (Phase 13).

## Out of Scope

Explicitly excluded. Documented to prevent scope creep.

| Feature | Reason |
|---------|--------|
| LangChain (anywhere in the project) | Deliberately excluded by owner decision; LangGraph (Phase 10 only) is the sole orchestration framework used |
| A loop around Stage 8→9 (gap ranking → Mode A explanation) | Considered and explicitly declined; Phase 9 is single-pass retrieval + generation by design |
| IEEE Xplore, Springer, ACM Digital Library APIs (or scripted access to them) | Licensing/access boundary set by the owner; those venues' papers enter only via Unpaywall or manual upload |
| Cross-domain gap discovery | Deferred to v2 (FUT-01) — not part of the phased MVP build |
| Learned research-opportunity ranking (trained ranker replacing the gap-score formula) | Deferred to v2 (FUT-02) until evaluation data exists to train against |
| Generalizing the pipeline beyond technical/scientific literature | Scope fixed to CS/AI/ML/NLP/Cybersecurity-core technical literature for v1 |
| Celery/task-queue or other abstraction layers not explicitly specified by a phase | No scope creep — engineering discipline requirement |
| `/code-review ultra` as a per-phase review gate | Reserved for exactly one full-system pass after Phase 14; free `/code-review` (+ `/security-review` on Phases 11/14) covers every other gate |

## Traceability

Which phases cover which requirements. Updated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| SETUP-01 | Phase 0 | Complete |
| SETUP-02 | Phase 0 | Complete |
| SETUP-03 | Phase 0 | Complete |
| SETUP-04 | Phase 0 | Complete |
| SETUP-05 | Phase 0 | Complete |
| SETUP-06 | Phase 0 | Complete |
| INGEST-01 | Phase 1 | Complete |
| INGEST-02 | Phase 1 | Complete |
| INGEST-03 | Phase 1 | Complete |
| INGEST-04 | Phase 1 | Complete |
| INGEST-05 | Phase 1 | Complete |
| INGEST-06 | Phase 1 | Complete |
| INGEST-07 | Phase 1 | Complete |
| INGEST-08 | Phase 1 | Complete |
| INGEST-09 | Phase 1 | Complete |
| PARSE-01 | Phase 2 | Complete |
| PARSE-02 | Phase 2 | Complete |
| PARSE-03 | Phase 2 | Complete |
| PARSE-04 | Phase 2 | Complete |
| PARSE-05 | Phase 2 | Complete |
| PARSE-06 | Phase 2 | Complete |
| EXTRACT-01 | Phase 3 | Complete |
| EXTRACT-02 | Phase 3 | Complete |
| NERRE-01 | Phase 4 | Complete |
| NERRE-02 | Phase 4 | Complete |
| EMBED-01 | Phase 5 | Complete |
| EMBED-02 | Phase 5 | Complete |
| EMBED-03 | Phase 5 | Complete |
| EMBED-04 | Phase 5 | Complete |
| CLUSTER-01 | Phase 6 | Complete |
| CLUSTER-02 | Phase 6 | Complete |
| CLUSTER-03 | Phase 6 | Complete |
| TREND-01 | Phase 7 | Complete |
| TREND-02 | Phase 7 | Complete |
| GAPRANK-01 | Phase 8 | Complete |
| GAPRANK-02 | Phase 8 | Complete |
| MODEA-01 | Phase 9 | Complete |
| MODEA-02 | Phase 9 | Complete |
| MODEA-03 | Phase 9 | Complete |
| MODEB-01 | Phase 10 | Complete |
| MODEB-02 | Phase 10 | Complete |
| MODEB-03 | Phase 10 | Complete |
| API-01 | Phase 11 | Complete |
| API-02 | Phase 11 | Complete |
| API-03 | Phase 11 | Complete |
| UI-01 | Phase 12 | Pending |
| UI-02 | Phase 12 | Pending |
| UI-03 | Phase 12 | Pending |
| UI-04 | Phase 12 | Pending |
| EVAL-01 | Phase 13 | Pending |
| EVAL-02 | Phase 13 | Pending |
| EVAL-03 | Phase 13 | Pending |
| EVAL-04 | Phase 13 | Pending |
| DEPLOY-01 | Phase 14 | Pending |
| DEPLOY-02 | Phase 14 | Pending |
| DEPLOY-03 | Phase 14 | Pending |

**Coverage:**
- v1 requirements: 56 total
- Mapped to phases: 56
- Unmapped: 0 ✓

---
*Requirements defined: 2026-08-21*
*Last updated: 2026-08-21 after initial roadmap creation*

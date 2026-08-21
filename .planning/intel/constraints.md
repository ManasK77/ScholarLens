# Constraints (extracted from SPEC-classified sources)

Source: `C:\Users\Manas\Desktop\NLP\README.md` (classified: SPEC, confidence: high, manifest_override: true, precedence: 0, locked: false)

Type breakdown: 9 api-contract · 6 schema · 7 nfr · 15 protocol (37 total)

---

## API Contracts

- **C-01 — `/health`** (Phase 0). Returns 200 with all service connections verified. Source: README §15 Phase 0.
- **C-02 — `/collect`** (Phase 1). Topic string → runs discovery across arXiv/Semantic Scholar/ACL Anthology + Unpaywall enrichment → N deduplicated papers in normalized corpus. Source: README §8 Stage 1, §15 Phase 1.
- **C-03 — `/upload`** (Phase 1). `POST /upload` accepts one or more PDFs, writes to shared PDF store, creates corpus records with `source='manual_upload'`; metadata may be empty and is backfilled in Phase 2. Source: README §15 Phase 1.
- **C-04 — `/papers`, `/graph`, `/clusters`, `/trends`, `/gaps`** (Phase 11). Query endpoints wiring Phases 1–10 into a REST surface. Source: README §15 Phase 11.
- **C-05 — `/reports`** (Mode A, Phase 9). Exposes stored Gap/Evidence/Confidence/Suggested-Directions reports. Source: README §8 Stage 9, §15 Phase 9.
- **C-06 — `/query`** (Mode B, Phase 10). Accepts a natural-language research question, returns a grounded answer with source citations. Source: README §9, §15 Phase 10.
- **C-07 — OpenAPI docs + API-key auth** (Phase 11). Source: README §15 Phase 11.
- **C-08 — Frontend consumes all of the above** via a chat-style interface for Mode B and dashboard views for Mode A/trends/clusters (Phase 12). Source: README §15 Phase 12.
- **C-09 — Backend API layer is FastAPI throughout**; frontend is React + TypeScript. Source: README §7.

## Schemas

- **S-01 — Normalized `papers` schema** (PostgreSQL): `title, authors, year, venue, DOI, abstract, source, pdf_path, has_full_text`. One canonical schema regardless of ingestion path. Source: README §8 Stage 1.
- **S-02 — Neo4j graph schema**: papers connected to entities (Model, Dataset, Task, Metric, Domain) via typed relations (`trained_on`, `evaluated_using`, `fails_on`, `achieves`, etc.), queryable via Cypher. Source: README §8 Stage 4, §15 Phase 4.
- **S-03 — Qdrant vector metadata schema**: paper ID, year, venue, section type — enables filtered semantic search, linked back to Neo4j node IDs. Source: README §8 Stage 5, §15 Phase 5.
- **S-04 — Gap score formula schema**: `gap_score = w1·mention_frequency + w2·trend_slope − w3·solution_density + w4·citation_impact + w5·contradiction_flag`; weights configurable, stored in central settings, not a trained ranker for the MVP. Source: README §8 Stage 8, §15 Phase 8.
- **S-05 — Mode B state object** (conceptual, LangGraph): `{question: str, retrieved_chunks: list, retrieval_rounds: int (capped at 3), sufficient_evidence: bool, draft_answer: str, grounded: bool}`. Source: README §9.
- **S-06 — Section-normalization schema**: inconsistent venue headings normalized onto Introduction / Methodology / Results / Discussion / Limitations / Future Work / Conclusion. Source: README §8 Stage 2, §15 Phase 2.

## Non-Functional Requirements

- **N-01 — Retrieval-round cap.** Mode B reformulation loop is capped at 3 rounds to prevent infinite loops. Source: README §9.
- **N-02 — Deduplication priority order.** DOI → arXiv ID → ACL Anthology ID → normalized title+year match (fallback for manual uploads with no identifiers). A paper appearing in multiple sources is stored once, keeping the record with usable full-text. Source: README §8 Stage 1, §15 Phase 1.
- **N-03 — Context Mode load per phase.** Heavy: Phases 1, 2, 4, 5, 9, 10 (bulk API responses, GROBID TEI-XML, embedding batches, Cypher/Qdrant results, LLM traces). Normal: Phases 0, 3, 6, 7, 8, 11, 12, 13, 14. Source: README §13.5.
- **N-04 — Upload size limit.** `MAX_UPLOAD_SIZE_MB` configurable for the manual-upload endpoint. Source: README §12.
- **N-05 — Auth/rate-limit posture.** `SEMANTIC_SCHOLAR_API_KEY` optional (API works unauthenticated at a lower rate limit); `UNPAYWALL_EMAIL` required as a contact-email query param (no key/account); arXiv/ACL Anthology need no credentials but a descriptive User-Agent/contact string is a courtesy for arXiv. Source: README §12.
- **N-06 — Reproducibility.** Services orchestrated via Docker Compose: `backend`, `frontend`, `neo4j`, `qdrant`, `postgres`, `grobid`. Fresh clone + `docker-compose up` + configured `.env` must fully stand up the system (Phase 14 done-when). Source: README §12, §15 Phase 14.
- **N-07 — Testing.** pytest for unit/integration tests per module, written before implementation. Source: README §7, §14 pt 8.

## Protocols (pipeline stages / phase build-and-done-when criteria)

- **P-01 — Stage 1 / Phase 0 — Scaffolding.** Repo structure per §11; `docker-compose.yml` bringing up empty Neo4j/Qdrant/Postgres + `/health`. No ML/NLP logic yet. Done when: `docker-compose up` runs cleanly and `/health` returns 200 with all service connections verified.
- **P-02 — Stage 1 / Phase 1 — Literature Collection.** Build normalized-corpus writer (`corpus.py`, `dedup.py`) first, then each source: arXiv (metadata+PDF), Semantic Scholar (metadata + `openAccessPdf` link, metadata-only by design), ACL Anthology (metadata+PDF, feeds same corpus writer, no parallel path), Unpaywall enrichment (post-discovery, DOI-with-no-PDF records only, creates no new records), manual upload (`POST /upload`, `source='manual_upload'`). Done when: a topic yields N deduplicated papers with Unpaywall enrichment applied, and a manual upload lands in the same corpus indistinguishable except for `source`.
- **P-03 — Stage 2 / Phase 2 — Parsing & Section Normalization.** GROBID (primary) → structured sections/paragraphs/sentences; PyMuPDF/pdfplumber fallback; SciBERT rhetorical-role classifier normalizes headings. Reads normalized corpus only, never branches on `source`. Also backfills metadata for `manual_upload` records via GROBID header extraction; DOI recovered this way becomes eligible for Semantic Scholar/Unpaywall enrichment. Done when: every paper has structured JSON with normalized sections/paragraphs/sentences, and no manual upload remains without recoverable metadata.
- **P-04 — Stage 3 / Phase 3 — Scientific IE.** spaCy + Transformer pipeline (SciBERT/DeBERTa) extracts Contribution/Limitation/Future Work/Dataset/Model/Metric statements, stored in Postgres linked to `paper_id`. Done when: each parsed paper has structured fact rows queryable by type.
- **P-05 — Stage 4 / Phase 4 — Joint NER + Relation Extraction.** SciERC-style joint model (DyGIE++ or equivalent) extracts entities + relations in a single pass (avoids NER-error cascade); writes to Neo4j. Done when: graph contains papers connected to entities via typed relationships, queryable via Cypher.
- **P-06 — Stage 5 / Phase 5 — Embeddings & Vector Indexing.** SPECTER2 batch-embeds every paragraph/limitation/future-work statement; Qdrant stores vectors with metadata; linked back to Neo4j node IDs. Done when: a semantic query returns relevant paragraphs with correct metadata and graph linkage.
- **P-07 — Stage 6 / Phase 6 — Clustering + Contradiction Detection.** HDBSCAN/BERTopic clusters by SPECTER2 similarity (no fixed cluster count); DeBERTa-MNLI runs pairwise NLI within clusters to flag contradictions. Done when: clusters stored with member statements and contradiction flags attached where applicable.
- **P-08 — Stage 7 / Phase 7 — Trend Analysis.** Pandas/NumPy aggregate per-cluster mention counts by year, compute trend slope/score. Done when: each cluster has a time series and trend score exposed via the API.
- **P-09 — Stage 8 / Phase 8 — Gap Ranking.** Implements the `gap_score` formula (S-04); ranks clusters, stores in Postgres. Done when: a ranked, queryable list of gap clusters with individual signal breakdowns is available via the API.
- **P-10 — Stage 9 / Phase 9 — Mode A RAG Explanation.** Top-N supporting paragraphs retrieved from Qdrant per top-ranked cluster, passed to LLM in a single pass (no LangGraph loop) producing structured Gap/Evidence/Confidence/Suggested-Directions report. Done when: top-K ranked gaps each have a complete, evidence-grounded report referencing real retrieved paragraphs/papers.
- **P-11 — Stage 10 / Phase 10 — Mode B LangGraph.** Stateful graph: retrieve (vector+graph) → assess sufficiency → reformulate/retry (capped rounds) → rerank (cross-encoder) → generate → grounding check → return or regenerate with stricter prompt. Done when: a test question reliably returns an answer whose claims trace back to specific retrieved papers, with visible retry behavior when initial retrieval is weak.
- **P-12 — Phase 11 — Backend API Integration.** Wires Phases 1–10 into the full REST surface (see API Contracts above). Done when: full backend usable end-to-end via documented REST endpoints without manual internal scripts.
- **P-13 — Phase 12 — Frontend Dashboard.** Views: ranked gap reports, trend charts, cluster/knowledge-graph exploration, chat-style Mode B interface with citations. Done when: a researcher can browse gap reports and ask interactive questions entirely through the UI.
- **P-14 — Phase 13 — Evaluation Harness.** Implements the three evaluation methods (time-shifted validation, survey cross-check, expert scoring) from §10. Done when: running the harness against a test corpus produces a concrete evaluation report with precision/validity indicators.
- **P-15 — Phase 14 — Full Deployment.** Single `docker-compose up` brings up backend, frontend, Neo4j, Qdrant, Postgres, GROBID together via env config, with documented local run procedure. Done when: fresh clone + `docker-compose up` + `.env` configured results in a fully working system end-to-end. **Followed by exactly one `/code-review ultra` full-system pass, once, after approval** (see D-12 in decisions.md).

---

## Additional SPEC content (not itemized above, available in source)

- §2 Why existing tools fall short (competitive positioning table: Google Scholar, Semantic Scholar, Connected Papers, Scite, NotebookLM/ChatPDF).
- §5 Two operating modes (Mode A batch/offline, Mode B interactive) share the same backend.
- §7 Complete tech stack table (Python 3.11+, PyTorch, Hugging Face Transformers, GROBID, PyMuPDF/pdfplumber, SciBERT, SPECTER2, spaCy, DyGIE++-style joint model, DeBERTa-MNLI, Neo4j, Qdrant, PostgreSQL/SQLite-dev, HDBSCAN/BERTopic, cross-encoder reranker (e.g. MS MARCO MiniLM), Pandas/NumPy, LangGraph, Llama 3.x/Qwen/GPT via API, Plotly/D3.js, FastAPI, React+TypeScript, pytest, Docker/Docker Compose).
- §10 Evaluation strategy detail (time-shifted validation, survey cross-check, expert scoring 1–5 scale).
- §11 Full repository structure tree.
- §13 Full skill-to-phase mapping table and command reference (`/superpowers:*`, `/gsd:*`, `/code-review`, `/code-review ultra`, `/security-review`, `/contextmode:ctx-stats`, mem-search).
- §16 Future extensions (post-MVP, explicitly out of scope for the phased build): cross-domain gap discovery, learned research-opportunity ranking.

All of the above is traceable to `README.md` and available for the roadmapper to pull additional detail from directly; this file captures the constraint-level extraction, not a full transcription.

# Priority Fix Plan: `/pipeline/run` Recovery, Performance, Incremental Processing, Observability

- **Date:** 2026-09-15
- **Status:** Plan only. Nothing here is implemented. Every step needs the owner's explicit go-ahead (CLAUDE.md operating model).
- **Basis:** Static code inspection of the current working tree. No data was read or modified, and no pipeline, network or database calls were made. The earlier performance audit is not recorded in the repo, so its findings come from the owner's summary. This plan checks them against the code.

---

## 0. Key findings

1. **PostgreSQL has one transaction for Stages 2–8.** The only commit is at [routes_backend.py:274](backend/app/api/routes_backend.py#L274). **Neo4j commits every Cypher statement as it runs** (plain `session.run`, [graph.py:25](backend/app/ner_re/graph.py#L25)). **Qdrant keeps each batch of vectors as soon as it is written** (`wait=True`, [indexer.py:258](backend/app/embeddings/indexer.py#L258)). A SIGKILL therefore leaves Neo4j and Qdrant *ahead* of PostgreSQL. This matches the audit.
2. **Stage 4 reads Stage 3's uncommitted facts** through the same SQLAlchemy session. Neo4j can hold entities built from facts that PostgreSQL later rolled back.
3. **Neo4j writes only ever add or update.** Nothing deletes a paper's old edges, so the graph can only grow: current facts plus leftovers from earlier runs.
4. **Header backfill calls `unpaywall.enrich()` for the whole corpus** ([header_backfill.py:77](backend/app/parsing/header_backfill.py#L77)). It downloads PDFs to `{pdf_dir}/{id}.pdf` inside the pipeline, and those files survive a PostgreSQL rollback.
5. **Any successful Stage 6 rebuild deletes all existing reports.** `replace_clusters` wipes every table with a foreign key to `clusters`, including `gap_reports` ([clustering/storage.py:98](backend/app/clustering/storage.py#L98)). Inside `/pipeline/run` that deletion is committed at line 274, so if Stage 9 then fails there are no reports at all.
6. **No logging is configured anywhere**: no `basicConfig`, no `dictConfig`, no `--log-level`. Every existing `logger.info("Stage N: …")` is discarded. Only WARNING and above reach stderr (Python's last-resort handler), with no timestamps.
7. **There is no standalone endpoint for parsing or extraction.** Stages 2–3 can only be committed by a full `/pipeline/run`, so recovery can't currently be done stage by stage.

---

## A. Current stage dependency map

```
papers(PG) + PDF files
  └─S2 parse (GROBID/fallback, SciBERT headings) ──► parsed_documents            [PG, request txn]
       └─ manual upload, no title: header backfill ──► papers.title/doi
            └─ Unpaywall enrich (network; writes PDF files) ──► papers.pdf_path   [PG txn + FILES, not txn]
parsed_documents ──S3 rules + SciBERT──► extracted_facts (delete+insert per paper) [PG, request txn]
papers.abstract + extracted_facts(uncommitted) ──S4 GLiNER2──► Neo4j Paper/Entity/MENTIONS/SUPPORTS/REL{paper_id}
                                                                          [Neo4j, auto-commit per statement]
parsed_documents + facts(limitation,future_work) + papers.year/venue + Neo4j elementId(Paper)
  ──S5 SPECTER2──► Qdrant points (uuid5 ids) then prune_paper              [Qdrant, per batch; prune best-effort]
Qdrant gap points (NOT PG facts) ──S6 HDBSCAN + DeBERTa NLI──► clusters, members, contradictions
                                   (wipes trends, rankings, gap_reports)  [PG, request txn]
clusters + members.year ──S7──► cluster_trends                            [PG, request txn]
clusters + trends + papers.citation_count + ALL contribution facts ──S8──► cluster_rankings  [PG, request txn]
═══ COMMIT (routes_backend.py:274) ═══
rankings + clusters + Qdrant retrieve + papers.title + Groq ──S9──► gap_reports  [PG, 2nd txn]
```

- **Transactional:** S2, S3, S6, S7 and S8 share one PostgreSQL transaction; S9 has its own.
- **Not transactional:** S4 (Neo4j), S5 (Qdrant), and the PDF downloads in S2's backfill.
- **Mixed snapshot:** S6 reads Qdrant while S8 reads PostgreSQL facts. Stages 6–8 only agree when those two stores agree.

---

## B. Current idempotency and recovery matrix

| Stage | Input → output | Store / transaction boundary | Safe to rerun? | Duplicates or stale data | Recovery after SIGKILL |
|---|---|---|---|---|---|
| S2 parse | PDF → `parsed_documents` (+ `has_full_text` flags, manual-upload metadata) | PG request txn; downloaded files not transactional | Yes. Skips papers that already have a row ([parsing/service.py:95](backend/app/parsing/service.py#L95)) | No duplicates (unique `paper_id`). Stale: a changed PDF is never re-parsed; a fallback parse is kept forever | All rows lost; redone next run. Orphaned Unpaywall PDFs stay on disk |
| S3 extract | sections → `extracted_facts` | PG request txn; per paper delete then insert ([extraction/storage.py:64](backend/app/extraction/storage.py#L64)) | Yes | No duplicates (unique constraint). Surrogate ids change every run, so Qdrant's `fact_id` payload no longer matches | Rolled back; previous facts survive. Every parsed paper is redone |
| S4 NER/RE | abstract + facts → graph | Neo4j, one auto-commit per statement ([graph.py:41](backend/app/ner_re/graph.py#L41)) | Yes, no duplicates (`MERGE` on constraints; relations keyed by `paper_id`) | **Stale**: edges are never removed; `e.name` only set on create; a kill mid-paper leaves that paper's graph partly written | Durable. May reflect rolled-back facts. A rerun re-merges, but leftovers stay |
| S5 index | parsed doc + facts + Neo4j node id → Qdrant points | Qdrant, durable per batch of 16; prune after each paper, and prune errors are swallowed ([indexer.py:325](backend/app/embeddings/indexer.py#L325)) | Yes (uuid5 ids + prune) | A paper cut off mid-way has new and old points mixed, un-pruned. Points for papers that stop being indexable (no parsed doc or no graph node) are **never pruned** ([indexer.py:411-417](backend/app/embeddings/indexer.py#L411-L417)). Payload `fact_id` can point at rolled-back ids | Durable up to the paper in progress. A rerun converges for papers that can still be indexed |
| S6 cluster | Qdrant gap points → `clusters`, `cluster_members`, `cluster_contradictions` | PG request txn; replaces everything | Yes (deterministic ordering by point id) | Cluster ids change every rebuild. **Also deletes `gap_reports`** | Rolled back; previous clusters survive (their point ids still exist in Qdrant) |
| S7 trends | clusters → `cluster_trends` | PG request txn | Yes, deterministic | None | Rolled back; previous rows survive |
| S8 rankings | clusters + trends + facts + citations → `cluster_rankings` | PG request txn | Yes, deterministic | None | Rolled back; previous rows survive |
| S9 reports | rankings + retrieval + LLM → `gap_reports` | PG, second txn | Costs LLM calls; generates different text each time | Reports were already deleted by S6 inside `/pipeline/run` | Zero reports if a kill lands after the line-274 commit |

---

## 1. Priority 1: interrupted-run consistency

### 1.1 What the code says the current state is (the kill happened in S5)

- **PostgreSQL** equals the last successful commit. The killed run left nothing: no new parsed documents, backfill metadata, `has_full_text` changes or facts. Previous clusters, trends, rankings and reports survive.
- **Neo4j** holds the earlier graph plus the killed run's Stage 4 output, probably for every paper. For papers first parsed in that run, it holds entities built from facts PostgreSQL doesn't have.
- **Qdrant** holds the earlier points, plus the killed run's re-index of every paper with an id up to K, where K is the paper in progress. That includes paragraph and fact points for papers whose parse was rolled back. Paper K itself may be partly written and un-pruned.
- **Files:** there may be `{id}.pdf` files that no `papers.pdf_path` points to.

The code alone does not prove the stores are *materially* inconsistent. For papers parsed before the killed run, a deterministic re-extraction writes the same facts, the same Neo4j `MERGE`s and the same Qdrant ids. The real divergence is limited to: papers first parsed in the killed run, paper K, the dangling `fact_id` payloads, and whatever leftovers earlier runs already produced.

### 1.2 Recovery strategy for the current state

**Step 0: freeze.** Don't call POST `/pipeline/run`, `/graph`, `/index`, `/clusters`, `/trends`, `/gaps` or `/reports`. POST `/clusters` in particular would cluster the orphaned Qdrant points and commit them into PostgreSQL. GET endpoints are safe.

**Step 1: backups (owner actions).**
- Export the reports: `GET /reports > reports.json`. The next successful run deletes them, and LLM output can't be regenerated identically.
- `pg_dump` the database.
- Take a Qdrant snapshot (`POST /collections/<name>/snapshots`).
- Dump Neo4j. On Community edition this requires stopping the database, or copying the volume while it's stopped.

**Step 2: find the cause of the SIGKILL before running anything again** (README §13.6).
- `docker inspect <backend> --format '{{.State.OOMKilled}} {{.RestartCount}}'`
- Docker Desktop / WSL memory limits.
- Whether Qdrant or Neo4j also restarted (`State.StartedAt`).

**Step 3: inspect first (read-only).**

PostgreSQL:

```sql
-- Last committed run: now() is the transaction START time, so one committed run shares one timestamp
SELECT created_at, count(*) FROM extracted_facts GROUP BY 1 ORDER BY 1;
SELECT DISTINCT created_at FROM clusters;          -- repeat for cluster_trends, cluster_rankings
SELECT min(created_at) FROM gap_reports;           -- later than the above = Stage 9 of the same run
SELECT max(id) FROM extracted_facts;
SELECT parser_used, count(*) FROM parsed_documents GROUP BY 1;
SELECT id FROM papers p WHERE has_full_text
  AND NOT EXISTS (SELECT 1 FROM parsed_documents d WHERE d.paper_id = p.id);  -- set B: pending parse
SELECT id FROM papers WHERE source = 'manual_upload' AND title IS NULL;
```

Neo4j:

```cypher
MATCH (p:Paper) RETURN p.id, size([(p)-[:MENTIONS]->() | 1]) AS mentions ORDER BY p.id;
MATCH ()-[r]->() WHERE r.paper_id IS NOT NULL RETURN r.paper_id, type(r), count(*);
MATCH (e:Entity) WHERE NOT ()-[:MENTIONS|SUPPORTS]->(e) RETURN count(e);   // orphaned entities
```

Qdrant:
- Read the collection info: `points_count`, `status`, vector size (768 expected).
- Scroll with `with_vectors=false` and payload fields `paper_id, source_type, fact_id, graph_node_id, text`, then aggregate offline.

Filesystem:
- List files in the PDF volume that no `papers.pdf_path` references. **Don't delete them.**

**Step 4: identify the last safely completed stage.**
- **PostgreSQL:** if `extracted_facts`, `clusters`, `cluster_trends` and `cluster_rankings` share one `created_at` (T), Stage 8 of the run at T is the last consistent point. `gap_reports.created_at > T` means Stage 9 of that run also completed. A different `clusters` timestamp means POST `/clusters` was run on its own.
- **Qdrant forensic:** PostgreSQL sequences don't roll back. Any point whose `fact_id` is greater than `max(extracted_facts.id)` was written by the killed run. The largest such `paper_id` is approximately K. Papers with only paragraph points can't be dated this way, but their content is position-keyed and normally identical.
- **Neo4j** stores no timestamps, so it can only be checked against PostgreSQL.

**Step 5: classify the damage.** Compute the *expected* Qdrant id set from PostgreSQL with a read-only script. `build_index_records(paper, doc, facts, "x")` works because point ids don't depend on `graph_node_id`. Then compare:

| Check | Meaning |
|---|---|
| `expected − actual` | missing points |
| `actual − expected`, for papers in set B | points from rolled-back parses |
| `actual − expected`, for papers in PG | legacy id schemes, or paper K partly written |
| duplicates on `(paper_id, source_type, text)` | leftovers from the old `fact.id`-keyed id scheme |
| `payload.graph_node_id ≠` current Neo4j `elementId` | broken link back to Neo4j |
| Neo4j `MENTIONS` for papers in set B | graph ahead of PostgreSQL |

**Step 6: what can be rebuilt and what must not be deleted.**
- **Safe to rebuild (derived data):**
  - `parsed_documents` (needs GROBID healthy, or fallback output will differ)
  - `extracted_facts`
  - the whole Neo4j graph
  - the whole Qdrant collection
  - `clusters`, `cluster_members`, `cluster_contradictions`, `cluster_trends`, `cluster_rankings`
- **Never delete blindly:**
  - the `papers` table: external metadata, and arXiv is currently IP-blocked (PROGRESS 2026-09-15)
  - the `pdf_data` volume: manual uploads can't be replaced
  - backfilled manual-upload metadata
  - `gap_reports`: LLM output that can't be reproduced, and costs Groq calls
  - the `corpus_cache` and `model_cache` volumes
  - orphaned PDF files: valid Unpaywall downloads that the next backfill overwrites anyway
  - never run `docker compose down -v`

**Step 7: targeted versus clean rebuild.**
- **Targeted rebuild is possible for Qdrant today.** Per-paper upsert plus prune already converges. Orphans for papers that can no longer be indexed can be removed with a per-`paper_id` filter delete, but only after a snapshot and owner approval.
- **Targeted rebuild of Neo4j needs new code:** a per-paper "delete this paper's edges, then merge" (C3). Shared entities must only be removed when orphaned.
- **A clean rebuild of the derived stores** (Neo4j graph and Qdrant collection, never papers or PDFs) is safer if any of these hold:
  - legacy duplicate point ids exist in Qdrant
  - Neo4j contains edges current PostgreSQL facts can't explain (earlier thresholds or models, fallback parses)
  - a reproducible baseline is needed for the Phase 13 evaluation
- **Preconditions for either:** backups taken, SIGKILL cause fixed, GROBID healthy, reports exported, O1 logging and C1 per-stage commits in place.
- **Rebuild order:** Neo4j first, then Qdrant, because Qdrant's `graph_node_id` payload stores Neo4j `elementId`s.

**Step 8: checks before the next full run (repeat after it).**
- **PostgreSQL:**
  - stage tables share one commit timestamp
  - `cluster_members.point_id` ⊆ Qdrant ids
  - `cluster_trends.cluster_id` and `cluster_rankings.cluster_id` ⊆ `clusters.id`
  - every `papers.pdf_path` exists on disk
- **Qdrant:**
  - actual ids = expected ids
  - no duplicate `(paper_id, source_type, text)`
  - payload text = expected text
  - `graph_node_id` = current Neo4j `elementId`
  - collection status green; vector size matches the encoder
- **Neo4j:**
  - Paper ids ⊆ PostgreSQL papers
  - every paper with an abstract or facts has a node
  - no relationship `paper_id` missing from PostgreSQL
  - orphaned-entity count reported (a superset of PostgreSQL is expected until C3 exists)

### 1.3 Recommendation

Don't use the current single-transaction `/pipeline/run` as the recovery step. If it's killed again, the stores end up in the same state. Instead:

1. Do Steps 0–5.
2. Implement O1 (logging), C1 (per-stage commits) and C2 (per-paper Neo4j transaction).
3. Run once with `generate_reports=false`, then re-run the Step 8 checks.
4. Decide on C3 or a clean rebuild from that result.

Even with `generate_reports=false`, the run may call Unpaywall through header backfill.

---

## 2. Priority 2: CPU performance

### 2.1 SciBERT sentence batching (P1, Stage 3)

- **Current behaviour:** `SemanticStatementClassifier` embeds each rule-unmatched sentence as a batch of one ([semantic.py:58](backend/app/extraction/semantic.py#L58)), for every parsed paper on every run.
- **Change:** add `classify_many()`. Collect a paper's rule-unmatched sentences, make one `model.encode` call, and emit statements in the original order. sentence-transformers already sorts by length inside `encode` and restores the order, so length bucketing comes for free.
- **Interaction with the exemplar logic:**
  - Reference vectors are computed on the *first* call, batched with whichever sentence came first. Today's reference vectors therefore already depend on processing order (numerically).
  - With batching, compute the reference vectors once, on their own.
  - The keyword/rule path is unaffected; only rule-unmatched sentences reach SciBERT.
- **Why meaning is kept:** mean pooling with an attention mask is mathematically unaffected by padding; only float noise (~1e-6) changes. Parity means "same labels", not identical bits. With thresholds of 0.55 similarity and a 0.03 margin, a label could only flip on an exact near-tie.
- **Speed:**
  - theoretical: 5–10× on the semantic path
  - expected on this codebase: 2–6× on Stage 3 wall time (regex and database inserts unchanged)
  - to benchmark: label parity and timing on a sample of stored parsed documents

### 2.2 Heading classifier exemplar cache (P2, Stage 2)

- **Current behaviour:** `classify_heading` embeds `[heading, *7 exemplars]` for every non-keyword heading, recomputing exemplars each time ([section_classifier.py:103](backend/app/parsing/section_classifier.py#L103)).
- **Change:** cache exemplar vectors once; batch a paper's non-keyword headings in one call.
- **Speed:**
  - theoretical: ~8× fewer sequences for those headings
  - expected: small absolute gain, since only newly parsed papers hit Stage 2
  - to benchmark: argmax label parity only
- **Risk:** very low.

### 2.3 SPECTER2 length-sorted batches (P3, Stage 5)

- **Current behaviour:** `EvidenceIndex.upsert` slices each paper's records into batches of 16 in document order ([indexer.py:254-269](backend/app/embeddings/indexer.py#L254-L269)). Paragraphs (20–512 tokens) come first, then short facts. Every batch is padded to its longest text, and padded tokens are fully computed on CPU.
- **Change:** sort the *records* (not just their texts) by length inside `upsert` before slicing.
  - `zip(batch, vectors, strict=True)` stays aligned by construction.
  - Point ids are content/position-derived, so upsert order is irrelevant.
  - `prune_paper` is unchanged.
  - Doing this in `upsert` rather than in `Specter2Encoder.encode` leaves the order assertions in `test_encoder.py` untouched.
- **Effect on stored data:** the CLS vector is mathematically unaffected by padding, but stored vectors shift by about 1e-6. That could move a borderline HDBSCAN point or reorder near-tied retrieval results.
- **Speed:**
  - theoretical: 2–4× with highly uneven lengths
  - expected on this codebase: 1.2–1.8× on Stage 5 encoding
- **Benchmark before implementing:**
  - padding ratio (Σ batch_max_len × B ÷ Σ real tokens), current vs sorted, computed with the tokenizer only, no model run
  - minimum cosine similarity between old and new vectors on a sample
  - cluster stability (adjusted Rand index) on a sample

### 2.4 GLiNER2 batching (P4, Stage 4)

- **Current behaviour:** `extract_many` loops over up to 12 chunks per paper, calling `model.extract` once per chunk ([extractor.py:111-121](backend/app/ner_re/extractor.py#L111-L121)).
- **Why batching is plausible:** in the installed gliner2 2.0.0, `score()` is literally `batch_score([text], batch_size=1)` ([gliner2/joint_ie/scoring.py:199-206](backend/.venv/Lib/site-packages/gliner2/joint_ie/scoring.py#L199-L206)). `JointIE.batch_extract(texts, schema, config=JointIEConfig(batch_size=N))` takes the same boundary-architecture path, then decodes each result separately ([gliner2/joint_ie/engine.py:366-381](backend/.venv/Lib/site-packages/gliner2/joint_ie/engine.py#L366-L381)). Output meaning is the same by construction, apart from float padding noise.
- **Risks:**
  - The entity threshold (0.15) and candidate threshold (0.05) leave many borderline candidates, and beam decoding can break near-ties differently.
  - Whether `boundary_head(..., return_candidates=True)` selects candidates strictly per row is not verified.
  - One bad chunk would fail the whole batch. The adapter must fall back to per-chunk `extract` on an exception to preserve today's skip-one-chunk behaviour.
  - Merge order must be preserved (results zipped back in chunk order), because `_merge` keeps the first of equal-confidence mentions.
- **Speed:**
  - theoretical: 1.5–3× on the model forward pass
  - expected on this codebase: 1.2–2× on Stage 4, because chunks are already packed to similar lengths and Python beam decoding is unchanged
- **Benchmark before implementing:** entity/relation set parity on a sample, and time split between forward pass and decoding.

### 2.5 Related items

- **C2, Neo4j:** write each paper in one write transaction with `UNWIND` instead of 1 + E + R auto-commit statements. Far fewer round trips, and a paper's graph becomes all-or-nothing.
- **P5:** batch across papers in Stages 4 and 5. Larger batches, but it conflicts with per-paper prune and fault isolation, so defer until the incremental design exists.
- **P6:** torch/OMP thread count vs the container's CPU quota. Benchmark only; not inspected.

---

## 3. Priority 3: incremental processing

### 3.1 Stages that currently reprocess the whole corpus on every run

- **S3, S4, S5:** every paper, every run.
- **S6:** full HDBSCAN plus NLI.
- **S7, S8:** full recompute (cheap).
- **S9:** top-K LLM calls every run.
- **S2** already skips parsed papers, but it never notices a changed PDF, and it retries header backfill (network calls) for untitled manual uploads on every run.

### 3.2 What counts as "changed": per-stage input fingerprints

| Stage | Input hash to store | Can run per paper? | Invalidated by |
|---|---|---|---|
| S2 | `pdf_sha256` + `PARSER_VERSION` constant + GROBID version | Yes | changed PDF, parser code or version bump. Re-parsing fallback output once GROBID is back is an owner decision |
| S3 | sha256(canonical sections JSON) + extraction version (rules version, semantic model, 0.55/0.03 thresholds) | Yes | S2 output |
| S4 | sha256(`collect_paper_text` chunks, abstract included) + NER model, thresholds, chunk caps, schema hash (`ENTITY_DESCRIPTIONS`, `RELATION_ENDPOINTS`) | Yes | S3 output or abstract change. Title/year/venue changes only need a cheap Paper node property update |
| S5 | per point: sha256(text) + embedding version (model, adapter, revision, max tokens, max chars); per paper: hash of sorted `record_id \| text_sha \| year \| venue \| section_type \| graph_node_id` | Yes | S2/S3 text. Year/venue changes use `set_payload`, no re-embedding |
| S6 | sha256(sorted gap `point_id \| text_sha`) + embedding version + clustering/NLI settings | **No, full recompute** | any gap point change. `center_vectors` subtracts the corpus mean ([cluster.py:87](backend/app/clustering/cluster.py#L87)), so one new point shifts every vector; the `max_statements` cut-off depends on point ids too. **Skip only** when the fingerprint is unchanged. Later option: cache NLI pair scores keyed by text hashes + model settings |
| S7 | — | Always recompute (milliseconds) | clusters, paper years |
| S8 | — | Always recompute | clusters, trends, citation counts, and **all** contribution facts. `solution_density` is corpus-wide ([ranking/service.py:65](backend/app/ranking/service.py#L65)), so any new paper changes every cluster |
| S9 | ranking row + member point ids + label + evidence ids + prompt/model version | Needs a report storage redesign | today reports are deleted on every S6 rebuild. Short term: skip S6–S9 entirely when upstream fingerprints are unchanged. Reusing report text is a semantics change needing owner approval |

### 3.3 How each store should track state

The principle: each store records "this paper is done" **in the same transaction as the data itself**, and PostgreSQL holds the desired state.

**PostgreSQL (additive DDL through [core/schema.py](backend/app/core/schema.py); there is no migration framework):**

```
papers.pdf_sha256            char(64) NULL
paper_stage_state(paper_id FK, stage, input_hash, stage_version, status pending|done|failed,
                  run_id, updated_at, error, PRIMARY KEY (paper_id, stage))
pipeline_runs(id, started_at, finished_at, status running|succeeded|failed|aborted,
              current_stage, config_fingerprint, counts JSON, error)
derived_state(name PK, input_fingerprint, run_id, updated_at)   -- written in the same txn as replace_clusters
```

- **S2/S3:** commit the data and the `done` marker in one transaction per paper.
- **External stores:** PostgreSQL records `done` only *after* the Neo4j or Qdrant write has succeeded.

**Neo4j:**
- One write transaction per paper that:
  1. deletes the paper's `MENTIONS`, `SUPPORTS` and `{paper_id}` relationships
  2. merges the new ones
  3. sets `p.nerre_input_hash` and `p.nerre_version`
- Clean up orphaned entities in a separate pass, because entities are shared between papers.

**Qdrant:**
- Add `text_sha256` and `embed_version` to the payload. This changes the `PAYLOAD_FIELDS` contract, and `retrieve()` spreads the payload into its results, so tests need updating.
- For each paper:
  1. upsert only new or changed points
  2. `set_payload` for metadata-only changes
  3. prune
  4. commit the PostgreSQL marker
- A prune failure must leave the marker not done (today it's swallowed).
- Stage 6 must read gap points only for papers whose S5 marker is `done` and current.

### 3.4 Deletions and changed PDFs

- **Today there is no paper delete path.**
- **Deleting a paper:**
  1. mark it `tombstone` in `paper_stage_state`
  2. delete Qdrant points by `paper_id` filter
  3. delete the paper's Neo4j edges and node, then orphaned entities
  4. delete its PG facts, parsed document and state rows
  5. delete the `papers` row
  6. run a full S6–S8 rebuild

  The tombstone lets an interrupted deletion resume.
- **Changed PDF (same paper id, new sha):** invalidates S2 and cascades through the hash comparisons. Positional paragraph ids get pruned, and Neo4j is replaced per paper.
- **Missing PDF:** current behaviour (mark metadata-only, keep derived evidence) stays until the owner decides otherwise.
- **Model or config version change:** a `stage_version` mismatch invalidates that stage for every paper and causes a controlled full rebuild of it. An embedding model change also needs a new collection (the existing dimension check refuses to mix them).

### 3.5 Resuming an interrupted incremental run

- **Run lock:** hold a PostgreSQL session advisory lock for the whole run. It's released automatically when the connection dies (including SIGKILL), and it also blocks two concurrent runs, which nothing prevents today.
- **On start:** mark a stale `pipeline_runs` row with status `running` as `aborted`.
- **S2–S5:** resume per paper by comparing input hashes with their `done` markers.
- **S6–S8:** run only when every eligible paper is `done` for S5, in one PostgreSQL transaction (as today).
- **S9:** stays in its own transaction.

---

## 4. Priority 4: observability

### 4.1 Setup

- **O1:** add `backend/app/core/logging.py` with `configure_logging()`, called from [main.py](backend/app/main.py).
  - add a `LOG_LEVEL` setting in `config.py` and document it in `.env.example`
  - format: `%(asctime)s %(levelname)s %(name)s run=%(run_id)s %(message)s`
  - set `httpx`, `httpcore`, `neo4j`, `transformers` and `adapters` loggers to WARNING
  - standard library only, no new dependency
- **O2:**
  - a `stage_timer(logger, name)` context manager that logs start, completion or failure, and elapsed time via `time.perf_counter`
  - a `ProgressLogger(total)` that logs every ~5% or every 30 s, whichever comes first, with done/total, rate and ETA
  - keeps output to roughly 20–40 lines per stage
- **O3:** a `run_id` per `/pipeline/run`, attached through a contextvar logging filter.
- **Levels:** stage events at INFO, per paper/batch at DEBUG, per-paper failures at WARNING/ERROR with the paper id.
- **Never log:** sentence/paragraph text, abstracts or API keys. Counts only.

### 4.2 What each stage should report

| Where | Start | Progress (throttled) | Completion |
|---|---|---|---|
| `run_pipeline` | run_id, `generate_reports`, papers / full-text / parsed counts | — | elapsed per stage, result counts, time spent in `session.commit()`; on failure: stage, elapsed, exception type |
| S2 `parse_all_pending` | total papers, pending, already parsed | papers parsed, GROBID vs fallback | processed, fallback count, missing PDFs, backfill tries/successes, Unpaywall downloads, keyword vs SciBERT heading classifications |
| S3 `extract_all_parsed` | parsed documents | papers, sentences seen, rule hits, semantic calls | facts per type, sentences/s |
| Model loads (SciBERT, GLiNER2, SPECTER2, DeBERTa-MNLI) | — | — | model name, load time |
| S4 `extract_all` | papers | papers done, chunks/s, skipped (no text), failed | entities, relations, model time vs Neo4j write time |
| S5 `index_all` | papers | papers, records, vectors/s | points written, prune failures, papers skipped for **no parsed doc** / **no graph node** (silent today), failed, encode time vs upsert time |
| S6 `rebuild_clusters` | gap points loaded, scroll time | NLI: clusters done | existing message + HDBSCAN time, noise count, NLI pairs scored, NLI time |
| S7 / S8 | — | — | existing messages + elapsed |
| S9 `generate_top_reports` | top-K | per cluster: retrieval time, LLM time, outcome | stored vs dropped, elapsed |

---

## 5. Per-change scorecard

Columns 1–8 answer the eight questions asked for each change: (1) expected speed improvement, (2) risk to correctness, (3) files/functions affected, (4) whether it changes stored outputs, (5) whether existing tests need modification, (6) whether it can be implemented independently, (7) whether to benchmark before implementing, (8) whether it depends on the interrupted-run recovery design.

| ID | Change | 1. Speed | 2. Correctness risk | 3. Files / functions | 4. Stored output changes? | 5. Tests to modify | 6. Independent? | 7. Benchmark first? | 8. Depends on recovery design? |
|---|---|---|---|---|---|---|---|---|---|
| O1–O3 | Logging config, stage timers, progress, run_id | none | none | new `core/logging.py`, `main.py`, `config.py`, each stage `service.py`, `indexer.index_all`, `routes_backend.run_pipeline` | no | new caplog tests only | yes | no | no |
| C1 | Commit per stage (per paper for S2/S3); S6–S8 stay one transaction | none | low–medium: readers can see a half-finished run | `routes_backend.run_pipeline`, `parsing/service.parse_all_pending`, `extraction/service.extract_all_parsed` | no (final state the same) | yes: `test_pipeline_run_calls_every_stage_and_commits` and related | yes | no | **is** the recovery design |
| C2 | One Neo4j write transaction per paper with `UNWIND` | large cut in Neo4j round trips (measure with O2) | low | `ner_re/graph.write_paper_graph` | no | yes: fake-session graph tests | yes | no | yes |
| C3 | Replace a paper's graph instead of only adding | none | medium: removes leftover edges | `ner_re/graph.py`, `ner_re/service.py` | **yes** (drops stale edges) | yes | after C2 | no | yes; owner decision |
| C4 | Read-only reconciliation script (PG vs Neo4j vs Qdrant) | none | none | new script | no | new tests | yes | no | yes |
| C5 | Advisory lock + `pipeline_runs` table | none | low | `routes_backend.run_pipeline`, new table in `core/schema.py` | adds a table | new tests | yes | no | yes |
| C6 | Limit backfill's Unpaywall call to the backfilled paper | removes N×M network calls per run | low: behaviour change | `parsing/header_backfill.backfill_metadata`, `ingestion/unpaywall.enrich` | no | yes: backfill tests | yes | no | no; owner decision |
| P1 | SciBERT sentence batching | Stage 3 ≈2–6× (estimate) | low | `extraction/semantic.py` (`classify_many`), `extraction/service.extract_sections` | same labels expected (verify) | likely additive; check `tests/extraction/test_semantic.py` | yes | parity check | no |
| P2 | Heading exemplar cache + per-paper heading batching | small absolute | very low | `parsing/section_classifier.py`, `parsing/service.parse_and_store` | same labels expected | yes: `tests/parsing/test_section_classifier.py` | yes | parity check | no |
| P3 | SPECTER2 length-sorted batches | Stage 5 encoding ≈1.2–1.8× (estimate) | low–medium (float noise) | `embeddings/indexer.EvidenceIndex.upsert` | vectors shift ~1e-6; ids and payloads unchanged | new alignment test; existing tests unaffected | yes | **yes** | best measured on a verified baseline |
| P4 | GLiNER2 `batch_extract` + per-chunk fallback | Stage 4 ≈1.2–2× (estimate) | medium | `ner_re/extractor.extract_many`, `JointIEModel` Protocol, new `nerre_batch_size` setting (+ `.env.example`) | same sets expected (verify) | yes: fake model in `tests/ner_re/test_extractor.py` | yes | **yes** | no |
| P5 | Batching across papers (S4/S5) | unknown | medium | `ner_re/service.py`, `embeddings/indexer.py` | numeric | yes | after I-series | **yes** | yes |
| P6 | Torch/OMP thread tuning vs container CPU quota | unknown | low | compose env / encoder load | numeric | no | yes | **yes** | no |
| I1–I6 | State tables, hashes, per-paper skips for S3–S5, fingerprint skip for S6–S9, resume, NLI score cache | steady state: S2–S5 cost scales with *new* papers, not the corpus | medium–high (a hash that misses an input means stale output) | new tables; S2–S6 services; `indexer`; `graph`; `embeddings/schema.PAYLOAD_FIELDS` | adds state; Qdrant payload gains 2 fields | yes, broadly | no | yes (convergence tests) | **strongly** |

---

## C. Recommended long-term architecture

- **Source of truth:** `papers` and the PDF store are the only authoritative data. Everything else can be regenerated.
- **Ledger:** PostgreSQL records the desired state (input hashes per paper and stage, run history).
- **Projections:** Neo4j and Qdrant are rebuildable projections. Every write to them is an idempotent per-paper replace.
- **Stage checkpoints:**
  - per paper for S2–S5
  - one fingerprint-gated atomic PostgreSQL transaction for S6–S8
  - S9 in its own transaction
- **Cross-store consistency without two-phase commit:**
  - each store records "paper done" in the same transaction as its data (PG row, Neo4j node properties, Qdrant point hashes)
  - PostgreSQL records `done` for an external store only after that store's write succeeds
  - a crash between the two leaves the marker not done, and the idempotent rewrite repairs it on resume
- **Reconciliation:** a read-only verifier (C4) compares PostgreSQL's desired state with each store's recorded state. Run it after any failure and before every full run.
- **Single writer:** an advisory lock plus a `pipeline_runs` row. No background jobs, task queues or status endpoints (out of scope per CLAUDE.md).

---

## D. Phased implementation order

| Step | Work | Involves code? |
|---|---|---|
| 0 | Freeze write endpoints, back up, export reports, find the SIGKILL cause, read-only inspection (§1.2 Steps 0–5) | no |
| 1 | Observability: O1, O2, O3 | yes |
| 2 | Consistency foundations: C1, C2, C4, C5 | yes |
| 3 | Recovery run with `generate_reports=false`, run the Step 8 checks, decide on C3 or a clean rebuild of the derived stores | run + owner decision |
| 4 | Safe optimizations: P2, P1, with parity tests | yes |
| 5 | Benchmark-gated: build an offline read-only harness (compute in memory on stored parsed documents, no store writes), then P3, P4, P6 | yes |
| 6 | Incremental: owner sign-off (schema additions, C3 replace semantics, C6, report reuse) → I1 state tables → S3/S4/S5 skips → S6–S9 fingerprint skip → resume → delete / changed-PDF handling | yes |

---

## E. What static inspection cannot determine

These need database inspection or a controlled offline test:

- **Where the killed run stopped:** the paper K in progress, and whether Stage 4 finished for every paper. ERROR lines in `docker logs` are the only record, because INFO is dropped.
- **Commit history:** whether any `/pipeline/run` ever committed, and its timestamp T (§1.2 Step 4 queries).
- **The kill itself:** whether the SIGKILL was an out-of-memory kill, a timeout or a manual kill, and whether Qdrant or Neo4j restarted.
- **GROBID:** whether it was reachable during the killed run, and whether its output is stable across the pinned image version.
- **Leftover data:**
  - whether Qdrant still has legacy `fact.id`-keyed points
  - whether Neo4j holds edges from earlier thresholds or models
- **Batching parity:**
  - SciBERT label parity
  - SPECTER2 vector drift and its effect on clusters
  - GLiNER2 entity/relation set parity, including whether `boundary_head` selects candidates per row
- **Runtime profile:** the actual split of run time across stages, the container's CPU quota and torch thread settings.

---

## F. Recommended implementation order, in short

1. **Immediate recovery work**
   - Freeze write endpoints.
   - Export `/reports`; take `pg_dump`, a Qdrant snapshot and a Neo4j dump.
   - Find the SIGKILL cause.
   - Read-only inspection: commit timestamps, the `fact_id` check, expected vs actual Qdrant ids, Neo4j vs PostgreSQL.
2. **Safe performance optimizations**
   - P2: heading exemplar cache
   - P1: SciBERT sentence batching (with a label parity check)
   - C2: one Neo4j transaction per paper
3. **Benchmark-required optimizations**
   - P3: SPECTER2 length sorting
   - P4: GLiNER2 `batch_extract`
   - P6: thread tuning
   - P5: batching across papers
4. **Incremental-processing design and implementation**
   - C1 per-stage commits and C5 run lock first.
   - Then design sign-off, state tables and hashes, S3–S5 skips, S6–S9 fingerprint skip, and deletion / changed-PDF handling.
5. **Observability improvements**
   - O1 logging configuration first (it makes every benchmark and the recovery run measurable).
   - Then O2 stage timers and progress, then O3 run id.

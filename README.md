# Automatic Research Gap Identification from Scientific Literature

An AI research assistant that reads scientific literature at scale, builds a structured knowledge representation of it, and surfaces **evidence-backed research gaps** — instead of asking an LLM to "read 600 papers and tell me what's missing."

---

## How to Use This Document

This README is written to be followed by **both a human maintainer and an AI coding agent or IDE assistant** (e.g. Claude Code, Cursor, Antigravity, Codex, or any comparable tool).

- The **human (project owner)** controls pacing. Implementation is broken into numbered **Phases** (see [Section 15](#15-implementation-phases)). No phase should begin until the owner explicitly says so (e.g., *"start Phase 2"*, *"proceed"*).
- The **AI agent** should read this entire document before writing any code, then implement strictly phase by phase, in order, using the tech stack specified for that phase. **Once a phase is authorized, the agent executes it autonomously start to finish** — it does not stop to ask approval for individual tasks, plans, tests, fixes, or routine engineering decisions inside that phase (see [Section 13.5](#135-when-to-pause-mid-phase-owner-only-actions) for the narrow set of things that *do* require a pause). After finishing a phase, the agent runs the required tests and code review, summarizes what was built, flags any deviations or open questions, and **stops** — not continuing to the next phase automatically. If the owner says *"continue"* while a phase is already authorized and in progress, the agent resumes that phase; it only asks which phase to start if none is currently in progress.
- This project follows a disciplined development workflow — plan before implementing, tests before code, and a code review at the end of every phase. [Section 13](#13-development-workflow--quality-gates) defines the workflow in detail and which parts apply at which phase. The agent is expected to apply it automatically as specified, without being reminded each phase.
- Every phase entry in Section 15 carries a **Workflow:** line. That line is part of the phase's requirements, not a suggestion.

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Why Existing Tools Fall Short](#2-why-existing-tools-fall-short)
3. [Why This Problem Is Hard](#3-why-this-problem-is-hard)
4. [Our Approach](#4-our-approach)
5. [Two Operating Modes](#5-two-operating-modes)
6. [System Architecture](#6-system-architecture)
7. [Complete Tech Stack](#7-complete-tech-stack)
8. [End-to-End Pipeline](#8-end-to-end-pipeline)
9. [Mode B in Detail: Interactive GraphRAG with LangGraph](#9-mode-b-in-detail-interactive-graphrag-with-langgraph)
10. [Evaluation Strategy](#10-evaluation-strategy)
11. [Repository Structure](#11-repository-structure)
12. [Environment & Configuration](#12-environment--configuration)
13. [Development Workflow & Quality Gates](#13-development-workflow--quality-gates)
14. [Instructions for the Implementing AI Agent](#14-instructions-for-the-implementing-ai-agent)
15. [Implementation Phases](#15-implementation-phases)
16. [Future Extensions (Post-MVP)](#16-future-extensions-post-mvp)

---

## 1. Problem Statement

Imagine a Master's or PhD student beginning research on, say, Cybersecurity using Large Language Models. Their supervisor asks: **"Find a research problem that nobody has solved yet."**

In practice this means searching Google Scholar and ending up with hundreds of papers (e.g. 250 on LLM Security, 180 on Prompt Injection, 90 on Agentic AI, 150 on RAG — roughly 600 papers). Even skimming each in 20 minutes costs ~200 hours (~25 working days) — and after all that reading, the question *"what is actually missing?"* still isn't answered, because authors rarely state gaps explicitly. Instead they write scattered phrases like *"future work…"*, *"one limitation…"*, *"our approach cannot…"*, *"further investigation is required…"* across hundreds of documents, with no structure connecting them.

The current manual workflow looks like:

```
Search papers → Download PDFs → Read abstract → Read methodology → Read
conclusion → Read future work → Take notes → Compare across papers →
Identify common limitations → Guess a research gap
```

This can take weeks to months, and the output is a guess, not evidence.

## 2. Why Existing Tools Fall Short

| Tool | What it does | What it can't do |
|---|---|---|
| **Google Scholar** | Keyword search → relevant papers | No comparison, no gap detection |
| **Semantic Scholar** | Recommendations, citation graphs | Leaves all reasoning to the human |
| **Connected Papers** | Citation relationship maps | Can't answer "what problems remain unsolved?" |
| **Scite** | Supporting vs. contradicting citations | Doesn't identify trends across limitations |
| **NotebookLM / ChatPDF** | Summarizes uploaded papers well | No structured understanding of the literature — "what's the research gap?" is answered purely from LLM guesswork with no grounding |

## 3. Why This Problem Is Hard

Research gaps are almost never stated explicitly, and the same underlying gap is phrased differently by every author. For example:

- Paper A: *"Current methods fail in low-resource languages."*
- Paper B: *"Performance decreases for multilingual datasets."*
- Paper C: *"More work is required for cross-language reasoning."*
- Paper D: *"Existing datasets focus primarily on English."*
- Paper E: *"Generalization to regional languages remains limited."*

A human instantly recognizes these as **the same underlying gap**. A system that relies on keyword matching sees five unrelated sentences. Teaching a system to recognize that these statements express the same limitation is fundamentally a semantic-understanding (NLP) problem — not a summarization problem.

## 4. Our Approach

We are not building another paper summarizer. We are building a system that can answer:

- What are the most common limitations in a field?
- Which problems are repeatedly mentioned but never solved?
- Which datasets are underexplored?
- Which methods consistently fail?
- Which future-work topics recur most often?
- Which combinations of ideas have never been explored together?

**Core innovation:** most existing tools follow `PDF → LLM → Summary`. We instead build a **structured representation of scientific knowledge first**, and only bring the LLM in at the very end, to *explain* evidence that has already been extracted, clustered, and ranked — not to *invent* an answer from raw text.

```
Papers → Structured Facts → Knowledge Graph → Semantic Clusters →
Frequency & Trend Analysis → Ranked Gaps → LLM Explanation (grounded)
```

The LLM comes **last**, not first. This is the single most important design decision in the project — it's what makes the output explainable and reproducible rather than a free-form hallucination-prone guess.

## 5. Two Operating Modes

The system operates in two complementary modes that share the same backend (knowledge graph + vector index):

### Mode A — Autonomous Discovery
A batch pipeline that runs over a collected corpus and proactively produces ranked, evidence-backed gap reports without the user asking anything specific. This is the core deliverable — the "brain" that discovers gaps at scale.

```
Ingest (arXiv · Semantic Scholar · ACL Anthology · Manual Upload)
  → Unpaywall enrichment → Normalized Corpus → Parse → Extract →
  Cluster → Analyze Trends → Rank Gaps →
  Generate Grounded Explanations → Gap Reports
```

### Mode B — Interactive Query
A user asks a specific question (*"What research gaps exist in prompt injection detection?"*) and the system performs on-demand GraphRAG — vector search + graph traversal + reranking + LLM generation — grounded in the same data Mode A built, with a retry loop if evidence is insufficient (see [Section 9](#9-mode-b-in-detail-interactive-graphrag-with-langgraph)).

```
Question → Retrieve (vector + graph) → Sufficient evidence? →
[no → reformulate → retry] → Generate Answer → Grounding Check → Return Answer
```

Both modes are implemented — Mode A is the batch/offline engine, Mode B is the interactive layer on top of the same data. Building Mode A's backend (graph + vector store) gives Mode B most of what it needs for free.

## 6. System Architecture

```
   ┌─────────── Ingestion Sources (Phase 1) ────────────────────────┐
   │  arXiv        Semantic Scholar      ACL Anthology    Manual    │
   │  (full PDF)   (metadata + OA link)  (full PDF)       PDF Upload│
   └───────┬───────────────┬──────────────────┬───────────────┬─────┘
           │               │                  │               │
           └───────────────┴────────┬─────────┴───────────────┘
                                    │
                   Unpaywall DOI Enrichment (fills in a legal
                   open-access full text where one is missing)
                                    │
                                    ▼
              Normalized Paper Corpus  (PostgreSQL + PDF store)
              — one canonical schema, source-agnostic from here on —
                                  │
                                  ▼
                         Research Papers (PDF)
                                  │
          ┌───────────────────────────────────────────┐
          │   Contributions · Limitations · Future     │
          │   Work · Dataset · Model · Metrics          │
          └───────────────────────────────────────────┘
                                  │
                                  ▼
                Scientific Information Extraction (IE)
                                  │
                                  ▼
             Joint Scientific NER + Relation Extraction
              (Models · Datasets · Tasks · Domains ·
               "trained on" / "fails on" / "evaluated using")
                                  │
                                  ▼
                 Knowledge Graph Construction (Neo4j)
                                  │
                                  ▼
        Semantic Clustering of Similar Limitations (embeddings)
              + Contradiction Detection (NLI) within clusters
                                  │
                                  ▼
                      Frequency & Trend Analysis
                                  │
                                  ▼
                       Research Gap Ranking
                                  │
                                  ▼
              LLM Explanation (RAG-grounded, Mode A)
                                  │
                                  ▼
        Ranked, Evidence-Backed Gap Reports  ──────►  Dashboard

                                                     ▲
                     Mode B (Interactive) ───────────┘
        User Question → LangGraph Retrieve/Assess/Reformulate
        Loop → GraphRAG (Neo4j + Qdrant + reranker) →
        Grounded Answer with Citations
```

## 7. Complete Tech Stack

| Layer | Technology | Role |
|---|---|---|
| Language / Runtime | Python 3.11+ | Entire backend |
| Ingestion — arXiv | `arxiv` Python package | Primary open-access source: metadata **and** downloadable full-text PDFs |
| Ingestion — Semantic Scholar | Semantic Scholar API via `httpx` | Cross-venue **metadata** (abstract, DOI, venue, citation counts) + `openAccessPdf` link when one exists. Metadata-only — it does not host paywalled full text |
| Ingestion — ACL Anthology | ACL Anthology bibliographic export + direct PDF URLs | Fully open-access full text for NLP/CL venues (ACL, EMNLP, NAACL, COLING, TACL, CL) that arXiv does not reliably cover |
| Ingestion — Unpaywall | Unpaywall REST API (`httpx`) | **DOI-based enrichment, not a discovery source.** Given a DOI already collected, locates a legal open-access full-text copy when the pipeline does not already have one |
| Ingestion — Manual Upload | FastAPI file upload → shared PDF store | Ingestion path for papers the owner has legitimate access to but that cannot be fetched programmatically |
| Deep Learning | PyTorch | Model training / fine-tuning |
| Transformer Models | Hugging Face Transformers | Access to BERT, SciBERT, DeBERTa, Longformer |
| Document Parsing | **GROBID** (primary) | Converts PDFs to structured TEI-XML with sections already segmented |
| Parsing Fallback | PyMuPDF + pdfplumber | Fallback for malformed PDFs; figure/table extraction |
| Section Normalization | SciBERT (rhetorical-role classifier) | Maps inconsistent venue headings ("Threats to Validity", "Discussion", etc.) onto a common section schema |
| Document/Paragraph Embeddings | **SPECTER2** | Paper- and paragraph-level embeddings for retrieval & clustering (citation-graph-aware) |
| Token-level Embeddings | SciBERT / DeBERTa | Span-level tasks: IE, NER |
| Information Extraction | spaCy + Transformer pipelines | Extract Contribution / Limitation / Future Work / Dataset / Model / Metric statements |
| Joint NER + Relation Extraction | SciERC-style joint model (DyGIE++ architecture) | Single-pass entity + relation extraction to avoid NER-error cascades |
| Contradiction Detection | DeBERTa-MNLI (NLI model) | Flags conflicting findings within a semantic cluster |
| Knowledge Graph | **Neo4j** | Stores papers, entities (models/datasets/tasks/metrics), and relationships |
| Vector Database | **Qdrant** | Semantic search with metadata filtering (year, venue, section type) |
| Relational Store | **PostgreSQL** (SQLite for local dev) | Paper metadata, structured extraction outputs, gap scores/reports — *added so metadata and structured facts have a queryable home outside the graph and vector stores* |
| Clustering | HDBSCAN / BERTopic | Groups semantically similar limitations without pre-specifying cluster count |
| Reranking | Cross-encoder (e.g. MS MARCO MiniLM cross-encoder) | Reorders retrieved chunks before they're passed to the LLM |
| Trend Analysis | Pandas + NumPy | Aggregate mention frequency over time per cluster |
| Orchestration (Mode B + grounding checks) | **LangGraph** | Stateful, conditional, loopable graph for retrieve → assess → reformulate → generate → verify (see Section 9). **LangChain is intentionally not used anywhere in this project.** |
| LLM | **Groq** (Llama 3.x / GPT-OSS class models, via API) | Final synthesis only — called directly via API with structured (JSON) output, no framework wrapper |
| Visualization | Plotly / D3.js | Trend graphs, cluster views, knowledge graph exploration |
| Backend API | FastAPI | Upload, analysis, querying, reports |
| Frontend | React + TypeScript | Researcher-facing dashboard |
| Testing | pytest | Unit/integration tests per module |
| Containerization | Docker + Docker Compose | Reproducible local & deployment environment |

## 8. End-to-End Pipeline

Each stage below corresponds to one or more implementation phases in [Section 15](#15-implementation-phases).

**Stage 1 — Literature Collection.** Given a topic (e.g. *"LLM Security"*), gather papers from multiple ingestion sources, then funnel all of them into a **single normalized corpus** that every downstream stage reads from. No source gets its own downstream path.

*Discovery sources* (each returns candidate papers for a topic):

| Source | Provides | Role |
|---|---|---|
| **arXiv** | Metadata + full-text PDF | Primary open-access source for CS/AI/ML/security preprints |
| **Semantic Scholar** | Metadata only (+ `openAccessPdf` link when available) | Broad cross-venue discovery and metadata enrichment; does **not** host paywalled full text |
| **ACL Anthology** | Metadata + full-text PDF | Open-access NLP/CL venue coverage (ACL, EMNLP, NAACL, COLING, TACL, CL) that arXiv misses |
| **Manual PDF Upload** | User-supplied PDF | Owner-supplied papers they have legitimate access to but that cannot be fetched programmatically |

*Enrichment step* (not a discovery source):

- **Unpaywall** — takes a **DOI the pipeline has already collected** and looks up whether a legal open-access full-text copy exists anywhere (publisher OA, institutional repository, author preprint). It runs **only for records that have a DOI but no full-text PDF yet** — typically Semantic Scholar metadata hits. It never initiates a search, never replaces arXiv/Semantic Scholar/ACL Anthology, and never touches paywalled content.

*Normalized corpus.* Every record — regardless of which of the four ingestion paths produced it — is written to one canonical `papers` schema in PostgreSQL (title, authors, year, venue, DOI, abstract, `source`, `pdf_path`, `has_full_text`) with its PDF in the shared store. **Stage 2 onward is entirely source-agnostic**: nothing downstream branches on where a paper came from. The `source` field exists for provenance and auditing only.

*Deduplication* runs across all sources in priority order: DOI → arXiv ID → ACL Anthology ID → normalized title+year match (the last is the fallback for manual uploads, which may arrive with no identifiers at all). A paper that appears in several sources is stored once, keeping the record that carries a usable full-text PDF.

*Manual uploads and metadata.* A manually uploaded PDF may arrive with no metadata whatsoever. It is still written to the same corpus immediately with `source='manual_upload'`; its metadata is backfilled after Stage 2, since GROBID extracts header metadata (title, authors, DOI) during parsing. Once a DOI is recovered that way, the record can optionally be enriched through Semantic Scholar and Unpaywall like any other. This is the one place where a source's metadata arrives late — the corpus schema and downstream path are otherwise identical.

**Stage 2 — Parsing & Section Normalization (merged).** GROBID converts each PDF into structured sections/paragraphs/sentences directly (it already performs both parsing and section segmentation, so these are treated as one stage rather than two). A SciBERT rhetorical-role classifier normalizes inconsistent headings across venues onto a common schema: Introduction, Methodology, Results, Discussion, Limitations, Future Work, Conclusion. PyMuPDF/pdfplumber is the fallback parser for documents GROBID can't handle cleanly.

**Stage 3 — Scientific Information Extraction.** Instead of summarizing, extract structured facts per paper, e.g.:
```
Input:  "Our model achieves SOTA accuracy. However, performance decreases
         significantly for multilingual datasets. Future work should
         explore few-shot multilingual learning."

Output: Contribution → SOTA accuracy
        Limitation   → Poor multilingual performance
        Future Work  → Few-shot multilingual learning
```

**Stage 4 — Joint Scientific NER + Relation Extraction (merged).** A single SciERC-style model extracts entities (Model, Dataset, Task, Metric, Domain) *and* the relations between them in one pass (e.g. `BERT —trained_on→ SQuAD`, `BERT —fails_on→ Low-resource Languages`, `GPT-4 —evaluated_using→ GSM8K`), avoiding the error-cascade risk of running NER and relation extraction as separate stages. Results are written into Neo4j.

**Stage 5 — Embeddings & Vector Indexing.** SPECTER2 embeds every paragraph/limitation/future-work statement; vectors go into Qdrant with metadata (paper ID, year, venue, section type) for filtered retrieval, linked back to the corresponding Neo4j nodes.

**Stage 6 — Semantic Clustering + Contradiction Detection.** HDBSCAN/BERTopic clusters semantically similar limitations (e.g. "English bias", "poor multilingual support", "low-resource languages" → one cluster) using SPECTER2 embeddings. Within each cluster, a DeBERTa-MNLI model checks sentence pairs for contradiction (e.g. one paper claims a method works where another says it fails under the same setup) — flagged contradictions become gap evidence.

**Stage 7 — Trend Analysis.** Pandas aggregates how often each cluster's limitation is mentioned per year, producing a trend slope (e.g. "Hallucination": 5 papers in 2021 → 140 in 2025 → strongly increasing).

**Stage 8 — Research Gap Ranking.** A transparent, weighted formula combines multiple signals per cluster:

```
gap_score = w1 · mention_frequency
          + w2 · trend_slope
          − w3 · solution_density        (papers proposing solutions ÷ papers mentioning the problem)
          + w4 · citation_impact
          + w5 · contradiction_flag
```

Weights are configurable; this stays a simple, interpretable formula for the MVP rather than a trained ranker (a learned-ranking upgrade is listed under [Future Extensions](#16-future-extensions-post-mvp)).

**Stage 9 — RAG-Grounded Explanation (Mode A).** For each top-ranked gap cluster, retrieve the actual top-N supporting paragraphs from Qdrant (not just counts/statistics) and pass them as context to the LLM, which produces a structured report:

```
Research Gap:  Existing LLM security methods focus predominantly on
               English prompts; multilingual prompt injection remains
               underexplored.
Evidence:      27 papers mention it · only 3 propose mitigations ·
               increasing trend since 2023 · frequently listed as future work.
Confidence:    High
Suggested Directions: Multilingual benchmark · Cross-lingual defense ·
               Low-resource language evaluation.
```

This is grounded in retrieved evidence, not a free-form LLM opinion — this is the RAG requirement satisfied for Mode A. (No LangGraph loop is used at this stage, by design — see [Section 14](#14-instructions-for-the-implementing-ai-agent).)

**Stage 10 — Interactive Query (Mode B).** Covered in full detail in [Section 9](#9-mode-b-in-detail-interactive-graphrag-with-langgraph).

## 9. Mode B in Detail: Interactive GraphRAG with LangGraph

Mode B answers a user's specific research question on demand. A single-shot `retrieve → generate` pipeline is fragile — the first vector search can miss key papers, especially for narrow or oddly-phrased questions. LangGraph is used here because it models the workflow as a **graph of nodes sharing state**, with conditional edges that can loop back — which a linear chain cannot do.

**State object** (conceptually):
```
{
  question: str,
  retrieved_chunks: list,
  retrieval_rounds: int,     # capped at 3 to prevent infinite loops
  sufficient_evidence: bool,
  draft_answer: str,
  grounded: bool
}
```

**Graph:**
```
[question]
    │
    ▼
[retrieve]  ── vector search (Qdrant) + graph traversal (Neo4j neighbors)
    │
    ▼
[assess sufficiency]  ── enough distinct papers / relevant chunks?
    │no                                   │yes
    ▼                                     ▼
[reformulate query]              [rerank with cross-encoder]
(expand terms / pull graph            │
 neighbors one hop out)               ▼
    │                            [generate answer]  ── LLM, structured output
    └───────────► back to [retrieve]  (rounds < 3)   │
                                                       ▼
                                          [grounding check]  ── does every
                                                                claim trace
                                                                back to a
                                                                retrieved
                                                                source?
                                              │fails            │passes
                                              ▼                 ▼
                                    [regenerate with      [return grounded
                                     stricter prompt]      answer + citations]
```

Key points:
- **Retrieval** combines Qdrant vector search with a one-hop Neo4j graph expansion (pull connected entities/papers — this is the "Graph" in GraphRAG), then a cross-encoder reranks the combined candidate set before it reaches the LLM.
- **Reformulation** only triggers when evidence is thin, and is capped to avoid runaway loops.
- **The grounding-check node** is the second required use of LangGraph in this project: after generation, verify the answer's claims are actually supported by retrieved text (via an NLI check or a structured LLM self-check) before returning it to the user. If it fails, regenerate with a stricter, more constrained prompt rather than silently returning an ungrounded answer.
- This node graph is implemented directly with LangGraph — **no LangChain** is used anywhere in this project, by explicit decision.

## 10. Evaluation Strategy

Since the entire premise of the project is "evidence-backed gaps, not LLM guesses," the system needs a way to check whether identified gaps are actually valid:

1. **Time-shifted validation.** Run the pipeline on a corpus snapshot up to date X; check whether flagged gaps were subsequently addressed by papers published after X.
2. **Survey cross-check.** Compare flagged gaps against the "open problems" / "future work" sections of recent survey papers in the same field, used as pseudo-ground-truth.
3. **Expert scoring.** A small panel rates a sample of generated gap reports on novelty and validity (1–5 scale).

## 11. Repository Structure

```
research-gap-ai/
├── backend/
│   ├── app/
│   │   ├── api/                # FastAPI routers (Phase 11)
│   │   ├── core/                # config, DB sessions, shared utils
│   │   ├── ingestion/            # Phase 1 — literature collection
│   │   │   ├── sources/           #   arxiv.py · semantic_scholar.py · acl_anthology.py
│   │   │   ├── unpaywall.py       #   DOI → legal OA full-text enrichment
│   │   │   ├── manual_upload.py   #   owner-supplied PDF ingestion
│   │   │   ├── dedup.py           #   DOI → arXiv ID → ACL ID → title+year
│   │   │   └── corpus.py          #   normalized-corpus writer (single entry point)
│   │   ├── parsing/              # Phase 2 — GROBID + fallback + section norm.
│   │   ├── extraction/           # Phase 3 — scientific IE
│   │   ├── ner_re/                # Phase 4 — joint NER + relation extraction
│   │   ├── embeddings/           # Phase 5 — SPECTER2 + Qdrant + Neo4j writers
│   │   ├── clustering/           # Phase 6 — HDBSCAN/BERTopic + NLI contradiction
│   │   ├── trends/                # Phase 7 — trend analysis
│   │   ├── ranking/               # Phase 8 — gap scoring
│   │   ├── explanation/          # Phase 9 — Mode A RAG explanation generation
│   │   ├── interactive/          # Phase 10 — Mode B LangGraph pipeline
│   │   ├── models/                # shared Pydantic + ORM models
│   │   └── main.py
│   ├── tests/
│   ├── pyproject.toml
│   └── Dockerfile
├── frontend/                      # Phase 12
│   ├── src/
│   └── package.json
├── evaluation/                    # Phase 13
├── docker-compose.yml             # Phase 0 + Phase 14
├── .env.example
└── README.md
```

## 12. Environment & Configuration

- All secrets/API keys (LLM provider, Semantic Scholar API, etc.) live in `.env`, never hardcoded. `.env.example` documents required variables.
- Ingestion-specific config: `UNPAYWALL_EMAIL` (Unpaywall requires a contact email as a query parameter instead of an API key — no account or key needed), `SEMANTIC_SCHOLAR_API_KEY` (**optional** — the API works unauthenticated at a lower rate limit), `PDF_STORAGE_PATH`, and `MAX_UPLOAD_SIZE_MB` for the manual-upload endpoint. arXiv and ACL Anthology require no credentials; set a descriptive User-Agent/contact string for arXiv as a courtesy.
- Services orchestrated via Docker Compose: `backend`, `frontend`, `neo4j`, `qdrant`, `postgres`, `grobid`.
- Config values (model names, cluster weights `w1..w5`, retrieval-round cap, top-N retrieval size) live in a central settings module, not scattered across files.

### Local development (Phases 0–13)

Copy `.env.example` to `.env`, replace the placeholder credentials, and start the backend services:

```bash
docker compose up -d --build postgres neo4j qdrant grobid backend
```

Generation runs on **Groq**, the project's only LLM provider. Create a key at
<https://console.groq.com/keys>, set `GROQ_API_KEY` in `.env`, and recreate the
backend container. Nothing needs to run locally for it. Until the key is set,
ingestion, parsing, extraction, clustering, trends and ranking all still work;
only report generation and **Ask ScholarLens** return an explicit
"Groq is unavailable" error.

Then start the dashboard in a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Open `http://127.0.0.1:5173`, choose **Connect API**, and enter the same `API_KEY` configured in `.env`. The Vite server proxies dashboard requests to the backend on port 8000. Backend OpenAPI documentation remains available at `http://127.0.0.1:8000/docs`.

The dashboard's **Add papers** workspace exposes the complete corpus setup
flow: search scholarly sources by topic, upload one or more local PDFs, inspect
recently added papers, and run the evidence-building pipeline. The API examples
below provide the equivalent command-line workflow.

Downloaded and uploaded PDFs are stored in the Docker volume `pdf_data`, so
recreating or rebuilding the backend container does not remove them. Avoid
`docker compose down -v` unless you intentionally want to delete all local
database, index, graph, and PDF data. If an older database record points to a
PDF that is no longer present, the pipeline skips that paper, marks it as
metadata-only, and reports its ID in `missing_pdfs`; fetch or upload that paper
again to restore its full text.

After changing backend Python code or dependencies, apply the change with
`docker compose up -d --build backend`. A plain restart uses the existing image
and does not include source changes.

Before using **Ask ScholarLens**, populate the local evidence base. You can use
the interactive OpenAPI page, or run the equivalent requests below:

```bash
curl -X POST http://127.0.0.1:8000/collect \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"topic":"multilingual prompt injection"}'

curl -X POST http://127.0.0.1:8000/pipeline/run \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"generate_reports":true}'
```

Collection and the first pipeline run can take time because papers and ML
models are downloaded. If the dashboard says **No indexed evidence**, the
PostgreSQL corpus or Qdrant evidence index is still empty; this is a data-state
message, not an LLM grounding failure. An empty index returns immediately and
does not invoke the configured LLM.

Run the complete local verification suite with:

```bash
cd backend && .venv/bin/pytest -q && .venv/bin/ruff check app tests && cd ..
PYTHONPATH=. backend/.venv/bin/pytest -q evaluation/tests
cd frontend && npm test && npm run build && npm run lint
```

See `evaluation/README.md` for the reproducible Phase 13 sample evaluation and input formats. Phase 14 will add the frontend container and one-command full-stack deployment; it is intentionally deferred.

## 13. Development Workflow & Quality Gates

This project is built with a disciplined, phase-based development workflow — plan before implementing, tests before code, and a code review gate at the end of every phase. **The implementing agent must follow it as specified below — it is not optional, it is part of the defined build process.** This workflow is tool-agnostic: it describes required practices (planning, TDD, review, context hygiene), not any specific IDE, plugin, or command set, so it applies equally whether the work is done in Claude Code, Cursor, Antigravity, Codex, or any other coding agent/IDE. Each phase in [Section 15](#15-implementation-phases) names the workflow required for that phase.

### 13.1 Core development discipline (every phase, Phase 0 → Phase 14)

- **Test-driven development.** Write the failing test first, then implement, then refactor. Every phase includes pytest coverage (or equivalent) for the code it adds.
- **Plan before executing.** Write a short implementation plan for the phase before writing any code — what will be built, in what order, and how it will be tested.
- **Code review at the end of every phase**, before it is considered done. Use whatever review capability is available in your environment — a self-review checklist, static analysis/linting, or an AI code-review feature. This step is required on every phase, with no exceptions.
- **Systematic debugging.** When something breaks, find the root cause before proposing a fix.
- **Context hygiene.** Several phases produce bulky raw output (arXiv / Semantic Scholar / ACL Anthology / Unpaywall API responses, GROBID TEI-XML, embedding batches, Cypher/Qdrant query results, LLM traces). Parse or summarize this kind of output programmatically rather than pasting it wholesale into the working context — see 13.7.
- **Persist decisions across sessions.** Because the project owner controls pacing, phases may span separate sessions and days. Keep a running decisions/progress log in the repo (e.g. `PROGRESS.md`, or the phase summaries produced at the end of each session) so architectural decisions (GROBID over PyMuPDF, the SPECTER2/SciBERT split, LangGraph only in Phase 10, LangChain excluded, gap-score weights, etc.) don't need to be re-derived or re-explained in a later session or by a different agent/IDE picking up the work.

### 13.2 Standard workflow the agent runs for every phase

When the project owner authorizes a phase (*"start Phase N"*, *"proceed"*), the agent runs this loop **autonomously, end to end, without pausing for approval at each step**:

1. **Recall.** Check the repo's progress log and prior-phase artifacts for anything already decided rather than guessing or re-deriving it.
2. **Confirm the phase spec.** Re-read the Phase N entry in Section 15 — its tech stack, deliverable, "Done when" criterion, and its **Workflow** line.
3. **Plan the phase's tasks before implementing them**, per its Workflow line.
4. **Execute the whole phase.** Build under TDD — red, then green, then refactor, for every task. Move through every task in the phase without stopping for interim approval; make routine engineering decisions independently from the spec. The only interruptions are the owner-only-action pauses in [Section 13.5](#135-when-to-pause-mid-phase-owner-only-actions).
5. **Review.** Run a code review at the end of the phase — it requires no owner prompting — and fix everything it raises before proceeding.
6. **Report and stop.** Summarize what was built, files created/modified, deviations/assumptions, and the review outcome. **Then stop and wait** for the owner to authorize the next phase. If the owner says *"continue"* while this phase is still in progress, resume it directly — don't ask which phase.

### 13.3 Phase → workflow mapping (authoritative)

| Phase | Testing | Review | Notes |
|---|---|---|---|
| 0 — Scaffolding | TDD | code review | — |
| 1 — Literature Collection | TDD | code review | Bulk API responses — heavy context hygiene |
| 2 — Parsing & Section Norm. | TDD | code review | GROBID TEI-XML output is large — heavy context hygiene |
| 3 — Scientific IE | TDD | code review | — |
| 4 — Joint NER + RE | TDD | code review | Errors cascade downstream; heavy context hygiene |
| 5 — Embeddings & Vector Index | TDD | code review | Batch embedding/Qdrant output is large — heavy context hygiene |
| 6 — Clustering + Contradiction | TDD | code review | — |
| 7 — Trend Analysis | TDD | code review | — |
| 8 — Gap Ranking | TDD | code review | Formula determines every gap's validity — thorough per-signal TDD |
| 9 — Mode A RAG Explanation | TDD | code review | Retrieved-paragraph batches + LLM traces are large — heavy context hygiene; verify no unsupported claims |
| 10 — Mode B LangGraph | TDD | code review | Highest-complexity phase; live user input — review logic and security; heavy context hygiene |
| 11 — Backend API | TDD | code review + security review | Exposed surface — review security carefully |
| 12 — Frontend Dashboard | — | code review | — |
| 13 — Evaluation Harness | TDD | code review | — |
| 14 — Deployment | — | code review + security review | — |

### 13.4 Tooling note

This project does not prescribe a specific IDE, plugin, or command set. Use whichever planning, testing, and code-review tooling your environment provides — the requirement is the underlying discipline (plan first, test first, review before done), not any particular tool's syntax.

### 13.5 When to pause mid-phase (owner-only actions)

Once a phase is authorized, the agent does not stop for routine task/plan/fix approval. It pauses mid-phase **only** when one of these applies:

1. The spec has a genuine contradiction or ambiguity that re-reading the README doesn't resolve.
2. An irreversible or materially consequential architectural/product decision can't be safely inferred from the spec.
3. Something requires credentials, external accounts, permissions, or another action only the project owner can perform.
4. A manual/system action is needed that the agent cannot safely or legitimately perform itself.

**Pause protocol:** state exactly what the owner needs to do, what the agent was attempting, and what remains in the phase — then resume the same phase once the owner responds. One clear instruction from the owner is sufficient; the agent does not re-confirm the same thing afterward.

**Standing pre-authorization — package legitimacy checks.** Before installing a new dependency, do a quick sanity check on it. For a package that is an unambiguous match to a well-known, real PyPI/npm project (name, maintainer, and repository all check out), the agent resolves this automatically and does not pause. A pause is warranted only when a package looks unfamiliar, misspelled, or otherwise suspicious (typosquat risk, unknown maintainer, no real history).

### 13.6 Resource and system safety

Phase completion is never worth machine instability:

- Never make system-wide or machine-level configuration changes without the owner's explicit permission.
- Before a resource-intensive operation (large model downloads/loads, big datasets, long-running batch jobs), weigh RAM/CPU/disk and expected runtime, and prefer a small representative run before scaling up. Batch or process incrementally where that reduces risk.
- If an operation causes repeated crashes, OOMs, timeouts, or other instability, **stop repeating it and diagnose the root cause** rather than retrying — even where a phase's spec implies the operation "must eventually succeed." Choose a safer implementation or testing strategy instead of brute-forcing a failing expensive operation.

### 13.7 Context and session efficiency

Context and session budget are finite resources the agent optimizes, not just spends:

- One coherent plan per phase, sized to that phase's actual complexity — never run competing planning workflows over the same work.
- Avoid unnecessary repeated file reads, redundant re-derivation of information already in the repo, and excessive commentary. Parse or summarize bulky raw output (see 13.1) instead of pasting it wholesale into context.
- Perform expensive or large-scale operations incrementally, validating early on a small slice before scaling up.
- If context/session usage becomes constrained mid-phase, checkpoint and commit whatever is safely done, write a precise summary of the exact remaining work, and stop cleanly — don't let a phase fail partway with no record of what's left.

## 14. Instructions for the Implementing AI Agent

If you are an AI coding agent or IDE assistant (e.g. Claude Code, Cursor, Antigravity, Codex, or similar) implementing this project:

1. **Read this entire README before writing any code.**
2. **Implement strictly phase by phase, in the order given in Section 15.** Do not start a phase until the project owner explicitly instructs you to (e.g. *"proceed with Phase 3"*). Do not skip ahead, even if it looks more efficient to build multiple phases together. **Once a phase is authorized, execute it autonomously start to finish** — do not pause for approval of individual tasks, plans, tests, fixes, or routine engineering decisions inside that phase; pause only for the owner-only-action cases in [Section 13.5](#135-when-to-pause-mid-phase-owner-only-actions). If the owner says *"continue"* while a phase is already in progress, resume that phase directly rather than asking which one.
3. **Use the tech stack specified for each phase exactly.** If something in the spec seems wrong, ambiguous, or you'd genuinely recommend a different choice, say so and ask — don't silently substitute a different library or approach.
4. **LangGraph is used only in two places**: the Mode B retrieve/assess/reformulate/grounding loop (Phase 10), and nowhere else. **Do not introduce LangChain anywhere in this project** — it has been explicitly excluded.
5. **Do not add a LangGraph (or any) loop around Stage 8→9 (gap ranking → Mode A explanation).** This was considered and explicitly declined — Phase 9 is a single-pass retrieval + generation step, not a loop.
6. **Ingestion sources are fixed.** The only permitted sources are arXiv, Semantic Scholar, ACL Anthology, Unpaywall (DOI enrichment only), and manual PDF upload. **Do not add IEEE Xplore, Springer, or ACM Digital Library APIs anywhere in this project** — they are explicitly excluded, as is any scripted access to those platforms. Do not add other sources without the owner's approval.
7. **After completing a phase**, run its required tests and code review, summarize what was built, list files created/modified, note any deviations or assumptions made, and stop. Wait for the next go-ahead.
8. **Write modular, tested code.** Each phase should include reasonable unit/integration tests (pytest) before being considered done. Tests are written **before** implementation, per the TDD discipline in [Section 13](#13-development-workflow--quality-gates).
9. **Keep the MVP scope disciplined.** Don't add extra frameworks, services, or abstraction layers "for completeness" — this project intentionally avoids overkill (e.g., no LangChain, no learned ranker in the MVP, no Celery/task-queue unless a phase explicitly calls for it).
10. **Protect machine stability over phase completion.** Never make system-wide/machine-level config changes without explicit permission; weigh resource cost (RAM/CPU/disk/runtime) before large operations and prefer a small representative run first; if something repeatedly crashes, OOMs, times out, or otherwise destabilizes, stop retrying and diagnose the cause instead of forcing it through (see [Section 13.6](#136-resource-and-system-safety)).
11. **Spend context and session budget deliberately.** One coherent plan per phase, no competing planning workflows, minimal redundant re-reading, bulky output parsed/summarized rather than pasted raw; if usage gets tight mid-phase, checkpoint, commit, summarize what's left, and stop cleanly (see [Section 13.7](#137-context-and-session-efficiency)).
12. **Apply the workflow defined in [Section 13](#13-development-workflow--quality-gates) automatically.** At the start of every phase, read that phase's **Workflow** line and run the loop in Section 13.2 without being prompted. Do not wait for the owner to ask you to plan, to run TDD, or to review — those steps are part of the phase.
13. **Never declare a phase complete before a code review has been run and its findings addressed.** This applies to every phase without exception.
14. **At the very first session**, confirm your environment can actually run the stack this project needs (Python 3.11+, Docker & Docker Compose, and whatever testing/review tooling your IDE or agent provides) and report anything missing to the project owner *before* starting Phase 0.

## 15. Implementation Phases

> The project owner controls when each phase starts. Do not proceed automatically.

### Phase 0 — Project Scaffolding & Environment
**Workflow:** Confirm your environment/tooling is set up first (Docker, Python, etc.) — everything after this depends on it. Plan and execute under TDD; close with a code review.
**Tech:** Python 3.11+, pyproject.toml, Docker & Docker Compose, pre-commit, .env handling, FastAPI skeleton, React+TS skeleton (empty shell), pytest setup.
**Build:** Repository structure as in Section 11. `docker-compose.yml` bringing up empty Neo4j, Qdrant, and Postgres containers plus a `/health` endpoint on the FastAPI backend. No ML/NLP logic yet.
**Done when:** `docker-compose up` runs cleanly and `/health` returns 200 with all service connections verified.

### Phase 1 — Literature Collection (Stage 1)
**Workflow:** Heavy context hygiene — bulk arXiv / Semantic Scholar / ACL Anthology / Unpaywall API responses are exactly the raw-output case to parse/summarize rather than paste wholesale. Build and test each ingestion source in isolation against the shared corpus contract. TDD; close with a code review.
**Tech:** `arxiv` Python package; Semantic Scholar API via `httpx`; ACL Anthology bibliographic export + PDF URLs; Unpaywall REST API via `httpx`; FastAPI file upload for manual ingestion; PostgreSQL for the normalized corpus.
**Build:** Build the **normalized corpus writer first** (`corpus.py` + the canonical `papers` schema + `dedup.py`), then each ingestion source against it:

1. **arXiv source** — topic query → metadata + PDF download.
2. **Semantic Scholar source** — topic query → metadata (+ `openAccessPdf` link where present). Metadata-only by design.
3. **ACL Anthology source** — topic/venue query → metadata + open-access PDF. Feeds the same corpus writer as every other source; **no parallel processing path**.
4. **Unpaywall enrichment** — runs *after* discovery, over corpus records that have a DOI but no full-text PDF; resolves a legal OA copy where one exists and attaches it to the existing record. It creates no new records of its own.
5. **Manual upload** — `POST /upload` accepts one or more PDFs, writes them to the shared PDF store, and creates corpus records with `source='manual_upload'`. Metadata may be empty at this point and is backfilled from GROBID header extraction in Phase 2.

Expose `/collect` (topic → run discovery + enrichment) and `/upload` (manual ingestion). Deduplicate across all sources in the order DOI → arXiv ID → ACL Anthology ID → normalized title+year.

**Constraint:** IEEE Xplore, Springer, and ACM Digital Library APIs are **prohibited** in this project — do not add them here or anywhere else (see [Section 14](#14-instructions-for-the-implementing-ai-agent), rule 6). Papers from those venues enter only via Unpaywall (if a legal OA copy exists) or manual upload.
**Done when:** A topic string yields N deduplicated papers in the normalized corpus from all discovery sources with Unpaywall enrichment applied; a manually uploaded PDF lands in that same corpus and is indistinguishable to Phase 2 apart from its `source` field.

### Phase 2 — Parsing & Section Normalization (Stage 2)
**Workflow:** GROBID TEI-XML output is large and repeats across hundreds of papers — heavy context hygiene. Plan, then execute under TDD; close with a code review.
**Tech:** GROBID (Dockerized service, primary parser), PyMuPDF + pdfplumber (fallback), SciBERT-based section-role classifier.
**Build:** A parsing module that sends each PDF to GROBID, extracts structured sections/paragraphs/sentences, falls back to PyMuPDF/pdfplumber on failure, and normalizes headings onto the common schema (Introduction, Methodology, Results, Discussion, Limitations, Future Work, Conclusion). It reads from the normalized corpus only and **must not branch on a paper's `source` field** — every paper is parsed identically regardless of ingestion path.

Additionally, **backfill metadata for manually uploaded papers**: GROBID also extracts header metadata (title, authors, DOI), so any corpus record with `source='manual_upload'` and missing metadata is completed here. If a DOI is recovered this way, the record becomes eligible for the same Semantic Scholar / Unpaywall enrichment as any other paper.
**Done when:** Every stored paper has a structured JSON representation with normalized sections, paragraphs, and sentences, and no manually uploaded paper remains without metadata that GROBID could recover.

### Phase 3 — Scientific Information Extraction (Stage 3)
**Workflow:** Brainstorm → plan → execute, with strict TDD — extraction rules are unit-testable sentence by sentence. Close with a code review.
**Tech:** spaCy + Transformer pipeline (SciBERT/DeBERTa), structured extraction schema.
**Build:** Extracts Contribution / Limitation / Future Work / Dataset / Model / Metric statements per paper from the normalized sections, stores them in Postgres linked to `paper_id`.
**Done when:** Each parsed paper has associated structured fact rows queryable by type.

### Phase 4 — Joint NER + Relation Extraction (Stage 4)
**Workflow:** Errors here cascade into the graph, clusters, and every downstream stage, so lean on thorough TDD coverage. Plan, then execute under TDD; heavy context hygiene; close with a code review.
**Tech:** SciERC-style joint entity+relation model (DyGIE++ architecture or an equivalent pretrained scientific IE model).
**Build:** Extracts entities (Model, Dataset, Task, Metric, Domain) and relations between them (`trained_on`, `evaluated_using`, `fails_on`, `achieves`, etc.) in a single pass; writes entities and relations into Neo4j as nodes/edges linked to the source paper.
**Done when:** The Neo4j graph contains papers connected to extracted entities via typed relationships, queryable via Cypher.

### Phase 5 — Embeddings & Vector Indexing (Stage 5)
**Workflow:** Heavy context hygiene (batch embedding jobs, Qdrant upserts, Neo4j link-back queries all produce bulky output). Plan; TDD; close with a code review.
**Tech:** SPECTER2, Qdrant.
**Build:** Batch-embeds every paragraph/limitation/future-work statement using SPECTER2; stores vectors in Qdrant with metadata (paper ID, year, venue, section type) enabling filtered search; links each vector back to its Neo4j node ID.
**Done when:** A semantic query against Qdrant returns relevant paragraphs with correct metadata and graph linkage.

### Phase 6 — Semantic Clustering + Contradiction Detection (Stage 6)
**Workflow:** Under TDD, establish a concrete expected outcome first — a small hand-labelled set of limitation sentences that *should* cluster together — before writing clustering code. Plan, then execute; close with a code review.
**Tech:** HDBSCAN or BERTopic, DeBERTa-MNLI.
**Build:** Clusters limitation/future-work statements by SPECTER2 embedding similarity (no fixed cluster count). Within each cluster, runs pairwise NLI checks to flag contradicting statements between papers.
**Done when:** Clusters are stored with member statements, and contradiction flags are attached where applicable.

### Phase 7 — Trend Analysis (Stage 7)
**Workflow:** Brainstorm → plan → execute with TDD — deterministic aggregation logic with clear expected outputs. Close with a code review.
**Tech:** Pandas, NumPy.
**Build:** Aggregates per-cluster mention counts by publication year and computes a trend slope/score per cluster.
**Done when:** Each cluster has a time series and a trend score, exposed via the API.

### Phase 8 — Gap Ranking (Stage 8)
**Workflow:** Plan → execute with TDD (test each signal term independently, then the combined score). Close with a code review. This formula determines the validity of every gap the system reports, so lean on thorough TDD coverage here.
**Tech:** Plain Python (no ML model — a transparent weighted formula).
**Build:** Implements `gap_score = w1·mention_frequency + w2·trend_slope − w3·solution_density + w4·citation_impact + w5·contradiction_flag` with configurable weights; ranks clusters and stores results in Postgres.
**Done when:** A ranked, queryable list of gap clusters with individual signal breakdowns is available via the API.

### Phase 9 — Mode A: RAG-Grounded Explanation Generation (Stage 9)
**Workflow:** Plan; TDD; heavy context hygiene (retrieved paragraph batches + LLM traces). Close with a code review, explicitly checking that no generated claim is unsupported by retrieved evidence — this is the project's core credibility claim.
**Tech:** Qdrant retrieval, direct Groq API call, structured JSON output/schema validation. **No LangGraph loop here.**
**Build:** For each top-ranked gap cluster, retrieves the top-N actual supporting paragraphs from Qdrant and passes them as context to the LLM in a single pass, producing a structured report (Gap / Evidence / Confidence / Suggested Directions). Stores and exposes reports via `/reports`.
**Done when:** Top-K ranked gaps each have a complete, evidence-grounded report referencing real retrieved paragraphs/papers.

### Phase 10 — Mode B: Interactive GraphRAG with LangGraph (Stage 10)
**Workflow:** The most complex phase — plan and implement it node by node (retrieve → assess → reformulate → rerank → generate → grounding check). TDD per node; heavy context hygiene; close with a code review (takes live user input, so logic *and* security angles both matter).
**Tech:** **LangGraph**, Qdrant, Neo4j, cross-encoder reranker, Groq API.
**Build:** Implements the stateful graph described in [Section 9](#9-mode-b-in-detail-interactive-graphrag-with-langgraph): retrieve (vector + graph) → assess sufficiency → reformulate/retry (capped rounds) → rerank → generate → grounding check → return or regenerate. Exposes a `/query` endpoint accepting a natural-language research question and returning a grounded answer with source citations.
**Done when:** A test question reliably returns an answer whose claims trace back to specific retrieved papers, with visible retry behavior when initial retrieval is weak.

### Phase 11 — Backend API Integration
**Workflow:** Plan, then implement under TDD. Close with a code review followed by a security-focused review — these endpoints are the system's exposed surface, so review security thoroughly here.
**Tech:** FastAPI.
**Build:** Wires Phases 1–10 into a complete REST surface: `/collect`, `/upload`, `/papers`, `/graph`, `/clusters`, `/trends`, `/gaps`, `/reports` (Mode A), `/query` (Mode B). Adds OpenAPI docs and basic API-key auth.
**Done when:** The full backend is usable end-to-end via documented REST endpoints without needing to run internal scripts manually.

### Phase 12 — Frontend Dashboard
**Workflow:** Brainstorm (worthwhile here for layout/UX decisions) → plan → execute. Close with a code review.
**Tech:** React + TypeScript, Plotly/D3.js, a graph-visualization component (e.g. force-directed graph view).
**Build:** Dashboard views for: ranked gap reports (Mode A), trend charts, cluster/knowledge-graph exploration, and a chat-style interface for Mode B queries with citations displayed.
**Done when:** A researcher can browse gap reports and ask interactive questions entirely through the UI.

### Phase 13 — Evaluation Harness
**Workflow:** Plan → execute with TDD — scripts with well-defined expected outputs. Close with a code review.
**Tech:** Python scripts.
**Build:** Implements the three evaluation methods from [Section 10](#10-evaluation-strategy) — time-shifted validation, survey cross-check, and an expert-scoring rubric/collection script — and produces a summary report.
**Done when:** Running the harness against a test corpus produces a concrete evaluation report with precision/validity indicators.

### Phase 14 — Full Deployment
**Workflow:** Plan the config carefully (secrets handling, service wiring, volumes), then execute. Close with a code review + a security-focused review.
**Tech:** Docker Compose (full stack).
**Build:** Single `docker-compose up` brings up backend, frontend, Neo4j, Qdrant, Postgres, and GROBID together, wired via environment configuration, with a documented local run procedure.
**Done when:** A fresh clone + `docker-compose up` + `.env` configured results in a fully working system end-to-end.

## 16. Future Extensions (Post-MVP)

Not part of the phased build above — worth considering only after the MVP is complete and evaluated:

- **Cross-domain gap discovery**: linking concepts across research fields (e.g., applying cybersecurity techniques to healthcare NLP) to surface opportunities the current single-domain pipeline can't see.
- **Learned research-opportunity ranking**: replacing the Phase 8 weighted formula with a trained learning-to-rank model once enough labeled/validated gap data exists from the evaluation harness (Phase 13).

These extensions would move the project from a literature-analysis tool toward a broader AI-powered scientific discovery platform, and are natural directions if the project is extended toward a publication.

# Automatic Research Gap Identification from Scientific Literature

An AI research assistant that reads scientific literature at scale, builds a structured knowledge representation of it, and surfaces **evidence-backed research gaps** — instead of asking an LLM to "read 600 papers and tell me what's missing."

---

## How to Use This Document

This README is written to be followed by **both a human maintainer and an AI coding agent (e.g. Claude Code)**.

- The **human (project owner)** controls pacing. Implementation is broken into numbered **Phases** (see [Section 15](#15-implementation-phases)). No phase should begin until the owner explicitly says so (e.g., *"start Phase 2"*, *"proceed"*).
- The **AI agent** should read this entire document before writing any code, then implement strictly phase by phase, in order, using the tech stack specified for that phase. After finishing a phase, the agent should summarize what was built, flag any deviations or open questions, and **stop** — not continue to the next phase automatically.
- This project is built using five Claude Code plugins/skills — **Superpowers, GSD, Code Review (`/code-review` & `/code-review ultra`), Context Mode, and Claude Mem**. [Section 13](#13-claude-code-skills--plugin-integration) defines which are used at which phase and how the agent should invoke them. The agent is expected to apply them automatically as specified, without being reminded each phase.
- Every phase entry in Section 15 carries a **Skills:** line. That line is part of the phase's requirements, not a suggestion.

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
13. [Claude Code Skills & Plugin Integration](#13-claude-code-skills--plugin-integration)
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
| LLM | Llama 3.x / Qwen / GPT (via API) | Final synthesis only — called directly via API with structured (JSON) output, no framework wrapper |
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

## 13. Claude Code Skills & Plugin Integration

This project is built with five Claude Code plugins/skills active. **The implementing agent must use them as specified below — they are not optional tooling, they are part of the defined build process.** Each phase in [Section 15](#15-implementation-phases) names the skills required for that phase.

### 13.1 The five skills and what each is for

| Skill | Problem it solves | How it activates |
|---|---|---|
| **Superpowers** | Development discipline via its skill library — `test-driven-development` (red-green-refactor), `systematic-debugging` (root-cause before fix), `brainstorming`, `writing-plans`, `subagent-driven-development`, `requesting-code-review` | Slash commands (`/superpowers:brainstorm`, `/superpowers:write-plan`, `/superpowers:execute-plan`) + automatic skill activation for the rest |
| **GSD (Get Sh*t Done)** | Context rot on large multi-step builds: locks decisions in a discuss phase, splits work into small plans each run in a **fresh context window**, atomic git commit per task, validates a test-feedback loop exists *before* code is written | Slash commands (`/gsd:*`) |
| **Code Review** (`/code-review`, `/code-review ultra`) | Catching bugs/edge cases/design issues before a phase is declared done. `/code-review` = fast, **free, unlimited** structured self-review — used at the end of every phase. `/code-review ultra` = multiple cloud reviewer agents attacking from different angles (logic, security, performance) — **Anthropic gives only 3 free runs, then bills usage credits ($5–20/run)**, so this project uses it exactly **once**, as a full-system pass after Phase 14, never per-phase | Slash commands (built into Claude Code) |
| **Context Mode** | In-session context bloat: routes large raw tool outputs through a sandbox so only compact summaries enter the context window | Automatic (PreToolUse hook) once installed |
| **Claude Mem** | Cross-session memory: captures decisions/edits per session and re-injects the relevant parts at the start of the next session | Automatic (session lifecycle hooks) once installed |

### 13.2 Always-on layer (every phase, Phase 0 → Phase 14)

- **Claude Mem** — always on. The project owner controls pacing, so phases will span separate sessions and days. Every architectural decision recorded in this README (GROBID over PyMuPDF, SPECTER2 vs SciBERT split, LangGraph only in Phase 10, LangChain excluded, gap-score weights) must survive between sessions without being re-explained.
- **Context Mode** — always on. Nearly every phase produces bulky raw output (arXiv / Semantic Scholar / ACL Anthology / Unpaywall API responses, GROBID TEI-XML, embedding batches, Cypher/Qdrant query results, LLM traces).
- **Superpowers** — always on for the actual coding inside every phase (TDD + debugging discipline).

**GSD** and the **code-review commands** are used selectively, per the phase table in 13.5.

### 13.3 Division of labour between GSD and Superpowers (important)

Do **not** run GSD's plan/execute cycle *and* Superpowers' plan/execute cycle as two competing top-level workflows in the same phase — that produces duplicate planning passes and wasted context.

- **GSD owns the macro structure** of a phase: discuss → break into small plans → run each plan in a fresh context → atomic commits per task.
- **Superpowers owns the micro discipline** *inside* each plan or task: write the failing test first, then implement, then refactor; use its debugging methodology when something breaks.

On phases where GSD is **not** listed, Superpowers' own `brainstorm → write-plan → execute-plan` flow is the top-level structure instead.

### 13.4 Standard workflow the agent runs for every phase

When the project owner says *"start Phase N"*, the agent runs this loop:

1. **Recall.** Claude Mem should have injected prior-phase context automatically. If anything about earlier phases is unclear, query memory (via the `mem-search` skill) rather than guessing or re-deriving.
2. **Confirm the phase spec.** Re-read the Phase N entry in Section 15 — its tech stack, deliverable, "Done when" criterion, and its **Skills** line.
3. **Plan.**
   - If the phase lists **GSD**: run GSD's discuss step (only where the phase says so), then have GSD break the phase into small plans, each executed in a fresh context with atomic commits.
   - Otherwise: `/superpowers:brainstorm` (only if the phase has real design ambiguity) → `/superpowers:write-plan` → `/superpowers:execute-plan`.
4. **Build under TDD.** Superpowers' red-green-refactor applies to every task in every phase. Tests fail first, then implementation.
5. **Review.** Run `/code-review` at the end of every phase — it's free and unlimited. **Never run `/code-review ultra` as part of a phase.** It is reserved for exactly one full-system pass after Phase 14 is complete (see Section 13.5 and the note at the end of Phase 14), because Anthropic only provides 3 free runs before it bills usage credits. Fix everything `/code-review` raises before proceeding.
6. **Report and stop.** Summarize what was built, files created/modified, deviations/assumptions, and the review outcome. **Then stop and wait** for the owner to authorize the next phase.

Context Mode and Claude Mem require no manual invocation during this loop — they operate via hooks.

### 13.5 Phase → skill mapping (authoritative)

| Phase | GSD | Superpowers | Review command | Context Mode load |
|---|---|---|---|---|
| 0 — Scaffolding | discuss + plans | execute + TDD | `/code-review` | normal |
| 1 — Literature Collection | plans | TDD | `/code-review` | **heavy** |
| 2 — Parsing & Section Norm. | plans (fresh context per batch) | TDD | `/code-review` | **heavy** |
| 3 — Scientific IE | — | brainstorm→plan→execute + TDD | `/code-review` | normal |
| 4 — Joint NER + RE | **discuss** + plans | TDD | `/code-review` | **heavy** |
| 5 — Embeddings & Vector Index | plans | TDD | `/code-review` | **heavy** |
| 6 — Clustering + Contradiction | **validation-before-code** + plans | TDD | `/code-review` | normal |
| 7 — Trend Analysis | — | brainstorm→plan→execute + TDD | `/code-review` | normal |
| 8 — Gap Ranking | — | plan→execute + TDD | `/code-review` | normal |
| 9 — Mode A RAG Explanation | plans | TDD | `/code-review` | **heavy** |
| 10 — Mode B LangGraph | **discuss + plan per graph node** | TDD | `/code-review` | **heavy** |
| 11 — Backend API | plans | TDD | `/code-review` + `/security-review` | normal |
| 12 — Frontend Dashboard | — | brainstorm→plan→execute | `/code-review` | normal |
| 13 — Evaluation Harness | — | plan→execute + TDD | `/code-review` | normal |
| 14 — Deployment | **discuss** + plans | execute | `/code-review` + `/security-review` → **project's one `/code-review ultra` pass after this phase** | normal |

**On `/code-review ultra`:** Anthropic gives 3 free runs before it bills usage credits ($5–20/run). To guarantee zero additional cost, this project does **not** call `/code-review ultra` per phase — every phase closes with the free `/code-review` instead (plus `/security-review`, also free, on Phases 11 and 14). `/code-review ultra` is used exactly **once** in the whole project: a full-system pass after Phase 14 is approved and complete (see the note at the end of Phase 14 in Section 15). Phases 4, 8, 9, 10, 11, and 14 were flagged earlier as the highest-stakes work (extraction errors cascading downstream, the scoring formula's validity, grounding correctness, Mode B's complexity, exposed endpoints, deployment config) — that risk is exactly why the one deep multi-agent pass happens after all of them are built and integrated, catching cross-phase issues a single-phase review can't see, rather than being spent piecemeal across six separate runs.

### 13.6 Command reference

```
# Superpowers
/superpowers:brainstorm      # interactive design refinement (use only where design is genuinely open)
/superpowers:write-plan      # produce an implementation plan
/superpowers:execute-plan    # execute the plan in batches

# GSD  (command namespace is /gsd:* )
/gsd:*                       # discuss → plan → execute → verify workflow commands

# Code review (built into Claude Code)
/code-review                      # fast, free, unlimited review — run at the end of EVERY phase
/code-review ultra                # multi-agent deep cloud review (logic / security / performance).
                              # Only 3 free runs, then billed usage credits ($5-20/run) — this project
                              # runs it ONCE, after Phase 14, never per-phase. /ultrareview (no hyphen)
                              # is a known alias for the same command.
/security-review             # security-focused scan (free, built-in) — used on Phases 11 and 14

# Context Mode (otherwise automatic)
/contextmode:ctx-stats       # check context savings / confirm it is active

# Claude Mem
# fully automatic via session hooks; to explicitly search stored memory, invoke
# the mem-search skill (e.g. ask "search memory for how we handled X")
```

**Agent note on command names:** Superpowers, Context Mode and the review commands are stable as written above. GSD's distribution has changed across releases and its exact `/gsd:*` subcommand names vary by packaging. At the start of the first session, run `/help` once, confirm which `/gsd:*` commands are actually available, and use those names — do not invent command names, and do not silently skip GSD if a command name differs. If any skill listed for a phase is unavailable, tell the project owner before proceeding rather than continuing without it.

## 14. Instructions for the Implementing AI Agent

If you are an AI coding agent (e.g. Claude Code) implementing this project:

1. **Read this entire README before writing any code.**
2. **Implement strictly phase by phase, in the order given in Section 15.** Do not start a phase until the project owner explicitly instructs you to (e.g. *"proceed with Phase 3"*). Do not skip ahead, even if it looks more efficient to build multiple phases together.
3. **Use the tech stack specified for each phase exactly.** If something in the spec seems wrong, ambiguous, or you'd genuinely recommend a different choice, say so and ask — don't silently substitute a different library or approach.
4. **LangGraph is used only in two places**: the Mode B retrieve/assess/reformulate/grounding loop (Phase 10), and nowhere else. **Do not introduce LangChain anywhere in this project** — it has been explicitly excluded.
5. **Do not add a LangGraph (or any) loop around Stage 8→9 (gap ranking → Mode A explanation).** This was considered and explicitly declined — Phase 9 is a single-pass retrieval + generation step, not a loop.
6. **Ingestion sources are fixed.** The only permitted sources are arXiv, Semantic Scholar, ACL Anthology, Unpaywall (DOI enrichment only), and manual PDF upload. **Do not add IEEE Xplore, Springer, or ACM Digital Library APIs anywhere in this project** — they are explicitly excluded, as is any scripted access to those platforms. Do not add other sources without the owner's approval.
7. **After completing a phase**, summarize what was built, list files created/modified, note any deviations or assumptions made, and stop. Wait for the next go-ahead.
8. **Write modular, tested code.** Each phase should include reasonable unit/integration tests (pytest) before being considered done. Tests are written **before** implementation, per Superpowers' TDD discipline.
9. **Keep the MVP scope disciplined.** Don't add extra frameworks, services, or abstraction layers "for completeness" — this project intentionally avoids overkill (e.g., no LangChain, no learned ranker in the MVP, no Celery/task-queue unless a phase explicitly calls for it).
10. **Apply the skills defined in [Section 13](#13-claude-code-skills--plugin-integration) automatically.** At the start of every phase, read that phase's **Skills:** line and run the workflow in Section 13.4 without being prompted. Do not wait for the owner to ask you to plan, to run TDD, or to review — those steps are part of the phase.
11. **Never declare a phase complete before `/code-review` has been run and its findings addressed.** This applies to every phase without exception — `/code-review` is free and unlimited, so there is no reason to skip it.
12. **At the very first session**, confirm the five plugins are active (`/help`, `/contextmode:ctx-stats`) and report any that are missing to the project owner *before* starting Phase 0.
13. **`/code-review ultra` is cost-limited — never call it as part of an individual phase.** Anthropic provides only 3 free runs before it bills usage credits. This project uses it exactly once: a single full-system pass after Phase 14 is complete and approved (see Section 13.5 and the note at the end of Phase 14). Do not call it during Phases 0–14 for any reason, even on a high-stakes phase — use `/code-review` there instead.

## 15. Implementation Phases

> The project owner controls when each phase starts. Do not proceed automatically.

### Phase 0 — Project Scaffolding & Environment
**Skills:** Verify Context Mode + Claude Mem are active first (`/contextmode:ctx-stats`, `/help`) — everything after this depends on them. GSD discuss + plans for repo shape; Superpowers execute + TDD; close with `/code-review`.
**Tech:** Python 3.11+, pyproject.toml, Docker & Docker Compose, pre-commit, .env handling, FastAPI skeleton, React+TS skeleton (empty shell), pytest setup.
**Build:** Repository structure as in Section 11. `docker-compose.yml` bringing up empty Neo4j, Qdrant, and Postgres containers plus a `/health` endpoint on the FastAPI backend. No ML/NLP logic yet.
**Done when:** `docker-compose up` runs cleanly and `/health` returns 200 with all service connections verified.

### Phase 1 — Literature Collection (Stage 1)
**Skills:** Context Mode **heavy** (bulk arXiv / Semantic Scholar / ACL Anthology / Unpaywall API responses are exactly the raw-output case it exists for). GSD plans — **one plan per ingestion source**, so each source is built and tested in isolation against the shared corpus contract. Superpowers TDD; close with `/code-review`.
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
**Skills:** GSD plans with a **fresh context per batch** — GROBID TEI-XML output is large and repeats across hundreds of papers. Context Mode **heavy**; Superpowers TDD; close with `/code-review`.
**Tech:** GROBID (Dockerized service, primary parser), PyMuPDF + pdfplumber (fallback), SciBERT-based section-role classifier.
**Build:** A parsing module that sends each PDF to GROBID, extracts structured sections/paragraphs/sentences, falls back to PyMuPDF/pdfplumber on failure, and normalizes headings onto the common schema (Introduction, Methodology, Results, Discussion, Limitations, Future Work, Conclusion). It reads from the normalized corpus only and **must not branch on a paper's `source` field** — every paper is parsed identically regardless of ingestion path.

Additionally, **backfill metadata for manually uploaded papers**: GROBID also extracts header metadata (title, authors, DOI), so any corpus record with `source='manual_upload'` and missing metadata is completed here. If a DOI is recovered this way, the record becomes eligible for the same Semantic Scholar / Unpaywall enrichment as any other paper.
**Done when:** Every stored paper has a structured JSON representation with normalized sections, paragraphs, and sentences, and no manually uploaded paper remains without metadata that GROBID could recover.

### Phase 3 — Scientific Information Extraction (Stage 3)
**Skills:** Superpowers-led (`/superpowers:brainstorm` → `write-plan` → `execute-plan`) with strict TDD — extraction rules are unit-testable sentence by sentence. Close with `/code-review`.
**Tech:** spaCy + Transformer pipeline (SciBERT/DeBERTa), structured extraction schema.
**Build:** Extracts Contribution / Limitation / Future Work / Dataset / Model / Metric statements per paper from the normalized sections, stores them in Postgres linked to `paper_id`.
**Done when:** Each parsed paper has associated structured fact rows queryable by type.

### Phase 4 — Joint NER + Relation Extraction (Stage 4)
**Skills:** Start with a **GSD discuss phase** to lock the model choice before any code — errors here cascade into the graph, clusters, and every downstream stage. Then GSD plans + Superpowers TDD, Context Mode **heavy**, and close with `/code-review` (free, unlimited). This phase's extra risk is covered later by the project's single `/code-review ultra` pass after Phase 14, not a per-phase ultra run.
**Tech:** SciERC-style joint entity+relation model (DyGIE++ architecture or an equivalent pretrained scientific IE model).
**Build:** Extracts entities (Model, Dataset, Task, Metric, Domain) and relations between them (`trained_on`, `evaluated_using`, `fails_on`, `achieves`, etc.) in a single pass; writes entities and relations into Neo4j as nodes/edges linked to the source paper.
**Done when:** The Neo4j graph contains papers connected to extracted entities via typed relationships, queryable via Cypher.

### Phase 5 — Embeddings & Vector Indexing (Stage 5)
**Skills:** Context Mode **heavy** (batch embedding jobs, Qdrant upserts, Neo4j link-back queries all produce bulky output). GSD plans; Superpowers TDD; close with `/code-review`.
**Tech:** SPECTER2, Qdrant.
**Build:** Batch-embeds every paragraph/limitation/future-work statement using SPECTER2; stores vectors in Qdrant with metadata (paper ID, year, venue, section type) enabling filtered search; links each vector back to its Neo4j node ID.
**Done when:** A semantic query against Qdrant returns relevant paragraphs with correct metadata and graph linkage.

### Phase 6 — Semantic Clustering + Contradiction Detection (Stage 6)
**Skills:** Use **GSD's validate-before-code step** first — establish a concrete test signal (a small hand-labelled set of limitation sentences that *should* cluster together) before writing clustering code. Then GSD plans + Superpowers TDD; close with `/code-review`.
**Tech:** HDBSCAN or BERTopic, DeBERTa-MNLI.
**Build:** Clusters limitation/future-work statements by SPECTER2 embedding similarity (no fixed cluster count). Within each cluster, runs pairwise NLI checks to flag contradicting statements between papers.
**Done when:** Clusters are stored with member statements, and contradiction flags are attached where applicable.

### Phase 7 — Trend Analysis (Stage 7)
**Skills:** Superpowers-led with TDD — deterministic aggregation logic with clear expected outputs. Close with `/code-review`.
**Tech:** Pandas, NumPy.
**Build:** Aggregates per-cluster mention counts by publication year and computes a trend slope/score per cluster.
**Done when:** Each cluster has a time series and a trend score, exposed via the API.

### Phase 8 — Gap Ranking (Stage 8)
**Skills:** Superpowers `write-plan` → `execute-plan` with TDD (test each signal term independently, then the combined score). Close with `/code-review`. This formula determines the validity of every gap the system reports, so lean on thorough TDD coverage here — the deep review pass happens once, at the end (see Phase 14).
**Tech:** Plain Python (no ML model — a transparent weighted formula).
**Build:** Implements `gap_score = w1·mention_frequency + w2·trend_slope − w3·solution_density + w4·citation_impact + w5·contradiction_flag` with configurable weights; ranks clusters and stores results in Postgres.
**Done when:** A ranked, queryable list of gap clusters with individual signal breakdowns is available via the API.

### Phase 9 — Mode A: RAG-Grounded Explanation Generation (Stage 9)
**Skills:** GSD plans; Superpowers TDD; Context Mode **heavy** (retrieved paragraph batches + LLM traces). Close with `/code-review`, explicitly checking that no generated claim is unsupported by retrieved evidence — this is the project's core credibility claim, and it gets a second, deeper look in the single final `/code-review ultra` pass after Phase 14.
**Tech:** Qdrant retrieval, direct LLM API call (Llama 3.x / Qwen / GPT), structured JSON output/schema validation. **No LangGraph loop here.**
**Build:** For each top-ranked gap cluster, retrieves the top-N actual supporting paragraphs from Qdrant and passes them as context to the LLM in a single pass, producing a structured report (Gap / Evidence / Confidence / Suggested Directions). Stores and exposes reports via `/reports`.
**Done when:** Top-K ranked gaps each have a complete, evidence-grounded report referencing real retrieved paragraphs/papers.

### Phase 10 — Mode B: Interactive GraphRAG with LangGraph (Stage 10)
**Skills:** The most complex phase — use **GSD discuss first**, then **one GSD plan per graph node** (retrieve → assess → reformulate → rerank → generate → grounding check), each in a fresh context with atomic commits. Superpowers TDD per node; Context Mode **heavy**; close with `/code-review` (takes live user input, so logic *and* security angles both matter — this phase gets particular attention in the single final `/code-review ultra` pass after Phase 14).
**Tech:** **LangGraph**, Qdrant, Neo4j, cross-encoder reranker, LLM API.
**Build:** Implements the stateful graph described in [Section 9](#9-mode-b-in-detail-interactive-graphrag-with-langgraph): retrieve (vector + graph) → assess sufficiency → reformulate/retry (capped rounds) → rerank → generate → grounding check → return or regenerate. Exposes a `/query` endpoint accepting a natural-language research question and returning a grounded answer with source citations.
**Done when:** A test question reliably returns an answer whose claims trace back to specific retrieved papers, with visible retry behavior when initial retrieval is weak.

### Phase 11 — Backend API Integration
**Skills:** GSD plans (one per endpoint group); Superpowers TDD. Close with `/code-review` followed by `/security-review` (both free, unlimited) — these endpoints are the system's exposed surface, so run `/security-review` thoroughly here.
**Tech:** FastAPI.
**Build:** Wires Phases 1–10 into a complete REST surface: `/collect`, `/upload`, `/papers`, `/graph`, `/clusters`, `/trends`, `/gaps`, `/reports` (Mode A), `/query` (Mode B). Adds OpenAPI docs and basic API-key auth.
**Done when:** The full backend is usable end-to-end via documented REST endpoints without needing to run internal scripts manually.

### Phase 12 — Frontend Dashboard
**Skills:** Superpowers-led (`brainstorm` is worthwhile here for layout/UX decisions) → `write-plan` → `execute-plan`. Close with `/code-review`.
**Tech:** React + TypeScript, Plotly/D3.js, a graph-visualization component (e.g. force-directed graph view).
**Build:** Dashboard views for: ranked gap reports (Mode A), trend charts, cluster/knowledge-graph exploration, and a chat-style interface for Mode B queries with citations displayed.
**Done when:** A researcher can browse gap reports and ask interactive questions entirely through the UI.

### Phase 13 — Evaluation Harness
**Skills:** Superpowers `write-plan` → `execute-plan` with TDD — scripts with well-defined expected outputs. Close with `/code-review`.
**Tech:** Python scripts.
**Build:** Implements the three evaluation methods from [Section 10](#10-evaluation-strategy) — time-shifted validation, survey cross-check, and an expert-scoring rubric/collection script — and produces a summary report.
**Done when:** Running the harness against a test corpus produces a concrete evaluation report with precision/validity indicators.

### Phase 14 — Full Deployment
**Skills:** **GSD discuss phase** before touching config (secrets handling, service wiring, volumes). Superpowers execute. Close with `/code-review` + `/security-review` (both free, unlimited). **Once this phase is approved, run the project's single, one-time `/code-review ultra` pass** — see the note at the end of this phase's entry below.
**Tech:** Docker Compose (full stack).
**Build:** Single `docker-compose up` brings up backend, frontend, Neo4j, Qdrant, Postgres, and GROBID together, wired via environment configuration, with a documented local run procedure.
**Done when:** A fresh clone + `docker-compose up` + `.env` configured results in a fully working system end-to-end.

**Final review (after Phase 14, once only, not a phase of its own).** Once the project owner approves Phase 14, run exactly one `/code-review ultra` pass over the complete, integrated codebase. This is the **only** time `/code-review ultra` is used anywhere in this project. Anthropic provides just 3 free runs before it starts billing usage credits, and this project is designed to spend only the first of them — every other review gate throughout Phases 0–14 uses the free `/code-review` (and `/security-review` on Phases 11/14) instead. Address anything the ultra pass raises; once resolved, the build is complete.

## 16. Future Extensions (Post-MVP)

Not part of the phased build above — worth considering only after the MVP is complete and evaluated:

- **Cross-domain gap discovery**: linking concepts across research fields (e.g., applying cybersecurity techniques to healthcare NLP) to surface opportunities the current single-domain pipeline can't see.
- **Learned research-opportunity ranking**: replacing the Phase 8 weighted formula with a trained learning-to-rank model once enough labeled/validated gap data exists from the evaluation harness (Phase 13).

These extensions would move the project from a literature-analysis tool toward a broader AI-powered scientific discovery platform, and are natural directions if the project is extended toward a publication.

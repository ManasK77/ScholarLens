# ScholarLens

## What This Is

ScholarLens is an AI research assistant that ingests scientific literature at scale (arXiv, Semantic Scholar, ACL Anthology, Unpaywall-enriched, and manually uploaded PDFs), builds a structured knowledge representation of it (Postgres corpus + Neo4j graph + Qdrant vector index), and surfaces **evidence-backed research gaps** — ranked, grounded, and explained — instead of asking an LLM to skim hundreds of papers and guess. It serves a Master's/PhD-level researcher who needs to find an unsolved problem in a field (core domains: CS/AI/ML/NLP/Cybersecurity) without spending weeks manually cross-referencing "future work" sections.

## Core Value

Every research gap the system reports is grounded in structured, retrieved evidence extracted before any LLM is involved — the LLM explains gaps that have already been discovered by extraction, clustering, trend analysis, and ranking; it never discovers gaps by itself. If this ordering breaks, the product's core credibility claim breaks with it.

## Requirements

### Validated

<!-- Shipped and confirmed valuable. -->

(None yet — ship to validate)

### Active

See `.planning/REQUIREMENTS.md` for the full v1 requirement list (56 requirements across 15 categories, one per phase).

### Out of Scope

- **LangChain (anywhere in the project)** — deliberately excluded; LangGraph (Phase 10 only) is the sole orchestration framework used.
- **LangGraph outside Phase 10** — no loop around Stage 8→9 (gap ranking → Mode A explanation); Mode A is single-pass retrieval + generation by explicit decision.
- **IEEE Xplore, Springer, ACM Digital Library APIs (or any scripted access to them)** — prohibited as ingestion sources; those venues' papers enter only via Unpaywall (if a legal OA copy exists) or manual upload.
- **Cross-domain gap discovery** (linking concepts across unrelated research fields) — deferred; README §16 future extension, not part of the phased MVP build.
- **Learned research-opportunity ranking** (trained learning-to-rank model replacing the Phase 8 weighted formula) — deferred until the evaluation harness (Phase 13) produces enough labeled/validated gap data; README §16 future extension.
- **Generalizing the pipeline beyond technical/scientific literature** — scope is fixed to CS/AI/ML/NLP/Cybersecurity-core technical literature.
- **Celery/task-queue or extra abstraction layers** not explicitly called for by a phase — no scope creep.
- **`/code-review ultra` as a per-phase gate** — reserved for exactly one full-system pass after Phase 14; every other review gate uses free `/code-review` (+ `/security-review` on Phases 11 and 14).

## Context

- **Problem being solved:** manually surveying ~600 papers to find a genuine research gap costs ~200 hours and still ends in a guess, because authors state limitations in scattered, differently-worded sentences ("fails in low-resource languages" / "decreases for multilingual datasets" / "requires further investigation") that a human recognizes as the same gap but keyword search does not.
- **Core design principle:** `Papers → Structured Facts → Knowledge Graph → Semantic Clusters → Frequency & Trend Analysis → Ranked Gaps → LLM Explanation (grounded)`. The LLM comes last, not first.
- **Two operating modes share one backend:** Mode A (autonomous batch discovery — the core deliverable) and Mode B (interactive GraphRAG Q&A on the same graph + vector index).
- **Evaluation is a first-class deliverable, not an afterthought:** time-shifted validation, survey cross-check, and expert scoring (README §10) exist specifically because "evidence-backed, not an LLM guess" is a claim that must be checked, not assumed.
- **This project is built strictly phase-by-phase** (README §15, 15 phases: 0–14), one at a time, only on the owner's explicit go-ahead. Finishing a phase is never permission to start the next.
- **Five Claude Code skills are mandatory across all phases:** Superpowers (TDD + debugging discipline), GSD (macro plan/execute structure, used selectively per phase), Code Review (`/code-review` free/unlimited every phase; `/code-review ultra` exactly once, after Phase 14), Context Mode (always on; heavy load on Phases 1, 2, 4, 5, 9, 10), Claude Mem (always on, cross-session memory). See README §13 for the full phase → skill mapping table.

## Constraints

- **Tech stack (fixed):** Python 3.11+ / FastAPI backend; React + TypeScript frontend; Docker + Docker Compose orchestrating `backend`, `frontend`, `neo4j`, `qdrant`, `postgres`, `grobid`. PyTorch + Hugging Face Transformers, GROBID (primary parser) with PyMuPDF/pdfplumber fallback, SciBERT (section normalization + token-level embeddings), SPECTER2 (paragraph embeddings), spaCy, a SciERC-style joint NER+RE model (DyGIE++ architecture), DeBERTa-MNLI (contradiction detection), Neo4j (graph), Qdrant (vectors), PostgreSQL/SQLite-dev (relational), HDBSCAN/BERTopic (clustering), a cross-encoder reranker (e.g. MS MARCO MiniLM), Pandas/NumPy (trend analysis), LangGraph (Mode B only), an LLM via direct API call (Llama 3.x / Qwen / GPT — no framework wrapper), Plotly/D3.js (visualization), pytest (testing). — *Why: this is the README §7 spec; substituting any of these requires flagging it to the owner, not silently swapping.*
- **Ingestion sources (fixed):** arXiv, Semantic Scholar (metadata-only), ACL Anthology, Unpaywall (DOI enrichment only, never discovery), manual PDF upload. IEEE Xplore/Springer/ACM Digital Library APIs and any scripted access to them are prohibited. — *Why: licensing/access boundaries the owner has set as a hard line.*
- **Orchestration scope:** LangGraph used only in Phase 10 (Mode B retrieve→assess→reformulate→rerank→generate→grounding-check loop, capped at 3 retrieval rounds). LangChain is excluded everywhere. — *Why: keep the dependency surface minimal and avoid an unnecessary abstraction layer around what is otherwise direct API calls.*
- **Review budget:** `/code-review` and `/security-review` are free/unlimited and run at the end of every phase (plus `/security-review` specifically on Phases 11 and 14). `/code-review ultra` is used exactly once, as a full-system pass after Phase 14 is approved — never per-phase. — *Why: Anthropic gives only 3 free `/code-review ultra` runs before billing $5–20/run; the project is designed to spend at most one.*
- **Secrets:** all API keys/secrets live in `.env` only, never hardcoded or committed; `.env.example` is updated whenever a new variable is introduced. — *Why: standard secrets hygiene, explicit owner requirement.*
- **Config centralization:** model names, gap-score weights (`w1..w5`), retrieval-round cap, top-N retrieval sizes live in one central settings module, not scattered inline. — *Why: these are the knobs most likely to need tuning after the evaluation harness runs; they must be discoverable in one place.*
- **Domain scope:** technical/scientific literature only, core domains CS/AI/ML/NLP/Cybersecurity. — *Why: pipeline assumptions (venue structure, citation norms, rhetorical-role schema) are tuned to this domain; generalizing further is explicitly out of scope for v1.*
- **Testing discipline:** pytest, tests written before implementation (TDD red-green-refactor) for every phase/task. — *Why: owner-mandated engineering discipline (Superpowers skill), catches extraction/ranking errors before they cascade downstream.*

## Key Decisions

<!-- Decisions that constrain future work. All sourced from CLAUDE.md (ADR) and reaffirmed verbatim in README.md; treated as locked project-wide constraints regardless of the doc-synthesizer's non-locked classification, per the project owner's explicit routing instruction. -->

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| README.md is the sole source of truth; CLAUDE.md defers to it on any disagreement (report discrepancy to owner) | Prevents dual-spec drift across a multi-session, multi-phase build | ✓ Locked |
| Phase control belongs to the project owner — one phase at a time, in README §15 order, never auto-advance past a finished phase | Solo-developer pacing; prevents runaway scope in a spec this large | ✓ Locked |
| Five skills (Superpowers, GSD, Code Review, Context Mode, Claude Mem) are mandatory, not optional, per the README §13 phase mapping | Defined build process — discipline, context management, and review are part of "done," not extras | ✓ Locked |
| No LangChain anywhere in the project | Deliberately excluded to avoid an unnecessary framework layer around direct API/LLM calls | ✓ Locked |
| LangGraph scoped to Phase 10 only (Mode B retrieve/assess/reformulate/rerank/generate/grounding-check loop) | Only Mode B genuinely needs a stateful, loopable graph; everywhere else is linear | ✓ Locked |
| No loop around Stage 8→9 (gap ranking → Mode A explanation) — Phase 9 is single-pass | Considered and explicitly declined; keeps Mode A simple, deterministic, and cheap to run at batch scale | ✓ Locked |
| Ingestion sources fixed to arXiv, Semantic Scholar, ACL Anthology, Unpaywall (DOI-enrichment only), manual upload; IEEE Xplore/Springer/ACM Digital Library APIs prohibited | Licensing/access boundary set by the owner; avoids scripted access to restricted platforms | ✓ Locked |
| Normalized corpus, source-agnostic downstream from Phase 2 onward — nothing branches on `source` | One canonical `papers` schema keeps every downstream stage simple and uniform regardless of ingestion path | ✓ Locked |
| The LLM comes last — structured extraction, clustering, and ranking happen before any LLM call | Core design principle; makes gap reports explainable and reproducible instead of a free-form guess | ✓ Locked |
| Scope restricted to technical/scientific literature (core: CS/AI/ML/NLP/Cybersecurity) | Keeps pipeline assumptions (venue norms, rhetorical structure) valid; no cross-domain generalization in v1 | ✓ Locked |
| No scope creep — no extra frameworks/services/task queues/abstraction layers beyond what a phase specifies | Engineering discipline; e.g. no Celery unless a phase explicitly calls for it, no learned ranker in the MVP | ✓ Locked |
| `/code-review ultra` used exactly once, as a full-system pass after Phase 14 is approved | Anthropic gives only 3 free runs before billing $5–20/run each; `/code-review` and `/security-review` (free, unlimited) cover every other review gate | ✓ Locked |
| TDD discipline — tests written before implementation for every phase/task | Superpowers red-green-refactor; catches extraction/ranking errors before they cascade to downstream phases | ✓ Locked |
| Secrets and API keys live in `.env` only, never hardcoded or committed; `.env.example` updated on new variables | Standard secrets hygiene | ✓ Locked |
| Config values (model names, gap-score weights, retrieval caps, top-N sizes) centralized in one settings module | Avoids scattered inline magic numbers that are hard to tune after evaluation | ✓ Locked |
| Small, atomic commits per task, per GSD's workflow | Engineering discipline; keeps history reviewable and bisectable | ✓ Locked |

---
*Last updated: 2026-08-21 after initial project creation (ingest-driven, source: README.md + CLAUDE.md)*

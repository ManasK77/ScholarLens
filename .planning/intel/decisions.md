# Decisions (extracted from ADR-classified sources)

Source: `C:\Users\Manas\Desktop\NLP\CLAUDE.md` (classified: ADR, confidence: high, manifest_override: true, precedence: 1, locked: false)

> Classifier note: this document is operating-instructions style (no Context/Decision/Consequences structure, no numbered filename, no explicit "Accepted" status marker), so per the classifier's rule that only Accepted-status ADRs are locked, `locked` was left `false`. All decisions below are therefore treated as **proposed, not LOCKED** for precedence purposes — but see the precedence override note at the end of this file, which governs how any future contradiction with README.md must be resolved.

---

### D-01 — Source of truth: README.md governs
- **Source:** CLAUDE.md
- **Status:** proposed (non-locked)
- **Decision:** README.md is the complete and authoritative specification for this project (problem statement, architecture, tech stack, pipeline, all 15 phases, skills workflow). CLAUDE.md is an operating-instructions summary, not a substitute. If CLAUDE.md and README.md ever disagree, **README.md wins**, and the discrepancy must be reported to the owner.
- **Scope:** precedence / source-of-truth
- **Cross-check:** Confirmed against README.md directly — no disagreement found in this ingest (see SYNTHESIS.md).

### D-02 — Phase control belongs to the project owner
- **Source:** CLAUDE.md (reaffirmed in README.md §14 pts 2, 7; §15 preamble)
- **Status:** proposed
- **Decision:** Work happens strictly one phase at a time, in the order defined in README §15. Never begin or advance a phase without the owner's explicit instruction. Finishing Phase N is not permission to begin Phase N+1. When a phase is done: summarize what was built, files created/modified, deviations/assumptions, review outcome — then stop and wait.
- **Scope:** workflow / phase gating

### D-03 — Five skills are mandatory, not optional
- **Source:** CLAUDE.md (reaffirmed in README.md §13)
- **Status:** proposed
- **Decision:** Superpowers, GSD, Code Review (`/code-review`, `/code-review ultra`, `/security-review`), Context Mode, and Claude Mem are all in use. README §13 defines which apply to which phase; that mapping is a phase requirement, not a suggestion. Confirm all five are active on the first session (`/help`, `/contextmode:ctx-stats`) and report anything missing before starting.
- **Scope:** tooling / skills workflow

### D-04 — No LangChain anywhere
- **Source:** CLAUDE.md (reaffirmed verbatim in README.md §7 tech-stack row, §9, §14 pt 4)
- **Status:** proposed
- **Decision:** LangChain is deliberately excluded from this project, in every phase.
- **Scope:** architecture / orchestration framework

### D-05 — LangGraph scoped to Phase 10 only
- **Source:** CLAUDE.md (reaffirmed in README.md §9, §14 pt 4)
- **Status:** proposed
- **Decision:** LangGraph is used only in Phase 10 (Mode B: retrieve → assess → reformulate → rerank → generate → grounding check). Nowhere else in the project.
- **Scope:** architecture / Mode B

### D-06 — No loop around Stage 8→9
- **Source:** CLAUDE.md (reaffirmed verbatim in README.md §8 Stage 9, §14 pt 5)
- **Status:** proposed
- **Decision:** Phase 9 (Mode A explanation) is single-pass retrieval + generation, not a loop. A LangGraph (or any) loop around gap ranking → Mode A explanation was considered and explicitly declined.
- **Scope:** architecture / Mode A

### D-07 — Ingestion sources are fixed
- **Source:** CLAUDE.md (reaffirmed in README.md §7, §8 Stage 1, §14 pt 6, Phase 1 entry)
- **Status:** proposed
- **Decision:** Permitted ingestion sources: arXiv, Semantic Scholar, ACL Anthology, Unpaywall (DOI enrichment only — never a discovery source), and manual PDF upload. IEEE Xplore, Springer, and ACM Digital Library APIs are prohibited, as is any scripted access to those platforms.
- **Scope:** ingestion / data sources

### D-08 — Normalized corpus, source-agnostic downstream
- **Source:** CLAUDE.md (reaffirmed verbatim in README.md §8 Stage 1 "Normalized corpus" + Phase 2 entry)
- **Status:** proposed
- **Decision:** All ingestion converges on one normalized corpus (canonical `papers` schema in PostgreSQL). Phase 2 onward must never branch on a paper's `source` field; the field exists for provenance/auditing only.
- **Scope:** architecture / data model

### D-09 — The LLM comes last
- **Source:** CLAUDE.md (reaffirmed verbatim in README.md §4)
- **Status:** proposed
- **Decision:** Structured extraction, clustering, and ranking happen before any LLM call. The LLM explains evidence already retrieved; it never discovers gaps on its own.
- **Scope:** architecture / core design principle

### D-10 — Scope restricted to technical/scientific literature
- **Source:** CLAUDE.md
- **Status:** proposed
- **Decision:** Scope is technical/scientific literature (core: CS/AI/ML/NLP/Cybersecurity). Do not generalize the pipeline to other domains.
- **Scope:** product scope
- **Note:** README.md does not explicitly state this restriction (it is silent on domain boundaries beyond the worked Cybersecurity/LLM-Security example in §1). Silence is not a contradiction — this is additive content found only in CLAUDE.md, not conflicting with README.md.

### D-11 — No scope creep
- **Source:** CLAUDE.md (reaffirmed in README.md §14 pt 9)
- **Status:** proposed
- **Decision:** No extra frameworks, services, task queues, or abstraction layers beyond what a phase specifies (e.g., no Celery/task-queue unless a phase explicitly calls for it, no learned ranker in the MVP).
- **Scope:** engineering discipline

### D-12 — `/code-review ultra` used exactly once, after Phase 14
- **Source:** CLAUDE.md (reaffirmed verbatim in README.md §13.2, §13.5, §14 pt 13, and the Phase 14 closing note)
- **Status:** proposed
- **Decision:** `/code-review` and `/security-review` are free and unlimited — use them at the end of every phase without hesitation. `/code-review ultra` gives only 3 free runs before billing usage credits ($5–20/run); it is used exactly once in this project, as a single full-system pass after Phase 14 is approved. Never call it during Phases 0–14, even on a high-stakes phase.
- **Scope:** tooling / review gates

### D-13 — TDD discipline (tests before implementation)
- **Source:** CLAUDE.md (reaffirmed in README.md §13.1 Superpowers row, §14 pt 8)
- **Status:** proposed
- **Decision:** Tests are written before implementation (Superpowers red-green-refactor TDD) for every phase/task.
- **Scope:** engineering discipline

### D-14 — Secrets management
- **Source:** CLAUDE.md (reaffirmed in README.md §12)
- **Status:** proposed
- **Decision:** Secrets and API keys live in `.env` only — never hardcoded, never committed. Update `.env.example` when adding a variable.
- **Scope:** configuration / security

### D-15 — Config values centralized
- **Source:** CLAUDE.md (reaffirmed verbatim in README.md §12)
- **Status:** proposed
- **Decision:** Config values (model names, gap-score weights, retrieval caps, top-N sizes) belong in the central settings module, not scattered inline.
- **Scope:** configuration / architecture

### D-16 — Commit discipline
- **Source:** CLAUDE.md
- **Status:** proposed
- **Decision:** Prefer small, atomic commits per task, per GSD's workflow.
- **Scope:** engineering discipline / process

---

## Precedence override note (applies to all decisions above)

This ingest's manifest assigned **README.md precedence=0 (SPEC, highest)** and **CLAUDE.md precedence=1 (ADR)** — inverting the doc-synthesizer's generic default (`ADR > SPEC`). This override was applied deliberately because CLAUDE.md's own text (D-01) states the same rule: README.md wins on disagreement. All decisions above were cross-checked line-by-line against README.md during synthesis; **no contradictions were found** — see `INGEST-CONFLICTS.md` for the transparency record. This file should still be treated as the effective decision register for downstream planning, with README.md as the fallback resolver for any future addition that creates a genuine conflict.

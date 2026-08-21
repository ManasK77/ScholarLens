# CLAUDE.md — Operating Instructions for Claude Code

This file defines **how to work in this repository**. It is deliberately short.

## Source of truth

`README.md` is the complete and authoritative specification for this project — problem statement, architecture, tech stack, pipeline, all 15 implementation phases, and the skills workflow.

**Read `README.md` in full at the start of every session before doing any work.** This file does not summarize it and is not a substitute for it. If this file and the README ever disagree, the README wins — and tell the owner about the discrepancy.

## Phase control belongs to the project owner

- Work happens **strictly one phase at a time**, in the order defined in README Section 15.
- **Never begin or advance a phase without the owner's explicit instruction** (e.g. *"start Phase 4"*, *"proceed"*). Finishing Phase N is not permission to begin Phase N+1.
- When a phase is done: summarize what was built, list files created/modified, note deviations and assumptions, report the review outcome — then **stop and wait**.
- If asked to "continue" with no phase named, ask which phase rather than guessing.

## Skills are mandatory, not optional

Five plugins are in use: **Superpowers, GSD, Code Review (`/code-review`, `/code-review ultra`, `/security-review`), Context Mode, Claude Mem**.

- README **Section 13** defines which skills apply to which phase and the per-phase workflow. Apply it automatically — do not wait to be reminded to plan, to write tests first, or to run a review.
- Every phase in Section 15 carries a **Skills:** line. That line is a requirement of the phase.
- A phase is **not complete** until `/code-review` has run and its findings are addressed. `/code-review` and `/security-review` are free and unlimited — use them at the end of every phase without hesitation.
- **`/code-review ultra` is not a per-phase tool.** Anthropic gives only 3 free runs before it bills usage credits ($5–20/run). It is used exactly **once** in this project: a single full-system pass after Phase 14 is approved. Never call it during Phases 0–14, even on a high-stakes phase.
- On the first session of a project, confirm all five plugins are active (`/help`, `/contextmode:ctx-stats`) and report anything missing before starting.

## Hard constraints (never violate, never silently "improve")

These are settled decisions. If one seems wrong, raise it with the owner — do not act on your own judgement.

1. **No LangChain.** Anywhere. It is deliberately excluded from this project.
2. **LangGraph only in Phase 10** (Mode B: retrieve → assess → reformulate → rerank → generate → grounding check). Nowhere else.
3. **No loop around Stage 8→9.** Phase 9 (Mode A explanation) is single-pass retrieval + generation. This was considered and declined.
4. **Ingestion sources are fixed**: arXiv, Semantic Scholar, ACL Anthology, Unpaywall (DOI enrichment only — never a discovery source), and manual PDF upload. **IEEE Xplore, Springer, and ACM Digital Library APIs are prohibited**, as is scripted access to those platforms.
5. **All ingestion converges on one normalized corpus.** Phase 2 onward must never branch on a paper's `source` field.
6. **The LLM comes last.** Structured extraction, clustering, and ranking happen before any LLM call. The LLM explains evidence that has already been retrieved; it never discovers gaps on its own.
7. **Scope is technical/scientific literature** (core: CS/AI/ML/NLP/Cybersecurity). Do not generalize the pipeline to other domains.
8. **No scope creep.** No extra frameworks, services, task queues, or abstraction layers beyond what a phase specifies.
9. **`/code-review ultra` costs money past 3 free runs.** Use it exactly once, after Phase 14 is complete — never as part of any individual phase. `/code-review` and `/security-review` are free and unlimited; use those for every other review gate.

## Working conventions

- Python 3.11+, FastAPI backend, React + TypeScript frontend, pytest for tests.
- **Tests are written before implementation** (Superpowers TDD). Red, then green, then refactor.
- Secrets and API keys live in `.env` only — never hardcoded, never committed. Update `.env.example` when adding a variable.
- Config values (model names, gap-score weights, retrieval caps, top-N sizes) belong in the central settings module, not scattered inline.
- Prefer small, atomic commits per task, per GSD's workflow.

## When something is unclear

Ask the owner. Do not substitute a different library, invent a source, skip a skill, or reinterpret a constraint to make progress. Stopping to ask is always the correct move here — this project is spec-driven by design.

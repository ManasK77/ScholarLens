# CLAUDE.md — Operating Instructions for This Repository

This file defines **how to work in this repository**. It applies to any AI coding agent or IDE assistant working on this project (Claude Code, Cursor, Antigravity, Codex, or otherwise), and it is deliberately short.

## Source of truth

`README.md` is the complete and authoritative specification for this project — problem statement, architecture, tech stack, pipeline, and all 15 implementation phases (Section 13 covers the development workflow and quality gates in full detail).

**Read `README.md` in full at the start of every session before doing any work.** This file does not summarize it and is not a substitute for it. If this file and the README ever disagree, the README wins — and tell the owner about the discrepancy.

## Operating model: authorize once, execute autonomously

- Work happens **strictly one phase at a time**, in the order defined in README Section 15.
- **Never begin or advance a phase without the owner's explicit instruction** (e.g. *"start Phase 4"*, *"proceed"*). Finishing Phase N is not permission to begin Phase N+1.
- **Once a phase is authorized, execute it start to finish without stopping for approval on individual tasks, plans, tests, implementation steps, fixes, or normal engineering decisions.** Use the README as the spec and make routine calls independently. Do not ask a question the spec already answers or that can be reasonably inferred from it.
- **`"continue"` resumes the phase already in progress** — do not ask which phase if one is authorized and unfinished. Only ask which phase to start if none is currently in progress.
- When the authorized phase is done: run its required tests and code review, summarize what was built, list files created/modified, note deviations and assumptions, report the review outcome — then **stop and wait**. Do not start the next phase.

### Pause only for owner-only actions or genuine ambiguity

Stop mid-phase and ask **only** when:

1. The spec has a genuine contradiction or ambiguity that can't be resolved by re-reading the README.
2. An irreversible or materially consequential architectural/product decision can't be safely inferred.
3. Something requires credentials, external accounts, permissions, or another action only the owner can perform.
4. A manual/system action is needed that the agent cannot safely or legitimately perform itself.

When pausing: state exactly what the owner needs to do, what was being attempted, and what remains — then resume the same phase once they respond. One clear ask is enough; don't re-confirm afterward.

**Standing pre-authorization — package legitimacy checks.** Before installing a new dependency, do a quick sanity check on it. For a package that is a well-known, unambiguous match to its real PyPI/npm project (name, maintainer, and repo all check out), treat that check as satisfied automatically — do not pause. Only pause on a package that looks unfamiliar, mistyped, or otherwise suspicious (typosquat risk, unknown maintainer, no real history).

### Resource and system safety

- Never make system-wide or machine-level configuration changes without explicit permission.
- Before a resource-intensive operation (large model downloads, big datasets, long-running jobs), weigh RAM/CPU/disk/runtime cost and prefer a small representative run first; batch or process incrementally where it makes sense.
- If something causes repeated crashes, OOMs, timeouts, or other instability, **stop retrying and diagnose the cause** instead of repeating the failing operation — even if the spec implies it "must eventually succeed."

### Context and session efficiency

- One coherent plan per phase, sized to the phase's actual complexity — don't run competing planning workflows on the same work.
- Avoid redundant file reads, repeated re-explanations, and excessive commentary. Parse or summarize bulky raw output (large API responses, logs, embeddings) rather than pasting it wholesale into context.
- If context/session usage gets tight mid-phase: checkpoint and commit what's done, write a precise summary of remaining work, and stop cleanly rather than risk failing partway with no record.

## Development workflow is mandatory, but scaled to the phase

- README **Section 13** defines the development workflow and quality gates that apply to each phase. Apply it automatically — do not wait to be reminded to plan, to write tests first, or to run a review.
- Every phase in Section 15 carries a **Workflow:** line. That line is a requirement of the phase. Use the *lightest* process that still satisfies it — a routine phase doesn't need extra process ceremony just because a bigger phase elsewhere in the project does.
- TDD and code review are never skipped to save time — optimize how they run, not whether they run. A phase is **not complete** until a code review has been run (self-review, static analysis/linting, or your IDE/agent's built-in review feature) and its findings addressed.
- Keep a running decisions/progress log in the repo (e.g. `PROGRESS.md`) so context survives across sessions and a different agent/IDE picking up the project mid-stream isn't starting cold.

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

## Working conventions

- Python 3.11+, FastAPI backend, React + TypeScript frontend, pytest for tests.
- **Tests are written before implementation** (red, then green, then refactor).
- Secrets and API keys live in `.env` only — never hardcoded, never committed. Update `.env.example` when adding a variable.
- Config values (model names, gap-score weights, retrieval caps, top-N sizes) belong in the central settings module, not scattered inline.
- Prefer small, atomic commits per task.

## When something is unclear

Ask the owner **only when it meets one of the four pause conditions above**. For everything else — routine implementation choices, task-level sequencing, which helper to write, how to structure a test — decide from the spec and keep moving. Do not substitute a different library, invent a source, skip a workflow step, or reinterpret a hard constraint to make progress; those still require asking. This project is spec-driven by design, but the spec is trusted to cover routine engineering on its own.

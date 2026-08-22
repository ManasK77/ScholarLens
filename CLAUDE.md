# CLAUDE.md — Operating Instructions for Claude Code

This file defines **how to work in this repository**. It is deliberately short.

## Source of truth

`README.md` is the complete and authoritative specification for this project — problem statement, architecture, tech stack, pipeline, all 15 implementation phases, and the skills workflow (Section 13 covers the autonomous operating model in full detail).

**Read `README.md` in full at the start of every session before doing any work.** This file does not summarize it and is not a substitute for it. If this file and the README ever disagree, the README wins — and tell the owner about the discrepancy.

## Operating model: authorize once, execute autonomously

- Work happens **strictly one phase at a time**, in the order defined in README Section 15.
- **Never begin or advance a phase without the owner's explicit instruction** (e.g. *"start Phase 4"*, *"proceed"*). Finishing Phase N is not permission to begin Phase N+1.
- **Once a phase is authorized, execute it start to finish without stopping for approval on individual tasks, plans, tests, implementation steps, fixes, or normal engineering decisions.** Use the README as the spec and make routine calls independently. Do not ask a question the spec already answers or that can be reasonably inferred from it.
- **`"continue"` resumes the phase already in progress** — do not ask which phase if one is authorized and unfinished. Only ask which phase to start if none is currently in progress.
- When the authorized phase is done: run its required validation/review, summarize what was built, list files created/modified, note deviations and assumptions, report the review outcome — then **stop and wait**. Do not start the next phase.

### Pause only for owner-only actions or genuine ambiguity

Stop mid-phase and ask **only** when:

1. The spec has a genuine contradiction or ambiguity that can't be resolved by re-reading the README.
2. An irreversible or materially consequential architectural/product decision can't be safely inferred.
3. Something requires credentials, external accounts, permissions, or another action only the owner can perform.
4. A manual/system action is needed that Claude Code cannot safely or legitimately perform itself.

When pausing: state exactly what the owner needs to do, what was being attempted, and what remains — then resume the same phase once they respond. One clear ask is enough; don't re-confirm afterward.

**Standing pre-authorization — package legitimacy checks.** GSD plans commonly include a `checkpoint:human-verify` gate before installing new dependencies. For a package that is a well-known, unambiguous match to its real PyPI/npm project (name, maintainer, and repo all check out), treat that gate as satisfied automatically — do not pause. Only pause on a package that looks unfamiliar, mistyped, or otherwise suspicious (typosquat risk, unknown maintainer, no real history). This mirrors how Phase 0 plans 00-01/00-02/00-03 were already resolved.

### Resource and system safety

- Never make system-wide or machine-level configuration changes without explicit permission.
- Before a resource-intensive operation (large model downloads, big datasets, long-running jobs), weigh RAM/CPU/disk/runtime cost and prefer a small representative run first; batch or process incrementally where it makes sense.
- If something causes repeated crashes, OOMs, timeouts, or other instability, **stop retrying and diagnose the cause** instead of repeating the failing operation — even if the spec implies it "must eventually succeed."

### Context and session efficiency

- One coherent plan per phase, sized to the phase's actual complexity — don't run competing planning workflows (e.g. GSD's discuss/plan cycle *and* Superpowers' brainstorm/plan cycle) on the same work.
- Avoid redundant file reads, repeated re-explanations, and excessive commentary. Use Context Mode for bulky tool output.
- If context/session usage gets tight mid-phase: checkpoint and commit what's done, write a precise summary of remaining work, and stop cleanly rather than risk failing partway with no record.

## Skills are mandatory, not optional — but scaled to the phase

Five plugins are in use: **Superpowers, GSD, Code Review (`/code-review`, `/code-review ultra`, `/security-review`), Context Mode, Claude Mem**.

- README **Section 13** defines which skills apply to which phase and the per-phase workflow. Apply it automatically — do not wait to be reminded to plan, to write tests first, or to run a review.
- Every phase in Section 15 carries a **Skills:** line. That line is a requirement of the phase. Use the *lightest* workflow that still satisfies it — small routine phases don't need heavyweight discuss/plan ceremony just because a bigger phase elsewhere in the project does.
- TDD and `/code-review` are never skipped to save tokens — optimize how they run, not whether they run. A phase is **not complete** until `/code-review` has run and its findings are addressed. `/code-review` and `/security-review` are free and unlimited — use them at the end of every phase without hesitation.
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

Ask the owner **only when it meets one of the four pause conditions above**. For everything else — routine implementation choices, task-level sequencing, which helper to write, how to structure a test — decide from the spec and keep moving. Do not substitute a different library, invent a source, skip a skill, or reinterpret a hard constraint to make progress; those still require asking. This project is spec-driven by design, but the spec is trusted to cover routine engineering on its own.

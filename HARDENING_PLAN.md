# Local pipeline hardening

Authorized 2026-09-16: implement the evaluated `priority_fix.md` changes, preserve
existing algorithms and API behavior, test first and review before completion.

1. Establish test baseline; inspect service availability without changing data.
2. Add logging/timers/progress, persistent run and stage state, and a single-writer
   lock. Test checkpoints, failure, interrupted-run resume and lock contention.
3. Make graph replacement atomic per paper; make index pruning mandatory for
   completion. Scope header enrichment to one paper. Test store convergence.
4. Integrate per-paper input fingerprints and checkpoints in S2–S5, gate derived
   rebuilds, preserve historical reports as explicitly stale snapshots. Keep
   S6–S8 atomic and Mode A single-pass; keep Mode B unchanged.
5. Batch SciBERT fallback sentences/headings; provide conservative opt-in controls
   and an offline benchmark for riskier SPECTER2/GLiNER2 batching. Do not change
   machine thread settings without measured evidence.
6. Add read-only reconciliation and resumable deletion/changed-PDF handling.
7. Run focused/full backend, evaluation, frontend, lint/format/build checks;
   review failure paths, fingerprint completeness, auth and compatibility.
8. Perform representative real-service checks if Docker/services are available.
   Do not rebuild or delete the user's corpus without a verified backup.

Live environment at start: Docker daemon is not reachable. Runtime recovery and
real-model performance acceptance require services/model caches; record these
separately from unit/integration results. No deployment work is in scope.

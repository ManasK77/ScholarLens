# PROGRESS

Running decisions/progress log (CLAUDE.md §"Development workflow"). `.planning/`
holds the per-phase GSD artifacts; this file is the cross-session summary.

---

## 2026-09-06 — Phase 11 (Backend API Integration): API-key auth landed, router registration BLOCKED

### Repository state discovered at the start of this phase

`.planning/STATE.md`, `.planning/ROADMAP.md`, and `.planning/REQUIREMENTS.md`
contain **unresolved git conflict markers** (`<<<<<<< Updated upstream` /
`>>>>>>> Stashed changes`) that were committed upstream in `478597b`. The two
sides disagree about what is built:

- "Updated upstream" side: Phase 3 complete, Phase 4 not authorized.
- "Stashed changes" side: Phases 0–13 complete, 146 backend tests passing.

The **working tree matches the first claim for backend source, and the second
for everything else**. `git log --all --diff-filter=A` confirms the Phase 4–9
implementation modules were *never* added in any commit on `main` or
`origin/main` — the upstream pushes were incomplete, not lost locally. There is
no stash to recover them from (`git stash list` is empty).

**Missing modules** (imported by code that *is* present, so they are load-bearing):

| Phase | Missing module | Imported by |
|---|---|---|
| 4 | `app/ner_re/schema.py` | `app/ner_re/graph.py` |
| 4 | `app/ner_re/service.py` | `app/api/routes_backend.py` |
| 5 | `app/embeddings/encoder.py`, `app/embeddings/schema.py` | `app/embeddings/indexer.py` |
| 6 | `app/clustering/service.py`, `app/clustering/storage.py` | `app/api/routes_backend.py` |
| 7 | `app/trends/service.py`, `app/trends/storage.py` | `app/api/routes_backend.py` |
| 8 | `app/ranking/service.py`, `app/ranking/storage.py` | `app/api/routes_backend.py` |
| 9 | `app/explanation/service.py`, `app/explanation/storage.py` | `app/api/routes_backend.py` |
| 10 | `langgraph` (not in `pyproject.toml`) | `app/interactive/workflow.py` |

`Settings` is also missing 11 fields that shipped code already reads:
`retrieval_round_cap`, `retrieval_top_n`, `llm_provider`, `llm_timeout_seconds`,
`gemini_api_key`, `gemini_api_url`, `gemini_model`, `ollama_url`,
`ollama_model`, `qdrant_collection`, `embedding_batch_size`.

### Decisions

- **D-11-01 — `routes_backend.router` is NOT registered in `main.py`.** It
  cannot be imported (12 missing modules above), so registering it would abort
  application startup and take `/health`, `/collect`, and `/upload` down with
  it. No `try/except ImportError` guard was added either: making the API's route
  set depend on whether an import happened to succeed is the wrong property for
  a security-reviewed surface. Registration is a one-line change once Phases
  4–10 land.
- **D-11-02 — Phases 4–10 were not implemented here.** README §15 / CLAUDE.md
  bind work to one authorized phase at a time; Phase 4 additionally requires an
  owner discussion to lock the SciERC model choice before any code. Writing them
  under a Phase 11 authorization would be seven unauthorized phases of ML code.
- **D-11-03 — API-key auth fails closed.** With `API_KEY` unset or blank, every
  authenticated endpoint returns 500 rather than allowing anonymous access. The
  `if configured_key and not match: reject` shape was explicitly rejected: it
  silently opens the whole API the moment the variable goes missing.
- **D-11-04 — `GET /health` stays unauthenticated.** Docker Compose and any
  orchestrator probe it without a credential, and it exposes only per-service
  up/down booleans.
- **D-11-05 — `/collect` and `/upload` are now behind the API key.** README §12
  already documents them being called with `X-API-Key`; before this change both
  were anonymous, so anyone able to reach the port could write PDFs to the store
  and drive outbound arXiv / Semantic Scholar / ACL / Unpaywall traffic.
- **D-11-06 — no CORS middleware.** The dashboard reaches the backend through
  the same-origin Vite proxy, so none is needed; adding one would only widen the
  surface. Revisit in Phase 14 if the frontend is ever served cross-origin.
- **D-11-07 — the configured key is `.strip()`ed, the presented header is not.**
  A CRLF-terminated `.env` line must not lock the owner out, but trimming caller
  input would widen what counts as a match.

### Files created / modified

- `backend/app/api/security.py` (new) — `require_api_key` dependency.
- `backend/tests/api/test_security.py` (new) — 23 auth tests.
- `backend/app/core/config.py` — added `Settings.api_key`.
- `backend/app/api/routes_ingestion.py` — router-level `require_api_key`.
- `backend/tests/api/test_routes_ingestion.py` — tests now authenticate.
- `backend/tests/core/test_config.py` — 3 `API_KEY` tests.
- `.env.example` — documents `API_KEY`; `.env` got a generated dev value.

### Verification

156 backend tests pass; 4 pre-existing collection errors remain
(`test_query_errors`, `test_indexing`, `test_workflow`, `test_graph`) — all four
caused by the missing modules above, unchanged from the pre-phase baseline.
`ruff check` clean on everything touched. Frontend: 7 tests, build, and lint
pass. Live-verified against the running Docker stack: `/health` 200 with all
three services up, `/collect` and `/upload` 401 without a key and accepted with
it, 500 for every authenticated route when `API_KEY` is unset.

### Next

Phase 11 is **not complete**. Remaining work needs Phases 4–10 to exist first —
either re-push them from the machine that authored `478597b`, or authorize
rebuilding them phase by phase. Also worth resolving: the conflict markers still
sitting in three `.planning/` files.

---

## 2026-09-06 — Project-state reconciliation (investigation only, no source changes)

### What was reconciled

All conflict markers removed from `.planning/STATE.md` (4 conflicts),
`.planning/ROADMAP.md` (2) and `.planning/REQUIREMENTS.md` (1). Resolutions were
derived from the source tree, tests and git history — **not** by picking a side.
Neither side was wholly correct: "Updated upstream" is right for Phases 4–11,
"Stashed changes" is right for Phases 12–13.

Corroborating evidence: the *non-conflicted* checkbox list in REQUIREMENTS.md
(lines 49–105) already marks NERRE/EMBED/CLUSTER/TREND/GAPRANK/MODEA/MODEB/API
as `[ ]` and UI/EVAL as `[x]` — independently matching the tree.

### Git recovery: definitively impossible

Searched all 43 commits, 285 trees, 272 blobs, both refs (`main`,
`origin/main` — both at 478597b), the 91-entry reflog, and every unreachable
object. Zero unreachable commits; `git stash list` empty. The only
`service.py`/`storage.py`/`schema.py` in history belong to `app/extraction/` and
`app/parsing/`. `encoder.py` has never existed in this object database.

478597b was authored by **Jain Kanish <jainkanish412@gmail.com>** — a
collaborator, not the repo owner — and pushed an incomplete tree.

### The uv.lock fossil (D-REC-01)

`backend/uv.lock` was added in 478597b but `backend/pyproject.toml` was last
touched in `fe9d462`. The lock therefore records an **uncommitted**
`pyproject.toml`, and is the only surviving evidence of the lost Phase 4–10
tech choices:

`gliner2[local]>=2.0,<3` · `langgraph>=1.2,<2` · `pandas>=2.2,<4` ·
`numpy>=2.2,<3` · `scikit-learn>=1.6,<2`

Two of these are README deviations the owner never recorded a decision for:
GLiNER2 in place of a SciERC/DyGIE++ model (Phase 4), and scikit-learn with
**no hdbscan and no bertopic** (Phase 6). Treat the lock as evidence of what was
installed, never as evidence that a phase was correctly implemented.

### Contracts the surviving modules pin down

The four orphaned modules constrain what any rebuild must provide:

- `ner_re/schema.py` → `RELATION_ENDPOINTS` (dict: relation → (head_type, tail_type)),
  `JointExtractionResult{.entities, .relations}`, `EntityMention{.text, .entity_type, .confidence}`,
  `RelationMention{.head, .tail, .relation_type, .confidence}`
- `embeddings/encoder.py` → `Specter2Encoder{.dimension, .encode(texts, batch_size)}`, `get_encoder()`
- `embeddings/schema.py` → `IndexRecord{.record_id, .text, .payload()}`
- Plus `Settings` fields: `retrieval_round_cap`, `retrieval_top_n`, `llm_provider`,
  `llm_timeout_seconds`, `gemini_api_key`, `gemini_api_url`, `gemini_model`,
  `ollama_url`, `ollama_model`, `qdrant_collection`, `embedding_batch_size`

### Not reconciled — owner decision required

1. Phase 4 joint NER+RE model choice (ROADMAP requires locking it before any code).
2. Phase 6 clustering library (README says HDBSCAN/BERTopic; the lost build used scikit-learn).
3. Whether Phases 4–13 were ever authorized.
4. Whether Phase 14 is "explicitly deferred by owner".

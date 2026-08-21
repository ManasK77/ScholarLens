# Phase 0: Project Scaffolding & Environment - Context

**Gathered:** 2026-08-21
**Status:** Ready for planning

<domain>
## Phase Boundary

Stand up a working, empty scaffold: the repository directory structure from README §11, a Docker Compose stack bringing up empty Neo4j/Qdrant/Postgres containers, a FastAPI backend with a `/health` endpoint that verifies all three service connections, a React+TypeScript empty-shell frontend, pytest wiring, pre-commit, and `.env`/`.env.example` handling. No ML/NLP logic, no ingestion, no real UI — purely infrastructure per README §15 Phase 0.

</domain>

<decisions>
## Implementation Decisions

**Owner directive:** the owner explicitly asked to skip the interactive discussion round for Phase 0 ("just go ahead with implementation... without asking any further Qs unless critical"). All gray areas below were decided by Claude using README/CLAUDE.md constraints and standard, low-scope defaults — no scope creep, nothing speculative beyond what SETUP-01..06 requires.

### Python tooling & pre-commit
- **D-01:** Packaging via a plain `pyproject.toml` (PEP 621, `hatchling` build backend) — no Poetry/uv dependency-manager lock-in, since README only mandates `pyproject.toml` and introducing a package manager opinion isn't specified.
- **D-02:** Dependencies installed via `pip install -e ".[dev]"`; a `requirements` extras group holds dev tooling (pytest, ruff, pre-commit).
- **D-03:** Pre-commit hooks: `ruff` (lint + format, replaces black/isort/flake8 in one tool) + standard `pre-commit-hooks` (trailing-whitespace, end-of-file-fixer, check-yaml, check-added-large-files). No mypy in Phase 0 — type checking can be added later if a phase calls for it; adding it now would be scope creep beyond SETUP-01..06.

### Docker Compose service config
- **D-04:** `docker-compose.yml` includes exactly `neo4j`, `qdrant`, `postgres`, and `backend` — matching Phase 0's literal "Done when" criterion (`docker-compose up` + `/health`). `frontend` and `grobid` are NOT containerized yet: frontend is an empty shell runnable via `npm run dev` (no backend dependency to containerize yet), and grobid is Phase 2's concern (parsing hasn't started). Both join the compose file when their phase needs them (frontend could be added incrementally, grobid explicitly in Phase 2; full multi-service compose is Phase 14's job).
- **D-05:** Versions/ports: `neo4j:5-community` (7474 HTTP / 7687 Bolt), `qdrant/qdrant:latest` (6333 REST / 6334 gRPC), `postgres:16-alpine` (5432), backend on 8000. Neo4j/Postgres get named volumes for persistence across restarts; Qdrant gets a volume too. All standard defaults, nothing exotic.

### FastAPI /health contract
- **D-06:** `GET /health` returns `{"status": "ok"|"degraded", "services": {"neo4j": true|false, "qdrant": true|false, "postgres": true|false}}`. HTTP 200 when all three are `true` (the Done-when case); HTTP 503 when any is `false` (standard health-check semantics — a monitoring tool should be able to page on a non-200). Each check does a real round-trip (Neo4j: `RETURN 1` Cypher query via the driver; Qdrant: `GET /collections` via its client; Postgres: `SELECT 1` via SQLAlchemy/psycopg).
- **D-07:** Connection settings (URIs, credentials) come from a central `app/core/config.py` (pydantic-settings), reading from `.env` — not scattered inline, per CLAUDE.md's config-centralization rule.

### React+TS skeleton & .env variables
- **D-08:** Frontend scaffolded with Vite's `react-ts` template — minimal, fast, no SSR needed for a dashboard SPA (Next.js would be scope creep; CRA is deprecated). Truly an empty shell: default Vite starter page, no routing/state libraries added yet (Phase 12 owns real UI work).
- **D-09:** `.env.example` seeds the full README §12 variable set now (even though most aren't consumed until later phases), since SETUP-06 says "`.env` handling and `.env.example` are established for secrets/config" — establishing the contract now avoids later phases silently inventing var names. Phase 0's own code only reads the DB/service connection vars (D-07); ingestion/LLM vars are documented but unused until their phase lands. Variables: `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `QDRANT_URL`, `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `PDF_STORAGE_PATH`, `MAX_UPLOAD_SIZE_MB`, `UNPAYWALL_EMAIL`, `SEMANTIC_SCHOLAR_API_KEY` (optional), `LLM_API_KEY` (placeholder for Phase 9/10).

### Claude's Discretion
- Exact pytest directory layout (`backend/tests/` mirroring `backend/app/` module structure) and test-discovery config (`pyproject.toml [tool.pytest.ini_options]`) — standard convention, no ambiguity worth a question.
- `.gitignore` contents (Python/Node/Docker standard entries plus `.env`) — no owner input needed.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project spec
- `README.md` §7 (Complete Tech Stack) — exact tech choices per layer
- `README.md` §11 (Repository Structure) — the directory layout SETUP-01 requires verbatim
- `README.md` §12 (Environment & Configuration) — the `.env` variable set and central-settings-module rule
- `README.md` §15 Phase 0 entry — Build/Done-when criteria for this phase

### Operating rules
- `CLAUDE.md` — hard constraints (config centralization, secrets in `.env` only, no scope creep), TDD requirement, phase-gating rule

### Planning artifacts (this ingest)
- `.planning/PROJECT.md` — locked decisions table (all 16 ADR constraints)
- `.planning/REQUIREMENTS.md` §"Setup (Phase 0)" — SETUP-01 through SETUP-06, the acceptance bar for this phase
- `.planning/ROADMAP.md` §"Phase 0" — Success Criteria (4 must-be-TRUE statements)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
None — this is a greenfield repository. Only `README.md`, `CLAUDE.md`, and the newly-bootstrapped `.planning/` exist prior to this phase.

### Established Patterns
None yet — Phase 0 establishes the first patterns (config centralization via `app/core/config.py`, test layout) that every later phase will follow.

### Integration Points
N/A for this phase — nothing to integrate with yet.

</code_context>

<specifics>
## Specific Ideas

No specific UI/UX references given — Phase 0 has no user-facing surface beyond `/health` and an empty frontend shell. Standard, boring infrastructure choices were preferred throughout (see Decisions) since the owner asked to skip discussion and avoid scope creep.

</specifics>

<deferred>
## Deferred Ideas

- Containerizing `frontend` and `grobid` in `docker-compose.yml` — deferred to when their respective phases (12 and 2) actually need them; full multi-service compose is Phase 14's explicit job.
- mypy / static type checking in pre-commit — not required by SETUP-01..06; can be added in a later phase if the owner wants it.

### Reviewed Todos (not folded)
None — no pending todos existed for Phase 0 (`todo.match-phase` returned 0 matches).

</deferred>

---

*Phase: 0-Project Scaffolding & Environment*
*Context gathered: 2026-08-21*

---
phase: 00-project-scaffolding-environment
plan: 02
subsystem: infra
tags: [docker, docker-compose, dockerfile, neo4j, qdrant, postgres, env-contract]

# Dependency graph
requires: ["00-01 (backend/pyproject.toml, backend/app/ tree, .gitignore excluding .env)"]
provides:
  - "docker-compose.yml — neo4j:5-community, qdrant/qdrant:latest, postgres:16-alpine, backend build service, named volumes, healthchecks, condition:service_healthy gating"
  - "backend/Dockerfile — python:3.11-slim, pip install -e '.[dev]', COPY app, EXPOSE 8000, CMD uvicorn app.main:app"
  - ".env.example — full 14-variable README §12 / D-09 contract with safe placeholders and in-network hostname defaults"
affects: ["00-03 (frontend scaffold)", "00-04 (adds app/main.py + runtime deps — makes backend/Dockerfile's CMD actually startable, wires app/core/config.py to these .env vars)"]

# Tech tracking
tech-stack:
  added: ["neo4j:5-community", "qdrant/qdrant:latest", "postgres:16-alpine", "Docker Compose (compose spec, no version key)"]
  patterns:
    - "All service credentials flow through ${VAR} interpolation from .env — never hardcoded in docker-compose.yml"
    - "Healthcheck + condition:service_healthy gating on all three data services before backend starts, avoiding startup-race flakiness (Neo4j Bolt listener is slowest to accept connections)"
    - "backend.env_file uses the compose-spec extended form (path + required:false) so `docker compose config` validates cleanly whether or not .env exists yet"

key-files:
  created:
    - docker-compose.yml
    - backend/Dockerfile
    - .env.example
  modified: []

key-decisions:
  - "Deviation from plan's literal `env_file: .env` scalar form: used the compose-spec extended list form (`env_file: [{path: .env, required: false}]`) instead. Reason: `docker compose config --quiet` (Task 1's own verify step) hard-fails when a scalar env_file target is missing, but .env does not exist until Task 3 runs — the literal plan text would make Task 1 fail its own acceptance criterion. required:false preserves identical runtime behavior (loads .env when present, same ${VAR} interpolation everywhere else) while letting Task 1 validate standalone. No hardcoded secrets introduced either way."
  - "Docker Desktop was not running at task start; started it via Start-Process and polled docker info until the daemon responded (~15s) before running Task 2's build and Task 3's up/down verification. Not a plan deviation — infrastructure precondition, not a design decision."
  - "Task 2's docker build succeeded fully (not just Dockerfile-syntax-valid) — COPY app ./app copies the existing backend/app/ tree from Plan 00-01, and pip install -e '.[dev]' installs cleanly against backend/pyproject.toml. CMD's app.main:app is only resolved at container *runtime*, not at build time, so the full build completing is expected and consistent with the plan's own note that end-to-end startup is Plan 00-04's job, not this one's."

patterns-established:
  - "Docker Compose stack is the reproducible local-dev environment for every phase from here on; new services (frontend, grobid) join this same file when their phase needs them, per D-04 — never a second compose file."
  - ".env.example is now the single source of truth for the environment variable contract; new variables added by later phases must be appended here with a same-commit comment noting which phase consumes them, never invented ad hoc."

requirements-completed: [SETUP-02, SETUP-06]

# Metrics
duration: 20min
completed: 2026-08-22
---

# Phase 0 Plan 02: Docker Compose Stack & Environment Contract Summary

**Docker Compose stack (neo4j, qdrant, postgres, backend) with healthcheck-gated startup ordering, a syntactically-valid backend/Dockerfile, and a fully documented 14-variable `.env.example` contract — all three data services verified running healthy end-to-end via a throwaway `.env`, then torn down clean with no orphaned volumes.**

## Performance

- **Duration:** ~20 min
- **Started:** 2026-08-22T00:27:00+05:30 (approx)
- **Completed:** 2026-08-22T00:47:00+05:30 (approx)
- **Tasks:** 3
- **Files modified:** 3 (one per task)

## Accomplishments

- Wrote `docker-compose.yml` with exactly the four Phase-0 services required by D-04 (neo4j, qdrant, postgres, backend) — no frontend, no grobid. All three data services carry a `healthcheck:` block (cypher-shell RETURN 1, raw TCP probe on 6333, `pg_isready`); `backend.depends_on` uses `condition: service_healthy` for all three rather than the bare-list form, avoiding startup-race flakiness against Neo4j's slower Bolt listener.
- Confirmed `docker compose config --quiet` resolves the full four-service config with zero hardcoded secrets — every credential is a `${VAR}` interpolation.
- Wrote `backend/Dockerfile` (`python:3.11-slim`, installs `backend/pyproject.toml` dev extras, copies `app/`, exposes 8000, `CMD uvicorn app.main:app`). Verified with a full `docker build` against the real `backend/` context — build completed successfully (Plan 00-01's `backend/app/` tree exists; `app.main` import is only resolved at container runtime, which is Plan 00-04's concern). Test image removed after verification.
- Wrote `.env.example` documenting all 14 variables from README §12 / CONTEXT.md D-09 (Neo4j, Qdrant, Postgres, PDF upload, ingestion sources, LLM), with safe `changeme_*` placeholders and in-Docker-network hostname defaults, plus per-section comments noting which phase consumes ingestion/LLM vars.
- End-to-end verification: copied `.env.example` to a throwaway `.env`, ran `docker compose up -d neo4j qdrant postgres` — all three containers reached `healthy` status within ~15s — then `docker compose down -v`, confirming clean teardown with all three named volumes removed (`docker volume ls` shows none remaining). Throwaway `.env` deleted; never committed (gitignored per Plan 00-01).

## Task Commits

Each task was committed atomically:

1. **Task 1: Write docker-compose.yml (neo4j, qdrant, postgres, backend)** - `8646468` (feat)
2. **Task 2: Write backend/Dockerfile** - `039e20e` (feat)
3. **Task 3: Write .env.example and bring up the three data services** - `5f15bf6` (feat)

_Note: no TDD red/green cycle applies here — this plan is pure infrastructure config (compose file, Dockerfile, env contract), no testable application logic exists yet, consistent with Plan 00-01's precedent._

## Files Created/Modified
- `docker-compose.yml` - neo4j:5-community, qdrant/qdrant:latest, postgres:16-alpine, backend build service; named volumes; healthchecks; `condition:service_healthy` gating; `env_file` on backend uses extended `required:false` form
- `backend/Dockerfile` - `python:3.11-slim`, `pip install -e ".[dev]"`, `COPY app ./app`, `EXPOSE 8000`, `CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]`
- `.env.example` - all 14 D-09 variables, safe placeholders, in-network hostname defaults, per-section phase-consumption comments

## Decisions Made
- Followed the plan exactly for D-04/D-05 service definitions, images, ports, and volumes.
- One implementation-level deviation from the plan's literal text (documented above under `key-decisions`): `env_file: .env` written as the compose-spec extended `path`/`required: false` form instead of the bare scalar, so Task 1's own `docker compose config --quiet` verify step passes before `.env` exists (created only in Task 3). This is a syntax choice with identical runtime semantics — no secrets exposed, no behavior change once `.env` exists — not a scope or security deviation.
- Docker Desktop required an explicit start (`Start-Process`) since it was not running at task start; this was an environment precondition, not a plan or design decision.

## Deviations from Plan

- `docker-compose.yml`'s `backend.env_file` uses the extended list/mapping form (`- path: .env` / `required: false`) instead of the plan's literal scalar `env_file: .env`. See `key-decisions` above for the full rationale — required to satisfy Task 1's own acceptance criterion (`docker compose config` parses cleanly) given .env doesn't exist until Task 3.
- No other deviations — all three tasks executed per their `<action>` blocks and passed their stated `<acceptance_criteria>` and `<verify>` steps.

## Issues Encountered
- Docker Desktop's daemon was not running when Task 2 began (`docker build` initially failed with a named-pipe connection error). Resolved by launching `Docker Desktop.exe` and polling `docker info` until the daemon responded (~15 seconds) — no file or plan changes required.
- The plan's illustrative verification command `grep -c "^[A-Z_]*=" .env.example` undercounts (returns 11, not 14) because `[A-Z_]*` excludes digits and three variable names contain a digit (`NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`). Verified independently with an exact-name check (`grep -q "^${v}="` for each of the 14 D-09 names) and a corrected count regex (`[A-Z0-9_]+`), both confirming all 14 variables are present. Flagging this here since the plan's own `<verification>` section step 4 will show 11 if re-run literally — not a defect in `.env.example`.

## User Setup Required

None — no external service configuration required. Docker Desktop must be running locally for `docker compose` commands to work in future sessions (was auto-started this session; owner may want it set to launch on login for a smoother Plan 00-04 workflow, but this is a convenience note, not a blocker).

## Next Phase Readiness
- `docker-compose.yml` is ready for Plan 00-04 to bring up the full stack (`backend` included) once `app/main.py` and runtime dependencies exist.
- `.env.example` gives Plan 00-04's `app/core/config.py` (pydantic-settings) a stable, complete variable contract to read from — no variable names need to be invented.
- `backend/Dockerfile` is proven structurally sound; its first real end-to-end container run (with a working `/health` endpoint) happens in Plan 00-04.
- No blockers for Plan 00-03 (frontend scaffold, independent of this plan) or Plan 00-04.

---
*Phase: 00-project-scaffolding-environment*
*Completed: 2026-08-22*

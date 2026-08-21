---
phase: 00-project-scaffolding-environment
plan: 01
subsystem: infra
tags: [python, pyproject-toml, hatchling, pytest, ruff, pre-commit, packaging]

# Dependency graph
requires: []
provides:
  - "backend/app/** subpackage tree (13 subpackages + models) matching README §11 exactly, empty __init__.py markers only"
  - "backend/pyproject.toml — PEP 621 metadata, hatchling build backend, dev-only extras group (pytest, ruff, pre-commit)"
  - "backend/tests/ scaffold (__init__.py, conftest.py placeholder) discoverable via [tool.pytest.ini_options] testpaths"
  - ".pre-commit-config.yaml — ruff (lint+format) + pre-commit-hooks (trailing-whitespace, end-of-file-fixer, check-yaml, check-added-large-files), no mypy"
  - ".gitignore — excludes .env before any .env file is ever created, plus standard Python/Node/editor entries"
  - "evaluation/.gitkeep placeholder"
affects: ["00-02", "00-03", "00-04 (adds main.py, runtime deps, app/core/config.py)"]

# Tech tracking
tech-stack:
  added: ["hatchling (build backend)", "pytest>=8.3,<9", "ruff>=0.6,<1", "pre-commit>=3.8,<4"]
  patterns: ["PEP 621 pyproject.toml packaging with dev extras group installed via pip install -e \".[dev]\"", "pre-commit hook chain: ruff --fix + ruff-format, then trailing-whitespace/end-of-file-fixer/check-yaml/check-added-large-files"]

key-files:
  created:
    - backend/pyproject.toml
    - backend/app/{api,core,ingestion,ingestion/sources,parsing,extraction,ner_re,embeddings,clustering,trends,ranking,explanation,interactive,models}/__init__.py
    - backend/tests/__init__.py
    - backend/tests/conftest.py
    - .pre-commit-config.yaml
    - .gitignore
    - evaluation/.gitkeep
  modified: []

key-decisions:
  - "backend/app/main.py deliberately NOT created in this plan — reserved for Plan 00-04 to avoid a cross-plan file conflict"
  - "pyproject.toml dependencies = [] at [project] level; runtime deps (FastAPI, DB drivers) deferred to Plan 00-04 — this plan is dev-tooling-only scaffolding"
  - "Package legitimacy checkpoint (pytest, ruff, pre-commit) verified inline as exact-name matches to well-known PyPI projects with no typosquat risk — no interactive pause needed per owner's Phase 0 pre-authorization"

patterns-established:
  - "Config centralization convention seeded: backend/app/core/ is the designated home for the future settings module (Plan 00-04), per CLAUDE.md's config-centralization rule"
  - "Test layout mirrors README §11: backend/tests/ discovered via [tool.pytest.ini_options] testpaths = [\"tests\"] in backend/pyproject.toml"

requirements-completed: [SETUP-01]

# Metrics
duration: 25min
completed: 2026-08-22
---

# Phase 0 Plan 01: Backend Scaffolding & Dev Tooling Summary

**Backend package tree (13 README §11 subpackages + models) scaffolded with empty `__init__.py` markers, dev-only `pyproject.toml` (hatchling + pytest/ruff/pre-commit extras), and a clean, verified pre-commit + .gitignore baseline — zero runtime dependencies, zero ML/NLP logic, no `main.py` yet.**

## Performance

- **Duration:** ~25 min
- **Started:** 2026-08-22T00:20:00+05:30 (approx)
- **Completed:** 2026-08-22T00:25:41+05:30
- **Tasks:** 2 (plus 1 checkpoint)
- **Files modified:** 21 (19 in Task 1, 2 in Task 2)

## Accomplishments
- Created the full `backend/app/` subpackage tree exactly as specified in README §11 (api, core, ingestion, ingestion/sources, parsing, extraction, ner_re, embeddings, clustering, trends, ranking, explanation, interactive, models), each with an empty, docstring-only `__init__.py` — no logic, no imports.
- Wrote `backend/pyproject.toml` as valid PEP 621 metadata (hatchling build backend, `dev` extras group with pytest/ruff/pre-commit, `[tool.pytest.ini_options]` testpaths, `[tool.hatch.build.targets.wheel]` explicit package target).
- Wrote `.pre-commit-config.yaml` (ruff lint+format, pre-commit-hooks quartet, no mypy) and `.gitignore` (Python/Node/editor entries, `.env` explicitly excluded, `.env.example` untouched).
- Installed dev extras (`pip install -e ".[dev]"`), installed the pre-commit git hook, and ran `pre-commit run --all-files` clean (all 6 hooks pass, including `check-yaml` re-verified after staging the yaml file itself).
- Confirmed `git check-ignore -v .env` correctly matches the `.gitignore:.env` line before any real `.env` file is ever created in this repo.

## Task Commits

Each task was committed atomically:

1. **Task 1: Scaffold backend package tree and pyproject.toml (dev extras only)** - `d0b290e` (feat)
2. **Task 2: Pre-commit config, .gitignore, and install verification** - `b911d0f` (feat)

_Note: no TDD red/green cycle applies here — this plan is pure scaffolding (directory tree + config files), no testable logic exists yet per the plan's own scope ("nothing runnable yet, no FastAPI code")._

## Files Created/Modified
- `backend/pyproject.toml` - PEP 621 packaging config; dev-only extras (pytest, ruff, pre-commit); pytest testpaths; hatchling wheel package target
- `backend/app/{api,core,ingestion,ingestion/sources,parsing,extraction,ner_re,embeddings,clustering,trends,ranking,explanation,interactive,models}/__init__.py` - empty package markers, one per README §11 subpackage
- `backend/tests/__init__.py`, `backend/tests/conftest.py` - test package scaffold; conftest fixtures deferred to Plan 00-04
- `.pre-commit-config.yaml` - ruff (--fix + format) + pre-commit-hooks (trailing-whitespace, end-of-file-fixer, check-yaml, check-added-large-files), pinned to v0.6.9 / v4.6.0
- `.gitignore` - Python/Node/editor ignores; `.env` excluded before any `.env` file exists; `.env.example` deliberately not excluded
- `evaluation/.gitkeep` - empty placeholder, no evaluation code until Phase 13

## Decisions Made
- Followed plan exactly for D-01/D-02/D-03 (plain pyproject.toml + hatchling, `dev` extras group, ruff+pre-commit-hooks with no mypy).
- Checkpoint (package legitimacy for pytest/ruff/pre-commit) resolved without an interactive pause: all three are exact-name matches to well-known PyPI projects (pytest-dev, Astral, the pre-commit org) with no typosquat risk, per the owner's Phase-0 pre-authorization to skip routine, well-known package confirmations.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
- `pip install -e ".[dev]"` placed console scripts (`pytest.exe`, `ruff.exe`, `pre-commit.exe`) in a user Scripts directory not on `PATH`. Not a plan deviation — invoked via `python -m pytest` / `python -m pre_commit` for verification; no file changes required. Future sessions should be aware `pytest`/`ruff`/`pre-commit` may not be bare-callable on this machine without a PATH update or a virtualenv.

## User Setup Required

None - no external service configuration required. (Note for the owner: the `PATH` issue above is cosmetic/local-environment only; it does not block any Phase 0 acceptance criterion, since all verifications used `python -m <tool>` successfully.)

## Next Phase Readiness
- `backend/app/core/` is ready to receive the centralized `config.py` settings module in Plan 00-04.
- `backend/app/` has no `main.py` yet — Plan 00-04 owns it, as designed, with zero conflict risk.
- Dev tooling (pytest, ruff, pre-commit) is installed and verified working; runtime dependencies (FastAPI, DB drivers, pydantic-settings) are intentionally still absent and belong to Plan 00-04.
- No blockers for Plan 00-02 or 00-03.

---
*Phase: 00-project-scaffolding-environment*
*Completed: 2026-08-22*

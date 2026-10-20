# PROJECT_STATUS.md

Single maintained status document for ScholarLens (research-gap identification).
It replaces the per-phase closeout, handoff and addendum files. `README.md` is
still the spec and wins any disagreement; `CLAUDE.md` holds the operating rules;
`PROGRESS.md` is the append-only decision log (D-xxx ids) and holds the detail
this file only summarises.

- Written: 2026-10-19, from the code, `.env` (key names and non-secret values
  only), the ten closeout/handoff files, `PROGRESS.md`, and fresh test runs.
- Git: `main` @ `bdfe21e`, **166 uncommitted worktree entries, nothing
  committed, pushed or stashed** since that commit. All work since Phase 10 is
  uncommitted. Most closeout/handoff files are untracked (see §12).
- How each claim is sourced: "(run)" = re-run on 2026-10-19 for this file;
  "(doc)" = taken from a closeout and **not re-verified today**; "(code)" = read
  from the repository.
- **Docker was not running when this was written**, so no live-store figure
  (row counts, 632 vectors, reconcile) could be re-checked today. Those are all
  "(doc)".

## 1. Current phase and status

| Item | Status |
|---|---|
| Phases 0-13 | Implemented. Phases 10, 11, 12, 13 and the Mode B work are each recorded as **complete with limitations**, not unconditionally complete |
| Phase 14 (full deployment) | **Not started.** No `frontend/Dockerfile` exists (code); `docker-compose.yml` has neo4j, qdrant, postgres, grobid and backend but no frontend service |
| Mode B (Ask ScholarLens) | Complete with limitations (§7). One requirement is not met in the current deployment: pacing/deadline conflict (§7, U1) |
| Owner authorization | Phase 14 needs an explicit "start Phase 14". Finishing earlier work is not permission |
| Last work | Mode B focused mission, 2026-10-19 (PROGRESS.md, D-B-13..16) |

`.planning/STATE.md` (GSD tooling) is **stale** (last updated 2026-09-08, says
Phase 11 partial). Do not use it as status.

## 2. Architecture and where things live

Pipeline stages S1-S9 follow README §8: collect -> parse -> extract -> NER/RE ->
embed/index -> cluster + NLI -> trends -> gap ranking -> Mode A reports. Mode B
(`/query`) is a LangGraph workflow over the same stores. Stores: PostgreSQL
(corpus, facts, clusters, rankings, reports, `derived_state`, pipeline runs),
Neo4j (papers/entities graph), Qdrant (one collection, 768-d SPECTER2 vectors),
PDF volume `pdf_data`, GROBID service. LLM: Groq only, direct API, no framework.

| Area | Location |
|---|---|
| Config (all tunables) | `backend/app/core/config.py`; documented in `.env.example` |
| API routers, auth, limits | `backend/app/api/{routes_backend,routes_ingestion,security,body_limit,write_guard}.py`, `backend/app/main.py` |
| Logging redaction | `backend/app/core/logging.py` |
| Ingestion (S1) | `backend/app/ingestion/` (`sources/`, `dedup.py`, `corpus.py`, `relevance.py`, `unpaywall.py`, `pdf_store.py`) |
| S2-S9 | `parsing/`, `extraction/`, `ner_re/`, `embeddings/` (`indexer.py` has `retrieve`), `clustering/`, `trends/`, `ranking/`, `explanation/` under `backend/app/` |
| Mode B | `backend/app/interactive/`: `workflow.py` (LangGraph graph, `assess_sufficiency`), `claim_check.py` (vetoes), `intent.py` (question scope), `deadline.py`, `calibration.py`, `live_check.py` |
| Mode A export / verification | `backend/app/explanation/{export,verification}.py` |
| Integrity tooling | `backend/app/core/reconcile.py` (read-only), `corpus_reset.py` (destructive), `model_revision.py` |
| Evaluation harness | `evaluation/` (`mode_b.py`, `threshold_policy.py`, `scholarlens.py`, `time_shift.py`, `survey.py`, `expert.py`); docs in `evaluation/README.md` |
| Evaluation data | `evaluation/datasets/mode_b_v1` (130 questions, 60 claims), `mode_b_claims_s2` (34 claims); runs in `evaluation/output/mode_b_eval/run-2026101{8,9}-v{1,2,3}/`; live results in `evaluation/output/live_query_result*.json` |
| Frontend | `frontend/src/{App.tsx,api.ts,types.ts}`, `components/{KnowledgeGraph,TrendChart}.tsx`; Vite proxy `/api` -> `127.0.0.1:8000`, no CORS |
| Test fixtures built from real passages | `backend/tests/fixtures/mode_b_{cross_paper,study_results,p035_pool,live_run_2}.json` |

Compose service/container names: backend container is `nlp-backend-1`.

### Mode B graph (README §9, unchanged)

retrieve (Qdrant + 1-hop Neo4j) -> assess sufficiency -> [reformulate, max 3
rounds] -> rerank (cross-encoder) -> generate (JSON claims) -> claim vetoes +
LLM verifier -> [regenerate once] -> return. The answer shown is the checked
claims only; free prose never reaches the user. Refusal outcomes carry no claims
or citations. `outcome` is one of `grounded_answer`, `no_indexed_evidence`,
`insufficient_evidence`, `grounding_failed`.

Changes since the README graph, all additive: question scope sets the paper
minimum (§3); failing claims are withheld and a draft survives if at most half
fail (`removed_claim_count`); the verifier also returns `answers_question`
(false -> refuse without regeneration, reason `not_answerable`); the
cross-encoder chooses from a 3x candidate pool.

### API surface

23 operations, 22 behind `X-API-Key` (constant-time compare, fail-closed,
placeholder `change_me*` keys rejected); only `GET /health` and FastAPI's
generated docs are public. Body limit (`MAX_REQUEST_BODY_KB`=256, uploads
<=20 files x `MAX_UPLOAD_SIZE_MB`=50), failed-auth throttle (20 per 60 s per
peer address -> 429), `/query` deadline 180 s -> 504, access-log query strings
redacted, exceptions logged as type only. `/clusters`, `/reports`, `/graph`,
`/papers`, `/trends`, `/gaps`, `/query` are typed; ingestion/pipeline/admin
responses are not.

## 3. Retrieval threshold, score semantics, calibration

**Production values (code defaults in `config.py`; `.env` overrides listed):**

| Setting | Value | Source |
|---|---|---|
| `RETRIEVAL_MIN_SCORE` | **0.2** | code default and `.env` |
| `RETRIEVAL_MIN_CHUNKS` | 2 | code default and `.env` |
| `RETRIEVAL_MIN_PAPERS` | 2 (synthesis, comparison, unclassifiable questions) | code default and `.env` |
| `RETRIEVAL_MIN_PAPERS_PAPER_SPECIFIC` | 1 (question points at one paper/study) | code default; not in `.env` |
| `RETRIEVAL_TOP_N` | 12 | code default and `.env` |
| `RETRIEVAL_ROUND_CAP` | 3 | code default and `.env` |
| `RETRIEVAL_POOL_FACTOR` | 3 (cross-encoder picks 12 from 36; `1` disables) | code default; not in `.env` |
| `GROUNDING_REGENERATION_CAP` | 1 | code default |
| `GROQ_MODEL` | `openai/gpt-oss-20b` | code default |
| `GROQ_MIN_INTERVAL_SECONDS` | **60 in `.env`**, 0.0 code default | `.env` |
| `QUERY_DEADLINE_SECONDS` | 180 | code default; not in `.env` |
| `LLM_TIMEOUT_SECONDS` | 120 | code default |
| Models | `allenai/specter2_base` + adapter `allenai/specter2`; `cross-encoder/ms-marco-MiniLM-L-6-v2`; `microsoft/deberta-base-mnli` | code / `.env` |

**Score semantics** (code: `workflow.assess_sufficiency`; doc: Part 2 §1):

- The gate compares the raw **Qdrant cosine** of each retrieved passage with
  `RETRIEVAL_MIN_SCORE` using `>=`. Values in practice are 0.72-1.0 on this
  corpus, so 0.2 rejects nothing.
- Graph-expansion rows carry a fixed 0.5, are never counted toward sufficiency,
  and a value outside the cosine range never counts.
- The cross-encoder score (`rerank_score`, an unbounded logit, about -11 to +8
  here) only orders evidence after the gate. It is never compared with the
  threshold. A test pins that a logit in the `score` field is not read as a
  cosine (`tests/interactive/test_retrieval_threshold_policy.py`).

**The threshold is deliberately not changed and no new gate was added.** The
gate is inert at 0.2: every question, including unrelated ones, passes it.
Abstention in practice rests on the claim vetoes and the LLM verifier.

**Calibration evidence** (all labels are **agent-authored, not independent**;
130 questions, 11 papers; nothing here is statistical validation). Held-out
half = 66 questions (27 relevant, 14 partial, 13 weak, 12 unrelated), chosen by a
hash of `question_id`; the other 64 fitted the thresholds:

| Policy | relevant | partial | weak | unrelated | answerable accepted | unanswerable accepted |
|---|---|---|---|---|---|---|
| cosine 0.2 (production) | 26/27 | 14/14 | 13/13 | 12/12 | 40/41 | 25/25 |
| cosine 0.9153 (best on calibration half) | 26/27 | 13/14 | 7/13 | 0/12 | 39/41 | 7/25 |
| cross-encoder floor -5.74 on top-1 rerank score | 27/27 | 14/14 | 11/13 | 0/12 | 41/41 | 11/25 |

- The one relevant question refused at 0.2 (p032) is refused by the two-paper
  rule, not the threshold.
- Why nothing is locked: cosine 0.9153 has a ~0.005 margin on a space that spans
  0.72-1.0 and loses a partial question; the -5.74 floor is the best candidate
  (no answerable lost, no unrelated accepted, in both cross-fit directions) but
  the two halves give floors 0.58 apart (more than its 0.38 margin), 11/13
  held-out weak questions still pass it, it needs a new gate after the rerank
  (a graph change the owner has not decided), and the labels are not
  independent. `evaluation/threshold_policy.py::lock_verdict` encodes this and
  always returns `defer` on agent labels.
- Single-paper rule: only 3/130 runs are single-paper; before the scope rule
  p031/p032 were refused at any threshold.
- To change the threshold legitimately: collect independent labels per
  `evaluation/output/mode_b_calibration/independent/PROTOCOL.md` (two labellers,
  at least 25 questions per class, then `--evaluate-labels`). Candidates are the
  cosine 0.915 and the rerank floor -5.74. Not before.
- Artifacts: `run-20261018-v1/REPORT.md` (cosine), `run-20261018-v2/REPORT.md`
  (cross-encoder), `run-20261019-v3/threshold-policy.json`.

## 4. Completed fixes (summary; ids and detail in PROGRESS.md)

- **Phase 10 (Mode B):** LangGraph graph; citation validity; claim-to-passage
  vetoes (D-10-14); answer = checked claims only; safe refusal on malformed
  verifier output; 23/23 mutations killed. Live run 3 grounded, 9/9 checks.
- **Phase 11 (API):** 11 defects fixed (D1-D11): pre-auth body buffering, derived
  markers wiped by malformed writes, placeholder key accepted, duplicate-header
  auth, unbounded `topic`/uploads, plain-text 500s, `/query` rate-limit
  mapping, untyped responses, frontend error parsing, `isdigit`/`root_path`.
  284-case route-inventory test. 20/20 runtime probe checks.
- **Phase 11 addendum:** access-log redaction (uvicorn), `/query` deadline,
  failed-auth throttle, typed `/clusters` `/reports` `/graph` (byte-identical
  output).
- **Phase 12 (frontend):** recovery after a rejected key, claim-to-citation
  rendering, 401/429/504 messages, in-flight guard, contradiction pairs,
  graph clipping/selection/keyboard access. 96/96 real-browser checks (one-off
  scratchpad harness, not committed).
- **Phase 13 (evaluation):** time-shift precision bug D-13-04 (unlabelled gaps
  counted as false positives); Mode B dataset, calibration, held-out
  evaluation, live-check, manifests with `--reproduce`; D-B-01..08 (claim-check
  false rejections 11/30 -> 1/30, catches 20/30 -> 22/30 on the development
  set; evidence offered = gated evidence; per-query wait budget so no Groq call
  starts after the 504; veto codes in the trace; duplicate claims).
- **Mode B quality mission (10-18):** scope-dependent paper minimum;
  partial answers; verifier sees the composed claims and the question;
  "Paper N" attribution; sentence-split and polarity fixes; `answer_relevance`
  refusal; `unsupported_absence` / `future_as_done`; citations = what shown
  claims cite; service errors labelled as not a finding about the literature.
- **Mode B focused mission (10-19):** `partial_total` veto (3,630 vs 7,892
  class); `answers_question`; `RETRIEVAL_POOL_FACTOR`; three software bugs
  (Trends test regex used backspace bytes and exposed "1 papers" wording;
  `/reports` 500 on archived snapshots missing fields; Library view aborted
  `/pipeline/run` when the dashboard errored).
- **Earlier S1-S9 hardening** (`HARDENING_PLAN.md`, authorised 2026-09-16):
  run/stage state, single-writer lock, atomic per-paper graph replacement,
  fingerprints/checkpoints, read-only reconcile. See PROGRESS.md.

## 5. Unresolved bugs and quality gaps (left unresolved on purpose)

| Id | Item | State |
|---|---|---|
| U1 | **Pacing vs deadline.** `GROQ_MIN_INTERVAL_SECONDS=60` and `QUERY_DEADLINE_SECONDS=180`: any path needing 4 or more Groq calls (regeneration, or reformulation) cannot finish and ends in 504 (since D-B-04, immediately, with no late call). Mode B makes at most 6 calls | **Owner decision.** Lower the interval (if the Groq tier allows) or set the deadline to at least 5 x interval + ~40 s, then document the pair. Touches Mode A rate-limit protection |
| U2 | Topical but unanswerable questions (e.g. p099, sandboxing defences for browsing agents) still get a grounded answer. `answers_question` has **not been shown to trigger live** (LV-R2 refused by `low_overlap`; LV-R3/R4 grounded). The 20B verifier passed claims a reader would reject | **Partly fixed.** Needs independent labels and a stronger verifier or a retrieval-side check |
| U3 | Paper 10 not retrievable for the cross-paper question p035: its labelled passages rank 270 and 397 of 632 by cosine; reachable ones are method text. Strict xfail `test_paper_10_evidence_reaches_the_model_for_the_p035_question` marks it | **Unresolved.** Lever is a lexical/hybrid retriever or query rewrite; not in any brief |
| U4 | Subset/granularity errors beyond `partial_total` (denominators, uncertainty) are not checked deterministically | Open |
| U5 | No independent labels or human review anywhere: questions, 94 claims, aspects, all live-claim reviews are agent-authored. The claim-check numbers are from a development set | Needs owner/people |
| U6 | Synthesis answers reflect what is retrieved, not the corpus (passage recall@12 was 0.29, a lower bound). Graph rows can displace passages in the reranked 12 | Open |
| U7 | Not exercised live: reformulation, regeneration success, the `off_question` path, `answers_question=false` | Blocked by U1 and cost |
| U8 | Live synthesis refusals cannot be diagnosed beyond codes; rejected drafts are never recorded (by design) | Owner design decision |
| U9 | Ask view has the same unmount-on-error pattern for an in-flight `/query` that Library had (not fixed; not in brief) | Open, low |
| U10 | `API_KEY` is 11 characters. Rotate to `python -c "import secrets; print(secrets.token_urlsafe(32))"` before any shared deployment; reconnect the dashboard afterwards | Owner |
| U11 | Local `POSTGRES_PASSWORD` equals the placeholder in `.env.example`. Change before any non-local use | Owner |
| U12 | `.planning/STATE.md` is stale (§1) | Open, trivial |

## 6. Known limitations (accepted, documented)

- **Mode A / evaluation:** the time-shift, survey and expert methods ran only
  on the hand-written example corpus plus the export of the frozen reports.
  Later papers, survey open problems and expert ratings are **not available**;
  templates exist (`evaluation/output/scholarlens/run-20261018/expert-scores-template.csv`);
  nothing was fabricated.
- **Auth/ops:** throttle is process-local and keyed by TCP peer (all local
  clients share one budget, so failing on purpose locks the owner out for up to
  60 s); single shared key, no roles, kept in `sessionStorage`; HTTP only;
  generated `/docs` public; long synchronous handlers (`/pipeline/run`,
  `/collect`, `/reports`) hold a worker, by design (no task queue); a call
  already in flight at the deadline is not interrupted; the write path
  `/collect` -> `/pipeline/run` was never exercised end to end against the
  frozen corpus.
- **Frontend:** the Ask view has only been driven against mocked `/query` plus
  one recorded live response in jsdom; a live grounded answer through a real
  browser has never been observed. Dense 180-node graph is a desktop task. Real
  data has no contradiction pairs, so pair rendering is tested with mocks. The
  1.1 MB Plotly chunk warning predates all of this.
- **Process:** `ruff format --check` repo-wide had pre-existing drift (Phase 11
  count: 30 files); two older test files remain unformatted. No backend type
  checker is installed or specified.
- **Mutation checks and the 1 strict xfail** are in scratch copies / tests; they
  are not CI.

## 7. Next actions (nothing below is started)

1. Owner: decide U1 (interval vs deadline) and record the pair in `.env` and
   the deployment docs.
2. Owner: decide whether to commission independent labels (PROTOCOL.md) before
   any threshold or `min_papers` change.
3. Owner: commit strategy for the 166 uncommitted entries (the project has
   never committed Phases 10-13 or the Mode B work). Commit before deleting
   any untracked closeout file (§12).
4. On "start Phase 14": plan config (secrets, volumes, service wiring), add the
   frontend container, move the committed browser e2e check there, apply the
   same query-string redaction to any proxy access log, forward a trusted client
   address to the app (`--proxy-headers`) or rate-limit at the proxy, consider
   disabling public `/docs`, and set `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1`
   on the backend (see §10). Close with code and security review.
5. Optional engineering (needs owner go-ahead): hybrid/lexical retrieval or query
   rewrite for U3; a gate after the rerank if the floor is ever adopted.

## 8. Tests and verification

Run on 2026-10-19 (Windows, venv at `backend/.venv`), working tree unchanged
afterwards (`git status --porcelain` identical before and after):

| Command | Result |
|---|---|
| `cd backend && .venv/Scripts/python.exe -m pytest -q` | **2550 passed, 10 deselected, 1 xfailed** (run) |
| `cd backend && .venv/Scripts/python.exe -m ruff check app tests` | clean (run) |
| `PYTHONPATH=. backend/.venv/Scripts/python.exe -m pytest -q evaluation/tests` | **65 passed** (run) |
| `cd frontend && npx vitest run` | **74 passed**, 4 files (run) |
| `cd frontend && npx tsc -b && npx oxlint src && npm run build` | clean / clean / built (doc; **not re-run today**) |
| `PYTHONPATH=. backend/.venv/Scripts/python.exe -m evaluation.mode_b --reproduce evaluation/output/mode_b_eval/run-20261019-v3/manifest.json` | reproduced, `code_changed: []` (doc; not re-run) |
| `docker exec nlp-backend-1 python -m app.core.reconcile --papers` | consistent, 632/632 (doc; Docker down today) |

- The 10 deselected tests are `integration` (real models/services); they have
  not been run at any recorded baseline.
- The xfail is intentional (U3) and is strict: it fails loudly when paper 10
  becomes reachable.
- Older manifests (`run-20261018-v1`, `-v2`) now report `code_changed`; that is
  expected because the claim checker changed.
- After backend code changes, deploy with `docker compose up -d --build backend`
  (a plain restart uses the old image). **Capture `groq usage` log lines first**:
  replacing the container discards its log.

## 9. Frozen research data: preservation rules (strict)

The S1-S9 corpus is frozen. A snapshot taken before and after every recent
mission was identical (doc): 20 papers, 11 PDFs and parsed documents, 714
extracted facts, 611 entities (707 MENTIONS), 632 vectors x 768, 19 pipeline
runs, 4 clusters/trends/rankings/reports, 335 NLI scores, `derived_state` 3
rows; reconcile consistent, 632/632.

**Never, without explicit owner instruction for that action:**

- call `POST /collect`, `/upload`, `/pipeline/run`, `/parse`, `/extract`,
  `/graph`, `/index`, `/clusters`, `/trends`, `/gaps`, or `/reports`
  (`/reports` calls Groq and regenerates reports), or `DELETE /admin/corpus`;
- run `corpus_reset`, any migration, re-embedding, NER, clustering or report
  regeneration;
- run `docker compose down -v` or delete the volumes `pdf_data`,
  `corpus_cache`, `model_cache` or any database/index volume;
- delete or rewrite `evaluation/output/**` (live results, reviews, manifests,
  run directories), `evaluation/datasets/**` (frozen with SHA-256 before
  scoring), or `backend/tests/fixtures/mode_b_*.json` (exact passages checked
  against Qdrant);
- change any `embedding_*` setting, `EMBEDDING_MODEL_REVISION` or
  `EMBEDDING_ADAPTER=...@rev`. `embed_version` fingerprints the `embedding_*`
  settings, so this marks all 632 stored vectors stale and triggers a
  re-embed. Do not pin revisions casually;
- change `RETRIEVAL_MIN_SCORE`, model names or model revisions without
  independent evidence (§3);
- fabricate labels, expert ratings, later papers or survey data.

**Allowed read-only checks:** `reconcile --papers`, the GET endpoints, the
snapshot approach (row counts and content MD5 per table, Neo4j label and
relationship counts, Qdrant point count and payload/vector digest, PDF count),
`evaluation.mode_b --reproduce`, and offline scoring with
`HF_HUB_OFFLINE=1` and Qdrant read-only.

**Live-call discipline (Groq, costs real calls):** one preflight (services
healthy, container code equals working tree, key configured, cached model
revisions equal upstream, dry run sends nothing, output path new); one request
per file; every repeat needs a stated reason and an outcome rule fixed before
it runs; respect 60 s pacing against the 180 s deadline; capture usage logs
before any rebuild; secret-scan outputs by value (0 hits so far). Use
`backend/app/interactive/live_check.py` (dry run by default; `--confirm-live`
sends one request).

**Secrets:** `.env` is gitignored and holds real values. Never print, log or
copy them. A past incident put the API key into 22 access-log lines (a
`?api_key=` probe); the container was recreated and logs are now redacted. Do
not send credentials in URLs.

## 10. Decisions and configuration needed to continue

**Hard constraints (CLAUDE.md, restated):** no LangChain; LangGraph only in
Mode B; no loop around Stage 8->9; sources fixed to arXiv, Semantic Scholar,
ACL Anthology, Unpaywall (enrichment only), manual PDF; no IEEE/Springer/ACM;
the LLM comes last; scope is technical literature; no scope creep. One phase at
a time, only on explicit owner instruction. TDD, a code review per phase,
`PROGRESS.md` updated.

**Decisions that stand (full list in PROGRESS.md):**

- D-B-13: threshold stays 0.2, not locked, by decision (§3).
- Paper minimum by question scope: 1 for one paper/study, 2 for synthesis,
  comparison and unclassifiable (p032 stays refused, deliberately).
- Partial answers: if the failing claims are at most half the draft, show the
  verified rest and the removed count; the README §9 regeneration still follows
  a rejected draft.
- Regeneration cap 1, round cap 3 and the 180 s deadline are unchanged.
- Rejected drafts are never recorded (design; revisit only with the owner).
- D-B-14 `answers_question`, D-B-15 `partial_total`, D-B-16 pool factor 3
  (deeper pools raised passage recall but cut held-out required-paper recall:
  24, 22, 20 of 32 at 36, 48, 100).
- Local development is HTTP, same-origin via the Vite proxy; the dashboard
  key lives in `sessionStorage` key `scholarlens-api-key`; never add a `VITE_`
  key variable.

**Configuration to carry into Phase 14:**

- Prefer `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` for the backend: the
  process currently loads SPECTER2 and the cross-encoder without them, so a new
  upstream commit could silently change the query encoder. Live runs compared
  upstream and cache first (cached = upstream: SPECTER2 base `3447645e`,
  adapter `2081559`, cross-encoder `233902d2`, doc).
- Env key names present in `.env`: Neo4j, `QDRANT_URL`, `POSTGRES_*`,
  `PDF_STORAGE_PATH`, `MAX_UPLOAD_SIZE_MB`, `UNPAYWALL_EMAIL`,
  `SEMANTIC_SCHOLAR_*`, `ARXIV_*`, `ACL_ANTHOLOGY_MAX_RESULTS`, `GROBID_URL`,
  `EMBEDDING_*`, `QDRANT_COLLECTION`, `CLUSTERING_*`, `NLI_*`,
  `CONTRADICTION_THRESHOLD`, `RETRIEVAL_*`, `RERANKER_*`,
  `GROUNDING_REGENERATION_CAP`, `TREND_*`, `LLM_API_KEY`, `GROQ_API_KEY`,
  `GROQ_MIN_INTERVAL_SECONDS`, `GROQ_MAX_COMPLETION_TOKENS`, `API_KEY`.
  Optional safe-default settings live commented out in `.env.example`
  (`QUERY_DEADLINE_SECONDS`, `AUTH_FAILURE_LIMIT`, `AUTH_FAILURE_WINDOW_SECONDS`,
  `RETRIEVAL_MIN_PAPERS_PAPER_SPECIFIC`, `RETRIEVAL_POOL_FACTOR`, ...).
  `LLM_API_KEY` and `GROQ_API_KEY` both appear in `.env`; the code uses
  `groq_api_key` and falls back to `llm_api_key` (`explanation/llm.py:412`).
- `GROQ_MIN_INTERVAL_SECONDS` default in code is 0.0; the 60 comes from `.env`.
- Docker is currently stopped on this machine; start Docker Desktop before any
  `docker exec`/reconcile check.

## 11. Reading order for a new session

1. `CLAUDE.md`, then `README.md` in full (required by CLAUDE.md).
2. This file.
3. `PROGRESS.md` tail (2026-10-19 entry) and grep it by decision id for detail.
4. `evaluation/README.md` for the Mode B evaluation commands.

## 12. Closeout / handoff files: deletion review

All ten files below are **untracked in git** (`git ls-files` lists none of
them), so deleting them is not recoverable from git. Back them up or commit
them first if there is any doubt. Nothing was deleted.

Searched the whole repo (excluding `node_modules`, `.venv`, `.git`, `dist`
and caches) for `CLOSEOUT`, `HANDOFF`, `HANDOVER`, `HARDENING_ADDENDUM`,
`HARDENING_PLAN`. No code, config, CI, pre-commit, compose, `.planning/`,
`.claude/` or evaluation tooling opens or parses any of them. The only
references are one code comment and prose in `PROGRESS.md`.

| File | References | Verdict |
|---|---|---|
| `PHASE10_CLOSEOUT.md` | PROGRESS.md (prose) | Safe to delete. Gates and limitations are in §4-6 here and PROGRESS.md |
| `PHASE11_CLOSEOUT.md` | PROGRESS.md (prose) | Safe to delete. Route inventory/auth table and the D1-D11 list are summarised in §2 and §4; the full 15-gate table and runtime probe detail live only here and in PROGRESS.md |
| `PHASE11_HANDOFF.md` | PROGRESS.md (prose) | Safe to delete (superseded; "no deadline" is out of date) |
| `PHASE11_HARDENING_ADDENDUM.md` | PROGRESS.md (prose) | Safe to delete (summarised in §2, §4, §6) |
| `PHASE12_CLOSEOUT.md` | PROGRESS.md (prose) | Safe to delete |
| `PHASE12_HANDOFF.md` | PROGRESS.md (prose) | Safe to delete. It holds the endpoint/response-key table; the same contract is in `frontend/src/types.ts`, `api.ts` and live OpenAPI (`/docs`), which are authoritative. Two of its limitations are already marked out of date |
| `PHASE13_CLOSEOUT.md` | PROGRESS.md (prose) | Safe to delete |
| `PHASE13_HANDOFF.md` | PROGRESS.md (prose) | Safe to delete (itself marked superseded) |
| `PHASE14_HANDOFF.md` | PROGRESS.md (prose) | Safe to delete. Its Phase 14 decisions and carry-forward items are in §5, §7, §10 |
| `MODE_B_QUALITY_CLOSEOUT.md` | PROGRESS.md (prose) and **a code comment** in `backend/tests/interactive/test_retrieval_threshold_policy.py:5` | Safe to delete **after** editing that docstring to point at §3 of this file. It is the only home of the per-defect audit matrices, the live-validation tables (LV-1..LV-R4) and the full threshold tables; the 130-question results are reproducible from `evaluation/output/mode_b_eval/` and PROGRESS.md D-B-01..16 |

Not in the list you named but related: `HARDENING_PLAN.md` is **tracked**,
25 lines, referenced by prose at `PROGRESS.md:111`. Keep it; it is the written
authorization for the 2026-09-16 hardening work.

Caveat on "safe": no tooling depends on these files, but PROGRESS.md will keep
pointing at the deleted names. Those are historical log lines and harmless, or
you can add a one-line note in PROGRESS.md that the files were consolidated
into `PROJECT_STATUS.md`. Detail that exists only in the deleted files
(per-gate evidence tables, live-run tables, runtime probe lists) is lost
unless PROGRESS.md also has it; this file keeps the decisions, numbers and
limitations, not every table.

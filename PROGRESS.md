# PROGRESS

Running decisions/progress log (CLAUDE.md §"Development workflow"). `.planning/`
holds the per-phase GSD artifacts; this file is the cross-session summary.

---

## 2026-09-13 — Phases 0–12 audited against README "Done when"; three blockers fixed

Scope: audit only, with fixes limited to concrete defects that stop a phase
meeting its README §15 "Done when". Nothing was redesigned. Phase 13 was not
started, and `evaluation/` was neither audited nor touched.

### Baseline, before any change

Backend **743 passed, 10 deselected** (integration marker), `ruff check`
clean. Frontend 13/13 (unchanged since the Phase 12 entry below). The
Docker daemon was **not running**, so no live `docker compose up`, `/health`,
GROBID, Neo4j, Qdrant or Groq call was possible this session. Every verdict
below rests on source, tests and wiring, not on a live run.

### Per-phase verdict

| Phase | Verdict | Basis / remaining note |
|---|---|---|
| 0 Scaffolding | COMPLETE (code) | Compose runs neo4j, qdrant, postgres, grobid, backend; `/health` round-trips three services, 200/503. Last live pass recorded 2026-09-08 (STATE.md), not re-run. |
| 1 Collection | COMPLETE after fix **F2** | All four sources + Unpaywall + upload + `/collect`, `/upload`; dedup chain in `dedup.py`. Risk R1 below. |
| 2 Parsing | COMPLETE after fix **F3** | GROBID → PyMuPDF/pdfplumber fallback; headings → seven roles (keyword map, then SciBERT exemplar similarity). Only full-text papers can have sections. |
| 3 IE | COMPLETE | Rules + SciBERT fallback; six fact types stored by `paper_id` in `extracted_facts`. |
| 4 NER+RE | COMPLETE | GLiNER2 `JointIE` (owner-approved D-04-01); `MENTIONS` + typed `trained_on`/`evaluated_using`/`fails_on`/`achieves` edges. |
| 5 Embeddings | COMPLETE | SPECTER2; payload carries paper id, year, venue, section type, Neo4j node id. Risk R2 below. |
| 6 Clustering | COMPLETE | `sklearn.cluster.HDBSCAN` + DeBERTa-MNLI flags. Decision D-06-01, see "owner items". |
| 7 Trends | COMPLETE | Per-cluster distinct-paper series + OLS slope; `/trends`. |
| 8 Ranking | COMPLETE | README formula, configurable weights, signal breakdown on `/gaps`. |
| 9 Mode A | COMPLETE (code) | Single pass, no LangGraph, invented evidence ids dropped; `/reports`. Live Groq generation still unevidenced. |
| 10 Mode B | **Code complete, Done-when UNPROVEN** | Unchanged from 2026-09-08: no live grounded answer has ever been produced. Needs Docker, a Groq key and one real pipeline run; owner-deferred. |
| 11 API | COMPLETE after fix **F1** | Every named route wired, OpenAPI, fail-closed API key. |
| 12 Frontend | COMPLETE | Closed out in the entry below. |

Hard constraints hold: LangGraph is imported only in
`interactive/workflow.py`; no IEEE/Springer/ACM access; Stage 2+ branches on
`source` only for the README-mandated manual-upload backfill, now one predicate
(`_needs_metadata_backfill`). `langchain-core` appears in `uv.lock` purely as a
transitive dependency of `langgraph`; nothing imports it.

### F1 — `/pipeline/run` threw away Phases 2–8 when the LLM stage failed

The handler committed once, after report generation. With rankings present,
`generate_top_reports` builds the Groq client **outside** its own try, so a
missing `GROQ_API_KEY` (`LLMError`) or a 429/outage (`LLMUnavailableError`)
escaped as a 500 and rolled back every parsed document, fact, cluster, trend
and ranking, while the Neo4j graph and Qdrant vectors written mid-run
survived. That contradicts README §12 ("until the key is set … parsing,
extraction, clustering, trends and ranking all still work") and the dashboard
sends `generate_reports: true` by default. Now the evidence base commits before
the LLM stage; an `LLMError` rolls back only the report stage and returns
503/502 naming the Groq cause. `POST /reports` returned a generic "Report
generation failed" for the same errors and now names them too; arbitrary
exceptions keep the generic, non-leaking 502.

### F2 — one Unpaywall timeout aborted enrichment, or the whole pipeline

`resolve_oa_pdf` caught no `httpx` error. In `/collect` a single timeout rolled
back the Unpaywall step's savepoint, losing every enrichment already made. In
Phase 2 the manual-upload backfill calls `unpaywall.enrich`, so the same
timeout escaped `parse_all_pending` and aborted `/pipeline/run`. Network errors
and non-JSON 200s now mean "no copy found this run".

### F3 — a manual upload parsed while GROBID was down stayed untitled forever

`grobid_client` returns `None` on failure, so `parse_and_store` stored a
fallback document and the header backfill no-opped. `parse_all_pending` then
skipped the paper on every later run because a document existed — directly
failing Phase 2's "no manually uploaded paper remains without metadata that
GROBID could recover". Already-parsed, untitled manual uploads now retry only
the header backfill (no re-parse).

### Risks found and deliberately not fixed

* **R1 — two dedup tiers never match across sources.** Semantic Scholar, the
  only source that overlaps both arXiv and ACL, does not map
  `externalIds.ArXiv`/`ACL`, and arXiv stores *versioned* ids
  (`get_short_id()` → `2301.00001v1`). Cross-source dedup therefore rests on
  DOI and title+year, which misses when S2 reports a venue year. Not fixed: no
  observed duplicate, and a real fix changes stored id and PDF filename formats.
  The S2 module docstring also claims `openaccess_pdf_url` is stored; it is
  dropped in `_to_paper_dict` (README: metadata-only by design).
* **R2 — a paper with no Neo4j node is never indexed** (EMBED-03, by design).
  A paper gets a node only if it has an abstract or Phase 3 facts *and* its
  GLiNER2 call succeeds, so e.g. an abstract-less manual upload with zero facts
  is invisible to clustering and Mode B.
* After a failed pipeline run the Add papers view does not auto-refresh; the
  header refresh button shows the now-committed evidence.

### Owner items

1. Phase 10's live "Done when" remains open (unchanged).
2. D-06-01 (scikit-learn HDBSCAN instead of the `hdbscan`/BERTopic packages)
   was decided inside the Phase 6 entry, but the reconciliation entry listed it
   as needing an owner decision, and no approval is recorded the way D-04-01's is.
3. Record-keeping only: PROGRESS has no entries for Phases 0–3 (their records
   are `.planning/phases/00–03`), `.planning/` has no Phase 9 or 11 directory,
   and `.planning/STATE.md` still reads "awaiting Phase 11".
4. arXiv's default `max_results` 50→10 is now committed (`057d485`) with its
   reason in the commit message only.

### Files changed

* `backend/app/api/routes_backend.py` (F1), `backend/app/ingestion/unpaywall.py`
  (F2), `backend/app/parsing/service.py` (F3).
* Tests, written first and each confirmed failing before its fix:
  `tests/api/test_routes_backend.py` (+4 parametrized cases),
  `tests/ingestion/test_unpaywall.py` (+1), `tests/parsing/test_service.py` (+1).
* `PROGRESS.md` (this entry).

### Verification

**749 passed, 10 deselected** (743 + 6 new). `ruff check app tests` clean;
`ruff format --check` clean on all six changed files. The 10 integration tests
(real models/services) were not run. No frontend file changed.

---

## 2026-09-13 — Phase 12 (Frontend Dashboard) audited, completed and closed out

### Phase 12 was already built — it was never recorded

The previous entry ends "Phase 12 (Frontend Dashboard) was **not** started".
That is wrong, and this session began by establishing it. `git log -- frontend/src`
shows the dashboard landed in `478597b` *Frontend and backend integration
implemented* (2026-09-06) and was touched again by `2c9086a`, `26067fc` and
`fdd03e3`. README §12 already documents its **Connect API**, **Add papers** and
**Ask ScholarLens** flows as though they exist — because they do.

So Phase 12 arrived with no PROGRESS entry, no audit, and **no code review**,
which README §13.3 requires for it (its Testing column is "—"; its Review
column is not). This session audited it, completed the genuine gaps, and ran
the missing review.

### Audit result — ~85% complete on arrival

Baseline before any edit: **7/7 vitest tests passed, `npm run build` succeeded,
`npm run lint` clean.**

COMPLETE and left untouched: dashboard shell and sidebar navigation; ranked
Mode A reports (`ReportsView`/`ReportCard`, `/gaps` order joined to `/reports`
by `cluster_id`); trend charts (`TrendChart.tsx`, Plotly, real `/trends`);
knowledge graph (`KnowledgeGraph.tsx`, d3-force, 180-node cap, click-to-select);
the corpus builder; loading, empty and error states; API-key auth; the three
responsive breakpoints.

Every frontend type was checked field-by-field against `routes_backend.py` and
`routes_ingestion.py` rather than assumed. All shapes match; nothing is mocked
in a production path (mocks exist only in `App.test.tsx`).

### What was actually missing, and what was done

* **Cluster exploration — MISSING.** README §15 Phase 12 names
  "cluster/knowledge-graph exploration"; only the graph half existed.
  `/clusters` was fetched but used solely for a count tile and a label lookup,
  so `members[]` — the differently-phrased statements that are the entire
  premise of README §3 — were never rendered. Added a `Clusters` view that
  **mirrors `ReportsView` exactly**, reusing `report-card`, `report-rank`,
  `report-meta`, `score-pill`, `contradiction-pill`, `text-button`,
  `evidence-list` and `blockquote`. Clusters sort by `member_count` desc.
* **Mode B was not chat-style — PARTIAL.** Each answer overwrote the previous
  one, so a researcher could not "continue asking questions" and review what
  came before. `AskView` now keeps an `Exchange[]` transcript rendered through
  a new `AnswerCard`. `/query` is stateless and stays that way — **no
  conversational context was invented**; the transcript is a frontend record
  only. The draft clears when the answer arrives, not on submit, which is what
  keeps the pre-existing in-flight/view-switch test honest.
* **Gap signals were never shown — PARTIAL.** `/gaps` returns all five Stage 8
  signals and the UI displayed only the composite score. Since a transparent,
  interpretable formula is the project's core credibility claim, added
  `SignalBreakdown` inside the expanded report card. Values are the backend's
  own, unnormalised and unweighted. The breakdown also now renders for a ranked
  cluster that has **no** report yet, which previously showed nothing at all.
* **A citation offered a link that does not exist — PARTIAL.** Each Mode B
  citation rendered an `ExternalLink` icon, but `_public_citation` exposes no
  URL. Removed the false affordance and replaced it with the real metadata the
  API does return (year), matching how Mode A's `<cite>` already reads. The
  `.citation` grid dropped its now-empty third column.
* **`aria-modal` was not honoured — PARTIAL.** The Connect API dialog claimed
  it without focus management. It now focuses the key field on open, closes on
  Escape, and restores focus to its opener.
* **A malformed payload blanked the page — MISSING.** `response.json()` was
  cast straight to `T`; a non-array body threw inside render and unmounted the
  app. `api.ts` now coerces the five collection endpoints and the graph
  snapshot through `asList`. Against a healthy backend this changes nothing —
  it degrades one panel to its empty state instead of losing the dashboard.

### UI/UX system preserved, not replaced

No redesign. Same Newsreader/Manrope/DM Mono type, same `--teal #087b6d` on
`#102b27` with `--amber`, same `--paper`/`--line`/`--ink`/`--muted` tokens, same
card, pill, blockquote and button patterns. The one structural tidy was
replacing the nested-ternary section heading with a `sectionHeadings` lookup so
a sixth view could be added without a fourth ternary branch. New CSS is 8 lines
(`.signal-grid`, `.ask-thread`, `.asked-question`, `.citation small`) built
from existing tokens. **No new dependency was added.**

### Files changed

* `frontend/src/App.tsx` — clusters view, signal breakdown, Mode B transcript,
  citation attribution, dialog focus handling, heading lookup.
* `frontend/src/api.ts` — `asList` response coercion.
* `frontend/src/App.css` — 8 lines added, `.citation` columns corrected.
* `frontend/src/App.test.tsx` — 6 tests added (written first, all 6 confirmed
  failing before implementation).
* `frontend/README.md`, `PROGRESS.md`.

`frontend/src/types.ts` and both visualization components were **not** modified.
No backend file was touched.

### Verification

**13/13 vitest tests pass** (7 pre-existing, unmodified and still green, plus 6
new). `npm run build` succeeds — `tsc -b` clean. `npm run lint` clean; the gate
was proven live by feeding oxlint a deliberate rules-of-hooks violation, which
it caught, before removing the probe.

### Code review — 2 findings in this session's own code, both fixed

1. `ConnectionPanel`'s focus effect keyed off `onClose`, which App passed as an
   inline arrow — a new identity every render would tear the effect down
   mid-interaction and bounce focus. Wrapped in `useCallback`.
2. `report.suggested_directions.map` and `report.evidence.length` were still
   unguarded; `asList` protects the top-level array but not a null JSON column
   inside a stored report row. Both now default to `[]`.

### Deviations and assumptions

* Phase 12's Testing column in README §13.3 is "—", but the repo already had a
  vitest suite, so the six gaps were still built test-first to match convention.
* `Promise.all` in `dashboard()` still fails the whole load if one of the five
  endpoints errors. Left as-is: all five share one Postgres session, so a
  partial failure is not a realistic state, and restructuring it would be
  unrequested churn. The graph call keeps its existing separate fallback.
* Cluster members carry no `title` from `/clusters` (the backend returns
  `point_id`/`paper_id`/`text`/`year`), so they attribute as `Paper {id}` via
  the shared `evidenceLabel` helper. No title was fabricated.

### Pre-existing working-tree state, untouched

Three uncommitted items predate this session and were deliberately left alone:
the Phase 11 `PROGRESS.md` entry, the untracked
`backend/tests/api/test_routes_backend.py`, and a change in
`backend/app/ingestion/sources/arxiv.py` lowering `search_topic`'s
`max_results` default from 50 to 10. That last one is an undocumented
behavioural change to ingestion breadth and is **worth a decision** — it is not
recorded in any entry.

### Phase 12 status: COMPLETE

README §15 Phase 12's "Done when" — *"a researcher can browse gap reports and
ask interactive questions entirely through the UI"* — is satisfied, and all
four named views (ranked gap reports, trend charts, cluster/knowledge-graph
exploration, chat-style Mode B with citations) now exist. Phase 13 was **not**
started.

---

## 2026-09-12 — Phase 11 (Backend API Integration) audited and closed out

### Why this session started with an audit, not an assumption

The last explicit Phase 11 entry (2026-09-06, below) says "Phase 11 is **not
complete**" and blames it on Phases 4–10 not existing yet. Phases 4–10 have
since all landed (2026-09-06 through 2026-09-08), so that status was stale.
Rather than trust either "still incomplete" or "must be done by now," this
session read `app/main.py`, `app/api/routes_backend.py`, `app/api/security.py`
and every test under `tests/api/`, and checked git history for when and how
the router actually went live.

### What was already true

* `routes_backend.router` **is** registered in `app/main.py`, with router-level
  `Depends(require_api_key)` covering every route on it.
* Every endpoint README §15 Phase 11 names is present and wired to the correct
  phase's service layer: `/collect`, `/upload` (routes_ingestion — Phase 1),
  `/papers`, `/graph`, `/clusters`, `/trends`, `/gaps`, `/reports`, `/query`
  (routes_backend — Phases 3–10), plus `/index` and `/pipeline/run`.
* API-key auth (`app/api/security.py`) is fail-closed, constant-time, and
  already had 23 focused unit/wiring tests from the 2026-09-06 session
  (`tests/api/test_security.py`) covering `/collect`/`/upload` and the
  dependency in isolation.
* FastAPI's default `/docs` / `/openapi.json` satisfy "Adds OpenAPI docs" —
  nothing was disabling them.

### The gap: the router went live with no Phase 11 TDD or security-review pass

`git log -- backend/app/main.py` shows exactly one commit registered
`routes_backend.router`: `26067fc` ("feat: Add PDF data volume to Docker
Compose and enhance pipeline handling", 2026-09-08, authored by the
collaborator who pushed the incomplete `478597b` tree). That commit's own
message is about a Docker volume and pipeline warnings — the router
registration is an incidental one-line diff inside it, not a planned Phase 11
step. No PROGRESS.md entry ever announced it, no dedicated plan or TDD pass
covered it, and no security review looked at the surface it exposed.

The evidence this left behind: `tests/{explanation,ranking,trends}/
test_api_contract.py` each carry a docstring stating "when Phase 10 makes
this module importable, these tests should be replaced by a `TestClient`
call" — and Phase 10 did, five days before this session started, and they
never were. `tests/api/test_main_routes.py` (added in the same commit) only
asserts the paths exist in the OpenAPI schema; it never calls an endpoint.
**No test anywhere sent a real HTTP request through the mounted app to
`/papers`, `/graph`, `/clusters`, `/trends`, `/gaps`, `/reports`, `/index` or
`/pipeline/run`, authenticated or not**, until this session. That is a real
Phase 11 gap against README §13.3's "TDD" + "code review + security review"
requirement for this specific phase — not a functional defect in the wiring
itself, which was already correct.

**Classification: PARTIALLY COMPLETE at the start of this session** — the
build was done, the required security-focused review of the live surface was
not.

### What this session did

Added `tests/api/test_routes_backend.py` (32 tests, all against a real
mounted `FastAPI`/`TestClient`, external services mocked): every GET/POST
route on `routes_backend` — including `/query` and `/pipeline/run` —
rejects an unauthenticated request; the whole router fails closed when
`API_KEY` is unset; each GET handler returns the right shape from real
seeded SQLite rows written through the same `replace_*` storage functions
the service layer uses; each POST handler calls the right service function
and commits; `/pipeline/run` calls every stage in order and correctly skips
report generation when asked; `/reports`' existing 502-on-failure path is
verified to never leak the underlying exception text into the response; and
the full mounted app's OpenAPI schema marks every required Phase 11 path as
authenticated.

**No functional bugs were found or fixed.** The auth wiring, the fail-closed
behavior, the per-route dependency, and the generic error responses were all
already correct — this was a test-coverage and review-process gap, not an
implementation one. Nothing in `routes_backend.py` or `security.py` was
changed.

### Security review of the now-verified live surface

* Every route is behind `APIRouter(dependencies=[Depends(require_api_key)])` —
  router-level, so a future route added to this file is protected by default
  rather than by remembering a per-route decorator.
* No CORS middleware exists anywhere in `app/` (confirmed by search) — matches
  D-11-06's deliberate decision; the dashboard is same-origin via the Vite
  proxy.
* No debug/reload mode: `Dockerfile`'s `CMD` runs plain `uvicorn app.main:app`
  with no `--reload`, so Starlette's default unhandled-exception path returns
  a generic response and logs the traceback server-side only — verified
  directly for `/reports`' explicit handler, and true by framework default for
  every other route since none override it.
* Noted, not fixed: `/reports` and `/query` catch broad exceptions and return
  a generic 502 detail; `/graph`, `/index`, `/clusters`, `/trends`, `/gaps`
  POST and `/pipeline/run` do not, so an internal failure there falls through
  to Starlette's default handler instead. That default handler already
  returns a generic body with no exception text (confirmed above), so this is
  a consistency gap, not a leak — adding matching `try/except` blocks across
  five more handlers is a drive-by refactor of already-correct behavior, which
  CLAUDE.md's no-unrelated-refactors rule argues against doing unprompted.
* List-returning GET routes bound their inputs (`limit`/`offset` `Query(...,
  ge=, le=)`), so none of them can be made to return or scan an unbounded
  result set.
* `/papers` filters go through SQLAlchemy's parameterized `select()`, not raw
  SQL — no injection surface.

### Files changed

* `backend/tests/api/test_routes_backend.py` (new).
* `PROGRESS.md` (this entry).

### Verification

`743 passed, 10 deselected` (`uv run pytest -q` from `backend/`, integration
marker excluded as usual). `ruff check app tests` clean. `ruff format --check`
reports the same two pre-existing files as D-VER-05
(`tests/ingestion/test_no_prohibited_sources.py` and `tests/trends/
test_storage.py`) — unchanged, still the ruff-0.6.9-vs-0.16.6 formatting
disagreement recorded in that entry (the pinned pre-commit hook, not the
venv's newer ruff, is the repo's authority). This session's new file formats
clean under both.

### Phase 11 status: COMPLETE

With the missing endpoint-level TDD coverage and security review now done and
no defects found, README §15 Phase 11's "Done when" — *"the full backend is
usable end-to-end via documented REST endpoints without needing to run
internal scripts manually"* — is satisfied and verified, not merely present in
source. Phase 12 (Frontend Dashboard) was **not** started; this session did
not touch `frontend/`.

---

## 2026-09-08 — Phase 10 independent verification + four ingestion/citation fixes

A second maintainer pulled `main` and verified Phase 10 rather than extending
it. No phase was started; every change below is a defect fix in already-shipped
code, each written test-first. Phase 10's own design — the LangGraph node set,
bounded retrieval, grounding check, regeneration cap — was reviewed and found
sound and is unchanged.

### Verification baseline reproduced

`langgraph` was declared in `pyproject.toml` but missing from the checked-in
`backend/.venv`, so five test modules failed to *collect* on a fresh pull.
Installing it into the existing venv reproduced the teammate's numbers exactly:
701 passed / 10 deselected. Frontend 7 passed, tsc, oxlint and build clean.
Evaluation harness 4 passed. Docker stack rebuilt: all five services healthy,
`/health` 200 with neo4j/qdrant/postgres all true.

Hard constraints re-checked mechanically: LangGraph is imported only by
`app/interactive/workflow.py`; no `langchain` import anywhere in `app/`
(`langchain-core` arrives only as langgraph's own transitive dependency); no
loop around Stage 8→9 in `app/explanation/service.py`; no IEEE / Springer /
ACM reference in `app/` or `frontend/src/`.

The leading space in `.env`'s `GROQ_API_KEY=` value was investigated and is
**not** a defect — both Compose's `env_file` parser and python-dotenv strip it.
A live Groq call from inside the container returned 200 with the key as
configured.

### D-VER-01 — `/collect` no longer loses a run to one flaky source

Semantic Scholar answers unauthenticated callers with 429/500 routinely. The
exception propagated out of the handler, so `session.commit()` never ran: a
live run left **50 arXiv PDFs on disk and 0 rows in Postgres**, with the PDFs
orphaned. Each source now runs inside its own `begin_nested()` savepoint;
a failure is rolled back to that savepoint, logged, and reported per source in
a new additive `sources` key on the response. Only an all-discovery-sources-down
run is an outage, and that returns 502 rather than a misleading empty success.

`CollectResult.sources` is optional in `frontend/src/types.ts`, and the
dashboard's collect status line now names any source that was unreachable.

### D-VER-02 — the ACL export was re-parsed in full on every `/collect`

`bibtexparser.load()` over the ~180MB Anthology export held ~3.0GB RSS and
pinned a core for **45+ minutes**, and it ran on *every* `/collect` call, not
just the first. `search_topic` now streams the file, splits on the `@` record
boundary the export guarantees, and hands the parser only records whose raw
text could match.

The prefilter is a deliberate strict superset of the real filter, so the set of
returned papers cannot change: braces are dropped so LaTeX protection
(`{I}ndian`) still matches `indian`, and whitespace is collapsed so a topic
wrapped across a line still matches. The original precise title/abstract check
on the parsed entry is untouched and still decides the result. All ten
pre-existing ACL tests pass unchanged, which is what pins the equivalence.

Measured: a full `/collect` went from 45+ min / ~3.0GB (never completing) to
**40 seconds / ~0.5GB**, returning 50 deduplicated papers while Semantic
Scholar was still 429ing.

### D-VER-03 — Mode B citations now name the paper

`app/embeddings/schema.PAYLOAD_FIELDS` has no `title`, and unlike Phase 9 —
which joins Postgres in `explanation.service._paper_titles` — Phase 10 took
titles only from Neo4j graph rows. With graph expansion empty, every Mode B
citation rendered as "Paper 7" while Mode A's reports showed real titles.
`InteractiveServices` gained an optional `title_lookup`, wired in
`default_services` to one batched `select(Paper.id, Paper.title)` per retrieval
round. A lookup failure is logged and swallowed: a missing title degrades a
citation's display, whereas failing the question over it would be worse.

### D-VER-04 — caches survive a backend rebuild

`ACL_ANTHOLOGY_BIB_PATH` defaults inside the image and the Hugging Face cache
lives at `/root/.cache/huggingface`, so every `up -d --build backend` — the
command README §12 prescribes after *any* code change — re-downloaded the
~180MB export **and** ~1.6GB of models (SPECTER2, GLiNER2, DeBERTa-MNLI, the
cross-encoder). Added the `corpus_cache` (`/app/data`) and `model_cache`
(`/root/.cache/huggingface`) volumes, matching the existing `pdf_data` pattern,
and documented both in README §12.

### D-VER-05 — the two "unformatted" test files are correct as they are

The Phase 10 entry noted `tests/ingestion/test_no_prohibited_sources.py` and
`tests/trends/test_storage.py` still failing `ruff format --check`. They are
not a defect and must not be reformatted: **`.pre-commit-config.yaml` pins
ruff `v0.6.9`, and `backend/.venv` currently has ruff `0.16.6`**, and the two
versions disagree on exactly these files (0.16 moved to the trailing-parenthesis
assert-message style). The pinned hook is the repo's authority — it reverted an
attempted reformat during commit.

So `ruff format --check` run from the venv reports 2 files while the pinned
pre-commit hook reports none. Use the hook's verdict. Reconciling the two by
bumping the pin is a deliberate change worth making on its own, not a drive-by.

### Still unproven — Phase 10's "Done when" is not yet demonstrated live

README §15 requires "a test question reliably returns an answer whose claims
trace back to specific retrieved papers". **That has still never happened.**
The Phase 10 entry above flagged the same thing; this session did not close it.

What is true now:

* `/collect` succeeded live — Postgres holds **50 papers** (arXiv + ACL;
  Semantic Scholar 429'd and was degraded past, as designed by D-VER-01).
* `/pipeline/run` was started and **interrupted at 202 s** when the containers
  were torn down. The handler commits only at the end, so nothing downstream
  persisted: `parsed_documents` is 0 and the Qdrant collection is still empty.
* Consequently `POST /query` still answers on its empty-corpus branch. Every
  grounded, reformulation and regeneration path remains covered only by unit
  and API tests with mocked external services.

The owner **deliberately deferred** the rerun: it needs GROBID plus four
CPU-bound models over 50 papers (15-40 min, no GPU on this machine), and
README 13.6 puts machine stability above phase completion. Closing this needs
one `/collect` (~40 s since D-VER-02) and one `/pipeline/run`, then a real
`POST /query`.

Phase 10 therefore stands as: **implementation and test coverage complete and
reviewed; live end-to-end evidence still outstanding.** No phase was started or
advanced in this session.

### Files modified

* `backend/app/api/routes_ingestion.py`, `backend/tests/api/test_routes_ingestion.py`
* `backend/app/ingestion/sources/acl_anthology.py`,
  `backend/tests/ingestion/sources/test_acl_anthology.py`
* `backend/app/interactive/workflow.py`, `backend/tests/interactive/test_workflow.py`
* `docker-compose.yml`, `README.md`, `frontend/src/App.tsx`, `frontend/src/types.ts`

Committed to `main` as five commits (`fdd03e3`, `e3f13fe`, `dbeacc6`, `34e3889`,
plus this entry). Nothing was pushed.

---

## 2026-09-08 — NumPy confidence PostgreSQL persistence fix

Fixed the live pipeline failure `psycopg2.ProgrammingError: can't adapt type
'numpy.float32'`. SciBERT cosine similarity can return NumPy scalar confidence
values even though `ExtractedStatement.confidence` is typed as `float`.
`replace_facts()` now converts every confidence to a native Python `float` at
the database boundary. A regression test uses a real `numpy.float32`; the full
backend suite passes (`701 passed`, 10 integration tests deselected), and a
rollback-only smoke against the real PostgreSQL container stored `0.72` as a
native float. The backend image was rebuilt and is healthy.

---

## 2026-09-08 — Phase 10 (Interactive GraphRAG with LangGraph) complete

Implemented README Phase 10 / MODEB-01..03 under plan → TDD → implementation
→ review. Phase 9's Mode A service was inspected and left unchanged. Mode B
uses LangGraph directly and reuses the Phase 5 `EvidenceIndex`, Phase 4 Neo4j
paper graph, and Phase 9 direct Groq structured-output client; there are no
application imports of LangChain and no Gemini/Ollama provider path.

### Workflow delivered

* `POST /query` accepts a bounded, trimmed natural-language question.
* Each retrieval round queries Qdrant first and seeds a bounded one-hop Neo4j
  paper/entity expansion from the returned paper IDs.
* Sufficiency requires configurable relevant-chunk and distinct-paper minima.
  Thin evidence is reformulated by Groq and retried, with an absolute cap of
  three total retrieval rounds and visible trace/counter fields.
* A lazy cross-encoder reranks the combined candidate set before generation.
* Groq returns a schema-validated answer and per-claim evidence IDs. Structural
  citation validation runs before a second structured Groq grounding check.
* A failed grounding check permits exactly one stricter regeneration. A second
  failure returns a safe failure message and never leaks the ungrounded draft.
* Citations contain only the public evidence fields and follow deterministic
  reranked order. Retrieved PDF text is length-bounded and explicitly fenced
  as untrusted prompt data.

### Review findings fixed

The pre-existing Phase 10 prototype was not production-safe: it treated any
non-empty text as sufficient, searched Neo4j independently of vector hits,
routed grounding failures back through retrieval, could return an ungrounded
draft after the retry budget, and built citations from an unordered set. The
implementation replaces each behavior with the bounded workflow above.

The live rebuild also exposed the Dockerfile pulling a CUDA Torch stack despite
the project's CPU-only instruction. The Dockerfile now installs Torch from the
official CPU wheel index before resolving backend dependencies.

### Verification

* Phase 10 focused suite: `104 passed`.
* Complete backend default suite: `700 passed, 10 deselected` (integration
  marker), run from `backend/` as required by fixture paths.
* Frontend: `7 passed`; lint clean; TypeScript/Vite build successful.
* Ruff: full `check` clean; Phase 10 changed-file format check clean. The full
  historical tree's format check still reports two unrelated pre-existing test
  files (`test_no_prohibited_sources.py`, `test_storage.py`).
* Docker: rebuilt successfully with CPU Torch; backend, PostgreSQL, Qdrant,
  Neo4j, and GROBID are healthy. Live authenticated `POST /query` returned 200
  and the correct one-round empty-corpus response. The one-hop Cypher executed
  successfully against real Neo4j without unknown-property notifications.

### Live successful-answer status

The current Qdrant collection contains zero points, so a real grounded-answer
Groq run has no evidence to cite. The upstream `numpy.float32` persistence
failure found during this verification was fixed in the subsequent entry
above; the evidence pipeline and Qdrant index still need to be rerun to
populate the corpus. Unit and API tests cover the successful grounded,
reformulation, and regeneration paths with mocked external services.

Phase 11 was not started. Awaiting explicit owner authorization.

---

## 2026-09-08 — LLM provider consolidated on Groq (Gemini and Ollama removed)

Owner decision: **Groq is the project's only LLM provider.** Gemini and Ollama
support was removed outright — no provider fallback of any kind remains. This
was a provider swap only: Phase 9's architecture (ranked gaps → retrieved
evidence → LLM → grounded report) is untouched, the prompt and grounding check
are unchanged, and Phase 10 was not started.

### What changed

* `app/explanation/llm.py` — `GeminiClient` and `OllamaClient` replaced by a
  single `GroqClient` against Groq's OpenAI-compatible REST API
  (`POST {GROQ_API_URL}/chat/completions`, `Authorization: Bearer`), still via
  plain `httpx`: no vendor SDK, no framework wrapper, no new dependency.
  `get_llm_client()` now accepts only `groq` and raises `LLMError` on anything
  else.
* `app/core/config.py` — `gemini_api_key`, `gemini_model`, `gemini_api_url`,
  `ollama_url` and `ollama_model` deleted; `groq_api_key`, `groq_model` and
  `groq_api_url` added. `llm_provider` now defaults to `groq`;
  `llm_api_key` and `llm_timeout_seconds` are unchanged, and `groq_api_key`
  still falls back to `llm_api_key` so a single-key `.env` keeps working.
* `.env.example`, `README.md`, `backend/README.md` — Ollama pull/Compose-profile
  instructions and the Gemini alternative replaced by one `GROQ_API_KEY` line.
* Test fixtures that used `provider="ollama" / model="qwen3"` now use
  `groq` / `groq-test`; `frontend/src/App.test.tsx` uses
  `groq` / `openai/gpt-oss-20b`. No test calls the real Groq API.

### D-PROV-01 — schema enforcement drove the default model

`GROQ_MODEL` defaults to `openai/gpt-oss-20b` rather than a Llama model because
Groq only honours `response_format: json_schema` on part of its catalogue (the
gpt-oss and qwen3.8 families). `REPORT_SCHEMA` is handed to the provider the
same way it was handed to Ollama's `format`; on a model without json_schema
support the request degrades to prompt-described JSON, which is exactly the
guarantee Stage 9 relies on. `strict: false` (Groq's best-effort mode) is used
because `REPORT_SCHEMA` does not set `additionalProperties: false`.

### Owner action

Create a key at <https://console.groq.com/keys> and set `GROQ_API_KEY` in
`.env`. Nothing else is needed — no local service, no Compose change. Until
then `POST /reports` returns 502 and `POST /pipeline/run` with
`generate_reports: false` skips the stage cleanly, exactly as before.

### Note on earlier entries

Entries below this one describe `gemini_*` / `ollama_*` settings as they existed
when written. They are kept as the historical record; this entry supersedes
them.

### Verification

`656 passed` (`uv run pytest -q`, excluding `tests/interactive/` and
`tests/api/test_query_errors.py`, both of which still fail to *collect* on the
pre-existing missing `langgraph` dependency — unchanged by this work), ruff
check and ruff format clean.

---

## 2026-09-07 — Phase 9 (Mode A RAG-grounded explanation) implemented

Built under TDD against the surviving `app/api/routes_backend.py` and
`frontend/src/types.ts`, both treated as frozen consumer contracts throughout.
Phases 0–8 were not modified apart from the two integration points Stage 9
cannot run without (below). Phase 10 was not started.

This is the first phase whose output is **not** deterministic — an LLM writes
the prose. Everything around it is: retrieval query, prompt, evidence
projection, ordering and the grounding check are all pure functions of stored
rows, so the same corpus always produces the same *prompt*, and every stored
report is checkable against the evidence it cites.

### The frozen contract Stage 9 had to satisfy

`routes_backend` already called this phase and fixed its whole shape:

* `explanation.service.generate_top_reports(session)` and `(session, settings)`
  positionally, returning cluster ids;
* `explanation.storage.list_reports(session)` rows exposing `.cluster_id`,
  `.gap`, `.evidence`, `.confidence`, `.suggested_directions`, `.provider`,
  `.model`;
* `frontend/src/types.ts` `Evidence` — `id`, `paper_id`, `title`, `text`,
  `year`, `source_type` — and `GapReport.confidence: number`.

`routes_backend` still cannot be *imported* (`langgraph` is Phase 10), so the
contract is tested by parsing its source, the technique Stages 7 and 8
established. `tests/api/test_query_errors.py` now fails only on `langgraph`; it
has advanced past `app.explanation.service`.

### D-09-01 — confidence is a number, not README's "High"

README §8's example report prints `Confidence: High`. `frontend/src/types.ts`
declares `confidence: number` and `App.tsx` renders
`Math.round(report.confidence * 100)`, so a string would render `NaN%`. The
frozen consumer wins; the LLM is asked for a float in [0, 1] and the field is
range-checked in `ReportDraft`, in `GapReportRecord` and by the column type.
`nan` and `inf` are both rejected by the pydantic bound (verified — this is the
class of defect the Phase 6 review found in the NLI score path).

### D-09-02 — the grounding check, and what it does on failure

The model is handed the retrieved passages each labelled with its **real Qdrant
point id** and must return the ids it relied on. Then:

* an id that was not retrieved is discarded and logged as invented;
* if *no* cited id survives, the report is **not stored at all**;
* the stored `evidence` is the cited subset, in retrieval (similarity) order —
  not everything Qdrant returned, and not the model's ordering, which carries no
  information the pipeline produced.

So `GET /reports` can only ever show text that a real retrieval returned. This
is README §15's "no generated claim is unsupported by retrieved evidence"
reduced to something a test can hold.

Nothing is retried. CLAUDE.md hard constraint 3 forbids a loop around Stage
8→9: a report that cannot be grounded is dropped, and recovering from weak
retrieval is Mode B's job (Phase 10), which is a different mode with a
different contract. `tests/explanation/test_service.py::TestSinglePass` asserts
exactly one retrieval and one generation per cluster.

### D-09-03 — the retrieval query is the label *plus* the cluster's statements

Phase 6 labels are frequency-derived and can be three thin words (D-06-06) —
not enough to place a query in SPECTER2 space. The query is the label followed
by up to five member statements in Phase 6's stored order, capped at 4000
characters (`embedding_max_chars`'s reasoning: SPECTER2 truncates at 512 word
pieces anyway).

No `source_type` filter is applied. README §9 says "supporting paragraphs", but
filtering to `paragraph` would exclude the limitation and future-work
statements that *constitute* the theme — the strongest evidence there is. Every
evidence item carries its `source_type` through to storage and the prompt, so
what each quote is remains visible.

### D-09-04 — one outage aborts the run; one bad cluster does not

`LLMUnavailableError` propagates immediately. Every remaining cluster would
fail the same way, and burning `report_top_k` timeouts to store nothing is the
resource failure README §13.6 warns about. Because the whole list is built
before `replace_reports` runs, an abort also means the delete never executes and
**the previous run's reports survive**.

A per-cluster failure — a provider schema error, a draft that fails validation,
a cluster with no retrievable evidence — is logged and skipped; the remaining
clusters still get reports. A cluster with no retrieved evidence is skipped
*before* the LLM call, so no generation is spent on something that cannot be
grounded.

### Prerequisite — the LLM settings `Settings` never defined

`app/explanation/llm.py` survived the incomplete upstream push and reads
`settings.llm_provider`, `gemini_api_key`, `gemini_model`, `gemini_api_url`,
`ollama_url`, `ollama_model` and `llm_timeout_seconds`. **None of them existed
on `Settings`** — only `llm_api_key` did — so `get_llm_client()` would have
raised `AttributeError` on the first call. Added, with `.env.example` entries.

Default provider is `ollama`: it needs no owner-supplied credential, so an
unconfigured deployment still starts and fails loudly at call time rather than
at import. `gemini_api_key` falls back to `llm_api_key` in `get_llm_client`, so
an existing single-key `.env` keeps working. `retrieval_round_cap` and
`reranker_model` are read by `app/interactive/workflow.py` and are **Phase 10
work — deliberately not added here.**

### Files

New: `app/explanation/{schema,prompt,storage,service}.py`,
`tests/explanation/{__init__,test_schema,test_prompt,test_storage,test_service,test_table_registration,test_api_contract}.py`.
Modified: `app/core/config.py` (Phase 9 + LLM settings), `app/main.py` (one
`noqa: F401` import), `tests/core/test_config.py`, `.env.example`.
`app/explanation/llm.py` and its tests were left untouched.

### Settings added

`report_top_k` (5), `report_evidence_top_n` (8) — both bounded on each end;
zero would explain nothing or ground nothing, silently. `llm_provider`,
`llm_timeout_seconds` (bounded: a hung request must not hold a pipeline run
open), `gemini_api_key`, `gemini_model`, `gemini_api_url`, `ollama_url`,
`ollama_model`.

### Storage

One derived table, `gap_reports`, on the shared `Base`, with a `ForeignKey` to
`clusters.id` — which is what makes `replace_clusters._dependent_tables()`
discover and clear it, so a report can never reattach to a recycled cluster id
describing a different theme. `replace_reports` wipes and rewrites in the
caller's transaction (flushes, never commits). `list_reports` orders by the
surrogate key, because insertion order *is* rank order — the service walks
`list_rankings` best-first — and the dashboard uses that order for its rank
numbers when `/gaps` is empty. `provider` and `model` are stored beside the
prose so a generated artefact can be attributed later.

### Verification

650 backend tests pass (649 before this phase's review fix; 71 of them are
Phase 9, plus 6 new config tests). Ruff check and format clean. The two
remaining collection errors are unchanged in count and both are now
`langgraph` alone — Phase 10's dependency.

No live LLM call was made: this phase's owner-facing prerequisite is a
configured provider (below), and the whole point of the injectable `index`/`llm`
parameters is that the pipeline's logic is testable without one.

### Code review — 2 findings, both fixed

1. **An empty retrieval query was sent to Qdrant.** A cluster with neither a
   label nor members produced `""`, and `retrieve("")` does not fail — it
   returns whatever sits nearest the zero vector, which would then have been
   presented as supporting evidence for a theme that describes nothing.
   Reachable when a ranking outlives its cluster, which SQLite permits because
   it does not enforce `ON DELETE CASCADE` — the same reason every table in
   Phases 6–8 wipes explicitly. Now skipped before the retrieval, with a
   regression test that deletes the members and nulls the label.
2. **`evidence_from_point` raised `ValueError` for a wrong-typed `paper_id`.**
   `IndexRecord.__post_init__` validates the same field on the way *into* the
   vector store and raises `TypeError`; Stage 9 now matches, and the caller
   catches both.

### Known characteristic (not a code defect)

Evidence text comes from PDFs, so a paragraph could contain text shaped like an
instruction ("ignore previous instructions…"). The grounding check bounds what
that can achieve — every stored quote is real retrieved text and every report
must cite a real point id — but it does not stop a crafted paragraph steering
the *prose*. Hardening the prompt boundary is not something README §15 asks for
in this phase; flagged here rather than added unilaterally.

### Owner action required before `POST /reports` returns anything

Stage 9 is the first phase that needs a generation provider. *(Superseded by
the 2026-09-08 entry above: the provider is now Groq — set `GROQ_API_KEY` in
`.env`.)*

Until it is configured, `POST /reports` returns 502 and `POST /pipeline/run`
with `generate_reports: true` fails the run; `generate_reports: false` skips
the stage cleanly.

### Still blocked

`routes_backend.router` remains unregistered: `app.explanation` is now
complete, but `langgraph` (Phase 10) is not installed. That is the only
remaining blocker — every other module `routes_backend` imports now exists.

---

## 2026-09-07 — Phase 8 (Gap Ranking) implemented, plus two prerequisite defect fixes

Built under TDD against the surviving `app/api/routes_backend.py`, which was
treated as a frozen consumer contract throughout. Plan:
`.planning/phases/08-gap-ranking-stage-8/08-01-PLAN.md`. Phase 6's clustering /
contradiction behaviour and Phase 7's trend analysis were not modified.

Like Stage 7 the whole phase is **offline and deterministic**: its input is the
persisted `clusters` + `cluster_members` + `cluster_trends` + `extracted_facts`
+ `papers` rows, it loads no model, opens no socket and reads no clock, and
running it twice over the same rows produces identical output. Plain Python, as
README §15 fixes for this phase — `app/ranking/` imports no NumPy, no Pandas, no
scikit-learn.

---

### Prerequisite fix P5 — the Phase 5 re-index defect (issue 1 of the Phase 7 review)

**Still present, and reproduced rather than assumed.** Two papers, two
extraction runs:

```
paper1 fact db id run1/run2: 1 3     point id stable: False
paper2 fact db id run1/run2: 2 4     point id stable: False
```

**Root cause.** `indexer._record_id("fact", paper.id, str(fact.id))` keyed the
Qdrant point on `ExtractedFact.id` — a **database surrogate key**.
`replace_facts` is delete-then-insert, so every run mints fresh autoincrement
ids for the same statements. A second `POST /pipeline/run` therefore wrote a
*new* point for every fact and orphaned the previous one, and `gap_points()`
returned each limitation N times after N runs. The module docstring already
claimed the invariant this broke: *"Point ids are `uuid5` of a stable
`kind:paper_id:position` key."* Paragraph ids honoured it (structural
`section:paragraph` position); fact ids did not.

The defect is **masked on a single-paper SQLite database** — rowid is reused
after deleting the only row — which is why no existing test caught it. The
regression tests use two papers so the surrogate key genuinely moves.

**Fix, in two parts.** Smallest change consistent with the existing
architecture; no new table, column, setting or dependency, and Phase 5's
semantics (what is indexed, what the payload carries, cosine config, batching)
and Phase 6's semantics are otherwise untouched.

1. `_fact_position()` keys the point on the fact's **natural evidence
   identity** — `fact_type:section_index:paragraph_index:sentence_index:text`,
   exactly the tuple `uq_extracted_fact_evidence` already uses to define when
   two rows are the same fact. Stable by construction: unchanged evidence,
   unchanged point id. The numeric fields precede the free text, so a `:`
   inside a statement can only shift the last component and never make two
   different facts collide.
2. `EvidenceIndex.prune_paper()` — a stable key stops *new* orphans but cannot
   clear ones an already-indexed corpus contains (written under the old scheme,
   or belonging to a statement the corpus no longer has). After a paper's
   records are successfully upserted, its points outside the current record set
   are deleted (`paper_id == N AND NOT has_id(current)`). Scoped to a paper just
   indexed, so an unrelated failure can never wipe good vectors; failures are
   logged, never fatal, so cleanup cannot cost a paper its indexed count.

Six regression tests in `tests/embeddings/test_indexer.py`, all red before the
fix: the point id survives a re-extraction, distinct statements still get
distinct ids, a second `index_all` over the same corpus leaves exactly one point
per statement, a removed statement is pruned, pruning never touches another
paper, and a failed prune still counts the paper as indexed.

**Effect on Phase 8.** `mention_frequency` counts distinct papers (D-08-02), so
it would have been insulated from this anyway — but the root cause is fixed, so
Phase 6's `member_count` is correct again too.

### Prerequisite fix P1 — `papers.citation_count` did not exist

`w4·citation_impact` had no input. The column was absent from the `Paper` model
even though `routes_backend._paper_dict` reads `paper.citation_count` and
`frontend/src/types.ts` declares `citation_count: number | null` — `GET /papers`
would have raised `AttributeError`. Added as a nullable `Integer`, populated
from Semantic Scholar (`citationCount` appended to the existing `FIELDS` string
and mapped in `_to_paper_dict`); README §7 names Semantic Scholar as the source
of citation counts. `dedup.merge` is fill-if-None, so an arXiv record later
matched to an S2 record picks the count up and never loses it. No new source, no
extra request, no branch on `source`.

Nullable rather than defaulted to `0`: an arXiv-only or manually uploaded record
has no count, and `0` would assert that nobody cited it — `citation_impact`
reads unknown as no-evidence-of-impact.

> **Owner action required on an existing database.** This is the project's
> first *new column on an existing table*; `Base.metadata.create_all()` creates
> missing tables but never `ALTER`s. An already-populated Postgres volume will
> fail every `Paper` read with `UndefinedColumn` until it is added:
>
> ```sql
> ALTER TABLE papers ADD COLUMN citation_count INTEGER;
> ```
>
> Or drop the volume and re-collect. The project has no migration tooling by
> design (README §7 lists none, CLAUDE.md constraint 8 forbids adding a
> framework), and altering the owner's live schema is a §13.5 owner-only action,
> so this is documented rather than automated.

---

### D-08-01 — the formula is implemented literally, with raw per-cluster signals

No cross-cluster normalization. Normalizing would make a cluster's score depend
on which *other* clusters happened to be in the run, so the same evidence would
score differently between runs and Stage 9's sentence *"27 papers mention it,
only 3 propose mitigations → score X"* would stop being derivable from the
cluster's own numbers. Each signal stays in its natural unit and the weights are
what put them on a common scale — which is what README §8's "weights are
configurable" is for. The stored breakdown is the raw values, so every score is
checkable by hand.

### D-08-02 — `mention_frequency` is distinct papers, not member statements

Same unit as D-07-01 and README §8 Stage 9's *"27 papers mention it"*.
`Cluster.member_count` counts *statements*, so one paper contributing four
limitation sentences would register four mentions.

### D-08-03 — `trend_slope` is Phase 7's stored slope, signed and unscaled

Read straight off `cluster_trends.slope`. Phase 7 already fixed the unit (signed
mentions per year), rounded it for reproducibility and normalized `-0.0`;
re-deriving or rescaling here would fork the number the dashboard plots from the
number the score uses. A missing trend and a non-finite slope both resolve to
`0.0` — a `NaN` would make every ordering comparison in the ranking silently
false.

### D-08-04 — `solution_density` matches contributions against the cluster label

The denominator is the cluster's distinct papers. The numerator needs *"does
this paper propose something for **this** theme"*, and the only non-LLM,
non-model evidence in the store is Phase 3's `contribution` statements. The
theme's own vocabulary is already computed, stored and displayed:
`Cluster.label`, Phase 6's deterministic document-frequency term selection
(D-06-06). So a paper proposes a solution when one of its `contribution` facts
contains **every** term of the label, matched case-insensitively on the same
word alphabet Phase 6 tokenizes with (`[a-z][a-z0-9-]{2,}`) — which is what
keeps "injection" out of "injectionless". Requiring every term rather than any
one keeps a single shared word ("multilingual") from marking half a focused
corpus as solvers.

Reusing the stored label rather than re-deriving terms keeps `app/ranking/` free
of scikit-learn/NumPy (README fixes "plain Python"), avoids duplicating Phase
6's stopword list, and makes the match auditable against something the user can
already see in the dashboard.

**The numerator is corpus-wide, not restricted to the cluster's papers.** A
paper that actually solved the problem does not usually list it as its own
limitation, so it is not in the cluster; restricting the numerator to members
would leave this signal at ~0 for every theme and hand `w3` nothing to do. The
ratio is clamped to `[0, 1]` so the `− w3·density` term stays bounded and the
value still reads as a density.

Considered and declined: matching contributions in SPECTER2 space. Contributions
are not indexed — `GAP_SOURCE_TYPES` is `{limitation, future_work}` — and
indexing them would be a Phase 5 change well beyond this phase's remit.

### D-08-05 — `citation_impact` is the mean citation count of the mentioning papers

Mean, not sum: a sum is a second copy of `mention_frequency` (a 40-paper theme
would out-cite a 5-paper theme on headcount alone) and `w1` already prices
frequency. An unknown count counts as `0`; dropping unknowns would let a single
Semantic-Scholar-matched paper set a whole theme's impact. A negative count is
corrupt external metadata, floored at zero rather than treated as a penalty.

A cluster member whose paper has left the corpus contributes an unknown count,
not a `KeyError` — `ClusterMember.paper_id` deliberately carries no foreign key
(Phase 6), because the vector store can lag the corpus.

### D-08-06 — `contradiction_flag` is the flag, at 0.0 / 1.0

README names a *flag* and `frontend/src/types.ts` types it `number` (`App.tsx`
reads it for truthiness to show a "Conflicting evidence" pill).
`Cluster.contradiction_score` is deliberately **not** substituted: a 0.71 and a
0.99 both mean "these papers disagree", and swapping in the score would silently
re-weight `w5` by the NLI model's confidence. The score stays what it is — the
audit value behind the flag, already on `GET /clusters`.

### D-08-07 — default weights, and why they are what they are

| weight | default | reading |
|---|---|---|
| w1 `GAP_WEIGHT_MENTION_FREQUENCY` | 1.0 | one point per mentioning paper — the reference unit |
| w2 `GAP_WEIGHT_TREND_SLOPE` | 3.0 | growing by one paper/year is worth three mentioning papers |
| w3 `GAP_WEIGHT_SOLUTION_DENSITY` | 25.0 | a fully-addressed theme loses 25 points |
| w4 `GAP_WEIGHT_CITATION_IMPACT` | 0.02 | 50 mean citations is worth about one mentioning paper |
| w5 `GAP_WEIGHT_CONTRADICTION` | 5.0 | a flagged cross-paper disagreement is worth five mentioning papers |

All five are `ge=0`: the formula's own signs carry each term's direction, so a
negative weight would invert a signal behind the operator's back — a negative
`w3` would turn *"this is already being solved"* into a bonus. Zero is allowed;
that is how a signal is switched off.

`w3` is large on purpose. `solution_density` is a ratio in `[0, 1]` while
`mention_frequency` is an unbounded count, so a small `w3` makes the one signal
that says "papers are already addressing this" decorative. **This is the honest
weak point of the additive form README §8 fixes**: on a corpus where a theme has
100 mentioning papers, even a fully-solved theme only loses 25 of them. A
multiplicative `(1 − density)` factor would behave better, but the formula is
specified additively and is not the agent's to change. `w3` is the weight most
worth tuning per corpus, and `.env.example` says so.

### D-08-08 / D-08-09 / D-08-10 — rounding, ordering, storage

Score rounded to 6 decimals with `-0.0` normalized, same reasoning as D-07-06.
Ordering is score descending, ties broken by ascending `cluster_id` — the
dashboard prints row order as the rank, so it must not depend on insertion
order. `cluster_rankings` is a derived table like Stage 6's and Stage 7's:
`cluster_id → clusters.id ON DELETE CASCADE`, unique, wiped explicitly in
`replace_rankings` (SQLite does not enforce foreign keys), one row per cluster
always — a cluster that scores `0.0` scored zero, it was not skipped. Its
`ForeignKey` is what makes Phase 7's `_dependent_tables()` fix discover it, so a
bare `POST /clusters` cannot leave a ranking attached to a recycled cluster id;
a test asserts that.

### Files

New: `app/ranking/{signals,storage,service}.py`,
`tests/ranking/{__init__,test_signals,test_storage,test_service,test_table_registration,test_api_contract}.py`,
`.planning/phases/08-gap-ranking-stage-8/08-01-PLAN.md`.
Modified: `app/embeddings/indexer.py` (P5), `app/ingestion/corpus.py`,
`app/ingestion/sources/semantic_scholar.py`, `app/api/routes_ingestion.py` (P1),
`app/core/config.py` (five weights + `gap_weights()`), `app/main.py` (one
`noqa: F401` import), `.env.example`, `.planning/{ROADMAP,REQUIREMENTS}.md`,
and the corresponding tests.

### Verification

**575 backend tests pass** (479 before this phase — 96 new; 80 of them Phase 8,
16 the two prerequisite fixes). Ruff check and format clean across `app` and
`tests`. The two collection errors are unchanged from the Phase 7 baseline:
`tests/api/test_query_errors.py` (`app.explanation.service`) and
`tests/interactive/test_workflow.py` (`langgraph`), both Phase 9/10 work.

No live database or Qdrant server was used this session (Docker not running).
Phase 5's regressions run against a real in-process `QdrantClient(":memory:")`
with real collection config, real filters and real scroll pagination; everything
in Phase 8 runs against real SQLAlchemy sessions with real Phase 6 rows written
through `replace_clusters` and real Phase 7 rows through `replace_trends`.

`routes_backend` still cannot be imported, so `/gaps` was tested by parsing that
module's source with `ast` and asserting the handler's projection against real
rows — the technique Phase 7 established. Pinned: the fields it reads off each
row, the `signals` sub-object's five keys, that `list_rankings(session, limit)`
gets the limit, that `POST /gaps` calls `rebuild_rankings(session)`, that
`POST /pipeline/run` calls `rebuild_rankings(session, settings)` positionally,
and that it ranks *after* clustering and trends.

### Code review — 4 findings in Phase 8 code, all fixed

1. **`solution_density` re-tokenized the whole corpus once per cluster.** It is
   called for every cluster against the *same* corpus-wide contributions, so a
   50-cluster corpus ran `re.findall` over every contribution statement 50
   times. Split out `tokenize_contributions()`, called once per run in the
   service; the signal now takes pre-built term sets and does a subset test.
2. `mention_frequency` and `citation_impact` were annotated `object`. Now
   `Iterable[int]` / `Iterable[int | None]`.
3. An unreachable `if computed else 0.0` in a log line (the early return already
   guarantees a non-empty list).
4. The prune-failure regression test asserted only `"1" in caplog.text`, which
   almost anything satisfies. Now asserts the actual message.

### Pre-existing issues surfaced (not introduced here, not fixed here)

The review swept the whole uncommitted tree, so it also raised defects belonging
to earlier phases. Recorded rather than patched, because CLAUDE.md binds work to
the authorized phase. Issues 4 and 5 from the Phase 7 review (`_clean` ignoring
`embedding_max_chars`; `NERRE_*` undocumented in `.env.example`) were raised
again and remain open. Three are new:

1. **Phase 5 — `index_all`'s blanket `except Exception` swallows the deliberate
   dimension-mismatch `ValueError`.** `check_collection_dimension` exists to
   fail loudly when `EMBEDDING_MODEL` changes under an existing collection, but
   the per-paper handler catches it, so `POST /index` returns
   `200 {"indexed": 0}` and the pipeline goes on to cluster stale vectors. The
   guard should be hoisted above the loop, or that one exception re-raised.
2. **Phase 6 — the NLI pair cap slices `combinations` in arrival order.**
   `itertools.combinations` emits every pair involving member 0 before any pair
   involving member 1, so above roughly `NLI_MAX_PAIRS_PER_CLUSTER` members only
   the first statement is ever compared and a disagreement between, say, the
   10th and 20th members is never scored — costing Phase 8 its `w5` term for
   exactly the largest, most important clusters. Deterministic (D-06-05 holds),
   but not representative.
3. **Phase 6 — `_dependent_tables()` reads `Base.metadata`,** so the cascade
   wipe only covers a child table whose module has been imported. Every real
   entry point (`app.main`, `routes_backend`) imports all of them and a Phase 8
   test asserts it, but a caller importing only `clustering.service` would leave
   orphans behind.

### Phase 8 "Done when" — satisfied at the contract level

*"A ranked, queryable list of gap clusters with individual signal breakdowns is
available via the API."* The ranking, the ordering, the persistence and the
breakdown are complete and tested, and `GET /gaps` / `POST /gaps` consume them.
As with Stages 6 and 7, the endpoints exist in source but are not mounted:
`routes_backend.router` stays unregistered until `app.explanation` (Phase 9) and
`langgraph` (Phase 10) exist. Phase 8 removes `app.ranking` from that blocker
list.

---

## 2026-09-07 — Phase 7 (Trend Analysis) implemented

Built under TDD against the surviving `app/api/routes_backend.py`, which was
treated as a frozen consumer contract throughout. Plan:
`.planning/phases/07-trend-analysis-stage-7/07-01-PLAN.md`. Phase 6's clustering
and contradiction behaviour was not modified; its known DeBERTa-MNLI false
positive remains an open design question.

The whole phase is **offline and deterministic**: its input is the persisted
`clusters` + `cluster_members` rows, it loads no model, opens no socket and
reads no clock, and running it twice over the same rows produces identical
output. Pandas does the aggregation, NumPy the fit, exactly as README §7/§15
specify.

### D-07-01 — a "mention" in a year is a *distinct paper*, not a statement

README §8 states the unit in its own Stage 7 example — *"'Hallucination': 5
papers in 2021 → 140 in 2025"* — and Stage 9's evidence line reads *"27 papers
mention it"*. Counting cluster *members* instead would let one paper that
contributed four limitation sentences to a cluster register four mentions in a
single year, manufacturing both frequency and slope out of one publication.
That is the failure D-06-04 already rejected on the contradiction side. Members
are therefore de-duplicated by `paper_id` within each year.

This also makes Stage 7 robust to the Phase 5 re-index defect found in this
phase's review (see "Pre-existing issues surfaced" below): duplicated Qdrant
points from the same paper collapse back to one mention.

### D-07-02 — interior years with no mentions are filled with zero

A cluster mentioned in 2020 and 2023 has the series `2020:1, 2021:0, 2022:0,
2023:2`, not two points. The zeros are the literal truth and they are what makes
the slope a rate *per year* rather than per observation — without them a dormant
theme fits as steeply as an accelerating one. Filling is bounded to the
cluster's own observed `[min_year, max_year]`: extrapolating outward would
assert something about years the corpus never spoke about.

### D-07-03 — undated members are excluded, not bucketed

A member whose `year` is NULL came from a paper with no publication year (a
manual upload GROBID could not date). Inventing a year for it would put
fabricated evidence into a Phase 8 signal. It is dropped from the series and
counted into an aggregate log line; it still counts toward `Cluster.member_count`,
which is Phase 8's separate `mention_frequency` input.

### D-07-04 — a bounded, configurable sanity window on years

`TREND_MIN_YEAR` (1900) / `TREND_MAX_YEAR` (2100). Broken metadata reaches this
phase as `year = 12` or `year = 99999`; combined with D-07-02's zero-filling,
one such value turns a five-year series into a several-thousand-entry dict and
flattens the slope toward nothing. Out-of-window members are dropped exactly
like undated ones. Both bounds are static constants rather than "now": a slope
whose value moved with the wall clock could not be reproduced, which is the
whole point of this phase. A validator rejects an inverted window, which would
otherwise silently empty every cluster's series.

### D-07-05 — ordinary least squares, written out in closed form

`slope = Σ(x−x̄)(y−ȳ) / Σ(x−x̄)²` over the zero-filled series, in **signed
mentions per year**. Chosen over `np.polyfit`, which emits a `RankWarning` and
returns unstable coefficients on exactly the degenerate inputs this phase meets
routinely, and over any normalization, which would bake a scaling choice into
Phase 8's `w2`. Every degenerate case resolves to a defined `0.0`, never `NaN`:
an empty series, a single year (denominator zero — one point defines no rate),
and a flat series (numerator zero).

### D-07-06 — the stored slope is rounded to 6 decimal places

Least squares on float64 leaves a bottom-bit tail that differs between BLAS
builds. Six decimals is far below any weight Phase 8 could give it and well
above the two the dashboard renders. `-0.0` is normalized to `0.0`, because the
dashboard prefixes `+` whenever `slope >= 0` and then calls `.toFixed(2)`,
printing `+-0.00`.

### D-07-07 — one row per cluster, always, in its own wipe-and-rewrite table

A cluster with nothing datable is stored with `{}` and `0.0` rather than
skipped, so Phase 8 never has to tell "no trend" from "not computed".

`cluster_trends` is a separate table with `cluster_id → clusters.id ON DELETE
CASCADE`, not a column on `clusters`, because Phase 6 rewrites `clusters`
wholesale and cluster ids are not stable across rebuilds. Verified against the
PostgreSQL dialect: `SERIAL` PK, `JSON` (not `JSONB` — key order is preserved),
a unique index on `cluster_id`, and the cascade in the DDL.

### `yearly_counts` keys are strings, by contract

`frontend/src/types.ts` declares `Record<string, number>` and `TrendChart.tsx`
reads `yearly_counts[String(year)]`. Storing int keys would come back from the
JSON column as strings anyway, making the round trip lossy; they are strings
going in.

### Files

New: `app/trends/{analysis,storage,service}.py`,
`tests/trends/{__init__,test_analysis,test_storage,test_service,test_table_registration,test_api_contract}.py`,
`.planning/phases/07-trend-analysis-stage-7/07-01-PLAN.md`.
Modified: `app/core/config.py` (trend year window + validator), `app/main.py`
(one `noqa: F401` import), `app/clustering/storage.py` (review finding 1),
`backend/pyproject.toml`, `backend/uv.lock`, `tests/core/test_config.py`,
`.env.example`, `.planning/{ROADMAP,REQUIREMENTS}.md`.

### Settings added

`trend_min_year` (default 1900) and `trend_max_year` (default 2100), both
bounded `1..3000`, with a validator rejecting an inverted window.

### Dependency

`pandas>=2.2,<4` declared (resolved to 3.0.5). It was the only package `uv sync`
installed; nothing else in the Phase 4–6 dependency set moved.

### Verification

479 backend tests pass (477 after implementation, +2 from the review fixes; 412
before this phase — 67 of the new ones are Phase 7). Ruff check and format clean
on everything touched. The two collection errors are unchanged from the Phase 6
baseline: `tests/api/test_query_errors.py` (`app.explanation.service`) and
`tests/interactive/test_workflow.py` (`langgraph`), both Phase 9/10 work.

The `cluster_trends` DDL was verified by compiling it against the PostgreSQL
dialect rather than against a live database — unlike Phase 6, Docker was not
running in this session. Everything else is exercised against real SQLAlchemy
sessions with real Phase 6 rows written through `replace_clusters`.

**`routes_backend` still cannot be imported**, so `/trends` was tested by
parsing that module's source with `ast` and asserting the handler's projection
against real rows: the fields it reads off each row, the JSON keys it emits,
that `POST /trends` calls `rebuild_trends(session)` with the session only, and
that `POST /pipeline/run` rebuilds trends too. Those tests should be replaced by
a `TestClient` call once Phase 8 makes the module importable.

### Code review — 2 findings in Phase 7 code, both fixed

1. **A trend survived a re-cluster and attached itself to a different theme.**
   `cluster_trends` was only wiped inside `replace_trends`, so a bare
   `POST /clusters` left it stale. On PostgreSQL the FK cascade covers this; on
   SQLite — the documented local-dev store — no foreign key is enforced *and*
   rowids restart after a full delete, so the old theme's series came back
   attached to the new cluster that inherited its id. Reproduced directly: a
   fresh "compute cost" cluster served the multilingual theme's 2020–2024
   series. Fixed in `app/clustering/storage.replace_clusters`, which now clears
   every table carrying a foreign key onto `clusters`, discovered from the
   metadata rather than enumerated — this generalizes the rule that function's
   docstring already stated (children are deleted explicitly *because* SQLite
   does not enforce foreign keys) and covers Phase 8's ranking table for free.
   No clustering or contradiction semantics changed.
2. **`app/main.py` was paying a ~0.6 s Pandas import at application startup**
   for a single type annotation, because it imports `app.trends.storage` to
   register the table and that module imported `TrendSeries` from `analysis`.
   Moved behind `TYPE_CHECKING`; a subprocess test asserts importing
   `app.trends.storage` does not pull in Pandas. This is the property
   `app/clustering/schema.py` already protects for Stage 6.

### Pre-existing issues surfaced (not introduced here, not fixed here)

The review swept the whole uncommitted tree, so it also raised five defects
belonging to earlier phases. They are recorded here rather than patched, because
CLAUDE.md binds work to the one authorized phase.

1. **Phase 5 — re-indexing duplicates every fact vector.** Fact point ids are
   `uuid5(...fact:<paper>:<ExtractedFact.id>)`, but `replace_facts` re-inserts
   facts with fresh autoincrement ids on every run, so a second
   `POST /pipeline/run` leaves the first run's vectors orphaned in Qdrant.
   `gap_points()` then returns each limitation twice, inflating cluster member
   counts and Phase 8's `mention_frequency`. **This is the most consequential of
   the five.** Phase 7's own output is insulated from it by D-07-01 (duplicates
   from one paper collapse to one mention per year), but Phase 6 and Phase 8 are
   not.
2. **Phase 6 — `CLUSTERING_MIN_SAMPLES=` raises `ValidationError`.** Exactly the
   form `.env.example` documents; an operator uncommenting that line takes the
   whole API to 500. Same class of bug as (3).
3. **Phase 5 — `EMBEDDING_MODEL_REVISION=` resolves to `''`, not `None`,** and
   is passed to `from_pretrained(revision='')`, failing the model load instead
   of tracking the default branch as documented. Same for `NLI_MODEL_REVISION`.
4. **Phase 5 — `_clean` truncates payload text at the module constant
   `MAX_CHARS`,** ignoring `settings.embedding_max_chars`; lowering the setting
   makes stored evidence text and embedded text diverge, which its own docstring
   says cannot happen.
5. **Phase 4 — the five `NERRE_*` settings were never documented in
   `.env.example`,** contrary to the project convention (Phases 5, 6 and 7 each
   have a block).

### Still blocked

`routes_backend.router` remains unregistered — `app.trends` is now complete, but
`app.ranking` (Phase 8), `app.explanation.service`/`storage` (Phase 9) and
`langgraph` (Phase 10) are not. Until then `GET/POST /trends` exist in source
but are not mounted, so Phase 7's "exposed via the API" criterion is satisfied
at the contract level (the handler consumes this phase and is tested against it)
and will be live the moment Phase 10 lands.

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

---

## 2026-09-06 — Phase 4 model choice APPROVED by owner

### D-04-01 — GLiNER2 `JointIE` is the Phase 4 joint NER+RE model

**Owner approved 2026-09-06**, closing the decision ROADMAP Phase 4 requires be
locked "before any code".

**Chosen:** `gliner2` (Apache 2.0, `fastino-ai/GLiNER2`) — specifically the
`gliner2.joint_ie.JointIE` typed-graph API against a GLiNER2.5 **boundary**
checkpoint built with `enable_relations=True`. Not the basic
`extract_relations()` API.

**Why not the README's named architecture.** DyGIE++ depends on AllenNLP, which
has been archived since 2023; its documented install now fails on a modern
Python/torch stack and upstream has said it lacks the bandwidth to fix it. Even
working, its pretrained SciERC checkpoint carries a fixed and *different* label
set (Task/Method/Metric/Material/OtherScientificTerm/Generic; Compare/
Conjunction/Evaluate-For/Used-For/Feature-Of/Part-Of/Hyponym-Of) — none of this
project's five entity types or four relations. Repurposing it would require
training data this project has never had and no phase plans to create. The same
objection applies to SciREX, SpERT and PL-Marker: fixed schemas, research code,
unmaintained packaging.

**Why GLiNER2 `JointIE` specifically.** It is the only evaluated option that is
installable and maintained today *and* accepts the entity/relation schema at
inference time, so no retraining is needed. `JointIE` declares relations as
`.relation(name, head_type, tail_type)` — a direct structural match for the
surviving `RELATION_ENDPOINTS` contract — and decodes entities and relations
together under those typed endpoint constraints in one forward pass, satisfying
NERRE-01's single-pass requirement.

**Deviation, recorded deliberately.** This is *not* the "SciERC-style joint
model (DyGIE++ architecture)" README §7/§15 names. It is accepted under README
§15's own "or an equivalent pretrained scientific IE model" clause, reading
"equivalent" as equivalent *purpose* (single-pass, typed, pretrained, no
training-data dependency) rather than identical architecture. Surfaced and
approved rather than silently substituted, per README §14 rule 3.

**Guard against the known footgun.** The same package also ships an untyped
`extract_relations()` that *cannot* constrain head/tail entity types. Using it
would violate the typed-endpoint contract `graph.py` depends on. Phase 4 code
must use `JointIE` only.

**Not decided here:** the Phase 6 clustering-library question (README specifies
HDBSCAN/BERTopic; the lost build used scikit-learn) remains open for Phase 6.

---

## 2026-09-06 — Phase 4 (Joint NER + Relation Extraction) implemented

Built under TDD against the owner-approved GLiNER2 `JointIE` choice (D-04-01).

### Files

New: `app/ner_re/schema.py`, `app/ner_re/extractor.py`, `app/ner_re/service.py`,
`tests/ner_re/{test_schema,test_extractor,test_service}.py`,
`.planning/phases/04-.../04-01-PLAN.md`.
Modified: `app/core/config.py` (`nerre_*`), `pyproject.toml`, `uv.lock`,
`app/parsing/section_classifier.py` (import shim), `tests/core/test_config.py`.

### D-04-02 — relation endpoints

Taken from README §8's own worked examples, not invented:
`trained_on (model,dataset)`, `evaluated_using (model,dataset)`,
`fails_on (model,domain)`, `achieves (model,metric)`. `task` is extracted as an
entity and reaches the graph via `Paper-[:MENTIONS]->Entity`, but no relation in
the fixed four targets it; README's "etc." leaves room to add one later.

### D-04-03 — dependency conflict forced a sentence-transformers downgrade

`gliner2[local]` requires `transformers<5`; `sentence-transformers>=6.0`
requires `transformers>=5`. Mutually exclusive. Resolved by pinning
`transformers>=4.57,<5` and `sentence-transformers>=5.1,<6` (5.x accepts
transformers 4.x). This reverses a Phase 2 pin, so `section_classifier.py` now
tries the 6.x module path and falls back to the 5.x one. Locked versions:
gliner2 2.0.0, transformers 4.57.6, sentence-transformers 5.7.0. The stale lock
also dropped langgraph/pandas/langchain-core, which the committed pyproject
never declared — they return with Phases 7 and 10.

### D-04-04 — entity descriptions are load-bearing, not cosmetic

Measured on the real checkpoint: without descriptions "GSM8K" types as `metric`;
with them, `dataset` — the difference between an `evaluated_using` edge existing
or not. `ENTITY_TYPES` now derives from `ENTITY_DESCRIPTIONS` so they cannot drift.

### D-04-05 — entity threshold calibrated to 0.15, not guessed

The initial 0.5 guess dropped correct entities. Measured over scientific
sentences: confidences are strongly bimodal — lowest correct entity 0.248
("BERT"), next 0.482, **zero false positives at any threshold in the sample**.
`confidence` is a *typing* score; JointIE's own `candidate_threshold` already
filters non-entities. Relation scores sit on a much higher scale (min 0.84), so
`nerre_relation_threshold` stays 0.5. Missing a `model` entity is especially
costly — every relation has model as its head.

### Verification

211 backend tests pass in both the global env and an isolated `uv sync` venv
carrying the real Phase 4 dependency set. Ruff check + format clean. The live
`@pytest.mark.integration` test passes against `fastino/gliner2.5-base-v1`.
End-to-end smoke against the running Neo4j wrote `(GPT-4)-[:FAILS_ON]->(low-resource
languages)` and read it back via Cypher — NERRE-02 satisfied.

### Known model-quality characteristic (not a code defect)

Zero-shot typing is imperfect: in the multi-sentence smoke run "BERT" typed as
`task`, so the endpoint guard correctly *dropped* the resulting bogus
`trained_on` edge rather than writing it. Span boundaries also drift ("SQuAD
dataset"). Precision is favoured over recall by design — a mistyped endpoint
would otherwise make graph.py's Cypher MATCH silently write nothing.

### Still blocked

`routes_backend.router` remains unregistered — `app.ner_re.service` now exists,
but Phases 5-10 modules do not. 3 collection errors remain (Phases 5, 10, 11).

---

## 2026-09-06 — Phase 5 (Embeddings & Vector Indexing) implemented

Built under TDD against the surviving `app/embeddings/indexer.py`, which was
treated as a frozen contract throughout. Plan:
`.planning/phases/05-embeddings-vector-indexing-stage-5/05-01-PLAN.md`.

### D-05-01 — SPECTER2 is `allenai/specter2_base` + the `allenai/specter2` proximity adapter

README §7/§15 fix the model as **SPECTER2**, and the official release is a base
transformer plus a task adapter — the *proximity* adapter is the variant the
model card prescribes for retrieval and clustering, which is exactly what
Phases 5, 6, 9 and 10 do with these vectors. Attaching it requires AdapterHub's
`adapters` package.

Checked before adding, because Phase 4's dependency set is delicate (D-04-03):
`adapters==1.3.0` declares `transformers~=4.57.6`, and the Phase 4 pin
(`transformers>=4.57,<5`) already resolves to 4.57.6. So the official SPECTER2
path costs **no change to the GLiNER2 dependency set** — `uv sync` installed
`adapters` alone and touched nothing else. Verified: the Phase 4 live GLiNER2
integration test still passes with `adapters` present.

`torch`/`transformers`/`adapters` are imported **inside** `load_specter2`, never
at module import, so `adapters`' model-class patching cannot reach a process
that only wanted GLiNER2.

Embeddings are the `[CLS]` token of `last_hidden_state`, per the model card.
768-d, cosine distance in Qdrant.

### D-05-02 — the surviving indexer's contracts were preserved, not rewritten

`indexer.py` and `tests/embeddings/test_indexing.py` survived the incomplete
upstream push and dictated the shape of both new modules:

- `IndexRecord` is constructed **positionally** with nine fields
  (`record_id, paper_id, text, source_type, fact_id, year, venue,
  section_type, graph_node_id`), and `payload()` may never carry `id` or
  `score` because `retrieve()` splats it beside those two keys.
- `Specter2Encoder` exposes `.dimension` and `.encode(texts, batch_size)`
  returning one vector per input text in input order (`upsert` zips
  `strict=True`).
- `get_encoder()` stays lazy so an empty index answers a query without loading
  a model — a surviving test asserts exactly that.

`ensure_collection()` was left byte-identical; the surviving 409-race test
drives it with a two-method fake client, so every hardening addition went into
new methods instead.

### D-05-03 — a read never creates the collection

`gap_points()` previously called `ensure_collection()`, which needs
`encoder.dimension` — so Phase 6 reading an empty index would have loaded
SPECTER2 purely to size a collection nothing was about to be written to. It now
short-circuits on a missing collection, matching what `retrieve()` already did.

### D-05-04 — a model swap fails loudly instead of corrupting the collection

`check_collection_dimension()` runs before every write: if the existing
collection stores vectors of a different width than the encoder produces
(the signature of an `EMBEDDING_MODEL` change), the upsert raises. It never
deletes or recreates the collection to "fix" it — destroying an indexed corpus
is the owner's call, not the pipeline's. Nothing in `app/embeddings/` calls
`delete_collection`.

### Files

New: `app/embeddings/schema.py`, `app/embeddings/encoder.py`,
`tests/embeddings/{__init__,test_schema,test_encoder,test_indexer}.py`,
`.planning/phases/05-.../05-01-PLAN.md`.
Modified: `app/embeddings/indexer.py` (additive hardening only),
`app/core/config.py` (embedding/retrieval settings), `backend/pyproject.toml`,
`backend/uv.lock`, `tests/core/test_config.py`, `.env.example`.

### Settings added

`embedding_model`, `embedding_adapter`, `embedding_model_revision` (optional HF
revision pin for reproducible vectors), `qdrant_collection`,
`embedding_batch_size` (1–256), `embedding_max_tokens` (1–512),
`embedding_max_chars` (1–100 000), `retrieval_top_n` (1–1000). All bounded on
both ends: a zero would silently index or return nothing, an unbounded value is
an OOM.

### Verification

304 backend tests pass (211 before this phase; the previously-blocked
`tests/embeddings/test_indexing.py` now collects and passes). Ruff check and
format clean. All 5 `integration`-marked tests pass **when run from the repo
root** — including Phase 4's, confirming `adapters` did not disturb GLiNER2.

Live smoke against the running Qdrant container (throwaway collection, deleted
afterwards): real SPECTER2 → 768-d collection created, all five payload indexes
actually built server-side (`payload_schema` confirmed), a semantic query
ranked the multilingual-prompt-injection paper first (0.964) ahead of the
cross-lingual benchmark (0.889) with the unrelated systems paper out of the top
three, metadata and `graph_node_id` correct on every hit, a
`year`+`venue` filter narrowed correctly, `gap_points()` returned 3 points with
768-d vectors, and a full re-run left the point count unchanged at 6
(idempotent).

### Known characteristic, not a defect

`gap_points()` materializes every gap vector in memory for Phase 6. At corpus
scale (~600 papers) that is a few hundred MB of Python floats. It is surviving
Phase 5 code and Phase 6's input contract; if it needs streaming, that is a
Phase 6 decision.

### Pre-existing issue surfaced (not introduced, not fixed here)

`Settings`' `env_file=".env"` is resolved relative to the **working
directory**, and `.env` lives at the repo root — so any test calling
`get_settings()` fails when pytest is run from `backend/`. Phase 4's live
GLiNER2 integration test does this. Phase 5's integration test deliberately
does not, so it passes from either directory. Out of scope for Phase 5; worth
fixing centrally.

### Still blocked

`routes_backend.router` remains unregistered — `app.embeddings` is now complete,
but `app.clustering.service` (Phase 6) and `langgraph` (Phase 10) are not. 2
collection errors remain, down from 3: `tests/interactive/test_workflow.py`
(langgraph) and `tests/api/test_query_errors.py` (clustering).

---

## 2026-09-06 — Phase 6 (Semantic Clustering + Contradiction Detection) implemented

Built under TDD against the surviving `app/api/routes_backend.py`, which was
treated as a frozen consumer contract throughout. Plan:
`.planning/phases/06-semantic-clustering-contradiction-detection-stage-6/06-01-PLAN.md`.
Phase 5 was not modified.

### D-06-01 — HDBSCAN via `sklearn.cluster.HDBSCAN`

This closes the clustering-library question the project-state reconciliation
left open. README §7/§15 name the **algorithm** ("HDBSCAN or BERTopic"), not a
package, and scikit-learn ≥1.3 ships HDBSCAN in-tree — the upstream
implementation, ported and maintained. So this is HDBSCAN proper.

Chosen over the standalone `hdbscan` package, which needs a Cython/C++ build
and lags NumPy 2 wheels on Windows — a build failure this environment does not
need. Chosen over BERTopic, which is a pipeline wrapper (UMAP + HDBSCAN +
c-TF-IDF + its own embedding backend) that would want to own the embedding step
SPECTER2 already owns, and pulls `umap-learn`/`numba` for a component whose
clustering core is the HDBSCAN we would run anyway (CLAUDE.md constraint 8).

The lost upstream build's bare scikit-learn is **not** inherited as-is: what it
lacked was HDBSCAN, and `sklearn.cluster.HDBSCAN` supplies exactly that.
`scikit-learn>=1.7,<2` and `numpy>=2.0,<3` are now declared directly (both were
already resolved transitively via sentence-transformers, so `uv sync` installed
nothing new and the Phase 4/5 dependency set is untouched). `>=1.7` is the floor
at which `HDBSCAN(copy=...)` can be set explicitly.

### D-06-02 — cosine agreement via L2-normalized vectors + euclidean

Qdrant scores this collection with cosine distance, so clustering must agree
with retrieval. `metric="cosine"` forces `algorithm="brute"` in scikit-learn's
HDBSCAN; on L2-normalized vectors `euclidean² = 2 − 2·cosine`, a strictly
monotone transform, so euclidean over normalized vectors gives exactly the
cosine neighbourhood ordering with no metric-support caveat.

### D-06-03 — `microsoft/deberta-base-mnli`, with label indices read from the checkpoint

README fixes "DeBERTa-MNLI". The base release (~139M params) over
`deberta-large-mnli` (~400M) per README §13.6; the checkpoint is `NLI_MODEL`, so
the large variant is a `.env` change.

**Label indices come from `model.config.label2id`, never hardcoded.** DeBERTa
MNLI releases disagree on label order — `deberta-large-mnli` starts at
CONTRADICTION, other MNLI heads start at ENTAILMENT — so a hardcoded index would
silently invert entailment and contradiction. `label_index()` maps the
checkpoint's own names onto the three canonical ones and refuses anything that
is not a three-way NLI head.

### D-06-04 — only cross-paper pairs are scored

CLUSTER-02 and README §8 both describe the signal as *one paper claims a method
works where another says it fails* — disagreement **between** papers. Two
sentences from the same paper contradicting each other is an extraction
artefact, not gap evidence, so same-paper pairs are never scored. This also
removes the dominant source of spurious pairs where one paper contributed
several statements. Confirmed live: cluster 2 of the smoke run held two
statements from paper 203 and they were correctly never paired.

### D-06-05 — both directions, and the pair work is capped

NLI is asymmetric, so `(a→b)` and `(b→a)` are both scored and the stronger
contradiction wins; otherwise a real disagreement is missed purely because of
statement order. Pairs are O(n²) in cluster size and each is a forward pass, so
candidates are capped (`NLI_MAX_PAIRS_PER_CLUSTER`, hard ceiling 2000) after a
deterministic point-id ordering — a truncated cluster must not flag a different
contradiction on each run.

### D-06-06 — cluster labels are derived deterministically, no extra model

`Cluster.label` is nullable in the surviving contract and the dashboard falls
back to `Cluster {id}`. A label is a display affordance, not evidence, so it is
frequency-ranked term selection over the members (stopword-filtered, ranked by
how many *distinct* statements use a term, ties alphabetical) rather than a
topic model or an LLM call — CLAUDE.md constraint 6 forbids the latter here.

### D-06-07 — noise points are not clusters

HDBSCAN assigns `-1` to points it cannot place; they are dropped, never swept
into a catch-all. A "cluster" of unrelated leftovers would earn a mention count,
a trend slope and a gap score in Phases 7–8, manufacturing a gap out of noise.

### D-06-08 — gap vectors are mean-centered before clustering

**Found by the integration test, not assumed.** Raw SPECTER2 `[CLS]` vectors are
strongly anisotropic — they occupy a narrow cone. Measured on the hand-labelled
set with the real encoder:

| | within-theme cosine | across-theme cosine |
|---|---|---|
| raw | 0.931 (min 0.893) | 0.871 (max 0.925) |
| mean-centered | 0.354 (min 0.164) | −0.182 (max 0.356) |

Untreated the two distributions overlap and HDBSCAN had almost nothing to work
with: the first real-model run pulled a compute-cost statement into the
multilingual cluster and lost the multilingual theme entirely. Centered, all
three themes are recovered exactly. Centering shifts only the copy Stage 6
clusters — the vectors stored in Qdrant, which Phases 9/10 retrieve against,
are untouched.

The synthetic unit-test vectors carry a deliberate shared cone component for the
same reason: a fixture of clean orthogonal directions would let an
implementation pass without ever confronting the geometry real embeddings have.

### Files

New: `app/clustering/{schema,cluster,nli,storage,service}.py`,
`tests/clustering/{__init__,labelled,test_schema,test_cluster,test_nli,test_storage,test_service,test_table_registration,test_integration}.py`,
`.planning/phases/06-.../06-01-PLAN.md`.
Modified: `app/core/config.py` (clustering/NLI settings), `app/main.py` (one
import — see the review findings), `backend/pyproject.toml`, `backend/uv.lock`,
`tests/core/test_config.py`, `.env.example`, `.planning/{ROADMAP,REQUIREMENTS}.md`.

### Settings added

`clustering_min_cluster_size` (≥2 — HDBSCAN's floor), `clustering_min_samples`,
`clustering_max_statements`, `nli_model`, `nli_model_revision`, `nli_batch_size`,
`nli_max_tokens`, `nli_max_chars`, `nli_max_pairs_per_cluster`,
`contradiction_threshold`. All bounded on both ends. **There is deliberately no
cluster-count setting** — CLUSTER-01 requires the count be discovered, and a
config test asserts the field does not exist.

### Storage

Three derived tables on the shared `Base`: `clusters`, `cluster_members`,
`cluster_contradictions`. `replace_clusters` wipes and rewrites in the caller's
transaction (it flushes, never commits — `routes_backend` owns the commit), so
cluster ids are **not stable across rebuilds**; Phases 7–8 rebuild after this
one. `ClusterMember.paper_id` deliberately carries no foreign key: it arrives in
a Qdrant payload and the vector store can lag the corpus, so one stale point
must not abort a corpus-wide rebuild. Every flagged pair is stored, so a
`contradiction_flag` a gap score depends on can actually be audited.

### Verification

412 backend tests pass (401 before this phase; 95 of the new ones are Phase 6).
Ruff check and format clean. All 5 Phase 6 `integration`-marked tests pass
against the real models: the three hand-labelled themes are each recovered as
exactly one cluster by real SPECTER2, the multilingual theme comes back whole,
the deliberate cross-paper disagreement is flagged by real DeBERTa-MNLI, and the
compute-cost theme (which contains no disagreement) is not flagged.

Live smoke against the running Qdrant (throwaway collection, deleted
afterwards): 12 real 768-d SPECTER2 points in, `gap_points()` out, 3 clusters
stored with labels, the multilingual contradiction flagged at 0.999 with all
three contributing pairs recorded, and a second rebuild leaving exactly 3
clusters / 12 members (idempotent). Table DDL verified against the live Postgres
through the real `app.main` startup path: all three tables created.

### Code review — 4 findings, all fixed

1. **`app/main.py` never imported `app.clustering.storage`**, so the three
   tables would not have been on `Base.metadata` at `create_all()` and
   `POST /clusters` would have failed against Postgres with "relation does not
   exist" the first time it ran. Phases 2 and 3 each carry an explicit
   `noqa: F401` import for exactly this reason; Stage 6 was missing it. Fixed,
   with `tests/clustering/test_table_registration.py` and a live Postgres check.
2. **`candidate_pairs` materialized the full O(n²) product before slicing.** A
   2000-member cluster is ~2M tuples (hundreds of MB) built only to keep fifty —
   the exact resource failure README §13.6 warns about, and the docstring's
   "capped before any model is called" was true of the forward passes but not of
   the memory. Now `islice` over a generator, with a test that counts how many
   pairs are examined.
3. **`cluster_statements` truncated in arrival order** under
   `CLUSTERING_MAX_STATEMENTS`. `gap_points()` returns whatever order Qdrant
   scrolled in, so if the cap ever bit, two runs over the same corpus could
   cluster different statements. Now sorted by point id before truncating.
4. **A NaN `contradiction` probability passed through `min`/`max` untouched**
   and surfaced as an opaque "score must be in [0, 1], got nan" from
   `ContradictionPair`. Now rejected with a message naming the actual cause.

### Known model-quality characteristic (not a code defect)

DeBERTa-MNLI produces false positives on statement pairs that are not a
premise/hypothesis about the same setup. In the smoke run the hallucination
cluster was flagged at 0.955 for "Generated summaries contain unfaithful
statements…" against "Future work should reduce hallucination by grounding every
generated claim…" — two statements that agree. `CONTRADICTION_THRESHOLD` is the
knob (0.7 default), but at 0.955 raising it costs recall before it costs this
false positive. Restricting candidates to `limitation` statements (excluding
`future_work` proposals) would remove this class of error and is worth
considering, but README §8 says "checks sentence pairs", so it was not added
unilaterally. Every flagged pair is stored in `cluster_contradictions` precisely
so this is auditable.

Cluster labels are frequency-derived and can pick up a stray term from one
member when a cluster shares little vocabulary ("inference afford budget").
Deterministic and nullable by contract; cosmetic.

### Still blocked

`routes_backend.router` remains unregistered — `app.clustering` is now complete,
but `app.trends` (Phase 7), `app.ranking` (Phase 8),
`app.explanation.service`/`storage` (Phase 9) and `langgraph` (Phase 10) are
not. 2 collection errors remain, unchanged in count from the Phase 5 baseline
but both now advanced past clustering: `tests/api/test_query_errors.py`
(`app.explanation.service`) and `tests/interactive/test_workflow.py`
(`langgraph`).

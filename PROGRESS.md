# PROGRESS

Running decisions/progress log (CLAUDE.md §"Development workflow"). `.planning/`
holds the per-phase GSD artifacts; this file is the cross-session summary.

---

## 2026-09-21 — Discovery breadth, duplicate reporting, id reset, library UI (owner-requested)

Five owner-requested changes, outside the README §15 phase list. No phase was
started or advanced.

### D-QUERY-02 — arXiv and ACL stopped demanding the topic as one exact phrase

Root cause of "`/collect` for *Multilingual prompt injection* stored ten papers,
all `semantic_scholar`": both of the other sources required the user's whole
topic as a single literal phrase.

* arXiv (D-QUERY-01) sent `all:"Multilingual prompt injection"` — words
  adjacent, in order, in one field. Probed against the live API while making
  this change: that query returns **0** results; the replacement returns **41**.
* ACL matched the topic as one case-insensitive substring of a title/abstract,
  with the same adjacency requirement.
* Semantic Scholar tokenizes the query server-side, which is exactly why it —
  and only it — answered.

`app/ingestion/topic.py` (`meaningful_terms`) is now the single place a topic is
split, so the two sources cannot drift apart again: quotes dropped, surrounding
punctuation stripped (hyphens/apostrophes kept inside a word), a short
function-word list dropped, duplicates collapsed, and a topic of nothing but
function words returned whole rather than emptied out.

arXiv now sends `all:"<term>" AND …`; ACL requires every term as a substring of
the title/abstract. Deliberately **AND, not OR** — every term the user typed
still constrains the search, so this widens the match without turning a topic
search into a keyword dragnet. ACL's raw-text prefilter (the thing that keeps
the 180MB export from being parsed in full) now tests the single longest term,
which is still a strict superset of "all terms present", so it cannot change
which papers come back.

### D-DUP-01 — /collect now reports what dedup did

The dedup rules themselves are untouched (`dedup.find_existing`: DOI → arXiv ID
→ ACL ID → normalized title+year). What changed is that the outcome is visible:
`added`, a `duplicates` breakdown, and `source_stats` per source (`candidates`
returned / `processed` under that source's cap / `added` / `duplicates`), so a
source that stored nothing says whether it returned nothing or returned only
papers the corpus already had — a bare `"status": "ok"` cannot.

Duplicate *categories* count distinct papers, never encounters: a paper all
three sources return is **one** cross-source duplicate, one row, one id. Each
duplicated paper lands in exactly one category, in priority order
`already_in_corpus` → `cross_source` → `within_source` (`RunLedger` carries the
reasoning). Per-source counts stay encounter-based, where the distinction does
not arise. A source that raises has its rows rolled back to its savepoint, so
its counts are discarded with them — merged into the run only after the step
returns.

### D-RESET-02 — a full corpus reset rewinds the id sequences

Deleting every row left PostgreSQL's sequence where it was, so the first paper
after a reset came back as id 21. `reset_corpus` now rewinds the identity of
each table it emptied, **in the same transaction as those deletes** and only for
tables emptied there — an id is never reissued while a row still holds it, and
ordinary collections keep appending as before. PostgreSQL: the sequence is
resolved server-side via `pg_get_serial_sequence` (covers `SERIAL` and
`GENERATED … AS IDENTITY` alike) and rewound with `setval(seq, 1, false)`, the
name bound as a value rather than interpolated into SQL. SQLite reuses rowid 1
on its own once a table is empty, so only an `AUTOINCREMENT` high-water mark in
`sqlite_sequence` needs clearing. `pipeline_runs` — which holds the row for the
reset itself — is never passed in. No second reset implementation was created:
`DELETE /admin/corpus` still calls this one function.

### Frontend — the whole collection, and a guarded delete

*Recently added papers* showed `.slice(0, 6)` of the corpus. It now renders every
paper of the **current** operation, resolved from the ids `/collect` (or
`/upload`) returned, so cross-source duplicates appear once and a paper the
corpus merely happens to hold is not presented as part of the search. It scrolls
instead of truncating; with no operation yet it falls back to the loaded corpus,
newest first. `/papers` returns a bounded window, so on a corpus larger than
that window a run's paper could fall outside it — the card says so rather than
quietly showing fewer papers than the search reported.

*Delete corpus* sits directly under *Find papers*, styled as destructive, and
goes through a real modal (`ConfirmDeleteDialog`, modelled on `ConnectionPanel`:
focus on open, Escape to dismiss, focus returned to the opener) with **No**
focused first so a stray Enter cannot wipe the corpus. No `window.confirm`. It
calls the existing `DELETE /admin/corpus` through the same authenticated client
every other request uses; frontend state is cleared only after the backend
confirms, and a failure leaves the corpus visible with the button re-enabled.

### Files

`app/ingestion/topic.py` (new), `sources/arxiv.py`, `sources/acl_anthology.py`,
`api/routes_ingestion.py`, `core/corpus_reset.py`; `frontend/src/{App.tsx,
App.css,api.ts,types.ts}`. Tests: `tests/ingestion/test_topic.py` (new),
`tests/ingestion/sources/test_{arxiv,acl_anthology}.py`,
`tests/api/test_routes_ingestion.py`, `tests/core/test_corpus_reset.py`,
`frontend/src/App.test.tsx`.

Verified: backend 1008 passed / 10 deselected (integration), `ruff check app
tests` clean, evaluation 4 passed, frontend 19 passed + `tsc -b` + `vite build`
+ `oxlint` clean, and the live arXiv probe above. The ACL change is covered by
the offline fixture only — the live 180MB export was not downloaded.

## 2026-09-20 — `priority_fix.md` completed: O2 stage telemetry, benchmark harness, lint gate restored

Audit of the working tree against `priority_fix.md` after pulling a teammate's
hardening commit (`066a437`, 2026-09-16), then implementation of what that
commit left open. `HARDENING_PLAN.md` records the authorization.

### Already implemented by the teammate (verified, not rebuilt)

| ID | Where it landed |
|---|---|
| O1 | `core/logging.py` `configure_logging()`, `LOG_LEVEL` in `config.py`, called from `main.py` |
| O2 (partial) | `stage_timer`, `ProgressLogger` wired into S2–S5 |
| O3 | `run_id` contextvar + `RunFilter` on the handler |
| C1 | `RunContext.stage`/`.paper` commit per stage and per paper; S6–S8 stay one transaction |
| C2 | `graph.write_paper_graph` — one `execute_write` per paper with `UNWIND` |
| C3 | Same transaction deletes the paper's `MENTIONS`/`SUPPORTS`/`{paper_id}` edges before merging; shared `Entity` nodes retained |
| C4 | `core/reconcile.py`, `python -m app.core.reconcile` |
| C5 | `writer_lock` (PG advisory lock, SQLite lock file for local dev) + `pipeline_runs`; `api/write_guard.py` puts every other write endpoint behind it |
| C6 | `unpaywall.enrich(..., paper_id=...)`, called that way from `header_backfill` |
| P1 | `SemanticStatementClassifier.classify_many`, wired through `extract_sections` |
| P2 | `HeadingClassifier` exemplar cache + `classify_sections`, wired through `parse_and_store` |
| P3 | `EvidenceIndex.upsert` length sort behind `EMBEDDING_SORT_BY_LENGTH` (default off) |
| P4 | `JointEntityRelationExtractor` `batch_extract` + per-chunk fallback behind `NERRE_BATCH_SIZE` (default 1) |
| I1–I6 | `paper_stage_state` / `derived_state` / `nli_pair_scores` tables, per-paper input hashes for S2–S5, `text_sha256`+`embed_version` Qdrant payload with `sync_paper`, S6–S9 fingerprint gate, resumable deletion in `core/maintenance.py`, report snapshots in `explanation/storage.py` |

C3 and C6 are marked "owner decision" in `priority_fix.md` §5. They arrived
already implemented and merged, so they are recorded here as the owner's
standing choice rather than re-opened.

### What this session added

1. **O2 §4.2 completed.** The per-stage report table was only partly covered:
   timers and progress existed, start lines and completion counts did not.
   Added, counts-only per §4.1 (no sentence/paragraph text, no credentials):
   - `pipeline start/complete` in `routes_backend._execute_pipeline`
   - `s2 start/complete` — grobid vs fallback, missing PDFs, backfill
     attempted/succeeded, keyword vs SciBERT heading decisions
   - `s3 start/complete` — sentences, rule hits, semantic calls, facts per type
   - `s4 start/complete` — processed, skipped-no-text, failed, entities, relations
   - `s5 start/complete` — points, papers ready, **skipped_no_document** and
     **skipped_no_graph_node** (§4.2 flags both as "silent today"), failed
   - `s6` — gap points + scroll time, HDBSCAN time + noise count, NLI time +
     pairs scored
   - Model-load lines for SciBERT and GLiNER2 (SPECTER2 and DeBERTa-MNLI
     already had them); per-cluster retrieval/LLM timings at DEBUG in S9
   - Unpaywall now reports how many OA PDFs a backfill attached

2. **Offline benchmark harness** (`core/benchmark.py`, §D step 5) — the
   measurement P3/P4/P6 are gated on, which did not exist. Read-only and
   bounded by `--limit`: `extraction` (P1 label parity + timing), `headings`
   (P2 argmax parity), `embeddings` (P3 padding ratio from the tokenizer alone,
   plus vector drift under `--with-model`), `nerre` (P4 entity/relation set
   parity + timing), `threads` (P6 torch/OMP vs the process CPU budget; reports
   only, never sets). A test asserts the session is left with nothing pending.

3. **`.env.example`** documented `LOG_LEVEL` (required by O1),
   `NERRE_BATCH_SIZE` (required by P4) and `EMBEDDING_SORT_BY_LENGTH`, each
   pointing at the benchmark suite that gates turning it on.

4. **Lint gate restored.** `ruff check app tests` was clean at `75a5c75` and
   returned **54 errors** at `066a437` — README §12 lists it in the verification
   suite. Now 0. Import ordering and unused imports were autofixed; the six
   `B023` closures in `extraction/service.py` and `ner_re/service.py` were fixed
   properly by binding the loop variables as default arguments, since those
   callables are handed to `run.paper` and outlive the iteration that built them.

### Deliberately not implemented

- **P5** (batching across papers) — `priority_fix.md` §2.5 defers it until the
  incremental design exists and notes it conflicts with per-paper prune.
- **P6** thread tuning — benchmark-only by the plan, and setting thread counts
  is a machine/container change (CLAUDE.md resource safety). The harness
  reports; the owner decides.
- **§1.2 Steps 0–5 and §D step 3** — freeze, `pg_dump`, Qdrant snapshot, Neo4j
  dump, `/reports` export, finding the SIGKILL cause, the read-only store
  inspection, the recovery run, and the C3-vs-clean-rebuild call. All owner
  actions against live services; Docker was unreachable this session.
- **Running the benchmarks** — needs the model caches and a populated corpus.

### Verification

`backend`: 966 passed, 10 deselected (945 before; +14 benchmark, +7 telemetry).
`ruff check app tests`: clean. `evaluation/tests`: 4 passed.
`frontend`: 14 passed, `npm run build` and `npm run lint` clean.
Benchmark CLI smoke-tested: `threads` against this machine, and
`extraction`/`headings`/`embeddings` against a throwaway in-memory corpus.
No real-service run: the Docker daemon was not reachable.

---

## 2026-09-15 — D-429-03: a 429 with no Retry-After is not retried

### The diagnostic that prompted it

One direct request to `export.arxiv.org/api/query` from inside the backend
container, using **no ScholarLens code** (bare `urllib`, no retry, identical
URL), returned `429 Rate exceeded.` in 0.36s. Headers: `server: Google
Frontend`, `via: 1.1 google, 1.1 varnish ×3`, `X-Cache: MISS, MISS, MISS`,
14-byte `text/html` body, and **no `Retry-After`, no `X-RateLimit-*`**. Same
container answered Semantic Scholar, ACL and Unpaywall fine; single uvicorn
process; no proxy env vars.

That is an IP-level block at arXiv's edge, independent of ScholarLens: one
request from a process with no prior arXiv traffic cannot exceed a rate limit.
**No code change can make arXiv answer 200 while that block stands.**

### What the old behaviour cost

`ARXIV_NUM_RETRIES=2` turned that one refusal into **three** requests spaced
10s and 20s — against a host refusing all of them — and added ~30s to every
`/collect`.

### The change

`send_with_retries` gained `retry_429_without_retry_after: bool = True`.
Default True, so **Semantic Scholar is byte-identical** (same opt-in pattern as
D-TIMEOUT-01's `retryable_exceptions`). `PoliteSession` passes `False`, so both
arXiv paths — metadata and PDF — stop retrying a 429 that names no Retry-After.

Deliberately **not** a shorter cooldown: declining to retry defers the shared
limiter by the full `backoff_max_seconds` (60s default), which is *longer* than
the exhausted-retry path's final `defer(40)`. Strictly fewer requests, strictly
longer wait.

Untouched: a 429 that *does* carry Retry-After still retries; every 5xx still
retries; transport failures keep D-TIMEOUT-01's budget; the limiter, spacing
floor, User-Agent/contact, PDF pacing, ≤10 caps and per-source isolation are
unchanged.

### Result

A throttled run makes **one** arXiv request, records
`arxiv: failed: HTTPError: ... 429`, and continues with Semantic Scholar and
ACL exactly as before.

### Files

`backend/app/ingestion/rate_limit.py` (opt-in parameter),
`backend/app/ingestion/sources/arxiv.py` (opts in),
`backend/app/core/config.py` + `.env.example` (document the contract). No
change to routes, Unpaywall, Semantic Scholar, ACL, caps or dedup.

### Verification

TDD: 11 tests red first. The end-to-end route test was additionally proven to
catch the regression by temporarily restoring the old flag — it failed
`assert 3 == 1` — then the flag was restored. Full backend suite: **913 passed,
10 deselected** (899 before). `ruff check app tests` clean.

---

## 2026-09-15 — arXiv PDF downloads brought under the shared limiter; all-fields query

Follow-up to the owner-requested arXiv integration audit. The audit confirmed
the metadata path is compliant (correct endpoint, ≥3s floor, bounded retries,
ScholarLens User-Agent, library retry/delay off) and found the real gap on the
**full-text** path, which had been flagged as "known and not changed" since
2026-09-13 and never fixed.

### Root cause: arXiv PDFs bypassed every politeness control

`arxiv.fetch_pdf` → `corpus.download_pdf` used a bare `httpx` client. It took
no turn on the limiter, sent httpx's default User-Agent, had no backoff and no
Retry-After handling. So one `/collect` sent **one paced** metadata request to
`export.arxiv.org` and then **up to ten unpaced** requests to `arxiv.org`,
back to back — plus any arXiv-hosted copy Unpaywall resolved afterwards. That
is the only traffic ScholarLens sends that is out of line with arXiv's
"play nice" guidance, and the most plausible trigger for an IP-level throttle
that then also answers 429 to the compliant metadata requests.

### D-PDF-01 — one session, one limiter, for every arXiv request

`sources/arxiv.download_pdf` streams the PDF through the same `PoliteSession`
and the same process-wide `LIMITER` the API requests use: ScholarLens
User-Agent with the configured contact, spacing at `ARXIV_MIN_INTERVAL_SECONDS`
(≥3s floor unchanged), and `send_with_retries`' existing bounded budget
honouring Retry-After. Metadata and PDF traffic now share one queue and cannot
form independent bursts, and a 429 answered to a PDF defers the limiter for the
*next* arXiv request of either kind. Retry counts were **not** raised.

Failure is graceful and never raised: a 404, a 429 that outlived the retry
budget, an active `CooldownActive` cooldown, or a transport error all return
`False`, leaving that paper as a metadata-only row while the run continues.
`corpus.download_pdf` is untouched and still serves every non-arXiv OA host —
arbitrary legal OA locations are deliberately not paced by arXiv's limiter.

`unpaywall.enrich` dispatches on `arxiv.is_arxiv_url(url)` (parsed hostname,
never a substring, so `arxiv.org.example` is not arXiv) and defaults to the
polite downloader, so no call site can silently fall back to an unpaced fetch.
`/collect` injects one downloader carrying the run's Settings into both the
discovery and the enrichment path.

**Cost:** a full 10-paper arXiv run is now 11 paced requests — roughly 50s at
the 5s default rather than a burst. That is the intended trade.

### D-QUERY-01 — the topic is sent as an explicit all-fields phrase

`search_query=prompt injection` left both the field and the word grouping to
arXiv's parser. It is now `all:"prompt injection"` — every field, the phrase as
typed. Embedded double quotes are dropped (arXiv documents no escape for one
inside a quoted phrase; left in they close the phrase early). Relevance sorting,
descending order, `start`, `max_results` and URL encoding are unchanged.
**This is a search-relevance fix and does not address the 429.** Trade-off:
a phrase query is narrower than AND-ing the topic's words, so a long topic may
return fewer arXiv hits — the alternative was rejected because AND-ing invents
an operator the user did not type and would match papers sharing only stray words.

### Verified unchanged (no code needed)

* **Per-source caps** — ≤10 each for arXiv/S2/ACL, uploads uncapped, duplicates
  don't consume a slot, no PDF fetched past a cap. Already enforced in
  `_process_capped` and covered by existing tests.
* **Config bounds** — `*_MAX_RESULTS` `le=10`, `ARXIV_MIN_INTERVAL_SECONDS`
  `ge=3.0`; 11 and 2.9 already rejected by tests. Nothing loosened.
* **Failure isolation** — per-source savepoints intact; ACL-failure case added
  to complete the matrix. arXiv remains best-effort, never required.
* **Link-back (audit UNKNOWN → resolved)** — `/papers` returns `has_full_text`
  only; no `pdf_path`, no `FileResponse`/`StaticFiles`, and the frontend renders
  no paper links. Downloaded PDFs are pipeline input only, so arXiv's
  redistribution guidance is already satisfied. Left alone, with a regression
  test guarding the boundary.
* **arxiv 4.0.1** — re-inspected: `self._session.get` at `__init__.py:567` is
  the package's only network call, `num_retries=0` disables its retry
  (`_try_index < 0` is never true) and `delay_seconds=0` its sleep. Package not
  upgraded or downgraded.

### Files

`backend/app/ingestion/sources/arxiv.py` (`is_arxiv_url`, `download_pdf`,
`_polite_session`, `_all_fields_query`, `PoliteSession` log label,
`fetch_pdf(download=...)`), `backend/app/ingestion/unpaywall.py` (host
dispatch), `backend/app/api/routes_ingestion.py` (one configured downloader for
both paths), `.env.example`. Tests: `tests/ingestion/sources/test_arxiv.py`,
`tests/ingestion/test_unpaywall.py`, `tests/api/test_routes_ingestion.py`,
`tests/api/test_routes_backend.py`, `tests/api/test_security.py`.
`corpus.py` and `config.py` unchanged.

### Verification

TDD: every new behaviour's test written and watched fail first (9 red on the
query form, 30 red on the PDF path, 4 red on Unpaywall dispatch, 2 red on the
route wiring). Full backend suite **899 passed, 10 deselected** (852 before).
`ruff check app tests` clean; changed files ruff-formatted. `evaluation/tests`
4 passed; frontend 13 passed. No test makes a real network call.

### Known limitations

* A download interrupted mid-stream leaves a partial file in
  `PDF_STORAGE_PATH`. It is never read — parsing iterates `Paper.pdf_path`,
  which is only set on success — and a later retry overwrites it. Matches the
  pre-existing `corpus.download_pdf` behaviour; not changed here.
* `parsing/header_backfill.py` calls `unpaywall.enrich` without injecting a
  downloader, so an arXiv copy resolved there uses `download_pdf`'s module
  defaults (still polite, still the same shared limiter) rather than the
  operator's configured contact/interval.
* None of this can lift a throttle arXiv is already enforcing on this IP. It
  removes ScholarLens's own contribution to triggering one.

---

## 2026-09-14 — arXiv `ReadTimeout` fixed: transport-level failures now retried

Owner-reported live failure after the per-source-cap change:

```
"arxiv": "failed: ReadTimeout: HTTPSConnectionPool(host='export.arxiv.org',
port=443): Read timed out. (read timeout=30.0)"
```

Semantic Scholar and ACL Anthology were unaffected and were not touched.

### Diagnosis

`export.arxiv.org`, the 30s per-request timeout, and the User-Agent/contact
config are all correct — not the cause. `send_with_retries` (`rate_limit.py`)
only retried **HTTP status codes** in `RETRYABLE_STATUS` (429/500/502/503/504),
checked on the `resp` that `send()` returns. A `ReadTimeout` is a
transport-level exception raised by `send()` itself — TLS/connection
succeeded, the request was sent, but the response never finished within 30s —
so it propagated immediately with **zero retries**, unlike a 429 (already
covered by 2 retries). Confirmed against the installed `arxiv` v4.0.1 source:
`Client.__try_parse_feed` calls `self._session.get(url, headers=...)` with no
timeout of its own (ours supplies 30s via `kwargs.setdefault`), and the
library's own retry wrapper (`_parse_feed`) only catches `HTTPError`,
`UnexpectedEmptyPageError` and `ConnectionError` — never `Timeout`/
`ReadTimeout` — and is a no-op regardless since `Client(num_retries=0)`.

### D-TIMEOUT-01 — `send_with_retries` gained an opt-in `retryable_exceptions` parameter

Default `()`, so any caller that does not pass it — Semantic Scholar's call
site was **not modified** — keeps byte-identical behavior. `PoliteSession`
(arXiv only) now passes `retryable_exceptions=(requests.exceptions.Timeout,
requests.exceptions.ConnectionError)`, so a read/connect timeout or a
connection failure is retried through the same bounded budget
(`num_retries`/backoff) a 429/5xx already gets — same limiter, same
exponential backoff, same `defer()` holding back other callers, but no
Retry-After (there is no response to read one from). Once retries are
exhausted the exception itself is re-raised, not swallowed into a fabricated
answer; `/collect`'s existing per-source exception handling records it as
that source's failure exactly like any other exception, without touching
Semantic Scholar or ACL Anthology.

### Explicitly not changed

Per-source caps (arXiv/S2/ACL ≤10, uploads unlimited), the rate limiter's
spacing/backoff/cooldown logic, the arXiv library's disabled retry/delay
layers, the 30s timeout value, Semantic Scholar, ACL Anthology, Unpaywall,
manual upload, and deduplication.

### Files

`backend/app/ingestion/rate_limit.py` (new `retryable_exceptions` param, opt-in),
`backend/app/ingestion/sources/arxiv.py` (`PoliteSession` opts in); tests in
`tests/ingestion/test_rate_limit.py` (8 new: default behavior unchanged,
retry-then-succeed, backoff doubling, exhaustion re-raises, limiter deferred,
untyped exception not caught, minimum-interval respected) and
`tests/ingestion/sources/test_arxiv.py` (4 new: `ReadTimeout` and
`ConnectionError` retried-then-succeed, repeated timeouts bounded and
re-raised, minimum interval still respected).

### Verification

TDD: all 12 new tests red before implementation (`AttributeError`/no retry
before the `retryable_exceptions` param existed). Full backend suite: 852
passed, 10 deselected (841 before this fix). `ruff check app tests` clean;
changed files ruff-formatted. No live network request made — findings verified
by reading the installed `arxiv` v4.0.1 source directly. Not yet re-verified
against the live `export.arxiv.org` outage; recommend one `/collect` once
rebuilt to confirm the retry recovers a transient timeout in practice, and if
timeouts persist across all retries, that arXiv itself is unavailable right
now (an outage, not a code defect) — the safest next diagnostic step, not a
larger timeout or a bypass.

---

## 2026-09-13 — Per-source caps on `/collect`: at most 10 papers from each automatic source

Owner request: arXiv, Semantic Scholar and ACL Anthology each add at most 10
papers per `/collect` run. Manual uploads stay unlimited. Dedup is unchanged,
and a source with fewer matches returns what it has.

### What was wrong

arXiv and Semantic Scholar were already asked for 10, but the setting allowed
up to 100. **ACL Anthology had no cap at all.** Every export record matching the
topic was written and had its PDF downloaded (`"prompt injection"` matches 70).

### D-CAP-01 — the cap is enforced in `/collect`, on distinct corpus rows

`routes_ingestion._process_capped` stops consuming a source's candidates once
`limit` distinct paper ids have been touched. The check comes before the PDF
fetch, so nothing past the cap is downloaded. A candidate that dedups onto a
row this source already touched is merged as usual and does not use up a slot.
A cross-source duplicate, for example an S2 hit merging into an arXiv row,
*does* count toward the later source's cap because that source did collect the
paper. Source modules are unchanged: arXiv/S2 still request exactly the cap, and
ACL's local search still returns every match. The first N distinct papers are
kept in export order.

### D-CAP-02 — 10 is a hard ceiling in Settings

`ARXIV_MAX_RESULTS` and `SEMANTIC_SCHOLAR_MAX_RESULTS` bounds tightened from
1–100 to **1–10**. The new `ACL_ANTHOLOGY_MAX_RESULTS` defaults to 10 with the
same 1–10 bound. Config can lower a cap but never raise it, so a `.env` value
above 10 now fails Settings validation at startup instead of being clipped
silently. (The local `.env` sets none of these.) The Settings table in the
rate-limiting entry below is superseded for the two `*_MAX_RESULTS` rows.

### Not changed

No over-fetching to backfill duplicates. If arXiv/S2 return a within-source or
already-counted duplicate among their 10, that run keeps fewer than 10. The
response shape of `/collect` is unchanged.

### Files

`backend/app/api/routes_ingestion.py`, `backend/app/core/config.py`,
`.env.example`; tests in `tests/api/test_routes_ingestion.py` (6 new: default
caps plus no PDF fetched past them, lower configured caps, fewer-than-cap,
within-source dup, cross-source merge, 12-file upload uncapped) and
`tests/core/test_config.py` (ACL default/env, 11 rejected for all three).

### Verification

TDD: 10 tests red before implementation. Full backend suite: 841 passed,
10 deselected. `ruff check app tests` clean; changed files ruff-formatted.

---

## 2026-09-13 — Process-wide rate limiting for arXiv and Semantic Scholar

Owner-requested follow-up: the bounded retry below did not stop live 429s from
either API. Goal is a compliant client that never causes its own violations and
handles a temporary 429 correctly — not a guarantee arXiv answers 200. No
bypass of any kind (parallelism, IP rotation, multiple identities).

### What was wrong with pacing

* Spacing lived per `arxiv.Client`, and `search_topic` built a new one on
  every call — two `/collect` runs, or concurrent requests, were never spaced.
* The `arxiv` library retried 429s itself at a flat 3s, dropped Retry-After
  (its `HTTPError` keeps only the status), and sent `arxiv.py/4.0.1` as the
  User-Agent on every request.
* Semantic Scholar had backoff but no spacing across calls or threads.

### D-RL-01 — one shared `RateLimiter` per source (`app/ingestion/rate_limit.py`)

Thread-safe minimum spacing between request starts: a slot is reserved under a
lock and slept for outside it, so concurrent callers queue one interval apart.
`defer()` pushes the next slot back for *every* caller after a 429/5xx. Each
source module owns one module-level `LIMITER`; uvicorn runs one worker, so
that covers every request the backend process makes. Intervals come from
Settings via `/collect`; source modules still never read Settings.

### D-RL-02 — one retry loop, `send_with_retries`

Every attempt, retries included, goes through `acquire()`. Wait before retry
n = max(Retry-After [seconds or HTTP-date], base × 2ⁿ). 403 and other 4xx are
never retried. A required wait above the backoff max ends retrying instead of
retrying early, and is still recorded: a later call inside that cooldown
raises `CooldownActive` without sending anything. Remembered cooldowns are
capped at 1 hour. Logs carry source, status, attempt and wait only — never a
URL or header, so the S2 key cannot appear.

### D-RL-03 — arXiv: `PoliteSession`, library retry/delay switched off

`arxiv.Client(num_retries=0, delay_seconds=0)` with `client._session` set to a
`requests.Session` subclass that applies the ScholarLens User-Agent
(`ScholarLens/<version> (automatic research-gap identification; contact: …)`),
a 30s timeout, and `send_with_retries` to every API request, pagination
included. One retry layer only, so a 429 is never retried twice. Exhausted
retries surface as the library's `arxiv.HTTPError(429)`, which `/collect`
records as that source's failure while the others continue. `_session` is a
private attribute, stable within the pinned `arxiv>=4,<5`; tests exercise the
real client end to end through a fake `requests` adapter, so a change there
fails loudly. `requests` is now declared in pyproject (already installed as
arxiv's dependency).

### Settings (all bounded)

| Setting | Default | Bounds |
|---|---|---|
| `ARXIV_MAX_RESULTS` | 10 | 1–100 |
| `ARXIV_NUM_RETRIES` | 2 | 0–3 |
| `ARXIV_MIN_INTERVAL_SECONDS` | 5 | 3–60 (3 = arXiv's ToU floor) |
| `ARXIV_BACKOFF_BASE_SECONDS` / `_MAX_SECONDS` | 10 / 60 | ≤120 / ≤600 |
| `ARXIV_CONTACT` | unset | ≤200 printable chars, blank = unset |
| `SEMANTIC_SCHOLAR_MAX_RESULTS` | 10 | 1–100 |
| `SEMANTIC_SCHOLAR_MAX_RETRIES` | 3 | 0–5 |
| `SEMANTIC_SCHOLAR_MIN_INTERVAL_SECONDS` | 1.5 | 1–60 |
| `SEMANTIC_SCHOLAR_BACKOFF_BASE_SECONDS` / `_MAX_SECONDS` | 2 / 30 | ≤30 / ≤300 |

### Files

New: `backend/app/ingestion/rate_limit.py`, `backend/tests/ingestion/test_rate_limit.py`,
`backend/tests/ingestion/fake_clock.py`. Modified: `sources/arxiv.py`,
`sources/semantic_scholar.py`, `core/config.py`, `api/routes_ingestion.py`,
`pyproject.toml`, `.env.example`, and tests for both sources, settings and the
route.

### Verification

TDD: new test modules failed to import before implementation. Full backend
suite: 833 passed, 10 deselected. No test makes a network request; all
limiters but one 0.4s wall-clock concurrency check run on a fake clock. Pinned
pre-commit hooks (ruff v0.6.9) pass. **Not yet verified live** — needs a rebuild
and one `/collect` once arXiv's throttle has cleared.

### Known and not changed

* arXiv PDF downloads (`fetch_pdf`, arxiv.org/pdf) are not API requests and
  are not paced by the limiter.
* `_failure_status` still truncates a source's error text at 200 characters.
* The limiter is per process; running uvicorn with several workers would give
  each worker its own.

---

## 2026-09-13 — `/collect` 429s: bounded Semantic Scholar retry, right-sized arXiv requests

Owner-approved fix after a diagnosis-only session. No architectural change: the
per-source savepoints and 200-with-`sources` / 502-when-all-down behaviour of
D-VER-01 are untouched.

### What the diagnosis found

* **Semantic Scholar** — the API key loads and is sent correctly (`x-api-key`);
  a keyed request returned 200. The `/collect` 429 was transient, but
  `search_topic` called `raise_for_status()` on the first answer, so one 429
  failed the source for the whole run. It also sent no `limit`, so S2 could
  return up to 100 papers (and 100 Unpaywall lookups) per run.
* **arXiv** — a single 10-result request still got `429 Rate exceeded.`: IP
  throttling, not page size. But `arxiv.Client()` put its default
  `page_size=100` into the URL to keep 10 results, and retried a 429 three
  more times, extending the throttle. Waiting it out is operational, not code.
* **Count 0** — ACL matches the topic as an exact title/abstract substring:
  `"multilingual prompt injection"` matches 0 records, `"prompt injection"` 70.
  Left as designed.

### D-429-01 — Semantic Scholar retries 429/5xx with capped backoff

Up to `SEMANTIC_SCHOLAR_MAX_RETRIES` (3) retries; each wait honours a numeric
`Retry-After`, otherwise `BASE * 2**attempt`, never above
`SEMANTIC_SCHOLAR_BACKOFF_MAX_SECONDS` (10). Other 4xx (e.g. 403 bad key) fail
immediately. Hand-written loop — no retry library added. `sleep` is injectable
so tests never wait.

### D-429-02 — explicit request sizes, fewer arXiv retries

`SEMANTIC_SCHOLAR_MAX_RESULTS=10` is sent as `limit`. `arxiv.Client` is now
built with `page_size=ARXIV_MAX_RESULTS` (10), `delay_seconds=3.0` and
`num_retries=ARXIV_NUM_RETRIES` (1). All six values are bounded `Settings`
fields passed in by `/collect`; source modules still never read Settings.

### Files changed

`backend/app/core/config.py`, `backend/app/ingestion/sources/semantic_scholar.py`,
`backend/app/ingestion/sources/arxiv.py`, `backend/app/api/routes_ingestion.py`,
`.env.example`; tests in `tests/ingestion/sources/test_semantic_scholar.py`,
`tests/ingestion/sources/test_arxiv.py`, `tests/core/test_config.py`,
`tests/api/test_routes_ingestion.py`, `tests/api/test_security.py` (source
fakes now accept the new keyword arguments).

### Verification

TDD: 24 new tests red before implementation. Full backend suite afterwards:
773 passed, 10 deselected. Pinned pre-commit hooks (ruff v0.6.9) pass on every
changed file. Not yet re-verified live: needs a backend rebuild and one
`/collect` after arXiv's throttle clears.

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

## Admin tooling — corpus reset (owner-requested, outside the phase list)

A dev/admin operation to clear the accumulated corpus without
`docker compose down -v` (which also destroys the Docker volumes). Not in
README §15 — the owner asked for it directly as infrastructure-preserving
tooling, not a pipeline phase.

`app/core/corpus_reset.py` — `reset_corpus(session, neo4j, index,
pdf_storage_path=None)` is the single implementation behind both
`DELETE /admin/corpus` (`app/api/routes_backend.py`, same X-API-Key auth as
every other route on that router) and `python -m app.core.corpus_reset
--confirm`, mirroring the `delete_paper`/`resume_deletions` +
`python -m app.core.maintenance` split already established for single-paper
removal.

Deletion order: PostgreSQL first (children before parents;
`clustering.storage._dependent_tables()` reused for the cluster family so a
future table FK'd to `clusters.id` is covered automatically, same rationale
that function's own docstring gives), then Neo4j (`DETACH DELETE` on the
`Paper`/`Entity` labels — the only two the pipeline ever writes), then Qdrant
(drop the collection outright; it is exclusively ScholarLens evidence and
`ensure_collection()` recreates it lazily), then — only once all three are
**verified** empty — the PDF files, last, using paths captured before the
Postgres delete ran.

The first version of this shipped with PDFs deleted *first* (reasoning: only
Postgres knows which files are "ours," so capture the paths, delete the
files, delete the rows). Owner review caught the real failure mode that
ordering has: if a PDF was deleted but Neo4j or Qdrant then failed, Postgres
still held a `Paper` row with `has_full_text=True` pointing at a `pdf_path`
that no longer existed — an actively wrong corpus record, not just an
unfinished reset. PDF deletion cannot be undone, so it now runs last, gated
behind Postgres/Neo4j/Qdrant all having converged; a `Paper` row can therefore
never survive to reference a PDF this function has already removed. The
residual risk this leaves is milder and accepted: if Neo4j or Qdrant fails
*after* Postgres has already committed, the PDFs are preserved un-deleted
(never destroyed ahead of a failed reset), and a retry's fresh `pdf_path` scan
(Postgres already has none) won't find them either — they become the same
harmless orphan `app.core.reconcile`'s `unreferenced_pdf_count` already
detects and reports, never a corpus record that lies about what it still has.

Every step is otherwise idempotent (an already-clean store is a no-op, not an
error), and `current_run() is None` is rejected the same way `delete_paper`
rejects it, so a reset can never run outside the pipeline writer lock.

Tests: `tests/core/test_corpus_reset.py` (empty/populated/idempotent resets,
Neo4j label-scoped deletion, a real in-memory Qdrant collection drop, managed
vs. stray vs. outside-storage-root PDFs, the writer-lock guard, and — for each
of Postgres/Neo4j/Qdrant in turn — a failure there surfacing rather than
reporting false success while leaving every PDF file untouched) plus
`DELETE /admin/corpus` coverage in `tests/api/test_routes_backend.py` (auth
required, lock contention, and a generic failure returning a detail with no
leaked exception text — matching `/reports`'s existing convention, not a new
one).

## Stage 9 rate-limit resilience (owner-requested, outside the phase list)

A run on the 20-paper corpus completed S2–S8 and then failed with
*"Groq rate limit reached. Retry later or lower REPORT_TOP_K."* The key was
valid and requests had reached `openai/gpt-oss-20b`, so this was metering, not
misconfiguration.

### D-09-09 — the limit hit was tokens per minute, not requests

`report_top_k` is 5 and the loop in `explanation/service.py` is sequential, so
one run makes at most 5 chat-completion calls, well under any request-rate
ceiling. The token arithmetic is what explains it. A Stage 9 prompt measures
~12,000 characters (~3,000 tokens) at the defaults — `report_evidence_top_n`
(8) passages capped at `MAX_EVIDENCE_CHARS` (1200) plus a ~1,500-character
scaffold — and nothing capped the *completion*, which matters because the
default model reasons before it answers and those hidden tokens are metered
too. Five such calls issued back to back land ~20–25k tokens inside one minute
against a free-tier ceiling of 8,000 TPM, so the second or third call is
refused.

This could not be confirmed from the artefacts, because `GroqClient` discarded
the 429's body and headers — exactly where Groq names the ceiling it enforced
and the `Retry-After` it wants. That is now captured (bounded to 300
characters) and surfaced, so the next occurrence is self-diagnosing.

### D-09-10 — reuse `app.ingestion.rate_limit`, don't write a second one

The retry loop the discovery sources already use (`send_with_retries` +
`RateLimiter`) is exactly the required behaviour — bounded retries,
Retry-After honoured over exponential backoff, a shared limiter every caller
defers, and logs that name only source and status. Importing it into
`explanation/` crosses a package boundary that reads oddly, but the
alternative — moving it to `app/core/` — would churn arxiv and
semantic_scholar for no behavioural gain, and a second implementation would
violate hard constraint 8. Reused as-is.

The limiter is **module-level**, so Stage 9 and Mode B `/query` — which spend
one quota — queue behind each other rather than each behaving as the only
caller. Consequence to know about: a 429 carrying a multi-hour `Retry-After`
(a daily quota) parks *every* Groq call in the process for up to
`MAX_COOLDOWN_SECONDS` (3600), `/query` included. That is deliberate — the
quota really is gone — and clears on backend restart.

### D-09-11 — `report_top_k` stays at 5

Lowering K treats the symptom. The cost driver was an uncapped completion and
a 5-call burst, not the number of reports, and K is what turns 27 ranked
clusters into a gap *report* rather than a single example. Instead:
`GROQ_MAX_COMPLETION_TOKENS` (2048) bounds the spend per call, retries absorb
a per-minute refusal, and `GROQ_MIN_INTERVAL_SECONDS` lets a constrained tier
spread the burst across the window while keeping all K reports. The evidence
budget (`report_evidence_top_n`, `MAX_EVIDENCE_CHARS`) was left alone: those
passages *are* the grounding, and trimming them to dodge a quota would trade
the phase's core credibility claim for a smaller bill. `REPORT_TOP_K` remains
the documented first lever if a deployment still needs one.

### D-09-12 — one error class per operator action

"LLM provider unavailable" was the answer to a rejected key, an unknown model
and an exhausted quota alike. Split into `LLMRateLimitError` (429, carrying
`retry_after`), `LLMAuthError` and `LLMConfigurationError` (both plain
`LLMError` — *not* "unavailable", because waiting cannot fix either), and
`LLMUnavailableError` for genuine outages. `_llm_http_error` maps these to
429 + `Retry-After` / 502 / 503, so the status code itself tells the caller
whether to wait or to change configuration.

### S9 is resumable — it always was

`POST /reports` calls `generate_top_reports` directly against the stored
clusters, rankings and vectors; no S2–S8 path is involved, and the pipeline's
`session.rollback()` on an LLM failure discards only uncommitted S9 work. What
was missing was any *indication* of this: the error said "reports were not
generated" and left rebuilding as the obvious next move. The message now names
`POST /reports` and states the evidence base is intact, and
`tests/explanation/test_service.py` pins the property
(`test_a_metered_run_is_resumable_against_the_same_evidence_base`).

The one rough edge left unchanged: a successful `POST /reports` does not mark
the `s9` derived checkpoint, so a later `/pipeline/run` regenerates the same
reports. Harmless but wasteful; noted rather than fixed, since the checkpoint
digest is computed inside `_execute_pipeline` and plumbing it out is more
coupling than the problem is worth.

Also unchanged: a run cut short still discards the drafts it completed
(`replace_reports` runs once, at the end, so the previous run's reports
survive — Phase 9's documented decision). The completed count is now logged so
the cost of the retry is visible.

### SPECTER2 adapter warning — harmless, no change

`"There are adapters available but none are activated for the forward pass"`
is emitted twice by `adapters.model_mixin.forward_context` *during*
`load_adapter`, before `set_active=True` has taken effect — which is why it
appears just above our own "Loaded SPECTER2 … (768-d)" line. Verified
directly against the cached checkpoints: after `load_adapter` returns,
`model.active_adapters` is `Stack[specter2]`, and a forward pass with the
adapter active differs from the same pass with it deactivated (L2 6.58,
cosine 0.956). Embeddings are genuine 768-d SPECTER2 proximity vectors. The
warning describes the loader's own internal pass, not ours. Left untouched.

### Settings added

`groq_max_retries` (4), `groq_backoff_base_seconds` (2.0),
`groq_backoff_max_seconds` (90.0 — above a minute on purpose, so a TPM ceiling
is waited out and a daily quota is not), `groq_min_interval_seconds` (0.0, opt
in), `groq_max_completion_tokens` (2048). All documented in `.env.example`.

### Tests

`tests/explanation/test_llm_rate_limit.py` (new, 22 tests on a fake clock —
retry/backoff, Retry-After precedence, bounded budget, over-budget waits
reported rather than slept on, the five error classes, completion cap and
truncation, proactive pacing, the shared limiter, and no key or bearer header
on any log path); rate-limit/auth/config/outage status mapping and the
rate-limited `/pipeline/run` message in `tests/api/test_routes_backend.py`;
resumability and mid-run progress in `tests/explanation/test_service.py`; the
new settings plus `REPORT_TOP_K`-from-env in `tests/core/test_config.py`.
`tests/explanation/test_llm.py` was updated, not just extended: its fakes
predated status-code classification, and its 429 case had started sleeping for
real once retries existed. Full backend suite green (1045); `ruff check`
clean.

---

## Gap-evidence validity fixes (owner-requested, outside the phase list)

Triggered by manual validation of Gap #1 on the 20-paper LLM-security corpus.
The stored report read:

> "Despite frequent prompt injection attacks, there is a lack of effective,
> adaptive defenses and inconsistent findings across studies, leaving LLMs
> vulnerable."

gap_score 11.01, confidence 70%, "Conflicting evidence". The diagnosis found
three independent defects, all confirmed against the live database:

1. **`contradiction=True` was a false positive.** Four stored pairs, all
   sharing one right-hand statement -- an ethics/availability disclaimer
   ("All attack templates are derived from the publicly available BIPIA
   benchmark ... no attack tooling is released") -- scored against a
   recommendation bullet, a related-work description, a future-work proposal
   and one genuine limitation. DeBERTa-MNLI scored all four above P=0.999.
   None is a disagreement. The single boolean contributed 5.0 of the 11.01.
2. **`solution_density` was structurally 0.00**, for all 27 clusters. The rule
   required a contribution sentence containing *every* term of the label; the
   top gap's label is "prompt attacks injection", and no contribution sentence
   in the corpus contains all three. The `- w3 * density` term was decorative.
3. **The evidence base was one paper.** Stage 9 retrieved 8 passages, 7 from
   one study and 1 from a paper not in the cluster; 5 of the cluster's 6 papers
   were never quoted. The report's "56% of tests" was that one study's own
   experiment, presented as the field's position.

### D-06-10 -- a contradiction needs two *claims*, and non-claims never reach the model

`app/clustering/claims.py` rejects headings, bullets, ethics/availability
disclaimers, section cross-references, related-work descriptions and
future-work proposals before pairing. A statement that asserts nothing cannot
be disagreed with, so it is not worth a forward pass either -- the filter runs
before `candidate_pairs`, not after the model.

Four of the five statements behind cluster 1's flagged pairs are rejected
outright (bullet / disclaimer / related_work / proposal); the fifth is a real
claim, and the topic gate drops its pairs instead.

**A finite-verb rule was written and removed.** Any closed list of verbs is a
list of the verbs its author thought of, and this project's own hand-labelled
genuine contradiction ("Cross-lingual transfer fully recovers accuracy for
low-resource languages") was rejected by it because "recovers" was not on the
list. Distinguishing a third-person "recovers" from a plural noun needs a POS
tagger this project does not carry. Losing a real disagreement is worse than
admitting a noun-phrase fragment, which `too_short` and `structural` already
catch and which still has to clear the topic and NLI gates.

### D-06-11 -- the topic gate is lexical overlap, **not** SPECTER2 cosine

Cosine was the obvious choice and was measured first, on the stored corpus,
before any code was written. Over the 239 cross-paper pairs inside stored
clusters it ran 0.836 - 0.975, median 0.923, and the four known false positives
sat at 0.89 - 0.93, the middle of the distribution. The *most* similar pairs of
all were boilerplate ("DISCUSSION AND LIMITATIONS" against "This study has
several limitations", cosine 0.975). SPECTER2 is a document-level model; on
single sentences from one field everything looks alike, and a cosine gate would
have selected *for* boilerplate.

Shared stemmed content terms separate the same pairs cleanly: the four false
positives share 0-1 terms (Jaccard <= 0.042), while `shared >= 3 AND
Jaccard >= 0.12` admits 3.8% of cross-paper pairs. Both bounds are required --
long sentences from one field clear the count on shared jargon, and two
fragments clear the ratio on a single word.

### D-06-12 -- both NLI directions, and corroboration across paper pairs

NLI is asymmetric, so one direction over the threshold is as easily an ordering
artefact as a disagreement; both must now clear `contradiction_threshold`. The
stronger direction is still what gets *stored* -- D-06-05 is unchanged for
scoring, this is a gate on whether the pair counts.

A cluster is flagged only when `contradiction_min_paper_pairs` (default 2)
distinct **paper** pairs disagree. Cluster 1's "four pairs" were four statements
against one, from two papers; counting statement pairs is what made one
disclaimer look like four independent disagreements. `detect()` returns `[]`
when corroboration fails, so nothing is stored and there is no flag to explain.

**Known cost, deliberate:** a genuine disagreement between exactly two papers is
not flagged at the default. On a 20-paper corpus that is the commonest shape a
real contradiction takes *and* the commonest shape a false positive takes, and
the corpus supplied four of the latter and none of the former. Configurable via
`CONTRADICTION_MIN_PAPER_PAIRS=1`.

### D-08-10 -- the contradiction signal is graded on breadth, not a boolean

`contradiction_signal(pairs, min_paper_pairs, full_strength)` returns `0.0`
below the corroboration floor and `pairs / full_strength` (clamped) above it,
so two disagreeing paper pairs earn two thirds of `w5` rather than all of it.

D-08-06's reasoning is unchanged: the NLI *probability* still does not enter
the score, because a 0.71 and a 0.99 both mean "these papers disagree" and
substituting it would re-weight `w5` by how sure a transformer felt. What does
distinguish a strong disagreement from a weak one is how much of the literature
takes part, and that is a count the pipeline can defend. Stage 8 re-checks the
floor rather than trusting the stored flag, so a ranking cannot inherit a flag
raised by an older run under looser settings.

### D-08-11 -- solution density splits "is this on-theme" from "is this a remedy"

Supersedes D-08-04's every-term rule, which scored 0.00 for all 27 clusters. A
paper counts as a solver when **both** hold:

* **on-theme** -- it is one of the cluster's own papers, or a contribution
  shares >= `gap_solution_min_label_overlap` (2) stemmed terms with the label;
* **proposes a remedy** -- a contribution matches a narrow solution cue
  (defen[cs] / mitigat / safeguard / countermeasur / harden / protect /
  prevent / neutraliz / sanitiz / remediat / robust / resilien / guard /
  detector / detection / filter / firewall / shield).

Lexical overlap is a poor test of "did you solve it" but an adequate test of
"are you working on this", and cluster membership answers the second outright.
Membership is what finally recognises Sentra-Guard, a prompt injection defense
whose contributions say "neutralized more than 98.7% of adversarial attempts"
and share *no* word with the label "prompt attacks injection".

The cue list is deliberately narrow. Measured on the corpus, a list that also
included "propose", "present", "introduce", "framework", "method" and
"benchmark" marked a solver in 26 of 27 clusters and drove density to 1.00
almost everywhere, which makes the term a constant. The narrow list produced a
real spread (0.00 - 1.00, most clusters between). D-08-04's corpus-wide
numerator is preserved: a paper that solved a problem rarely lists it as its
own limitation.

### D-09-04 -- evidence is capped per paper and prefers the cluster's own papers

Retrieval widens to `report_evidence_top_n * report_evidence_oversample` (8x4),
then `app/explanation/selection.py` selects breadth-first -- every paper's best
passage before any paper's second -- preferring the cluster's papers, capped at
`report_evidence_per_paper_cap` (3) each. Without the headroom the cap could
only ever *remove* passages; with it, a crowded-out paper can be promoted.

It is a preference, not a filter: a passage from outside the cluster is still
real retrieved evidence and a thin cluster needs it. Qdrant still ranks;
nothing here re-scores. `evidence_summary` reports passages and unique papers
separately, in the prompt and on `GET /reports`, because eight passages from
one study and eight from eight studies are not equally supported and only the
second number tells them apart.

### D-09-05 -- (A), (B) and (C) are judged separately, and grounded in code

The prompt asks for `problem_evidence` (the problem exists),
`solution_insufficiency_evidence` (existing solutions fall short) and
`research_gap_evidence` (a specific gap remains) as separate cited claims, plus
`conflicting_evidence`, and states that evidence for (A) does not establish (B)
or (C). "Prompt injection remains common" is a reason to believe prompt
injection remains common.

The prompt is not the enforcement. `GroundedClaim` downgrades any claim
asserted with no citation, and `service.py` re-grounds every claim against the
ids actually retrieved, so a claim whose citations were invented becomes
unsupported before it is stored. All four default to unsupported, so a provider
that omits them reads as "not established" rather than "all of it holds".

### D-09-06 -- pipeline measurements are heuristics, not facts

The old prompt said "computed from the corpus -- treat these as facts", and the
model duly wrote "inconsistent findings across studies" out of a
`contradiction_flag` of 1.0 with no opposing passage anywhere in its evidence.
They are now labelled heuristics, the contradiction measure is named as one
known to produce false positives, and the prompt states that where the
measurements and the passages disagree, the passages win.

### Confidence is unchanged, and still not calibrated

Per the owner's instruction the confidence field remains the LLM's own number.
The prompt now tells the model to lower it when evidence comes from few unique
papers, is dominated by one paper, lacks direct support for (B) or (C), rests
on an unconfirmed contradiction, or is incomplete, and the EVIDENCE BASE line
gives it the numbers to do that with. **This is an LLM-generated estimate, not
a statistically calibrated probability**, and is documented as such in
`frontend/src/types.ts`. A deterministic confidence remains open.

### Expected effect on the stored corpus (simulated read-only, not yet rerun)

* All four cluster-1 pairs are dropped, by the claim gate *and* independently by
  the topic gate.
* Across all 27 clusters, at most **one** candidate paper pair survives the
  gates in any cluster, so at `CONTRADICTION_MIN_PAPER_PAIRS=2` **no cluster in
  this corpus will be flagged**. "Conflicting evidence" disappears from the
  dashboard for this corpus. That is the honest reading of the evidence, not a
  bug.
* `solution_density` becomes non-zero for 17 of 27 clusters (0.25 - 1.00).
* **Consequence to watch:** `w3 = 25` was set when density was always 0. At
  density 0.33 it now subtracts 8.33 against a mention_frequency of ~6, so the
  ranking is dominated by this term and cluster 1 falls out of the top 5
  entirely (projected ~-2.3). The weight was **not** tuned here -- the owner
  asked for no untested weight changes -- but recalibrating
  `GAP_WEIGHT_SOLUTION_DENSITY` is now the live question, and it is an S8-only
  rerun.

### Files

New: `app/clustering/claims.py`, `app/explanation/selection.py`.
Changed: `app/clustering/nli.py`, `app/ranking/signals.py`,
`app/ranking/service.py`, `app/explanation/prompt.py`,
`app/explanation/schema.py`, `app/explanation/service.py`,
`app/explanation/storage.py`, `app/core/config.py`, `app/core/schema.py`,
`app/api/routes_backend.py`, `frontend/src/types.ts`, `.env.example`.

### Settings added

`CONTRADICTION_MIN_CLAIM_WORDS` (8), `CONTRADICTION_MIN_SHARED_TERMS` (3),
`CONTRADICTION_MIN_TOPIC_JACCARD` (0.12),
`CONTRADICTION_REQUIRE_BOTH_DIRECTIONS` (true),
`CONTRADICTION_MIN_PAPER_PAIRS` (2),
`CONTRADICTION_FULL_STRENGTH_PAPER_PAIRS` (3),
`GAP_SOLUTION_MIN_LABEL_OVERLAP` (2), `REPORT_EVIDENCE_PER_PAPER_CAP` (3),
`REPORT_EVIDENCE_OVERSAMPLE` (4). The Stage 8 knob carries the `gap_` prefix so
the pipeline's derived-state fingerprint already watches it.

### API (additive, backward compatible)

`GET /clusters` gains `contradictions[]` with both statements' ids, paper ids,
text and score, so "Conflicting evidence" can be explained rather than
asserted. `GET /reports` gains `justification` (the grounded A/B/C) and a
derived `evidence_summary`. `gap_reports.justification` is a nullable JSON
column with an additive `ALTER TABLE ... IF NOT EXISTS` for existing volumes.
No existing key changed; the frontend was not redesigned.

### Tests

New: `tests/clustering/test_claims.py` (25), `tests/clustering/test_nli_gates.py`
(17, driven by cluster 1's real statements against an NLI stand-in that scores
every pair 0.9995), `tests/ranking/test_signals_v2.py` (18, Sentra-Guard's real
contribution sentences), `tests/explanation/test_selection.py` (19),
`tests/explanation/test_justification.py` (20),
`tests/explanation/test_service_diversity.py` (10, end to end through
`generate_top_reports`).

Updated rather than merely extended, because their fixtures encoded the old
behaviour: `test_nli.py` (its stand-in contradicted in one direction only, and
`statements[:2]` no longer clears the topic gate), `test_service.py` (the same
one-directional stand-in), `test_signals.py`, `tests/ranking/test_service.py`,
`tests/ranking/test_api_contract.py`, `tests/explanation/test_schema.py`,
`tests/explanation/test_service.py`, `tests/core/test_schema.py`,
`tests/explanation/test_api_contract.py`.

Backend 1160 passed, `ruff check app tests` clean, frontend `tsc --noEmit`
clean and 19 vitest tests green.

**Two pre-existing failures remain and were not touched**:
`tests/ner_re/test_extractor.py::test_batch_extraction_matches_single_pass_results`
and `::test_batch_extraction_falls_back_without_dropping_chunk_order`. Both are
in the uncommitted Stage 4 JointIE batching work, fail on entity content from
the batch path, and are independent of everything changed here.

### D-06-13 -- non-research statements are filtered at the S6 input boundary

The read-only ranking audit found ~11 of 27 clusters were not themes at all:
reference-list entries ("In Findings of the Association for Computational
Linguistics: EMNLP 2023, pages 12365-12394, Singapore."), bibliography author
lists ("Zhengbao Jiang, Antonios Anastasopoulos, Jun Araki, ..."), citation
fragments ("2023.", "(Ghosal et al., CVPR 2025)"), acknowledgements and funding
lines ("the PaliGemma Academic Program award from Google"), and section
cross-references. Stage 2/3 extracted them as `limitation` statements and Stage
5 embedded them.

They did not merely clutter the ranking, they *won* it: four reached the
projected top 10, because a cluster of author names has no on-theme solver and
so loses nothing to `solution_density`. The density fix was rewarding
extraction noise.

**Boundary: `load_gap_statements`.** It is the single place Stage 6 reads its
input and the only consumer of `gap_points()`. Filtering there is downstream of
Stages 2-5, so nothing is re-parsed, re-extracted or re-embedded and the
vectors stay in Qdrant exactly as Stage 5 wrote them -- the filter is
reversible by changing one setting rather than by re-running an expensive
stage. It is upstream of everything derived, so clusters, labels, member
counts, Stage 7's per-year counts and Stage 8's signals are all fixed at once.

**A new predicate, not `claim_rejection`.** That one answers "could another
paper contradict this?" and rejects proposals -- and a future-work statement is
a proposal, so reusing it here would have discarded half of what README S8
Stage 6 exists to cluster. `research_statement_rejection` answers the weaker
"is this research, or is it document furniture?" and rejects only: too_short
(below `clustering_min_statement_words`, default 6), heading (all-caps),
bibliography, author_list, acknowledgement, structural, disclaimer. It does
*not* reject proposals, imperatives, related-work or bullets.

**Three over-rejections found and fixed during the work**, all from reusing the
contradiction filter's patterns wholesale:

* `supported by` matched "claims that are not supported by the retrieved
  context" and dropped this project's own hand-labelled hallucination
  limitation. A funding sentence is now caught by its funder instead.
* `^(section|table|figure)` dropped "Table 4 shows that accuracy drops sharply
  for low-resource languages". The cross-reference rule now requires the
  structural verb as well as the opener.
* `ethic(s|al)` and `publicly available` dropped "the ethical implications of
  deploying these models remain underexplored" -- in an AI-safety corpus that
  is a research gap -- and "Our dataset is publicly available and covers
  fourteen languages", a contribution claim. Only unambiguous conduct
  boilerplate (consent, IRB, "does not reflect the view of the authors") is
  matched now.

Each is pinned by a test in `TestOverRejectionGuards`.

### Settings added

`CLUSTERING_MIN_STATEMENT_WORDS` (6). Carries the `clustering_` prefix so the
pipeline's derived-state fingerprint invalidates S6 when it changes. Lower than
`contradiction_min_claim_words` (8) on purpose: clustering needs enough words
to carry a theme, contradiction needs a full assertable claim.

### Files

Changed: `app/clustering/claims.py` (the new predicate and its patterns),
`app/clustering/service.py` (the boundary), `app/core/config.py`,
`.env.example`. New tests: `tests/clustering/test_research_statements.py`,
`tests/clustering/test_input_filter.py`. Updated fixtures:
`tests/clustering/test_service.py`, `tests/core/test_telemetry.py` (its
placeholder text "statement 0" is now correctly filtered as too_short).

Backend 1228 passed, 2 pre-existing `ner_re` failures unchanged; `ruff check
app tests` clean. No frontend change.

### Stages that must be rerun

S6 -> S7 -> S8 -> S9. Not S1-S5: the corpus, PDFs, parses, facts, graph and
embeddings are untouched, and no vector is re-encoded. S6 changes detection, S7
must follow because `replace_clusters` wipes dependent tables and cluster ids
are not stable across rebuilds, S8 depends on both, S9 depends on S8.

## 2026-09-23 -- Pre-reset engineering audit (code-level only)

Scope: one consolidated code audit before the owner's clean corpus reset. No
corpus reset, no collection, no S1-S9 run, no LLM call. The existing corpus
and cached ACL export were inspected read-only.

### D-S2-TYPES-01 -- Semantic Scholar rejects clearly non-research records

`semantic_scholar.search_topic` now requests `publicationTypes` and drops a
record tagged `Editorial`, `News`, `LettersAndComments` or `CaseReport` --
unless it is also tagged `Study` or `Review` (explicit research content wins;
`JournalArticle`/`Conference` name a container, not content, so they do not
rescue). Missing/null/empty types are accepted. No title/abstract/PDF text is
read. So rejected records never take a slot, the single search request asks
for `SEMANTIC_SCHOLAR_REQUEST_LIMIT` (default 20, 1-100, never below the cap)
and the first `limit` accepted records are returned in relevance order -- no
extra request, no pagination.

### D-15-02 -- ACL track markers are also read in parenthesised volume labels

Surveying the cached 127,839-record Anthology export showed the D-15-01 regex
missed every `"(Volume 3: System Demonstrations)"` / `"(Volume 5: Tutorial
Abstracts)"` venue (the final-colon segment ends in `)`) and NAACL 2025's
`"...Technologies (System Demonstrations)"`: all ACL 2023-2026, EACL 2024 and
NAACL 2024/2025 demo and tutorial volumes. The marker list is unchanged; only
those two placements are now read. Rejections in the export: 1,226 -> 1,655,
all of them the six listed markers. Not added (no owner decision yet):
"Demonstrations", "Demonstration Papers", "Tutorials", keynotes, invited
talks, panels.

### D-PDF-02 -- a non-PDF body is never stored as full text

The corpus held `14.pdf` = a Scientific Reports "JavaScript is disabled"
HTML page resolved by Unpaywall (`best_oa_location.url`), flagged
`has_full_text=True`, parsed by the fallback into a one-sentence document and
never retried. `corpus.download_pdf` (ACL + non-arXiv Unpaywall) now requires
`%PDF-` within the first 1024 bytes, and writes to `<dest>.part`, moving it
into place only on success -- /collect fetches before dedup, so re-collecting
downloads onto a stored paper's existing file and a bad second fetch must not
destroy it.

### Operational note

The running backend image predates the ACL filter (`is_non_research_track`
was absent in the container). `docker compose up -d --build backend` is
required before recollection.

### Tests

New: `tests/ingestion/sources/test_semantic_scholar_publication_types.py`
(23); additions to `test_acl_venue_validation.py` (15), `test_corpus.py` (7),
`test_routes_ingestion.py` (1 end-to-end S2 cap test; pass-through test
extended). Backend 1297 passed, `ruff check app tests` clean. The two
previously failing `ner_re` batch-extraction tests now pass (untouched here).

## 2026-09-23 -- S9 reliability after the first clean baseline

Baseline S9: 5 ranked gaps attempted, 2 reports stored, 3 failed (clusters 5
and 7: HTTP 400 "Failed to validate JSON"; cluster 9: `finish_reason:
length`), yet `/pipeline/run` answered a plain 200. No corpus data, S1-S8 code
or weights were touched; no Groq call was made.

### D-09-07 -- output failures are classified, diagnosed and budgeted

* Model: `openai/gpt-oss-20b` (config default), `response_format:
  json_schema`, `strict: false` (Groq best-effort mode: the finished
  generation is validated and a mismatch answers 400 `json_validate_failed`).
  The request shape is compatible -- the same model/schema/request produced
  the other two reports -- so "check GROQ_MODEL" was a misdiagnosis. A 400
  `json_validate_failed` is now `LLMOutputError` (per report, not retried);
  the message says whether `failed_generation` was unparseable (cut off) or
  a finished object missing keys, and the generation itself is only logged,
  bounded (200-char head/tail), never returned by the API.
* Truncation is `LLMTruncatedError(LLMOutputError)` and reports the reasoning
  share of the completion when Groq sends `usage`. Every generation logs
  `groq usage prompt= completion= reasoning= finish= cap=`.
* Budget, measured: stored report JSON ~500-700 tokens, ~1.2K worst case
  (UUID evidence ids repeated across five id lists); prompt ~2.4-2.6K,
  ~4K worst case. 2048 left <~900 tokens for gpt-oss reasoning, which is
  billed against the same cap. Default raised to 4000: ~2.8K reasoning room,
  and worst-case prompt + cap stays within an 8K-TPM request (Groq answers
  413 when prompt + cap cannot fit; 413 now has its own message).
* Most likely cause of the two 400s (not proven -- `failed_generation` was
  not logged at the time): the same budget exhaustion, with the JSON cut off
  after reasoning finished, then failing validation. All three failures
  returned ~5s after their paced slot. The next run's `failed_generation` /
  `usage` log lines settle it.

### D-09-08 -- S9 reports its outcome and resumes only what failed

* `generate_reports(...) -> ReportOutcome`: requested, stored, reused,
  failures (retryable: output/validation/ungrounded) and skips
  (deterministic: no evidence / no label). Status is `complete` (no
  failures), `partial` or `failed`. `generate_top_reports` is its ids-only
  wrapper. An auth/configuration error now stops the pass after one call.
  A pass that stores nothing leaves existing reports untouched.
* `/pipeline/run` and `POST /reports` gain an additive `report_generation`
  block; HTTP status is unchanged (200 -- the evidence base is built).
* The `s9` marker is written only for a complete pass; a partial pass writes
  `s9-partial`, and the next run resumes (`resume=True`) -- reusing stored
  reports and generating only the missing ones. The report digest gained
  `s9-outcome-v2`, so the baseline's `s9` marker (written after 2/5) is no
  longer current and the next run regenerates S9 (all five, once).
* Frontend: `PipelineResult.report_generation` typed; the pipeline card shows
  a warning for `partial`/`failed`. No redesign.

## 2026-09-24 -- S1 final relevance gate (D-S1-GATE-01)

Source results are candidates, not proof of relevance. `/collect` now judges
every candidate before it can enter `papers`, and records every decision.
S2-S9 untouched; corpus not reset or recollected.

* **Gate** (`app/ingestion/relevance.py`, gate `relevance-gate-v1`): topic
  split into facets through the versioned lexicon
  (`app/ingestion/concept_lexicon.json`, `2026-09-24.1`); unknown words are
  word-boundary facets. Order: no text -> `insufficient_text`; unevidenced
  facet whose head word appears in another sense ("error injection") ->
  `sense_mismatch:<facet>`; unevidenced facet with no abstract ->
  `insufficient_text`; unevidenced facet -> `missing_facet:<facet>`;
  cross-encoder (raw logit, `RELEVANCE_MODEL`) below
  `RELEVANCE_SEMANTIC_FLOOR` -> `low_semantic_relevance`; else
  `relevant:strong` (>= `RELEVANCE_STRONG_THRESHOLD`) / `relevant:conceptual`.
  Only facet-complete candidates are scored, in one batch.
* **Calibration caveat:** floor -1.0 and strong 3.5 were calibrated on ONE
  topic ("multilingual prompt injection", 50 hand-labelled documents). They
  are settings, not validated constants.
* **Audit:** `collection_runs` (topic, facets, gate/lexicon versions,
  settings, source status, counts) + `candidate_decisions` (every candidate:
  admitted / rejected / capped, reason code, reason, score, tier, evidence,
  paper id). `GET /collections/{id}/decisions` reads it without the write
  lock. Corpus reset clears both tables.
* **Caps and pools:** arXiv pool 30 (one request), Semantic Scholar 40, ACL
  up to 100 local matches; admitted cap per source stays 10. Admitted
  candidates are ranked by gate score before the cap; rejected ones never
  take a slot; admitted-but-over-cap are recorded as `capped` /
  `source_cap_reached`. A rejected copy of a paper already admitted for the
  same topic is `duplicate_of_admitted`; a rejected candidate never modifies
  an existing row.
* **ACL candidates:** legacy all-terms rule OR the gate's facet evidence
  (normalized-phrase prefilter), so "machine translation ... prompt
  injection" is found; legacy-only matches stay candidates so their
  rejection is recorded.
* **Uploads:** `owner_upload` decision under an `upload` run; no gating.
* **Offline evaluation (50 docs, real cross-encoder):** ON 12/12 admitted;
  PARTIAL 2 admitted / 2 rejected; NEIGHBOR 2 admitted (both explicitly
  discuss prompt injection) / 15 rejected; OFF 0 / 17.

## 2026-10-09 -- S2-S5 audit on the 17-paper "multilingual prompt injection" corpus

Scope: S2-S5 only. Corpus not reset or recollected; S1 and S6-S9 code
untouched. S2-S5 were run checkpointed (same functions and order as
`/pipeline/run`), stopping before S6.

### D-S3-PRIOR-01 -- a section prior needs a heading that names the section

S2's SciBERT fallback maps any heading its keywords miss onto the nearest of
seven labels, and S3 then labelled *every* sentence in a `Limitations` /
`Future Work` section as that type (confidence 1.0, method `rule`). Measured on
this corpus: 48 of 239 gap statements (20%) existed only because of a guessed
prior -- "Ethics Statement" (papers 2, 16, 17), "Prompts" (17), a jailbreak
related-work section "LLMs misuse & Jailbreak prompts" (6), a method section
"Prompts: Two sides." (6), "Potential Mitigations" (9), and raw table rows
under a table header (6, typed `future_work`). Paper 6 (jailbreak-adjacent)
alone contributed 25. Example stored as a *limitation*: "In this work, we
investigate the vulnerability of LLMs to Prompt Injection Attacks."

Fix (`app/extraction/service.py`): the prior is withheld when a heading exists
and its keyword match does not name that section type; such sentences go to
the cue rules and the semantic classifier like any other. Heading-less
sections keep the old behaviour (S2 never types them as a gap section).
`EXTRACTION_VERSION` bumped to `...-prior-keyword-v1` so the S3 checkpoint
re-runs. Result: guessed-prior statements 48 -> 0; gap statements 239 -> 191;
Qdrant points 977 -> 929 (0 re-embedded -- surviving facts keep their
deterministic ids). Unaffected papers are unchanged.

### D-S2-LOG-01 -- GROBID failures and empty parses are logged per paper

`grobid_client` now logs the endpoint and HTTP status or transport error class
(never the service URL); `parse_and_store` logs `s2 paper=<id> GROBID unusable
(<reason>); using fallback` and `s2 paper=<id> parsed document has no text`.
Behaviour is unchanged; previously the stage summary gave only counts.

### Verified, not changed

* **Orphan Entity nodes (S4).** Replacing a paper's projection deletes its
  edges but not entities it alone mentioned; the S3 fix's S4 re-run left 30
  orphans (0 before). Not fixed: `test_empty_graph_replaces_previous_edges_
  without_deleting_shared_entities` and `core/maintenance.py` record keeping
  entities as deliberate. Owner decision. A safe fix is to delete, in the same
  write, only entities this paper previously mentioned that now have no
  relationships. Orphans appear only in `GET /graph`; S6-S9 do not read Neo4j.
* **SPECTER2 adapter is active.** Re-encoding stored texts reproduces stored
  vectors (cosine 1.000); the same texts without the adapter differ (mean
  cosine 0.960).
* **Entity normalization** is casefold + whitespace only: 35 near-duplicate
  groups (`llm`/`llms`, `gpt-4`/`gpt4`, `llama 2`/`llama2`/`llama-2`) and
  model typing errors ("Figure 8" as dataset). All 842 entities and 20
  relations occur verbatim in S4's input text. Only Mode B consumes the graph.
* **Table rows in body text.** GROBID leaves some tables as `<p>`; paper 16
  has ~47 numeric `metric` facts. Not gap types, so S6 never clusters them.
* **Abstracts are not parsed into sections**, so S3 never reads them.

### Stages that must be rerun

S6 -> S7 -> S8 -> S9. Stored clusters/rankings/reports predate this change.


## 2026-10-10 -- S1 collection quality: gate v2, quota counting, identifiers

The S1 audit of collection run 1 ("Multilingual prompt injection", 17 papers)
found the caps respected but the corpus weak: 7 of 16 judged admissions were
not CORE, and relevant candidates were capped. Every defect below was first
reproduced by a failing test. No corpus row was touched; no API was called.

### Labelled regression set

`backend/tests/ingestion/data/s1_relevance_regression.json`: run 1's 17
admitted papers plus its 3 rejected ACL candidates, labelled CORE (9) /
PARTIAL (3) / ADJACENT (4) / IRRELEVANT (3) / INSUFFICIENT_EVIDENCE (1). The
labels are one reviewer's human judgments, not ground truth. Each item carries
the production cross-encoder's score (recomputed in the backend container; equal
to run 1's), which `tests/ingestion/relevance_eval.py` replays so the
evaluation needs no model. Run: `python -m tests.ingestion.relevance_eval`.

| metric (19 judged items)      | gate v1 | gate v2 |
|-------------------------------|---------|---------|
| admitted                      | 16      | 10      |
| precision, CORE               | 0.562   | 0.900   |
| precision, CORE or PARTIAL    | 0.688   | 0.900   |
| recall, CORE                  | 1.000   | 1.000   |
| CORE ranked above non-CORE    | 0.762   | 1.000   |
| title-only item (paper 12)    | strong  | title_only tier |

Remaining false admission: paper 7 (Sentra-Guard, ADJACENT), whose
multilinguality is real but shallow. No rule was written for it.

### D-S1-GATE-02 -- meaningful facet evidence (gate v2, lexicon 2026-10-10.1)

An occurrence of a facet form no longer counts inside a future-work sentence
or when it names a tool ("multilingual BERT"); a facet with only such
occurrences is rejected `future_work_only:<facet>` /
`incidental_mention:<facet>`. A concept used with a technique context and no
threat context is `benign_sense:<facet>`; a text whose title names a distinct
neighbour (jailbreak) and not the facet, and mentions it at least as often, is
`adjacent_topic:<neighbour>`. All rule vocabularies live in
`concept_lexicon.json`. Held-out check (71 ACL Anthology prompt-injection
records, topic "prompt injection"): 1 rejection,
"Proxy Barrier" (`adjacent_topic:jailbreak`, borderline). For "multilingual
prompt injection" the admitted ACL set is unchanged.

Title-only candidates are admitted only in tier `title_only`. `rank_key`
orders admitted candidates: abstract before title-only, then facets evidenced
in the title, then semantic score.

### D-CAP-03 -- a source's quota counts the papers it contributes (supersedes D-CAP-01's cross-source rule)

A candidate that merges onto a paper an earlier source collected *in the same
run* is merged, recorded as admitted with `cross_source_duplicate_of`, counted
in `source_stats[...]["cross_source_duplicates"]`, and uses no slot. A paper
stored by an earlier run still uses a slot (otherwise each re-run could add
another full quota). So one run stores at most 10 new papers per source, 30 in
all -- an upper bound, not a target. No PDF is fetched for a copy of a paper
that already has full text. Under D-CAP-01, Semantic Scholar spent 5 of its 10
run-1 slots on arXiv papers and capped 9 relevant candidates.
`test_collect_cross_source_duplicates_still_merge_under_the_caps` now expects
20 papers, not 19, because of this rule change.

### D-DEDUP-02 -- identifiers compared normalized

Semantic Scholar now keeps `externalIds.ArXiv`/`ACL`. Dedup compares DOIs case-
and resolver-insensitively, arXiv IDs without version suffix or `arXiv:`
prefix, maps arXiv DataCite DOIs (`10.48550/arXiv.<id>`) and ACL DOIs
(`10.18653/v1/<id>`) onto those IDs, reads punctuation in titles as spaces,
and lets a title of 6+ words match across adjacent years. Stored values are not
rewritten.

### D-QUERY-03 -- arXiv queries use the gate's validated facet synonyms

`/collect` passes `gate.query_groups()`: a concept facet's `query_forms` are
OR'd (multilingual, multi-lingual, cross-lingual, crosslingual, code-switching,
code-mixed, low-resource language, machine translation); facets stay AND'd.
Topics without query forms keep D-QUERY-02's query. The candidate pool is
unchanged (30). Live yield is unverified: no live call was made.

### Not changed / open

* Semantic Scholar's query is unchanged (its search tokenizes the topic).
* The semantic floor and strong threshold are unchanged; the original 50-doc
  calibration pool is still not in the repo.
* The ACL export cache (2026-09-21) still has no refresh policy.
* The existing corpus was collected by gate v1. A fresh `/collect` is needed
  for v2 to apply; it is the owner's call.

## 2026-10-12 -- S1 audit follow-ups (dedup titles/DOIs, component followers)

Defects from the S1 audit, each reproduced by a failing test first. No S1
design, cap, query or S2-S5 change; no corpus touched, no API called.

* **D-DEDUP-03 -- title key.** `normalize_title` is now the title's words with
  every separator dropped, so "Pre-training"/"Pretraining" and
  "Don't"/"Don’t"/"Dont" agree (D-DEDUP-02's punctuation-as-space had broken
  both) while "Qwen -- X"/"Qwen--X" still agree. A title match across
  *different years* now also needs a shared author surname (yearly shared-task
  overviews share a title); same-year matching is unchanged.
* **Stored DOIs** carrying `doi:` / `(dx.)doi.org` prefixes now match bare DOIs.
* **D-S1-GATE-03 -- component followers (lexicon 2026-10-12.1).** `encoder`,
  `embedding`, `tokenizer`, `sentence transformer`, `backbone` moved from
  `incidental_followers` to `component_followers`: they are incidental unless an
  attack cue stem (`component_target_cues`) occurs in the five words before the
  form ("attacks on multilingual embedding models"). Model names (BERT, XLM...)
  stay unconditionally incidental. Regression-set metrics unchanged.

## 2026-10-12 -- Phase 1 closeout: evidence history, audit evidence, outcomes, paper language

Code audit and fixes only. No external API called, no `/collect` run, corpus
and PDFs untouched (the live DB was only read, to confirm the defects). Every
change was first a failing test.

* **D-AUDIT-01 -- decision history (gate v3).** Promoting a rejected copy
  (`duplicate_of_admitted`) and then capping it replaced `evidence["gate"]`
  with the promotion; run 2's capped records 78/86/89/93/122/126 kept the
  gate's verdict only inside a reason string. `RelevanceDecision._then` now
  keeps `evidence["gate"]` = the gate's own verdict (never overwritten) and
  appends every transition to `evidence["gate_history"]` (steps `gate`,
  `duplicate_promotion`, `source_cap`). `GET /collections/{id}/decisions`
  adds `gate_history` + `gate_history_source` (`recorded`, or
  `reconstructed` for older rows; a capped promotion's original verdict is
  recovered from its reason text and marked `recovered_from`).
* **D-AUDIT-02 -- bounded text evidence.** Rejected and capped decisions carry
  `evidence["text_audit"]`: SHA-256 and length of `relevance.normalize(title +
  ' ' + abstract)`, and at most `RELEVANCE_AUDIT_EXCERPT_CHARS` (default 400;
  0 = none) of the abstract. Admitted records unchanged. No sentence-level
  evidence was added: the gate does not keep match positions, and the facet
  evidence (matched / discounted forms) is already recorded.
* **D-UNPAYWALL-02 -- outcomes.** `unpaywall.lookup_oa_pdf` names each result
  (`oa_location_found`, `no_oa_copy`, `doi_not_found`, `http_error`,
  `invalid_response`, `timeout`, `network_error`); `enrich(outcomes=...)`
  counts one per row looked up, plus `oa_pdf_stored` / `download_failed`.
  `resolve_oa_pdf` and `enrich`'s return value are unchanged. Transport
  errors are never logged with their message (it can contain the email).
* **D-COUNT-01 -- counters.** `source_stats` adds `already_in_corpus`
  (papers), `duplicate_promotions`, `language_unverified` (candidates). The
  response and `run.counts` add `full_text` (unique papers touched / added:
  with full text, metadata-only, metadata-only without DOI, coverage),
  `unpaywall` (scope, completed, looked_up, outcomes) and `paper_language`.
  `RunLedger`'s docstring defines every counter's unit.
* **D-S1-LANG-01 -- paper language (gate step 1b).** New
  `app/ingestion/paper_language.py`, no new dependency. Judged on the
  abstract only, never the title: Latin share of letters (Greek and
  mathematical alphanumerics neutral) >= 0.5, else
  `paper_language:non_latin_script`; share of distinctively English function
  words >= 0.10, else `paper_language:not_english`. Fewer than 40 letters / 20
  words, or no abstract: `unverified` -- not rejected, recorded in
  `evidence["language"]` and counted. On the 26-paper corpus and the
  regression set every English abstract measured 100% Latin and a marker
  ratio of 0.18-0.29; regression metrics unchanged (precision 0.9/0.9, CORE
  recall 1.0, ranking 1.0).
* **XRXNet** (run 2, decision 117) pinned in
  `tests/ingestion/data/s1_known_borderline.json` as a known borderline
  `relevant:conceptual` admission. Unlabelled, not claimed CORE; no rule
  written for it.

### Verified, not changed

* Thresholds (floor -1.0, strong 3.5), caps (10/source), candidate pools,
  dedup order and `rank_key` are unchanged. Promotion and slot rules are
  unchanged: an earlier run's paper that a source re-finds still uses a slot,
  and a promoted copy of a v1-era admission (score None, tier None) ranks
  above title-only candidates -- in run 2, S2 spent a slot on paper 4 (gate
  verdict `incidental_mention:multilingual`) while a title-only candidate was
  capped. On a clean corpus this cannot occur in the first run: promotions
  then only match papers collected earlier in the same run, which are
  cross-source merges and use no slot.
* Unpaywall looks up every corpus row with a DOI and no full text, not only
  this run's; its counts are scoped accordingly.
* `GET /collections/{id}/decisions` stays behind `X-API-Key`; excerpts are
  exposed only there.

### Settings added

`RELEVANCE_AUDIT_EXCERPT_CHARS`, `PAPER_LANGUAGE_MIN_LATIN_FRACTION`,
`PAPER_LANGUAGE_MIN_ENGLISH_RATIO`, `PAPER_LANGUAGE_MIN_WORDS`,
`PAPER_LANGUAGE_MIN_LETTERS` (documented in `.env.example`).

## 2026-10-12 -- S1 hardening: retrieval, full text, ACL cache, run audit (final S1 pass)

Owner-authorized final S1 pass. Every behaviour change below was first a
failing test. Read-only probes made: arXiv x4, Semantic Scholar x2 (one
rate-limited without a key), all search/metadata only; no collection was run.

* **Test network guard.** `tests/conftest.py` makes any non-local name lookup
  fail (except `integration` tests). It caught a live arXiv request during
  this refactor. `tests/api/conftest.py` makes every full-text provider
  answer instantly as offline.
* **D-S1-RETRIEVAL-01 -- paged retrieval with an honest report.** Sources
  return `retrieval.Candidates` (a list plus a `retrieval` report: reported
  total, pages requested/succeeded, records fetched/dropped, stop reason,
  `complete`, query + fingerprint). arXiv pages to its reported total or
  `ARXIV_CANDIDATE_POOL` (now 200, page 100); Semantic Scholar follows `next`
  until `SEMANTIC_SCHOLAR_REQUEST_LIMIT` research records (now 100; API max
  1,000), backfilling dropped records, bounded by `SEMANTIC_SCHOLAR_MAX_PAGES`
  (3). Both de-duplicate across pages; a failed later page keeps what was read
  and is reported incomplete; a failed first page still fails the source.
* **D-QUERY-04 -- prompt-injection phrases (lexicon 2026-10-12.2).** The
  facet's `query_forms` replace the separate words `prompt` AND `injection`.
  Live check: 59 -> 28 arXiv matches, all 20 gate-passing records kept, none
  new. Every query form is a gate form (test).
* **D-CAP-04 -- the cap counts new papers only (supersedes D-CAP-03).** Owner
  rule: at most 10 *new* papers per source per run. Any merge onto an
  existing paper (same run or earlier run, including promoted copies) uses no
  slot; `source_stats` adds `cap` and `slots_used` (= `added`). A promoted
  copy can no longer be capped; the promote-then-cap history stays pinned at
  unit level.
* **D-FULLTEXT-01 -- full-text acquisition with a per-paper trail.** New
  `fulltext_attempts` table (append-only: provider, stage
  discovery/enrichment, outcome, HTTP status, retryable, location host+path
  without query, pdf path). After discovery, each paper without full text is
  tried: Semantic Scholar `openAccessPdf` -> arXiv id -> ACL id -> Unpaywall
  (DOI). `papers` gains `openaccess_pdf_url` and `source_url`. All non-arXiv
  downloads go through `fulltext.safe_download` (public HTTP(S) hosts only,
  redirects re-checked, size limit, timeouts, bounded retries); every body
  through `pdf_store.store` (PDF signature, not HTML, size, atomic replace).
  The arXiv downloader validates the same way. Step budget 600 s; papers not
  reached are `not_attempted`. Residual risk: DNS rebinding between the
  address check and the connection is not excluded.
* **D-ACL-CACHE-01 -- export freshness.** `.meta.json` beside the export
  (fetched_at, source URL, records, SHA-256, last refresh result). Refreshed
  by /collect when older than `ACL_ANTHOLOGY_MAX_AGE_DAYS` (7); validated
  (gzip, BibTeX, UTF-8, >= min records and >= 90% of the cached count) before
  an atomic replace under a lock; failure keeps the last known-good file and
  the run gets a `stale_cache` warning. On demand:
  `python -m app.ingestion.sources.acl_anthology --refresh`.
* **D-AUDIT-02 -- the run is auditable on its own.** `collection_runs` gains
  `status` (running -> completed / completed_with_warnings / partial /
  failed), `finished_at`, `summary` (the response minus `ids`, including
  `warnings`, `reconciliation`, `units`, retrieval reports, full-text and ACL
  cache status). The run row is committed before any source runs; failures
  and defects still close it. New read-only, API-key-protected endpoints:
  `GET /collections/{id}` and `/collections/{id}/fulltext`.
  `gate_history_source` is now `recorded`, `no_transition` (72 of 75 rows in
  the previous audit were mislabelled `reconstructed`) or `reconstructed`.
* **Relevance evidence.** Facets record `evidenced_by` (`surface_form` or
  `language_names`); language verdicts record the deciding `rule`. No gate
  threshold, order or rule changed. Language-name lists were checked against
  the real cross-encoder: three off-topic candidates scored -11.05 / -7.13 /
  -2.78 (rejected by the floor) while a relevant Tamil/Tanglish study depends
  on language names -- so the rule was not tightened. Fixtures:
  `s1_known_borderline.json` (the six flagged admissions, `needs_review`, no
  labels), `s1_language_list_cases.json`, `language_gate_cases.json` (20
  synthetic labelled cases; not real-world validation).
* **Frontend.** The collect panel shows the run status, its warnings and
  full-text coverage; types extended; existing contract unchanged.
* **Reset.** `reset_corpus` also clears `fulltext_attempts`.

## 2026-10-13 -- S2-S5 audit, repair and validation on the 20-paper corpus

Scope: S2-S5, plus a read-only audit of the graph view, clusters, trends,
gaps and reports. S1 code, the corpus rows and the collection run were not
touched; no `/collect` call; no S6-S9 rerun. Every behaviour change below was
reproduced by a failing test first, except the S3 tests (characterization:
S3 had no defect) and the reconciler (new tool).

Initial state: 20 papers -- 11 full text (all GROBID-parsed, 714 statements,
632 Qdrant points), 9 Semantic Scholar metadata-only (abstract-only S4
projections, no S2/S3/S5 artifacts by design). One pipeline run (1fcb0754)
through S9; stores consistent per `app.core.reconcile`.

* **D-S2-STATUS-01 -- S2 outcomes are explicit.** `grobid_client.fulltext`
  returns `ok` / `rejected` (non-200 other than 502-504: the PDF) /
  `unavailable` (transport error, timeout, 502-504: the service).
  `parser_used` gains `fallback_grobid_unavailable` and `none`. (a) GROBID TEI
  with headings but no paragraph text now falls back (it used to be stored as
  a text-less GROBID parse). (b) A PDF no parser reads is stored empty as
  `none`, not as a `fallback` parse. (c) A fallback made during a GROBID outage
  is not accepted by the S2 checkpoint, so GROBID is retried on the next run;
  a PDF GROBID rejected is not re-sent. `/parse` and `/pipeline/run` add
  `no_text_pdfs` and `grobid_unavailable` (additive).
* **D-S4-PROV-01 -- graph provenance.** S4 input units carry stable refs
  (`abstract`, `statement:<section>.<paragraph>.<sentence>`). MENTIONS edges
  store `evidence` (<=25 refs) + `evidence_count`; relation edges store
  `evidence` + `cross_sentence`; Paper nodes store `nerre_model`,
  `nerre_model_revision` (resolved cache commit), `nerre_input_basis`,
  `nerre_units_used/total` (exposes the chunk cap), `nerre_provenance`. An
  entity whose text occurs in no input unit (word-boundary match) is dropped
  with its relations. `paper_graph_current` requires the provenance version,
  so pre-provenance projections are rewritten once under an unchanged input
  hash (S6-S9 fingerprints unaffected). A failing chunk now fails the paper in
  every context instead of writing a partial projection.
* **D-GRAPH-VIEW-01 -- `/graph` is a coherent subgraph.** Papers first, then
  entities by degree, edges only between returned nodes; adds `total_nodes`,
  `total_edges`, `truncated`. Live before/after for the dashboard's 180 drawn
  nodes: papers 3 -> 20, drawn edges 139 -> 289, isolated nodes 49 -> 1,
  dangling edges 29 -> 0. The UI states "Showing N of M nodes".
* **D-S5-REV-01 -- adapter revision.** `EMBEDDING_MODEL_REVISION` was also
  passed to the SPECTER2 *adapter* repo, whose commits differ, so pinning the
  base model made the adapter unloadable. The adapter is now pinned with
  `EMBEDDING_ADAPTER=org/repo@<rev>`; resolved commits are logged at load.
  Current deployment unchanged (neither is set). Both repos still float.
* **D-RECON-01 -- `python -m app.core.reconcile --papers`.** Per-paper S2-S5
  status (`completed`/`failed`/`pending`/`not_started`/`not_applicable`),
  counts, and checks: S3 statement == sentence at its location; S4 mentions in
  source and evidence valid; S5 expected vs stored points.

### Applied to the live corpus (run 06f1b886, checkpointed S2-S5, no S6)

Dry run first: all 20 S4 and 20 S5 digests unchanged by the new code. Result:
S2/S3 skipped (11/11), S4 re-projected 20 papers, S5 re-embedded 0 points.
19/20 papers identical entity and relation sets; paper 20 lost one entity,
`30.6% FPR` (a span GLiNER read across two packed statements -- not in the
paper), and its `ACHIEVES` relation. Mentions 708 -> 707, relations 13 -> 12,
all with evidence; 1 relation cross-sentence. Reconciliation: 0 warnings,
0 statement-location mismatches (714), 632/632 points. `s6-s8` and `s9`
derived state still current; clusters, trends, rankings, reports unchanged.

### Verified, not changed (limitations)

* GLiNER `main` has moved (two cached snapshots); now recorded per paper.
  SPECTER2 base/adapter also unpinned. Pinning re-embeds and rebuilds S6-S9.
* 5/632 points exceed 512 tokens (max 2278): the vector covers a prefix; the
  payload holds the full (<=4000-char) text.
* 4 sentences are typed both `limitation` and `future_work`, so indexed twice;
  clusters 3 and 5 `member_count` +1 each. Distinct-paper signals unaffected.
* S3 has no cited-claim type; cited limitations are stored verbatim.
* Entities merge across papers on exact (casefold name, type) only. The
  `30.6% fpr` Entity node is now an orphan (orphans retained by design).
* Trends: 1-4 papers per year, and 2026 is a partial year; slopes are
  descriptive of this sample, not of the field.
* Report cluster 3's `justification.conflicting_evidence` is an LLM judgment
  citing 2 passages from 2 papers; S6 NLI flagged no contradiction. It is
  stored only in `justification` and not shown in the UI.

## 2026-10-13 -- Final quality pass (clusters, report claims, orphan, citations, models)

Owner-requested. S1, corpus rows, PDFs and S2-S5 artifacts untouched (paper
rows hash-identical; 632/632 points; reconcile 0 warnings). Derived tables were
backed up before the change (scratchpad export).

* **D-06-14 -- one statement per sentence in S6.** A sentence typed both
  `limitation` and `future_work` is indexed twice with the same vector; both
  copies were clustered. A copy at distance 0 shrinks HDBSCAN core distances:
  clusters 3 ("diverse ensembles includes") and 5 ("adversarial templates
  threshold") existed only because of such copies. S6 now clusters one
  representative per (paper, text) and stores the other label rows with the
  same cluster; `member_count` counts distinct statements; NLI and labels use
  distinct statements; S9 queries use distinct texts. `/clusters` lists one
  member per sentence with `source_types`, `point_ids` (additive).
  `_DERIVED_VERSION` = 2 so older S6-S8 state is recomputed.
  Live: 6 clusters -> 4. Old 1 -> 11 (23 statements, 9 papers, new label),
  2 -> 12, 4 -> 13, 6 -> 14 identical; 3 and 5 dissolved.
* **Report carry-over.** `report_inputs` / `carry_over_reports` keep a report
  across a rebuild when its generation inputs (label, distinct member texts,
  member papers, five signals) are identical. Live: reports for old 2 and 6
  carried byte-identical to 12 and 14; 11 and 13 generated (2 Groq calls);
  old reports archived in `report_snapshots`.
* **D-09-13 -- conflict claims verified.** Cluster 3's report claimed
  conflicting evidence citing paper 8 (its detector, >96% detection) and
  paper 7 (LLM judge false positives under cross-lingual injection):
  different systems, tasks, metrics; DeBERTa-MNLI 0.0066/0.0047. A conflict
  claim is now `supported` only if two cited passages from different papers
  contradict in both NLI directions (threshold 0.7); the LLM's view is kept as
  `llm_supported`, with `verification`. Every claim has `basis`
  (`llm_cited`, `not_supported`, `nli_verified`, `llm_unverified`,
  `not_claimed`). UI labels the gap text as a model-written summary, evidence
  as verbatim passages, and shows a conflict pill only for `nli_verified`.
* **Orphan `30.6% FPR`** (metric) removed with the guarded
  `remove_orphan_entity` (exact key, no edges, never detaches). It was the
  cross-statement span dropped by D-S4-PROV-01. Orphan policy otherwise
  unchanged.
* **D-S3-CITE-01 -- citation markers.** 7 of 113 gap statements carry numeric
  markers; 1 of 7 is the paper's own limitation citing a term, so no
  "cited claim" label. `/clusters` members and `/reports` evidence expose
  `citation_markers` (derived from text); UI shows "cites [n]".
* **Long passages.** 5/632 points exceed 512 tokens: all Results paragraphs,
  4 tables (papers 8, 4, 10, 3) and 1 method paragraph (paper 10, 632 tokens,
  ~82% of characters covered). None is a gap point, cluster member or report
  evidence. Not regenerated: a fix means splitting paragraphs, which changes
  S5 record ids and invalidates S6-S9 for no measured benefit.
* **D-S4-REV-01 -- GLiNER pinning.** `NERRE_MODEL=org/repo@<commit>` is passed
  to gliner2 as `revision`. Not configured.

### Model versions in use (verified at runtime, NOT pinned in configuration)

* GLiNER `fastino/gliner2.5-base-v1` main = `54785d51df8d86a0d8f2eb212fec15871aad2c4d`
  (recorded on all 20 Paper nodes). The other cached snapshot `1a8bc24` has
  byte-identical weights/tokenizer; only config.json format differs.
* SPECTER2 base `allenai/specter2_base` main = `3447645e1def9117997203454fa4495937bfbd83`
  (`pytorch_model.bin` is what loads; the cached `refs/pr/10` safetensors is
  tensor-identical, 200/200). Adapter `allenai/specter2` = `2081559630a80fc5851d8f798a05ba81e9468089`.
* Proof: loading exactly those commits offline reproduces stored outputs --
  SPECTER2 cosine 1.0000000 on 24 sampled points; GLiNER entity and relation
  sets identical for papers 7 and 13.

## 2026-10-13 -- S6-S9 verification and presentation (no recomputation needed)

S1, corpus, PDFs, S2-S5 outputs, the 632 vectors and model versions untouched.
No pipeline run, no Groq call, no model load beyond read-only checks.

### What ran (all read-only or rolled back)

* S6: re-clustering the current gap points (one statement per sentence)
  reproduces the stored partition (clusters 11-14). 39 member rows: every
  one resolves point -> Qdrant payload -> S3 fact (same paper, text, type);
  every label row of each clustered sentence is stored (4 multi-label
  sentences of 109); no table references a dissolved cluster id.
* S7/S8: rebuilt inside a rolled-back transaction; identical to stored.
  `mention_frequency` = distinct member papers for every cluster.
* S9: derived markers `s6-s8` (e2e241cc60) and `s9` (917e22a057) current,
  so no report was invalidated or regenerated. All report evidence resolves
  to its Qdrant point and paper; every claim has a `basis`; no report claims
  a conflict.

### Changes (presentation only; stored artifacts unchanged)

* **D-08-12 -- single-paper themes are exploratory.** `/gaps` and `/clusters`
  add `paper_count`, `evidence_scope` (`single_paper` | `corroborated`, at
  `CORROBORATION_MIN_PAPERS` = 2) and `exploratory` (gaps). Ranking and scores
  unchanged; cluster 13 (1 paper, 4 statements) is kept and labelled
  "Exploratory · single paper" on its report and cluster cards.
* **D-07-08 -- trend caveats.** `/trends` adds `qualifiers` (papers, years,
  max papers per year, `small_sample` below 5 per year, `partial_years` for
  the current calendar year onward, `single_year`). The Trends view states
  that slopes describe this corpus and that 2026 is partial; rows say
  "N papers" (the series counts distinct papers, not mentions).
* **Claim statuses in the UI.** Each report shows its four claims with their
  basis (model judgment with cited passages / not supported / NLI-verified /
  model-asserted but unconfirmed / not claimed).
* Frozen API contract tests for `/gaps` and `/trends` extended with the
  additive keys (exact sets, not loosened).

### Live state

Gaps: 11 (-0.37, 9 papers), 12 (-1.73, 6), 14 (-5.63, 3), 13 (-24.0,
1 paper, exploratory). Hashes: papers 333469c83c7a6c28, PDFs 6ddd25f830b79b7a,
parsed_documents 394537883c102d18, extracted_facts 1e8f3f26b8821d8a, Qdrant
payload+vectors c41050e2a4e38cdd (632). Reconcile: consistent, 0 warnings.

## 2026-10-13 -- Final S6-S9 release-gate audit (presentation fixes only)

S1, corpus rows, PDFs, S2-S5 outputs, vectors, graph, model versions and every
stored S6-S9 artifact untouched: a 45-value read-only snapshot (counts and
content hashes of papers, collection run, PDFs, parsed documents, facts, stage
states, Qdrant payload+vectors, Neo4j entities/edges, clusters, members,
trends, rankings, reports, snapshots, NLI cache, derived markers) is identical
before and after. No pipeline run, no Groq call, no model load. The prior
checkpoint's 16-hex hashes could not be reproduced (definition not recorded);
this audit's own baseline is the comparison point.

### Verified on the live evidence base (read-only / rolled back)

* S6: re-clustering the 90 eligible points (87 sentences) reproduces clusters
  11-14 exactly, also from shuffled input; NLI re-run from the cache alone (model
  call forbidden) finds 0 contradictions, 0 cache misses; all 39 member rows
  resolve point -> payload -> fact -> paper; 0 dangling cluster references in
  5 dependent tables; 0 label rows lost.
* S7/S8 rebuilt in a rolled-back transaction: identical. Per cluster, distinct
  papers = trend total = `mention_frequency` (9, 6, 1, 3); one year per paper.
* S9: 19 quotes match their Qdrant passage under the stored rule
  (`strip()[:MAX_EVIDENCE_CHARS]`), paper ids and titles correct; every claim
  has a valid `basis`; no claim cites a passage absent from the report.
  `s6-s8` / `s9` markers current.

### Changes

* **D-09-14 -- quotes cut at the length cap are marked.** 4 of 19 report
  passages (reports for 11 and 13) are the first 1200 characters of a longer
  passage, shown under "quoted verbatim" with nothing saying so. `/reports`
  evidence adds a derived `truncated` (text reached `MAX_EVIDENCE_CHARS`);
  the UI appends "…" and "the passage may continue". Stored rows unchanged.
* **D-07-09 -- the trend series is papers, not mentions.** Chart axis and
  hover said "mentions"; the series counts distinct papers. The Trends title
  ("where the field is moving") and "Fastest rising" heading contradicted the
  corpus-only caveat; now "See how this corpus changes over time" and
  "Highest slope first / Change in this corpus".
* **Conflict claim with no `basis`** (a pre-D-09-13 report or archived
  snapshot, e.g. snapshot abc6c6f9d99d's old cluster 3) now reads "Asserted by
  the model, not confirmed by NLI", not "Model judgment, cites retrieved
  passages". Never "verified".
* **"Ranked reports" counts current reports only**, not archived (`stale`)
  snapshot rows.

### Not changed / limitations

* `carry_over_reports` / `report_inputs` are not wired into `/pipeline/run`;
  a Stage 6 rebuild there regenerates every report.
* NLI cache key covers `nli_*` settings only; with `NLI_MODEL_REVISION`
  unset, a moved upstream checkpoint would reuse cached scores. Same unpinned
  model policy as GLiNER/SPECTER2.
* `ruff format --check` flags pre-existing lines in
  `tests/api/test_cluster_members_payload.py` (not part of the README gate).
* 10 `integration` tests (real models/services) not run.

## 2026-10-13 -- Phase 10-13 acceptance audit (before Phase 14)

Read-only on the corpus: the 45-value snapshot (S1-S9 counts and content
hashes, derived markers, 19 pipeline runs) is identical before and after;
reconcile consistent, 632/632, 0 warnings. No Groq call, no model load, no
pipeline run. Phase 14 not started; deployment config untouched.

### Findings

* **D-10-05 -- graph rows no longer count towards Mode B sufficiency.** Graph
  neighbourhood rows carry a fixed score 0.5, so two low-similarity vector
  hits plus their papers' entity rows were judged "sufficient" and generation
  ran on weak evidence (every live paper has entity edges). `assess` now counts
  only similarity-scored vector passages. Regression test first (failed: an
  answer call on 0.1-similarity hits).
* **Open, needs an owner decision -- `RETRIEVAL_MIN_SCORE` is not calibrated.**
  Querying Qdrant with 40 stored SPECTER2 vectors: the lowest cosine to any of
  the other 631 points is 0.741 (median 0.774); the 12th-best is >= 0.885;
  0 of 25,240 pairs fall below 0.2. The relevance gate never rejects, so on
  this corpus "weak retrieval" is never detected and the retry loop cannot
  fire live (inferred from stored vectors; a real query embedding was not
  measured). Recalibrating needs real-query measurements (SPECTER2 load).
* **Phase 10 Done-when still not demonstrated live**: no grounded `POST /query`
  has ever run on a populated corpus (needs Groq + SPECTER2 + cross-encoder).
* **D-13-02 -- expert validity normalized to 0-1 as documented.** The index
  used `mean_validity / 5` (rating 1 "unsupported or false" earned 0.2); now
  `(mean - 1) / 4`. Example report regenerated with the documented command:
  validity_index 0.7222 -> 0.7083; nothing else changed. Before the fix the
  documented command reproduced the committed report exactly.
* Phase 13 runs only on the hand-written example corpus; there is no export
  from stored gap reports to `gaps.json`, so ScholarLens's own gaps have never
  been evaluated.
* Phase 12: dashboard served by Vite reaches every endpoint it loads through
  the `/api` proxy against the live backend (200s, 401 without key). The Ask
  view is tested only against a mocked `/query`.

## 2026-10-13 -- Phase 10-13 remediation: calibration, live-query harness, real-report evaluation

Owner-scoped to items 1-3. No live `/query`, no Groq call, no model load, no
pipeline run; `RETRIEVAL_MIN_SCORE` unchanged (0.2). 45-value snapshot
identical before/after; reconcile consistent; NLI cache unchanged (335).

* **D-10-06 -- one sufficiency rule.** `workflow.assess_sufficiency` is what
  `assess` and the calibration harness both call: vector passages only,
  `score >= min_score` on the raw Qdrant cosine, missing/non-finite scores
  never count. Boundary tests at 0.2 / 0.19999.
* **Calibration (`app/interactive/calibration.py`, read-only).** Stored mode
  (no model): all 398,792 passage pairs have cosine >= 0.716; 0.2 rejects
  nothing; in-corpus proxy queries are first rejected above ~0.88 (90.2% pass
  at 0.90, 15.7% at 0.95); 7.9% fail on the two-paper rule at any threshold.
  No false-accept evidence is possible without embedding real off-topic
  questions: `--mode embed --allow-model-load` (SPECTER2, local, no LLM) is
  prepared, not run. No threshold recommended. Report:
  `evaluation/output/mode_b_calibration/`.
* **Live query check (`app/interactive/live_check.py`).** Dry run by default;
  `--confirm-live` sends exactly one `POST /query` (no client retry) and checks
  schema, citation structure, claim->citation links, citation text vs Qdrant
  payload, titles vs corpus, graph citations vs Neo4j MENTIONS, loop bounds;
  classifies grounded answer vs safe non-answer. Dry run verified live.
  Cost per run: 2-6 Groq calls (<=4 retries each), SPECTER2 + cross-encoder
  loaded locally from cache.
* **D-13-03 -- unavailable is not zero.** `run_evaluation` takes optional
  later papers / surveys / expert ratings, reports each method `computed` or
  `unavailable` (with what it needs), and averages the validity index over
  computed methods only (`null` if none). Blank template rows are skipped;
  partly filled rating rows fail. Example scores unchanged (0.7083, pinned by
  a test); the committed example report lacks the new `availability` keys.
* **Real-report evaluation.** `app/explanation/export.py` (read-only,
  deterministic, schema `scholarlens-gap-export/1`) -> `evaluation/scholarlens.py`
  (stable gap ids `scholarlens:cluster-N:report-M`, provenance, rating
  template). Live: 4 reports exported (byte-identical twice); validity index
  not computable -- no later papers (cutoff 2026), surveys or expert ratings
  exist. Model confidence and claim judgments stay under `generated`, unused.
* **Calibration, real questions (owner-approved, offline).** 14 synthetic
  queries embedded with the cached SPECTER2 commits (HF offline). 2nd-best
  cosine: relevant 0.945-0.961, partial 0.926-0.938, weak 0.923-0.933,
  unrelated 0.869-0.889. Relevant/unrelated separable in (0.889, 0.945];
  weak/partial not separable. 0.90 = candidate only (margin 0.011 on 4
  unrelated queries); threshold unchanged at 0.2. Details:
  `evaluation/output/mode_b_calibration/README.md`.
* **Live `POST /query` (owner-approved, one request, 2026-10-13 12:49Z).**
  Upstream HF `main` checked first (metadata only) = cached commits for
  SPECTER2 base/adapter and the cross-encoder, so no download/version change.
  Result: **safe non-answer** (`grounding_failed`), HTTP 200, 195 s. Trace:
  1 retrieval round (sufficient: 12 relevant / 7 papers at 0.2), no
  reformulation, reranked, 2 generations (1 strict regeneration), both drafts
  structurally grounded but rejected by the LLM grounding check. 4 Groq calls
  (7,442 prompt / 5,518 completion tokens), no rate-limit retries logged,
  60 s pacing. All 9 checks passed; 4 returned citations (papers 2, 2, 3, 8)
  match their Qdrant passages exactly. Phase 10 Done-when therefore still not
  shown: the grounded-answer path did not complete, and retry cannot fire at
  0.2. Corpus snapshot unchanged. Output: `evaluation/output/live_query_result.json`.

## 2026-10-13 -- Mode B weak-retrieval and refusal verification (offline)

No live query, no LLM call, no model load, no pipeline run; threshold 0.2
unchanged; 45-value snapshot identical; reconcile consistent.

* `tests/interactive/test_weak_retrieval.py` drives the compiled graph with
  production `Settings` defaults (0.2, 2 chunks, 2 papers, 3 rounds, 1
  regeneration; caps hard-clamped in `run_query`). Six weak cases (zero
  passages, one qualifying, one paper only, below threshold, graph rows beside
  sub-threshold or single hits) each reformulate and retry, and end in the
  insufficient-evidence refusal after 3 rounds with no generation call; two
  qualifying papers take the normal path; graph rows never count; raised caps
  are clamped; verifier-rejected and invented-citation drafts end in the
  grounding-failure refusal after one regeneration, with no reformulation.
  These are mocked: they prove control flow, not live model behaviour.
* **D-10-07 -- a grounding-failure refusal carries no citations.** The live
  run returned the rejected draft's 4 citations beside the refusal
  (`grounding_failed` never cleared them) and the UI listed them as "Sources".
  Now cleared; `/query` adds `outcome` (`grounded_answer` /
  `no_indexed_evidence` / `insufficient_evidence` / `grounding_failed`); the
  Ask view labels by outcome and lists sources only for a grounded answer.
  Grounded answers keep their cited evidence (tests). The live check fails a
  refusal that still lists citations.

## 2026-10-14 -- Phase 10 deep audit and hardening (offline)

No live query, no LLM call, no model load, no pipeline run; threshold 0.2 and
model versions unchanged; 45-value snapshot identical; reconcile consistent.
Phase 10 remains NOT RESOLVED: no grounded answer has been produced live.

Proven defects fixed (each red first, `tests/interactive/test_mode_b_hardening.py`):

* **D-10-08 -- refusals cite nothing.** The insufficient-evidence refusal
  returned every retrieved passage as `citations`; now `[]` like every
  refusal. `/query` gets a `QueryResponse` model (OpenAPI documents the
  `outcome` enum; an unknown outcome is not served).
* **D-10-09 -- answer prose is verified.** The grounding prompt only asked
  whether each claim was supported; it now also fails an answer that states
  anything not in a supported claim.
* **D-10-10 -- unusable model output ends in a defined outcome.** A draft that
  does not fit the schema (e.g. a decline with no claims -- the prompt allowed
  it, the schema forbade it: HTTP 502) or a malformed/contradictory verdict now
  ends in `grounding_failed`; an invalid rewrite keeps the previous query.
  Provider errors (`LLMError`, retrieval exceptions) still raise -- never a
  success.
* **D-10-11 -- untrusted text cannot close an evidence block.** Section
  markers inside passages, titles, the question or a draft are defused.
* **D-10-12 -- duplicates and impossible scores do not count.** Sufficiency
  counts one passage per (paper, text) and ignores |score| > 1. No effect at
  the configured 2/2 (stored calibration identical).
* **D-10-13 -- grounding diagnostics and feedback.** Each check records counts
  and a reason code (trace + server log, no text); the strict regeneration is
  told which claims / citation IDs failed (bounded, never returned).

Live failure (2026-10-13): root cause not established -- both drafts were
structurally valid, cited passages untruncated and verbatim; the verifier's
reasons were never recorded. Calibration: leave-one-out on the 14 saved
queries 8/8 (thresholds 0.913-0.919); labelling template and procedure added;
no threshold recommended. Plan: `evaluation/output/mode_b_live_plan.md`.

## 2026-10-14 -- Phase 10 fix verification and independent-label protocol (offline)

No live query, no LLM, no model load, no pipeline run; threshold/models
unchanged; 45-value snapshot identical; reconcile consistent.

* Mutation check (scratch copies only; working tree untouched): each of the
  six fixes (D-10-07..13) plus D-10-05 reintroduced one at a time -- 15
  backend and 2 frontend mutations, all killed by a regression test; the
  unmutated controls pass. New: `test_regeneration_feedback_is_bounded` (the
  feedback bounds had no test). D-10-09 is a prompt instruction: its test
  proves the instruction is sent, not that the model obeys it.
* Calibration tooling (offline): Cohen's kappa, hash-based tune/test split,
  threshold chosen on tune only, held-out false-accept / false-reject / retry
  frequency / paper and passage recall@12 / distinct papers; `--questions`
  (independent set, embed mode, approval needed) and `--evaluate-labels`
  (offline). Protocol, templates, paper list and passage catalogue (no
  scores): `evaluation/output/mode_b_calibration/independent/`.
* Live plan: preflight checklist added (three named external actions, new
  output file, secret scan, before/after snapshot). Not executed.

## 2026-10-14 -- Phase 10 live validation #2 (owner-approved, one request)

Preflight passed (baseline, parity, dry run, 3 HF metadata checks: upstream =
cache for SPECTER2 base/adapter and the cross-encoder). One `POST /query`:
**exit 0, `grounded_answer`**, 74 s, all automated checks passed; 1 round, no
reformulation, no regeneration, grounding `reason=ok`; 2 Groq calls (3,629
prompt / 3,011 completion tokens), no retries. Secret scan 0 hits; log held
counts/codes only. 45-value snapshot identical; reconcile consistent.

Claim review (`evaluation/output/live_query_result_2_review.md`): claim 0
supported verbatim; claim 1 partly supported (two items verbatim, English
focus misattributed, "low-resource" absent); the answer's "the only ... is
MIPIAD" is unsupported. Only expected paper 2 was cited. The same-model
grounding check passed text that overreaches its evidence: the Done-when
("claims trace back to specific retrieved papers") is met for citations but
not for content. Retry behaviour still not observable at 0.2.

## 2026-10-17 -- D-10-14 claim-to-passage vetoes (offline)

No live query, no LLM, no model load, no Hugging Face access, no pipeline
run; threshold/models unchanged; live results and review unchanged (md5);
45-value snapshot identical to the end of live run #2; reconcile consistent.
Docker was down at the start, so the pre-change snapshot could not be taken;
the comparison is against the last recorded one.

* **Defect.** Live run #2 returned `grounded_answer` with prose the claims did
  not contain ("the only ... is MIPIAD") and a claim asserting "low-resource"
  coverage no cited passage mentions. Two bypasses: the user saw the draft's
  free `answer` text, checked only by the same LLM; and a claim needed only a
  valid citation id plus that LLM verdict.
* **Fix.** `app/interactive/claim_check.py`: deterministic vetoes on each
  claim against *its own* cited passages -- `unsupported_absolute`,
  `unsupported_term` (languages, resource levels, numbers),
  `misattributed` (literature-level sentence attributed to studies),
  `low_overlap` (heuristic). Vetoes can only reject; acceptance still needs
  the LLM verdict; a veto skips the verification call and takes the existing
  regenerate-then-refuse path, with the reasons in the feedback. A grounded
  answer's text is now its checked claims (`answer_from_claims`); trace adds
  `vetoed=` and `prose_uncovered=`. The UI says the check is automatic, not
  human-verified, and heads citations "Cited passages".
* **Tests.** `tests/interactive/test_claim_grounding.py` replays the live
  draft and passages (`tests/fixtures/mode_b_live_run_2.json`): refused, no
  claims/citations, no verify call. 23/23 backend mutations and the frontend
  caveat mutation killed. Old fakes whose claims their passages did not state
  were made content-consistent; three assertions updated to the new contract.
* **Limits.** Pattern vetoes can over-reject (e.g. "the first" stated in other
  words) and cannot prove entailment; a paraphrase that changes meaning while
  keeping the terms passes them. Live behaviour of the new layer is untested.

## 2026-10-18 -- PHASE 10 CLOSED -- WITH DOCUMENTED LIMITATIONS

Final closeout (owner closure rule). No code changed. Verification: backend
1992 passed (10 integration deselected), focused 190, evaluation 20, frontend
38, ruff/oxlint/type-check/build clean, 23/23 mutations killed. One authorized
live run on the final code (`live_query_result_3.json`): `grounded_answer`,
9/9 checks, `vetoed=0`, `prose_uncovered=2` dropped, 2 Groq calls; developer
review: claim 0 supported, claim 1 mostly supported with one unstated
inference. Preservation: 45/45 identical, 19 runs, 632/632, reconcile
consistent. Accepted limitations: no independent human review, uncalibrated
threshold, live retry unobserved, thin retrieval coverage. Reports:
`PHASE10_CLOSEOUT.md`, `PHASE11_HANDOFF.md`. Phase 11 not started.

## 2026-10-18 -- Phase 11 closed: API integration, auth hardening, verification

Decision: PHASE 11 COMPLETE -- WITH DOCUMENTED LIMITATIONS (`PHASE11_CLOSEOUT.md`).
Audited the real surface: 23 operations, `/health` the only public API route
(plus FastAPI's generated docs), `require_api_key` on all 22 others. Fixed:
pre-auth body buffering (FastAPI reads bodies before dependencies) with
`app/api/body_limit.py` (`MAX_REQUEST_BODY_KB`, `MAX_UPLOAD_FILES`, `/upload`
key checked before reading); 422s on `/collect`/`/upload` clearing
`derived_state` (now `exclusive_lock` + `invalidate_derived` after
validation); `.env.example` placeholder key accepted (now fails closed);
duplicate `X-API-Key` headers (now 401); unbounded `/collect` topic and upload
file count; plain-text 500s (now sanitized JSON); `/query` rate limit 503 ->
429 + Retry-After; typed `/papers`, `/trends`, `/gaps`; `api.ts` 422-list,
network-failure and non-JSON handling. Decisions: no CORS (same-origin via the
Vite proxy); no auth middleware duplicate (the dependency stays the boundary;
the middleware only pre-checks `/upload`); `/query` deadline deferred (Mode B
behaviour). Tests: backend 2276 passed / 10 deselected, evaluation 20,
frontend 51; ruff/oxlint/tsc/build clean; 8/8 mutations killed. Runtime 20/20
plus Vite proxy check. Incident: a query-string probe put the key into the
backend's local access log (22 lines, all 401); the container was recreated,
and its log now has 0 hits. Preservation: 45/45 identical, 19 runs, 632
vectors, reconcile consistent. Handoff: `PHASE12_HANDOFF.md`. Phase 12 not
started.

## 2026-10-18 -- Phase 11 hardening addendum (four targeted fixes)

`PHASE11_HARDENING_ADDENDUM.md`. (1) Access and error logs: uvicorn.access
filter strips query strings; uvicorn.error and the 500 handler log the
exception type only. Verified on a real uvicorn server in tests and in the
rebuilt container (0 dummy-secret hits). (2) `/query` overall deadline
`QUERY_DEADLINE_SECONDS=180`: worker thread + guarded services, so the caller
gets 504 and no further retrieval or model call starts; a call already in
flight is not interrupted (accepted). `workflow.py` untouched. Runtime not run
(would need a live query). (3) Failed-auth throttle `AUTH_FAILURE_LIMIT=20` /
`AUTH_FAILURE_WINDOW_SECONDS=60`, sliding window per peer address; 429 +
Retry-After; a throttled source gets 429 even with the valid key (no oracle);
refused requests are not counted (no extended lockout). Process-local; in
Docker every host client shares the gateway address (accepted). (4) Typed
`/clusters`, `/reports` (exclude_unset; snapshot fallback modelled), `/graph`;
live responses byte-identical before and after. Two route tests' unrealistic
stubs were updated, not the models loosened. Tests: 2324 passed / 10
deselected, evaluation 20, ruff clean, 10/10 mutations killed. Preservation:
45/45, 19 runs, 632 vectors, reconcile consistent.

## 2026-10-18 -- Phase 12 closed: frontend dashboard audit, fixes, real-browser verification

Decision: PHASE 12 COMPLETE -- WITH DOCUMENTED LIMITATIONS (`PHASE12_CLOSEOUT.md`).
Most of the dashboard already existed. A real-browser audit found one broken
flow: saving a valid key after a rejected one left "Connection needed" on
screen until a reload. It also found three partly built features. The graph
overflowed its canvas, its legend covered the truncation note, and a selected
node showed no relationships. Mode B citations were not tied to their claims.
Cluster contradiction pairs were typed but never shown. Fixed in place with
the existing design system: four CSS rules, no new dependencies. Smaller
fixes: missing-key wording, 429 "Try again in about N s" (new
`ApiError.retryAfter`), 401 wording in Ask, pluralised retries, an in-flight
query guard, graph Space key and accessible names. No backend, contract or
`types.ts` change. Tests: frontend 62 (51 before; 11 new, each red first);
tsc, oxlint and build clean. Real browser (headless Chrome over CDP, zero
dependencies): 96/96 checks. Read-only GETs hit the live backend, `/query`
was always mocked, and every write was blocked (none attempted). Secrets: 0
key hits in dist, src or the backend log. Preservation: 0 non-GET requests in
the backend log, reconcile consistent (632 points), Phase 10/11 artefacts
untouched. Corrected two stale handoff notes (the `/query` 504 deadline now
exists; `/clusters`, `/reports` and `/graph` are typed). Handoff:
`PHASE13_HANDOFF.md`. Phase 13 not started.

## 2026-10-18 -- Phase 13 closed; Mode B quality hardening (owner mission brief)

Decision: MODE B COMPLETE WITH LIMITATIONS (one requirement NOT COMPLETE in
the current deployment: the pacing/deadline conflict below); PHASE 13
COMPLETE WITH LIMITATIONS (`PHASE13_CLOSEOUT.md`, run report
`evaluation/output/mode_b_eval/run-20261018-v1/REPORT.md`).

* **Dataset** `evaluation/datasets/mode_b_v1`: 130 questions (41 relevant,
  28 partial, 31 weak, 30 unrelated) plus 60 labelled claims. Agent-authored,
  provisional, frozen with SHA-256 before scoring; passage ids validated
  against the catalogue, which matches the live index 632/632.
* **Harness** `evaluation/mode_b.py`: dataset validation, gate metrics with
  Wilson CIs, threshold chosen on the calibration split only and reported
  once on held-out, ranking, claim-veto, live and review summaries, and a
  manifest. `--reproduce` was verified on the run.
* **Calibration:** at 0.2 the gate accepts 12/12 unrelated and 13/13 weak
  (held out). 0.9153 (chosen on calibration) accepts 0/12 unrelated and 7/13
  weak. Relevant-vs-unrelated AUROC 0.951; partial-vs-weak 0.742. Espresso
  question 0.9131 vs lowest partial 0.9133. **Threshold unchanged**
  (agent labels, ~0.005 margin); the two-paper rule refuses single-paper
  questions (p031, p032) at any threshold.
* **Fixes (each red first; 25/25 mutations killed):**
  * D-B-01: % and thousands separators falsely vetoed (11/30 → 1/30 false
    rejection).
  * D-B-02: inflection, compound and acronym overlap; titles only vouch for
    names.
  * D-B-03: generation sees only gated evidence (graph rows by seed).
  * D-B-04: a pacing-queued Groq call was sent after the 504; now a wait
    budget refuses it.
  * D-B-05: `veto_codes=` in the trace.
  * D-B-06: regeneration feedback explains the codes.
  * D-B-07: duplicate claims removed.
  * D-B-08: the calibration run no longer says "independent".
  * New vetoes: `polarity_mismatch`, `unsupported_name`, `unsupported_cause`.
  * D-13-04: time-shift scored unlabelled gaps as negatives; the adapter
    wrote `[]` (now `null`).
* **Live (brief-authorised): 6 requests, 14 Groq calls.**
  * LV-1: 504 (found D-B-04).
  * LV-1b: grounded, 9/9 checks, expected passage.
  * LV-3: correct abstention.
  * LV-2, 2b, 2c (synthesis): refused by the pre-existing `unsupported_term`
    and `misattributed` vetoes; correctness unverifiable (no draft text).
* **Owner action:** `GROQ_MIN_INTERVAL_SECONDS=60` with
  `QUERY_DEADLINE_SECONDS=180` makes every Mode B path of 4 or more calls end
  in a 504.
* **Phase 13 audit:** export byte-identical to 2026-10-13; corrected adapter
  re-run in `output/scholarlens/run-20261018/`.
* **Tests:** backend 2385 passed / 10 deselected, evaluation 48, frontend 63
  (including the live LV-1b response); ruff, tsc, oxlint and build clean.
* **Preservation:** snapshot identical, reconcile identical, no
  pipeline/collect/reset, 4 backend rebuilds (volumes kept), no commit.
  Handoff: `PHASE14_HANDOFF.md`. Phase 14 not started.

## 2026-10-18 -- Mode B quality mission (retrieval, grounding, synthesis, refusal)

Decision: COMPLETE WITH LIMITATIONS (`MODE_B_QUALITY_CLOSEOUT.md`; metrics
`evaluation/output/mode_b_eval/run-20261018-v2/REPORT.md`). Phase 14 not
started; no commit.

* **D-B-09 -- scope-aware paper minimum.** `app/interactive/intent.py` reads
  the question: a "the ... paper/study" question needs
  `RETRIEVAL_MIN_PAPERS_PAPER_SPECIFIC=1` paper (new setting). Synthesis,
  comparison and unclassifiable questions keep `RETRIEVAL_MIN_PAPERS=2`, and a
  comparison needs each named system in the evidence. Held-out at 0.2:
  relevant 25/27 -> 26/27 (p031); no weak or unrelated change; none of the 13
  two-paper questions is read as single-paper. p032 stays refused.
* **D-B-10 -- withheld claims.** Vetoed or verifier-named claims are withheld.
  The rest stand if at least half survive, with `removed_claim_count`
  (additive API field, shown in the Ask view). The verifier now checks the
  composed claims and the question, not the draft prose.
* **D-B-11 -- the cross-paper (LV-2) refusal, root cause.** Replayed on the
  real passages (`tests/fixtures/mode_b_cross_paper.json`). "Paper N" was read
  as a number the passage must state (live LV-Q2 confirms the model writes it,
  which fits LV-2b's `unsupported_term` on every claim). Correct claims were
  also falsely vetoed: ", and"-joined clauses, "none of" vs "all ... not", and
  a missed split at "[15].Since" (44/632 passages). Paper 1's prior-work claim
  was a correct veto.
* **D-B-12 -- live defects, both fixed and replayed as tests:**
  * LV-Q2 showed paper 1's "Prior studies have observed ..." as paper 1's own
    finding (the attribution subject was lost when "Paper 1" was stripped,
    and "observes" was not an attribution verb);
  * LV-Q3 answered an unanswerable question with a supported but off-question
    claim from a phishing paper. An answer whose claims and passages mention
    none of the question's subjects is now refused (`off_question`).
* **New vetoes:**
  * `wrong_paper`;
  * `unsupported_absence` (gap inferred from absence);
  * `future_as_done` (only when the passage states a plan: Stage 3 types some
    result sentences `future_work`);
  * "all LLMs";
  * digits inside a title-stated name are not numbers.
* **Claim sets** (development, agent-authored):
  * v1: false rejection 1/30, catch 22/30 (unchanged);
  * new `mode_b_claims_s2` (34): false rejection 0/15, catch 13/19.
* **Threshold 0.2 kept.** A cross-encoder top-1 floor (-5.74, calibration
  only) accepts 27/27 relevant and 0/12 unrelated on held-out, but its margin
  is 0.38: candidate only.
* **Live (owner brief):** 6 requests, 13 Groq calls, 20,751 prompt + 9,234
  completion tokens; 0 secrets; 0 retries.
  * LV-Q1 grounded; claim 1 gives a subset count.
  * LV-Q2 grounded, with the misattribution.
  * LV-Q2b partial and correct.
  * LV-Q3 wrong (off-question).
  * LV-Q3b refused, via `unsupported_term`, not the new rule.
  * LV-Q1b grounded.
* **Tests:** backend 2385 -> 2508 passed / 10 deselected; evaluation 48 -> 54;
  frontend 63 -> 71; mutation check 34/35 killed (1 equivalent).
* **Preservation:** snapshot 23/23 identical (before, after deploy, after
  live); reconcile identical; 0 write requests; 5 backend rebuilds (volumes
  kept); no collect/pipeline/reset.

## 2026-10-19 -- Mode B focused mission (threshold, three quality gaps, three bugs)

Decision: COMPLETE WITH LIMITATIONS (`MODE_B_QUALITY_CLOSEOUT.md`, Part 2;
artifacts `evaluation/output/mode_b_eval/run-20261019-v3/`). Phase 14 not
started; no commit, push, stash or discard.

* **D-B-13 -- threshold not locked.** `RETRIEVAL_MIN_SCORE=0.2` (Qdrant cosine,
  `>=`) stays. The cross-encoder floor (-5.74, a logit, not in production) loses
  no answerable question and accepts no unrelated one in either cross-fit
  direction, but labels are agent-authored, the halves' floors differ by 0.58
  (more than the 0.38 margin), 11/13 held-out weak questions still pass, and it
  needs a gate after the rerank. `evaluation/threshold_policy.py` encodes the
  criteria (`lock_verdict`, always `defer` with agent labels).
* **D-B-14 -- `answers_question`.** The verifier returns whether the cited
  evidence answers any part of the question; false refuses without
  regeneration (reason `not_answerable`); the trace has `answers=1|0|-`. Lexical
  coverage cannot decide it (relevant 0.50-1.00, weak 0.29-1.00). **Not shown
  to trigger live:** LV-R2 refused by `low_overlap`; LV-R3/R4 (p099) were
  grounded answers with doubtful claims.
* **D-B-15 -- `partial_total` veto.** A whole-study claim giving a figure that
  is a component of a labelled total (components sum to it) is withheld unless
  scoped or stating the total. Prompts ask for scope, denominators and
  uncertainty. Fixes the 3,630-versus-7,892 class; LV-R1 gave 7,892.
* **D-B-16 -- candidate pool.** `RETRIEVAL_POOL_FACTOR=3`: the cross-encoder
  picks the twelve shown passages from 36 cosine hits; the gate and graph
  expansion still see twelve. Offline: 30 to 40 of 69 answerable questions get a
  labelled passage into the twelve; required papers 31 to 32 of 42.
* **Paper 10 (p035/LV-Q2) is unresolved:** its labelled passages rank 270 and
  397 of 632 by cosine; the reachable ones (ranks 29, 30) are method text the
  cross-encoder rightly rejects. Strict xfail
  `test_paper_10_evidence_reaches_the_model_for_the_p035_question` marks it.
* **Bugs fixed:** the Trends test used literal backspace characters for `\b`
  (the repaired assertion exposed "1 papers in corpus" and "at most 1 papers");
  `/reports` raised `ResponseValidationError` for archived rows missing
  `justification` or evidence keys; the Library view was unmounted, and its
  pipeline request aborted, when the dashboard errored.
* **New:** `backend/tests/interactive/{test_retrieval_threshold_policy,
  test_answerability,test_partial_total,test_candidate_pool}.py`,
  `backend/tests/fixtures/{mode_b_study_results,mode_b_p035_pool}.json`,
  `evaluation/threshold_policy.py`, `evaluation/tests/test_threshold_policy.py`,
  `run-20261019-v3/` (results, manifest, threshold-policy, analysis scripts and
  data, live results, review, pre-registered expectations).
  **Changed:** `claim_check.py`, `workflow.py`, `routes_backend.py`, `config.py`,
  `.env.example`, `App.tsx`, `App.test.tsx`, tests pinning the diagnostics
  string, route arguments and fake settings, `evaluation/README.md`.
* **Live:** 4 requests, 8 Groq calls (LV-R4 tokens 3,843 + 2,394; R1-R3 usage
  lost when the container was replaced); 6 Hugging Face metadata requests; 2
  backend rebuilds (volumes kept); 0 secret hits.
* **Tests:** backend 2508 -> 2550 passed / 10 deselected / 1 strict xfail;
  evaluation 54 -> 65; frontend 71 -> 74; ruff, tsc, oxlint, build clean;
  mutation check 10/10 killed.
* **Preservation:** snapshot (17 tables, Neo4j, 632 Qdrant points, 11 PDFs)
  identical before, after each rebuild and at the end; reconcile identical and
  consistent; no collect/upload/pipeline/reset/re-embedding.
* **Process note:** capture the `groq usage` log lines before rebuilding the
  backend; replacing the container discards its log.

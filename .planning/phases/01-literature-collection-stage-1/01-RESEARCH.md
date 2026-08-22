# Phase 1: Literature Collection (Stage 1) - Research

**Researched:** 2026-08-22
**Domain:** External bibliographic API integration (arXiv, Semantic Scholar, ACL Anthology, Unpaywall) + normalized-corpus persistence (SQLAlchemy/Postgres) + FastAPI file upload
**Confidence:** HIGH (arXiv package, Unpaywall, Semantic Scholar response shape, SQLAlchemy 2.0 idiom — all verified live or via source inspection) / MEDIUM (ACL Anthology approach — verified export exists, but topic-filtering is a hand-rolled loop either way)

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-10:** The `papers` table lives in PostgreSQL via SQLAlchemy (already a Phase-0 runtime dependency), with a plain declarative model. Table columns match README §8 Stage 1 exactly: `id`, `title`, `authors` (JSON array), `year`, `venue`, `doi`, `abstract`, `source`, `pdf_path`, `has_full_text`, plus per-source identifier columns needed for dedup (`arxiv_id`, `acl_id`) and `created_at`/`updated_at`.
- **D-11:** No migration framework (Alembic, etc.) in this phase — `Base.metadata.create_all(engine)` run at app startup is sufficient for a single evolving table at this stage of the project; introducing a migration tool now would be scope creep beyond INGEST-01..09. Revisit if a later phase's schema changes make hand-rolled migration risky.
- **D-12:** PDFs are stored on the local filesystem under `settings.pdf_storage_path` (already defined in Phase 0's `.env` contract), named by a stable identifier (e.g. `{paper_id}.pdf`) — no S3/blob storage, since README's tech stack never introduces one.
- **D-13:** arXiv via the official `arxiv` PyPI package (already named in README §7 tech stack) — query by topic string, pull metadata (title, authors, year via `published`, `summary` as abstract, arXiv ID) and download the PDF via the package's own download helper.
- **D-14:** Semantic Scholar via `httpx` against the public Graph API (`https://api.semanticscholar.org/graph/v1/paper/search`), requesting `title,authors,year,venue,externalIds,abstract,openAccessPdf` fields. Metadata-only per README — if `openAccessPdf.url` is present, store the URL but do not download through this source. `SEMANTIC_SCHOLAR_API_KEY` is sent as the `x-api-key` header when set; unauthenticated (lower rate limit) when absent.
- **D-15:** ACL Anthology via its official bibliographic export (the `acl-anthology` Python package's read-only data API over the public Anthology data, or — if that package proves unsuitable during implementation — direct parsing of the Anthology's published `anthology.bib`/venue XML export). The implementing plan records whichever concrete mechanism it used and why; both are within README's tech-stack line. PDFs are fetched from the Anthology's stable per-paper PDF URL pattern.
- **D-16:** `httpx` moves from Phase 0's `dev` extras to the main `[project] dependencies` in `backend/pyproject.toml`.
- **D-17:** Unpaywall enrichment runs over corpus rows with a `doi` set and `has_full_text=false`, calling `https://api.unpaywall.org/v2/{doi}?email={UNPAYWALL_EMAIL}`; on a `best_oa_location.url_for_pdf`, download and attach to the existing row (never create a new row) and flip `has_full_text=true`.
- **D-18:** Manual upload (`POST /upload`, multipart form, one or more files) writes each PDF to `pdf_storage_path`, creates a corpus row with `source='manual_upload'`, `has_full_text=true`, and metadata fields left `None`. Enforces `MAX_UPLOAD_SIZE_MB` from Settings; rejects non-PDF content types with 415.
- **D-19:** `dedup.py` checks, in order, DOI → arXiv ID → ACL Anthology ID → normalized-title+year, against existing corpus rows before inserting a new one. On a match, the existing row is enriched (fields merged, preferring a value that's present over `None`, and preferring a record that already carries a full-text PDF) rather than duplicated. Normalization for the title+year fallback is lowercase + strip punctuation/whitespace.
- **D-20:** `/collect` (`POST`, JSON body `{"topic": str}`) runs arXiv + Semantic Scholar + ACL Anthology discovery for the topic, then Unpaywall enrichment over the resulting DOI-bearing gaps, returning the count and IDs of papers now in the corpus for that run (new + enriched).
- **D-21:** `/upload` (`POST`, multipart) is the manual-ingestion path described in D-18.
- **D-22:** Both endpoints live in `app/api/routes_ingestion.py`, included from `app/main.py` alongside the existing health router — no new top-level app changes beyond that.
- **D-23:** Per Superpowers TDD, all four source clients and `corpus.py`/`dedup.py` are unit-tested against mocked HTTP responses — no tests make real network calls. `respx` is added as a `dev`-only test dependency if `httpx` mocking needs it (evaluated during Task 1; skip if plain `unittest.mock` suffices).

### Claude's Discretion
- Exact response-field mapping per source (e.g. which Semantic Scholar JSON keys map to which corpus columns) — implementation detail, not a scope decision.
- Whether ACL Anthology integration uses the `acl-anthology` package or direct file parsing — whichever the phase's research/implementation finds more reliable; either satisfies README's tech-stack line.
- Retry/backoff behavior on transient HTTP errors — standard, conservative defaults (a couple of retries with backoff), not a designed feature.

### Deferred Ideas (OUT OF SCOPE)
- Downloading Semantic Scholar's `openAccessPdf` link directly at discovery time — deferred; Unpaywall enrichment (D-17) is the designated full-text-recovery step.
- A learned/fuzzy dedup matcher beyond normalized title+year — out of scope.
- Rate-limiting/backoff tuning beyond conservative defaults — revisit only if real-world testing surfaces throttling problems.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| INGEST-01 | Normalized corpus writer (`corpus.py`) implements the canonical `papers` schema in PostgreSQL | SQLAlchemy 2.0 `DeclarativeBase`/`Mapped`/`mapped_column` pattern documented below; `Base.metadata.create_all()` startup pattern |
| INGEST-02 | `dedup.py` deduplicates across sources in priority order DOI → arXiv ID → ACL ID → normalized title+year | Dedup/merge pattern documented below; normalization approach specified |
| INGEST-03 | arXiv source fetches metadata + full-text PDF for a topic query | `arxiv` package v4.0.1 API fully documented below, **including a package-behavior change that breaks D-13's literal wording** (see Critical Finding) |
| INGEST-04 | Semantic Scholar source fetches metadata (+ `openAccessPdf` link where present); metadata-only by design | Live-verified request/response shape documented below |
| INGEST-05 | ACL Anthology source fetches metadata + open-access PDF via the same corpus writer | Both `acl-anthology` package and direct `anthology+abstracts.bib.gz` parsing documented below with a concrete recommendation |
| INGEST-06 | Unpaywall enrichment resolves OA full text for DOI-bearing, full-text-missing records | Live-verified response shape (success + "no OA" + malformed-DOI 404 cases) documented below |
| INGEST-07 | Manual upload endpoint accepts PDFs, writes to shared PDF store, creates `source='manual_upload'` records | FastAPI `UploadFile`/multipart pattern, `MAX_UPLOAD_SIZE_MB` enforcement, 415 rejection documented in Code Examples |
| INGEST-08 | `/collect` endpoint runs discovery + enrichment for a topic string | Orchestration order documented in Architecture Patterns |
| INGEST-09 | IEEE Xplore, Springer, ACM Digital Library APIs not integrated anywhere | No such package/API researched or recommended anywhere in this document — confirmed absent from Standard Stack |
</phase_requirements>

## Summary

Phase 1 is four independent HTTP/package integrations converging on one SQLAlchemy-backed corpus writer. Three of the four external surfaces were verified directly in this research session (live API calls or installed-package source inspection), which surfaced one finding significant enough to require a plan-level correction and one design choice worth revisiting.

**Critical finding:** D-13 says arXiv PDFs should be downloaded "via the package's own download helper." The currently-published `arxiv` package (v4.0.1, confirmed installed and inspected — `arxiv/__init__.py` from the actual wheel) **has no `download_pdf`/`download_source` method on `Result` or `Client`.** That helper existed in older major versions (pre-3.0) but was removed. The package now only exposes `Result.pdf_url` (a plain string); the PDF must be fetched by the plan's own code (e.g. `httpx.get(result.pdf_url)` streamed to `pdf_storage_path`), consistent with how ACL Anthology and Unpaywall PDFs are already being fetched in this phase. This is not a scope change — it's an implementation detail the plan needs to get right the first time instead of discovering it during TDD.

**ACL Anthology finding:** the `acl-anthology` PyPI package (v1.3.1) is the officially documented Python API, but it pulls a very heavy transitive dependency tree (rich, langcodes+data ~5.6MB, citeproc-py, gitpython, scipy, omegaconf, rnc2rng, ~30 packages total) and its `Anthology.from_repo()` entry point downloads ~120MB of Anthology metadata from GitHub on first use — a lot of weight for "give me papers matching a topic string." The Anthology also publishes `anthology+abstracts.bib.gz` (confirmed live: 42MB gzipped, updated daily) — one BibTeX-formatted file with every paper's title/authors/year/venue/DOI/abstract/canonical URL, from which the Anthology ID and PDF URL are trivially derived (`{url}.pdf`). Parsing that file with `bibtexparser` (mature, stable, 1.4.4) and a keyword-substring filter is lighter, has a much smaller dependency surface, and every field needed for the corpus schema is directly present. This document recommends the direct-parsing approach as primary, with the `acl-anthology` package as the documented fallback per D-15's own wording (both satisfy README's tech-stack line; this is Claude's-discretion territory per CONTEXT.md).

Semantic Scholar's response shape was confirmed with a live unauthenticated call against the real `/graph/v1/paper/search` endpoint — the `fields` parameter and response envelope (`total`, `offset`, `next`, `data[]`) work exactly as D-14 assumes. Unpaywall's response shape was confirmed with two live calls (a real DOI with OA copies, and a malformed/nonexistent DOI) — the "no OA" and "not found" cases are distinguishable and documented below since D-17's happy path alone isn't enough to implement robustly.

**Primary recommendation:** Build `corpus.py` + `dedup.py` first against a SQLAlchemy 2.0 `DeclarativeBase`/`Mapped` model (idiomatic, matches installed SQLAlchemy 2.0.52), then each source module as a thin adapter that maps raw responses onto `corpus.upsert_paper(...)`, fetching PDFs directly via `httpx` (not a source-package helper) in every case except the arXiv/ACL-Anthology "already has a full-text URL" case, which is the same download pattern reused three times.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Topic → candidate paper discovery (arXiv/S2/ACL) | API/Backend | — | Pure server-side HTTP integration; no client involvement |
| DOI-based OA enrichment (Unpaywall) | API/Backend | — | Runs as a backend post-processing step over existing rows |
| Normalized corpus persistence | Database/Storage | API/Backend | Postgres owns durability; backend owns the write path (`corpus.py`) |
| PDF file storage | Database/Storage (filesystem) | API/Backend | Local filesystem under `pdf_storage_path`, written by backend, read by Phase 2 |
| Deduplication logic | API/Backend | — | Pure Python logic against Postgres rows before insert; no separate service |
| Manual PDF upload | API/Backend | Browser/Client (upload form, future phase) | This phase only builds the backend multipart endpoint; no frontend exists yet |
| `/collect`, `/upload` request handling | API/Backend | — | FastAPI routers, no other tier involved in Phase 1 |

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `arxiv` | 4.0.1 [VERIFIED: PyPI/pip index, installed & source-inspected] | arXiv topic search + metadata (`Client`, `Search`, `Result`) | Official/canonical Python wrapper for the arXiv API; already named in README §7 |
| `httpx` | 0.28.1 [VERIFIED: PyPI/pip index, already installed] | Semantic Scholar + Unpaywall + PDF downloads (arXiv/ACL) | Already a Phase-0 dependency (dev); README §7 names it explicitly for S2/Unpaywall |
| `sqlalchemy` | 2.0.52 [VERIFIED: PyPI/pip index, already installed as project dependency] | `papers` table declarative model + engine | Already a Phase-0 runtime dependency (D-10) |
| `bibtexparser` | 1.4.4 [VERIFIED: PyPI/pip index] | Parse ACL Anthology's `anthology+abstracts.bib.gz` export | Mature, single-purpose BibTeX parser; avoids the `acl-anthology` package's heavy dependency tree for a simple topic-filter use case |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `acl-anthology` | 1.3.1 [VERIFIED: PyPI/pip index — installs cleanly, `[OK]` per slopcheck] | Official ACL Anthology data API | Fallback per D-15 if direct `.bib.gz` parsing proves insufficient (e.g. needs richer venue metadata than BibTeX carries) |
| `respx` | 0.23.1 [VERIFIED: PyPI/pip index] | `httpx` request mocking for tests | Add only if `unittest.mock`/`monkeypatch` proves awkward for the S2/Unpaywall client tests (see Testing section) — evaluate during Task 1 per D-23 |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Direct `.bib.gz` parsing for ACL Anthology | `acl-anthology` package | Package gives richer structured objects (Person, Volume, SIG) and stays in sync automatically, but costs ~30 extra transitive packages and a 120MB first-run download for a use case that only needs title/authors/year/venue/DOI/abstract/PDF-URL |
| `httpx` manual PDF download | A hypothetical `arxiv` package `download_pdf()` helper | **Does not exist in the installed v4.0.1** — this alternative is not actually available; document as a corrected assumption, not a live tradeoff |
| `bibtexparser` regex parsing | Hand-rolled regex over the `.bib` file | `bibtexparser` handles BibTeX's brace-nesting and escaping (e.g. `{I}ndian`, multi-line `author` fields) correctly; hand-rolled regex is a classic footgun on real bibliography files |

**Installation:**
```bash
# main dependencies (backend/pyproject.toml [project.dependencies])
arxiv>=4.0,<5
httpx>=0.28,<1   # moves from dev to main per D-16
bibtexparser>=1.4,<2

# dev-only, added conditionally per D-23/Task-1 evaluation
respx>=0.23,<1   # if unittest.mock/monkeypatch proves insufficient
```

**Version verification:** All four core packages confirmed via `pip index versions <pkg>` against the live PyPI index during this research session (2026-08-22): `arxiv` 4.0.1 (latest, released within the last month), `httpx` 0.28.1 (already installed, matches Phase 0), `sqlalchemy` 2.0.52 (already installed, matches Phase 0), `bibtexparser` 1.4.4 (latest stable, 1.x branch has been stable API for years — no known 2.x breaking rewrite yet). `acl-anthology` 1.3.1 confirmed as latest via the same method.

## Package Legitimacy Audit

| Package | Registry | Age | Downloads | Source Repo | slopcheck | Disposition |
|---------|----------|-----|-----------|-------------|-----------|-------------|
| `arxiv` | PyPI | Long-established (v1.0 predates 2020; current maintainer lukasschwab) | High (widely used in academic tooling) | github.com/lukasschwab/arxiv.py | [OK] | Approved |
| `httpx` | PyPI | Long-established, widely used | Very high | github.com/encode/httpx | [OK] | Approved (already installed) |
| `sqlalchemy` | PyPI | Long-established (SQLAlchemy project, 15+ years) | Very high | github.com/sqlalchemy/sqlalchemy | [OK] | Approved (already installed) |
| `bibtexparser` | PyPI | Established (1.x since ~2014) | Moderate-high, standard citation-tooling dependency | github.com/sciunto-org/python-bibtexparser | [OK] | Approved |
| `acl-anthology` | PyPI | Official ACL org package, actively maintained | Moderate (niche/domain-specific, but official) | github.com/acl-org/acl-anthology (confirmed via aclanthology.org/faq/api/ official docs, though slopcheck itself couldn't auto-detect the repo link) | [OK] with a metadata caveat — see note | Approved as fallback |
| `respx` | PyPI | Established, standard `httpx` testing companion | Moderate-high (standard in the httpx ecosystem) | github.com/lundberg/respx | [OK] | Approved, conditional (dev-only, add if needed) |

**Note on `acl-anthology`:** `slopcheck` flagged "No source repository linked" in its PyPI metadata scan — this is a metadata-completeness gap on PyPI's project page, not a legitimacy concern. The package's source repository and official maintainership by the ACL Anthology organization were independently confirmed via `aclanthology.org/faq/api/` (official project documentation, HIGH confidence), which explicitly names `pip install acl-anthology` as the officially supported access method. Since it is listed here only as a fallback (primary recommendation is direct `.bib.gz` parsing), this does not block approval, but the planner should still gate its install behind the standard package-legitimacy checkpoint per CLAUDE.md's pre-authorization rule (well-known, unambiguous match — should not require a pause).

**Packages removed due to slopcheck [SLOP] verdict:** none.
**Packages flagged as suspicious [SUS]:** none.

## Architecture Patterns

### System Architecture Diagram

```
                    POST /collect {"topic": str}
                              │
                              ▼
                 ┌────────────────────────┐
                 │  routes_ingestion.py    │
                 └───────────┬────────────┘
                              │ orchestrates, in order:
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
   sources/arxiv.py   sources/semantic_scholar.py  sources/acl_anthology.py
   (Client.results)   (httpx GET /paper/search)    (bibtexparser over
   → metadata + PDF   → metadata + optional         anthology+abstracts.bib.gz,
     via httpx           openAccessPdf URL           filtered by topic keyword)
     download                (no download)          → metadata + PDF via httpx
          │                   │                   │
          └───────────────────┴───────────────────┘
                              │  each candidate paper passed to:
                              ▼
                        dedup.py.find_existing()
                    (DOI → arXiv ID → ACL ID → title+year)
                              │
                 match found ─┴─ no match
                     │                │
                     ▼                ▼
              merge/enrich       corpus.py.insert_paper()
              existing row        (INSERT into papers)
                     │                │
                     └───────┬────────┘
                              ▼
                  corpus.py.rows_missing_fulltext()
                  (doi IS NOT NULL AND has_full_text = false)
                              │
                              ▼
                      unpaywall.py.enrich()
              (GET /v2/{doi}?email=... per row,
               download best_oa_location.url_for_pdf
               if present, UPDATE existing row)
                              │
                              ▼
              response: {"papers_in_corpus": [...ids], "count": N}


                    POST /upload (multipart, 1+ files)
                              │
                              ▼
                 ┌────────────────────────┐
                 │  routes_ingestion.py    │
                 └───────────┬────────────┘
                              │ per file: validate content-type (PDF only,
                              │ else 415) + size (MAX_UPLOAD_SIZE_MB)
                              ▼
                   manual_upload.py.save_upload()
             write to pdf_storage_path/{paper_id}.pdf
                              │
                              ▼
                corpus.py.insert_paper(source='manual_upload',
                                        has_full_text=True,
                                        metadata=None)
```

### Recommended Project Structure
```
backend/app/ingestion/
├── sources/
│   ├── arxiv.py             # topic → arxiv.Search → Result list → corpus rows + PDF fetch
│   ├── semantic_scholar.py  # topic → httpx GET → JSON → corpus rows (metadata only)
│   └── acl_anthology.py     # topic → parsed .bib.gz entries → corpus rows + PDF fetch
├── unpaywall.py              # doi → httpx GET → enrich existing row
├── manual_upload.py          # UploadFile(s) → filesystem + corpus row
├── dedup.py                  # find_existing(candidate) -> Paper | None; merge(existing, candidate)
└── corpus.py                 # SQLAlchemy model + insert_paper/enrich_paper/rows_missing_fulltext
```

### Pattern 1: Source module returns plain dicts, never touches the DB directly
**What:** Each `sources/*.py` module's public function returns a list of plain dicts shaped like the corpus schema (`title`, `authors`, `year`, `venue`, `doi`, `abstract`, `arxiv_id`/`acl_id`, and — separately — a `pdf_bytes`/`pdf_url` to fetch). `corpus.py` is the only module that touches SQLAlchemy sessions.
**When to use:** Every source integration in this phase — keeps `dedup.py`/`corpus.py` source-agnostic (mirrors README's "Stage 2 onward is source-agnostic" principle one stage early, which makes the source modules trivially unit-testable without a DB).
**Example:**
```python
# app/ingestion/sources/arxiv.py
import arxiv

def search_topic(topic: str, max_results: int = 50) -> list[dict]:
    client = arxiv.Client()
    search = arxiv.Search(query=topic, max_results=max_results)
    results = []
    for r in client.results(search):
        results.append({
            "title": r.title,
            "authors": [a.name for a in r.authors],
            "year": r.published.year,
            "venue": None,
            "doi": r.doi,
            "abstract": r.summary,
            "arxiv_id": r.get_short_id(),
            "pdf_url": r.pdf_url,   # fetched separately via httpx — see Critical Finding
        })
    return results
```

### Pattern 2: PDF download is a shared helper, not per-source logic
**What:** A single `_download_pdf(url: str, dest_path: str) -> bool` helper (in `corpus.py` or a small `pdf_fetch.py`) used by the arXiv source, the ACL Anthology source, and `unpaywall.py` — all three do "GET a URL, stream bytes to `pdf_storage_path/{id}.pdf`."
**When to use:** Anywhere a source hands back a PDF URL rather than raw bytes (which is every source in this phase, now that arXiv's package-level download helper is confirmed gone).
**Example:**
```python
# app/ingestion/corpus.py (or a small shared pdf_fetch.py)
import httpx

def download_pdf(url: str, dest_path: str, timeout: float = 30.0) -> bool:
    try:
        with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as resp:
            if resp.status_code != 200:
                return False
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
        return True
    except httpx.HTTPError:
        return False
```

### Pattern 3: Unpaywall enrichment is a targeted UPDATE, never an INSERT
**What:** `unpaywall.py.enrich()` selects rows via `corpus.rows_missing_fulltext()` (WHERE `doi IS NOT NULL AND has_full_text = false`), calls Unpaywall per DOI, and on success calls `corpus.enrich_paper(paper_id, pdf_path=..., has_full_text=True)` — an UPDATE against the existing primary key, never `insert_paper`.
**When to use:** The entire Unpaywall step (INGEST-06's "creating no new records" requirement).

### Anti-Patterns to Avoid
- **Branching on `source` outside `corpus.py`/dedup:** Once a row is in the corpus, nothing in this phase (or Phase 2+) should special-case `source == 'arxiv'` vs `'manual_upload'` except the dedup priority chain itself and the manual-upload metadata-is-None case, which are the only source-aware logic README allows.
- **Trusting `arxiv.Result.doi` as a dedup key without a null check:** it's `str | None` — most arXiv preprints have no DOI. Dedup must fall through to `arxiv_id` for these, not fail/skip.
- **Assuming Unpaywall 404 means "no OA copy":** a 404 means the DOI itself wasn't found in Unpaywall's index (bad/unregistered DOI) — a JSON error body isn't even returned (see Common Pitfalls). A *valid* DOI with no OA copy returns 200 with `"is_oa": false, "best_oa_location": null`. Conflating these silently drops a retry-worthy signal into the same bucket as "genuinely no OA copy exists."

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| BibTeX parsing (ACL Anthology export) | Regex over `.bib` text | `bibtexparser` | BibTeX has brace-nested, multi-line, and escaped fields (`{I}ndian`, multi-author `and` continuation lines) that break naive regex on real data — confirmed by inspecting the live export in this session |
| arXiv API pagination/backoff | Manual `requests` loop with sleep | `arxiv.Client(page_size=..., delay_seconds=..., num_retries=...)` | The package already implements arXiv's documented rate-limit courtesy (3s between requests) and retry-with-backoff; reimplementing it risks violating arXiv's terms of use |
| HTTP retry/backoff for Semantic Scholar & Unpaywall | Custom retry loop | `httpx`'s built-in `transport=httpx.HTTPTransport(retries=N)` or a small tenacity-free manual 2-retry wrapper (per CONTEXT.md's "conservative defaults, not a designed feature") | Simple, avoids adding `tenacity` as a new dependency for what CONTEXT.md explicitly scoped as "a couple of retries," not a resilience framework |

**Key insight:** every "don't hand-roll" item here is about not re-solving a problem a well-tested library already owns (BibTeX grammar, arXiv's own rate-limit-compliant client) — not about avoiding writing the small amount of glue code (dedup logic, corpus schema) that is this phase's actual job.

## Common Pitfalls

### Pitfall 1: Assuming `arxiv.Result` still has a `download_pdf()` method
**What goes wrong:** Code written from memory/training data (or from tutorials referencing arxiv package v1.x/2.x) calls `result.download_pdf(dirpath=..., filename=...)` and gets an `AttributeError`.
**Why it happens:** Older major versions of the `arxiv` package did expose this helper; it was removed by the time of v4.0.1 (confirmed absent from the actual installed wheel's source in this session — no `download` string anywhere in `arxiv/__init__.py`).
**How to avoid:** Fetch `result.pdf_url` and download it with the shared `httpx` helper (Pattern 2 above), exactly as the ACL Anthology and Unpaywall paths already need to.
**Warning signs:** `AttributeError: 'Result' object has no attribute 'download_pdf'` at runtime; a TDD red-phase test asserting the method exists will fail immediately if written first — good, that's exactly the discovery TDD is for here.

### Pitfall 2: Unpaywall 404 vs. "no OA location" look similar but aren't
**What goes wrong:** Code that does `resp.json()` unconditionally on any 2xx-or-not response either crashes on the 404 HTML body, or worse, silently treats a malformed/unregistered DOI the same as "no OA copy exists for this valid paper."
**Why it happens:** Live-verified in this session: a genuinely unregistered/malformed DOI returns a **plain HTML 404 page** (not JSON) with `Content-Type` not `application/json`. A syntactically well-formed but unrecognized-parameter request (e.g. missing/placeholder email) returns a **422 JSON body** `{"error": true, "message": "..."}`. A valid, registered DOI with no OA copy returns **200** with `"is_oa": false, "best_oa_location": null`.
**How to avoid:** Check `resp.status_code == 200` before parsing JSON; treat non-200 as "enrichment failed for this row, log and move on" (don't flip `has_full_text`); only treat `best_oa_location is None` as the true "no OA copy found" case.
**Warning signs:** Unhandled `json.JSONDecodeError` in tests/logs when a DOI turns out to be bad; corpus rows with `has_full_text=True` but no actual PDF on disk.

### Pitfall 3: ACL Anthology PDF URL construction from BibTeX `url` field
**What goes wrong:** The `.bib` export's `url` field is the **landing page** (`https://aclanthology.org/2026.wildre-1.1/`, with a trailing slash), not the PDF. Appending `.pdf` directly to that string yields `.../2026.wildre-1.1/.pdf` (wrong — double slash before the extension).
**Why it happens:** Confirmed by inspecting live `.bib.gz` content in this session — the `url` field always has a trailing `/`.
**How to avoid:** Strip the trailing slash before appending `.pdf`: `url.rstrip("/") + ".pdf"`. Verify against a live paper URL (e.g. `https://aclanthology.org/2023.acl-long.310.pdf`, confirmed reachable via WebSearch cross-reference) before trusting the pattern in tests.
**Warning signs:** 404s when downloading ACL Anthology PDFs despite metadata parsing succeeding.

### Pitfall 4: Semantic Scholar `authors` objects are not plain strings
**What goes wrong:** Code that does `paper["authors"]` and stores it directly as the corpus's JSON `authors` array ends up storing `[{"authorId": "...", "name": "..."}]` objects instead of the plain name strings the arXiv/manual-upload paths use, breaking downstream consistency (README's "source-agnostic" schema).
**Why it happens:** Live-verified response shape: each author is `{"authorId": str, "name": str}`, not a bare string.
**How to avoid:** Map `[a["name"] for a in paper["authors"]]` in `sources/semantic_scholar.py` before handing the dict to `corpus.py`, matching the plain-string-list shape used by the arXiv source (`[a.name for a in r.authors]`).
**Warning signs:** `authors` column values inconsistent in shape across rows from different sources; any downstream code that does `", ".join(row.authors)` crashing on Semantic-Scholar-sourced rows.

### Pitfall 5: `acl-anthology` package's first-use cost surprises CI/test runs
**What goes wrong:** If the `acl-anthology` package fallback is used, `Anthology.from_repo()` downloads ~120MB on first call — acceptable for a one-time interactive discovery run, but disastrous if accidentally invoked inside a unit test (slow, network-dependent, violates D-23's "no tests make real network calls").
**Why it happens:** `from_repo()` is designed for long-lived processes (build the Anthology once, query many times), not per-request use.
**How to avoid:** If the fallback is used, only call `from_repo()` once at module import or app-lifespan time, never inside a request handler or a test; unit tests must mock the `Anthology`/`Paper` objects, never instantiate a real one.

## Code Examples

### Semantic Scholar search request (live-verified shape)
```python
# Source: live GET https://api.semanticscholar.org/graph/v1/paper/search
#          ?query=test&fields=title,authors,year,venue,externalIds,abstract,openAccessPdf
# Verified in this research session, 2026-08-22
import httpx

def search_topic(topic: str, api_key: str | None) -> list[dict]:
    headers = {"x-api-key": api_key} if api_key else {}
    params = {
        "query": topic,
        "fields": "title,authors,year,venue,externalIds,abstract,openAccessPdf",
    }
    resp = httpx.get(
        "https://api.semanticscholar.org/graph/v1/paper/search",
        params=params, headers=headers, timeout=10.0,
    )
    resp.raise_for_status()
    body = resp.json()   # {"total": int, "offset": int, "next": int, "data": [...]}
    results = []
    for p in body.get("data", []):
        results.append({
            "title": p.get("title"),
            "authors": [a["name"] for a in p.get("authors", [])],
            "year": p.get("year"),
            "venue": p.get("venue") or None,
            "doi": (p.get("externalIds") or {}).get("DOI"),
            "abstract": p.get("abstract"),
            "openaccess_pdf_url": (p.get("openAccessPdf") or {}).get("url"),
        })
    return results
```

### Unpaywall enrichment call (live-verified shape)
```python
# Source: live GET https://api.unpaywall.org/v2/{doi}?email=... — verified in this session
import httpx

def resolve_oa_pdf(doi: str, email: str) -> str | None:
    resp = httpx.get(f"https://api.unpaywall.org/v2/{doi}", params={"email": email}, timeout=10.0)
    if resp.status_code != 200:
        return None   # 404 = DOI not found in Unpaywall's index; treat as "cannot resolve", not "no OA"
    body = resp.json()
    best = body.get("best_oa_location")
    if not best:
        return None   # valid DOI, genuinely no OA copy: is_oa=false, best_oa_location=null
    return best.get("url_for_pdf") or best.get("url")
```

### SQLAlchemy 2.0 declarative model for `papers` (D-10 schema)
```python
# Source: SQLAlchemy 2.0 official ORM mapping style (docs.sqlalchemy.org/en/20/orm/mapping_styles.html)
from datetime import datetime
from sqlalchemy import JSON, String, Boolean, Integer, DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Paper(Base):
    __tablename__ = "papers"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    authors: Mapped[list | None] = mapped_column(JSON, nullable=True)
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    venue: Mapped[str | None] = mapped_column(String, nullable=True)
    doi: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    abstract: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, nullable=False)
    pdf_path: Mapped[str | None] = mapped_column(String, nullable=True)
    has_full_text: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    arxiv_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    acl_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
```

```python
# app/main.py startup — no Alembic (D-11)
from app.ingestion.corpus import Base
from sqlalchemy import create_engine

@app.on_event("startup")   # or lifespan context, matching FastAPI version already in use
def _create_tables() -> None:
    engine = create_engine(get_settings().postgres_url)
    Base.metadata.create_all(engine)
```

### FastAPI multipart upload with size/type enforcement
```python
# Source: FastAPI official docs pattern (UploadFile + File(...)) — standard, HIGH confidence
from fastapi import APIRouter, UploadFile, File, HTTPException

router = APIRouter()

@router.post("/upload")
async def upload_papers(files: list[UploadFile] = File(...)):
    settings = get_settings()
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    created_ids = []
    for f in files:
        if f.content_type != "application/pdf":
            raise HTTPException(status_code=415, detail=f"{f.filename} is not a PDF")
        contents = await f.read()
        if len(contents) > max_bytes:
            raise HTTPException(status_code=413, detail=f"{f.filename} exceeds max upload size")
        # write to pdf_storage_path, insert corpus row with source='manual_upload'
        ...
    return {"created": created_ids}
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|---------------|--------|
| `arxiv.Result.download_pdf()` package helper | Manual `httpx`/`urlretrieve` download of `result.pdf_url` | Removed by v3.0.0/v4.0.0 (confirmed absent in installed v4.0.1) | D-13's literal wording ("via the package's own download helper") no longer matches the package's actual API — plan must implement the download itself |
| SQLAlchemy 1.x `Column`/`declarative_base()` | SQLAlchemy 2.0 `DeclarativeBase` + `Mapped`/`mapped_column` | SQLAlchemy 2.0 (2023) | Already the version installed (2.0.52) — use the 2.0-native style, not the 1.x style, for any new model |

**Deprecated/outdated:**
- `arxiv` package's built-in PDF download helper — removed; do not write code assuming it exists.
- SQLAlchemy's untyped `Column(...)` declarative style still works under 2.0 but is legacy; `Mapped`/`mapped_column` is the documented idiomatic style for new models.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `acl-anthology` package's source repository is `github.com/acl-org/acl-anthology` despite slopcheck's "no source repo linked" flag | Package Legitimacy Audit | Low — corroborated by official `aclanthology.org/faq/api/` documentation (independent source), and the package is used only as a fallback in this plan |
| A2 | `bibtexparser` 1.4.4 handles the ACL Anthology's specific BibTeX dialect (LaTeX-escaped titles like `{I}ndian`, multi-author continuation lines) without custom pre-processing | Standard Stack / Don't Hand-Roll | Medium — if it doesn't, the plan needs a pre-processing step or the `acl-anthology` package fallback; verify with a real parse of a sample chunk during Task 1, not just at review time |
| A3 | `bibtexparser`'s 1.x branch remains the recommended version (no forced migration to a 2.x API) as of this research | Standard Stack | Low — 1.4.4 is the latest available version on PyPI at research time; if a breaking 2.x exists later, pin `<2` in `pyproject.toml` as already recommended |

## Open Questions

1. **(RESOLVED) Does the ACL Anthology's BibTeX export reliably carry a machine-derivable "Anthology ID" for dedup (INGEST-02's "ACL Anthology ID" key)?**
   - What we know: the `url` field's path segment (e.g. `2026.wildre-1.1`) is the Anthology ID and is present on every verified entry in this session's sample.
   - What's unclear: whether every historical entry (pre-2020 "old style" IDs like `E91-1001`) follows the same extractable pattern from the `url` field, or needs the BibTeX cite-key (e.g. `kranti-vajjala-2026-metricalargs`) as a fallback.
   - **Resolution (post-plan-check):** derive the Anthology ID from the `url` field's last path segment first; when `url` is absent, fall back to the BibTeX cite key (`entry["ID"]`, always present) and construct `pdf_url` as `https://aclanthology.org/{acl_id}.pdf`. Implemented and unit-tested in Plan 01-04 Task 2 (Test 6) against a dedicated url-less fixture entry (`E91-1001`).

2. **(RESOLVED) Semantic Scholar unauthenticated rate limit — exact requests/second?**
   - What we know: an API key gives 1 req/sec; unauthenticated is documented as "lower" without this research session finding an exact published number.
   - What's unclear: the precise unauthenticated ceiling (community reports vary; Semantic Scholar's own docs are not fully explicit on the exact shared-pool number).
   - **Resolution (post-plan-check):** rather than guessing a requests/sec ceiling, both the Semantic Scholar client (Plan 01-03) and the Unpaywall client (Plan 01-05) issue requests via `httpx.get(..., transport=httpx.HTTPTransport(retries=2))` — a conservative, hardcoded-number-free retry default, not a rate-limit-aware resilience framework. This directly addresses the plan-checker's Warning 1 (no retry/backoff was implemented) and closes this open question without inventing an unverified number.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Internet access to arxiv.org | arXiv source | ✓ (verified via WebFetch/pip during research) | — | — |
| Internet access to api.semanticscholar.org | Semantic Scholar source | ✓ (live-verified in this session) | — | — |
| Internet access to aclanthology.org | ACL Anthology source | ✓ (live-verified in this session, `.bib.gz` fetched via curl) | — | — |
| Internet access to api.unpaywall.org | Unpaywall enrichment | ✓ (live-verified in this session) | — | — |
| PostgreSQL (Docker Compose) | `corpus.py` | ✓ (Phase 0 established, `check_postgres` pattern exists) | — | — |
| `PDF_STORAGE_PATH` writable directory | PDF storage (all sources + manual upload) | Assumed ✓ per Phase 0 `.env.example` contract | — | Verify directory exists/is writable at app startup, matching Phase 0's Settings pattern |

**Missing dependencies with no fallback:** none identified.
**Missing dependencies with fallback:** none identified — all four external APIs responded successfully during this research session.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 8.3.x (already configured, `backend/pyproject.toml` `[tool.pytest.ini_options]`) |
| Config file | `backend/pyproject.toml` (`testpaths = ["tests"]`) |
| Quick run command | `pytest backend/tests/ingestion -x` |
| Full suite command | `pytest backend/tests -x` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| INGEST-01 | `corpus.py` creates/reads/updates a `Paper` row with the D-10 schema | unit | `pytest backend/tests/ingestion/test_corpus.py -x` | ❌ Wave 0 |
| INGEST-02 | `dedup.py` matches in DOI→arXiv-ID→ACL-ID→title+year order and merges rather than duplicates | unit | `pytest backend/tests/ingestion/test_dedup.py -x` | ❌ Wave 0 |
| INGEST-03 | arXiv source maps `Result` objects to corpus dicts; PDF downloaded via shared helper | unit (mocked `arxiv.Client.results`, mocked `httpx`) | `pytest backend/tests/ingestion/sources/test_arxiv.py -x` | ❌ Wave 0 |
| INGEST-04 | Semantic Scholar source maps JSON response to corpus dicts, never downloads a PDF itself | unit (mocked `httpx.get`) | `pytest backend/tests/ingestion/sources/test_semantic_scholar.py -x` | ❌ Wave 0 |
| INGEST-05 | ACL Anthology source parses `.bib` entries (or package objects) to corpus dicts + derives PDF URL correctly (trailing-slash pitfall) | unit (mocked HTTP fetch of a small `.bib` fixture) | `pytest backend/tests/ingestion/sources/test_acl_anthology.py -x` | ❌ Wave 0 |
| INGEST-06 | Unpaywall enrichment distinguishes 200/no-OA vs. non-200, updates existing row only | unit (mocked `httpx.get` with 200/404/422 fixtures) | `pytest backend/tests/ingestion/test_unpaywall.py -x` | ❌ Wave 0 |
| INGEST-07 | `/upload` accepts PDFs, rejects non-PDF (415), rejects oversized (413), creates `manual_upload` rows | integration (FastAPI `TestClient`) | `pytest backend/tests/api/test_routes_ingestion.py -x -k upload` | ❌ Wave 0 |
| INGEST-08 | `/collect` orchestrates all sources + enrichment and returns counts/IDs | integration (FastAPI `TestClient`, all sources mocked) | `pytest backend/tests/api/test_routes_ingestion.py -x -k collect` | ❌ Wave 0 |
| INGEST-09 | No IEEE/Springer/ACM package or URL anywhere in `app/ingestion/` | static check | `grep -rEi "ieee|springer|acm\.org" backend/app/ingestion/` (expect no matches) — can be a cheap unit test asserting the source module list is exactly `{arxiv, semantic_scholar, acl_anthology}` | ❌ Wave 0 (trivial) |

### Sampling Rate
- **Per task commit:** the relevant single test file above (e.g. `pytest backend/tests/ingestion/sources/test_arxiv.py -x`)
- **Per wave merge:** `pytest backend/tests/ingestion backend/tests/api -x`
- **Phase gate:** `pytest backend/tests -x` full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `backend/tests/ingestion/__init__.py`, `backend/tests/ingestion/sources/__init__.py` — new test package directories
- [ ] `backend/tests/ingestion/test_corpus.py`, `test_dedup.py`, `test_unpaywall.py` — new fixture-based unit tests
- [ ] `backend/tests/ingestion/sources/test_arxiv.py`, `test_semantic_scholar.py`, `test_acl_anthology.py`
- [ ] `backend/tests/api/test_routes_ingestion.py`
- [ ] Small ACL Anthology `.bib` fixture file (5-10 real entries, copied from the live export sample obtained during this research) for `test_acl_anthology.py` — avoids any test depending on the live 42MB download
- [ ] Decision needed in Task 1 per D-23: does `unittest.mock`/`monkeypatch` on `httpx.get`/`httpx.stream` suffice, or is `respx` needed? (Recommendation: try monkeypatch first, matching Phase 0's `test_health.py` precedent; add `respx` only if mocking streaming responses proves awkward)

## Security Domain

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-------------------|
| V2 Authentication | No | This phase has no user-facing auth; `/collect`/`/upload` are unauthenticated internal endpoints (API-02's API-key auth is Phase 11's job) |
| V3 Session Management | No | No sessions in this phase |
| V4 Access Control | No | Not in scope for Phase 1 (deferred to Phase 11 per README) |
| V5 Input Validation | Yes | FastAPI/Pydantic request-body validation for `/collect`'s `{"topic": str}`; explicit content-type check (`application/pdf`) and size check (`MAX_UPLOAD_SIZE_MB`) for `/upload` |
| V6 Cryptography | No | No crypto operations in this phase |
| V12 File Handling (ASVS 4.0 renumbering; V5.x areas depending on version) | Yes | PDF upload path handling — see Threat Patterns below |

### Known Threat Patterns for this stack

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|----------------------|
| Path traversal via uploaded filename (`../../etc/passwd`) | Tampering | Never use the client-supplied filename directly for the destination path; always derive the on-disk filename from a server-generated `paper_id` (D-12 already specifies this — `{paper_id}.pdf`), never `f.filename` |
| Malicious/oversized file upload (zip bomb-style PDF, disk exhaustion) | Denial of Service | Enforce `MAX_UPLOAD_SIZE_MB` by checking `len(contents)` (or streaming with a running byte counter for very large files) before writing to disk; reject with 413 |
| Content-type spoofing (a non-PDF file with `Content-Type: application/pdf` header) | Tampering | `f.content_type` from the client is self-reported and not fully trustworthy; a defense-in-depth option is checking the file's magic bytes (`%PDF-`) before accepting — flagged here as a recommended hardening the plan should consider even though CONTEXT.md's D-18 only specifies content-type rejection |
| SSRF via attacker-influenced PDF URLs (Unpaywall/ACL Anthology returning a URL to an internal address) | Spoofing/Tampering | Both Unpaywall and ACL Anthology are trusted, fixed-domain external services (not user input), so this risk is low; still, the shared `download_pdf()` helper should use `httpx`'s default redirect-following with a reasonable timeout and should not resolve/fetch arbitrary user-supplied URLs — the only user-controlled string in this phase is the `/collect` topic, which is used as a query parameter, never as a URL |
| DOI injection into Unpaywall URL path | Tampering | DOIs come from S2/GROBID-recovered metadata, not raw user input, but should still be URL-escaped (`httpx` does this automatically via `params=`) rather than manually string-concatenated into the path |

## Sources

### Primary (HIGH confidence)
- Live `pip index versions` against PyPI registry (arxiv, httpx, sqlalchemy, bibtexparser, acl-anthology, respx) — 2026-08-22
- Installed-package source inspection: `arxiv==4.0.1` wheel extracted and read directly (`arxiv/__init__.py`) — confirms `Result`/`Search`/`Client` fields and the absence of any download method
- Live API call: `GET https://api.semanticscholar.org/graph/v1/paper/search?query=test&fields=...` — confirms response envelope and field shapes
- Live API calls: `GET https://api.unpaywall.org/v2/{doi}?email=...` (valid DOI with OA, invalid params, and nonexistent DOI) — confirms 200/422/404 behaviors
- Live `curl -I`/byte-range fetch: `https://aclanthology.org/anthology+abstracts.bib.gz` and `anthology.bib.gz` — confirms file exists, size, and BibTeX field shapes
- `aclanthology.org/faq/api/` — official ACL Anthology documentation on programmatic access
- `slopcheck` package-legitimacy scan run locally against all six candidate packages

### Secondary (MEDIUM confidence)
- WebFetch of `acl-anthology` GitHub README and readthedocs pages — confirms `Anthology.from_repo()` usage pattern and ~120MB first-run download, but did not fully enumerate venue/year filtering methods (readthedocs pages returned partial/loading-error content in this session)
- WebSearch cross-references for SQLAlchemy 2.0 `DeclarativeBase`/`Mapped` idiom (standard, well-known pattern, not independently re-verified against docs.sqlalchemy.org in this session beyond search-result excerpts)

### Tertiary (LOW confidence)
- Semantic Scholar exact unauthenticated rate limit (requests/second) — not found as an explicit published number in this session; flagged as Open Question 2

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — every core package version verified against the live PyPI registry; arXiv package behavior verified against actual installed source, not documentation alone
- Architecture: HIGH — corpus/dedup/source-module separation follows README's own stated "source-agnostic" principle and Phase 0's established `Settings`/router patterns
- Pitfalls: HIGH for arXiv-download-helper-removed, Unpaywall response-shape, and ACL-Anthology-PDF-URL findings (all live-verified in this session); MEDIUM for the exact scope of `acl-anthology` package's venue/year filtering API (readthedocs pages didn't fully render during research)

**Research date:** 2026-08-22
**Valid until:** ~30 days for the four external APIs' general behavior (bibliographic APIs are slow-moving); re-verify the `arxiv` package version specifically before implementation if more than a few weeks pass, since it has changed major-version behavior before.

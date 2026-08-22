# Phase 2: Parsing & Section Normalization (Stage 2) - Research

**Researched:** 2026-08-22
**Domain:** GROBID-based PDF parsing, TEI-XML section extraction, transformer-embedding section-heading normalization
**Confidence:** HIGH (GROBID API/TEI shape — verified via a real, live GROBID run this session) / MEDIUM (rhetorical-role classifier approach — no pretrained shortcut model exists, confirming D-30's design by elimination)

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

**GROBID service**
- **D-24:** GROBID is added to `docker-compose.yml` as a new `grobid` service using the official `grobid/grobid` Docker image, exposing its REST API on port 8070. No GPU/CRF-only lightweight variant is specified by README, so the full image is used.
- **D-25:** The backend talks to GROBID over HTTP via its `/api/processFulltextDocument` endpoint, using `httpx` — no dedicated GROBID Python client library.
- **D-26:** `GROBID_URL` is added to Settings/`.env.example`, read via `get_settings()` — never inline.

**Parsing pipeline structure**
- **D-27:** `app/parsing/grobid_client.py` wraps the GROBID HTTP call; `app/parsing/tei_parser.py` extracts sections/paragraphs/sentences from TEI-XML; `app/parsing/fallback.py` wraps PyMuPDF/pdfplumber; `app/parsing/section_classifier.py` implements heading-normalization; `app/parsing/service.py` orchestrates: try GROBID -> on failure/timeout fall back -> normalize sections -> persist.
- **D-28:** Parsed output is stored as a single `ParsedDocument` table (JSON column: `sections: list[{type, heading, paragraphs: list[{text, sentences: list[str]}]}]`) linked to `papers.id` via `paper_id` FK — not three fully-normalized relational tables.
- **D-29:** Sentence splitting uses a standard, lightweight tokenizer (regex-based, or `nltk` punkt if warranted) rather than a heavyweight NLP pipeline.

**Section-heading normalization**
- **D-30 (resolved via research):** Implemented as embedding-based semantic similarity, not supervised fine-tuning: each heading is embedded with SciBERT (or a suitable sentence-embedding model) and matched via cosine similarity against embeddings of the 7 canonical category labels (+ exemplar phrases). A cheap exact/near-exact keyword pre-filter short-circuits the embedding call for the common case. This session's research reconfirms this is the correct approach — no pretrained rhetorical-role classifier exists as a shortcut (see Assumptions Log A2).
- **D-31:** GROBID's TEI already tags `<div>` elements with a `type` attribute for many standard sections where present, falling back to classifying raw heading text when absent. **This session's research found this premise only half-true — see Summary and Pitfall 1 for the correction:** body-section `type` attributes are essentially never present in practice; only back-matter categories (acknowledgement/references/annex/availability/conflict/contribution/funding) are reliably typed.

**Manual-upload metadata backfill**
- **D-32:** GROBID's `/api/processHeaderDocument` (or equivalently, header fields in the full-text TEI) extracts title/authors/DOI for `source='manual_upload'` records with `title IS NULL`. Backfilled fields are written via `corpus.enrich_paper` (UPDATE-only, never a new insert).
- **D-33:** After backfill, if a DOI was recovered, the record is eligible for the same Semantic Scholar/Unpaywall enrichment path Phase 1 built — reusing existing logic, not duplicating it.

**Fallback and error handling**
- **D-34:** GROBID failure modes handled: connection error/timeout, and non-200/malformed-TEI response — both trigger the PyMuPDF/pdfplumber fallback. Fallback produces a best-effort, materially lower-fidelity section structure.
- **D-35:** A conservative retry (`httpx.HTTPTransport(retries=2)`) is used before falling back; a generous timeout (not the 5-10s used for Phase 1's lightweight APIs) accounts for GROBID's substantial startup/first-request latency.

**Testing strategy**
- **D-36:** TDD per CLAUDE.md — all GROBID/TEI-XML parsing logic is unit-tested against small, real-but-small TEI-XML fixture files (captured once from a real GROBID call, then used offline) — no test makes a real network call to a live GROBID container. The classifier's embedding-similarity logic is tested with mocked/small embedding vectors, not the full SciBERT model in every test run.

### Claude's Discretion
- Exact GROBID Docker image tag/version — pin to a specific recent stable release rather than floating `latest`. **This research recommends `0.9.1`** (current stable per GROBID docs as of this session).
- Whether the section classifier uses raw `AutoModel` SciBERT embeddings via `transformers` + `torch`, or a lighter `sentence-transformers` wrapper — either satisfies "SciBERT-based" in spirit; this research did not benchmark both (see Assumptions Log A3), left to the planner.
- Exact fallback heuristic for PyMuPDF/pdfplumber section detection — a reasonable best-effort heuristic, not a research problem in itself.

### Deferred Ideas (OUT OF SCOPE)
- Training or fine-tuning a supervised rhetorical-role classifier on a labeled dataset — out of scope; no such dataset or training phase exists anywhere in the 15-phase roadmap.
- Extending `/health` to verify GROBID connectivity — not required by PARSE-01..06; Phase 0's `/health` contract (D-06) is locked to neo4j/qdrant/postgres.
- A more sophisticated PDF-fallback heuristic (e.g. a learned layout model) — PyMuPDF/pdfplumber is explicitly a fallback, not a co-primary parser.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|--------------------|
| PARSE-01 | GROBID parses each PDF into structured sections/paragraphs/sentences | Verified real GROBID request/response shape (`/api/processFulltextDocument`, `segmentSentences=1`); real TEI fixture captured — see Standard Stack, Architecture Patterns, Code Examples |
| PARSE-02 | PyMuPDF/pdfplumber fallback handles PDFs GROBID cannot parse cleanly | Verified current stable versions (`pymupdf` 1.28.2, `pdfplumber` 0.11.10) and their font/block metadata APIs suited to a heading heuristic — see Standard Stack, Don't Hand-Roll |
| PARSE-03 | A SciBERT rhetorical-role classifier normalizes inconsistent venue headings onto the common schema | Confirmed no pretrained shortcut classifier exists (D-30 reconfirmed); confirmed GROBID does not provide the `div[@type]` hint D-31 assumed (correction) — see Summary, Pitfall 1, Assumptions Log A2 |
| PARSE-04 | Parsing reads from the normalized corpus only and never branches on a paper's `source` field | Architectural Responsibility Map + Validation Architecture test map (`test_service.py` parametrized over `source`) |
| PARSE-05 | Manual-upload records missing metadata are backfilled via GROBID header extraction (title, authors, DOI) | Verified real `/api/processHeaderDocument` response shape (empty `<text>`, populated `<teiHeader>`); real header TEI fixture captured — see Pattern 3, Code Examples |
| PARSE-06 | A DOI recovered via backfill becomes eligible for the same Semantic Scholar/Unpaywall enrichment as any other paper | Confirmed D-33's reuse-not-duplicate design against Phase 1's `enrich_paper`/`rows_missing_fulltext` contract — see Architectural Responsibility Map, Validation Architecture |
</phase_requirements>

## Summary

GROBID's REST API and TEI-XML output were verified directly in this session by pulling `grobid/grobid:0.9.1-crf`, running a real container, and POSTing a real arXiv PDF ("Attention Is All You Need") to both `/api/processFulltextDocument` and `/api/processHeaderDocument`. This surfaced one finding that **corrects an assumption in CONTEXT.md D-31**: GROBID's body `<div>` elements are **not** tagged with section-semantic `type` attributes like `type="introduction"` — that only happens for a narrow, fixed set of back-matter categories (`acknowledgement`, `references`, `annex`, `availability`, `conflict`, `contribution`, `funding`). Ordinary body sections (Introduction, Methods, Results, Discussion, Conclusion, etc.) come through as untyped `<div><head>...</head><p>...</p></div>` — the heading *text* is the only signal GROBID gives you for those. This is a **hard correction to D-31's premise**, not just a caveat: the section classifier cannot lean on a GROBID-provided type hint for the seven canonical categories and must classify heading text (and, where useful, first-paragraph content) in effectively all cases. The keyword-then-embedding cascade in D-30 remains the right design — it's simply doing more of the classification work than D-31 implied.

For the rhetorical-role classifier (D-30's open question), no ready-to-use pretrained HuggingFace classifier for IMRaD/rhetorical-role section labeling was found that requires zero training data. CSAbstruct (`allenai/csabstruct`) is a labeled **dataset**, not a shipped classifier checkpoint; ScholarBERT and SciBERT are base encoders, not classifiers. This confirms — by elimination, not by finding a better alternative — that D-30's embedding-similarity-against-label-exemplars approach (keyword pre-filter → SciBERT/sentence-embedding cosine similarity against the 7 canonical labels) is the correct buildable-without-training-data approach for this MVP.

A materially useful bonus finding: GROBID's `segmentSentences=1` request parameter makes GROBID itself return sentence-segmented TEI (`<s>` tags inside each `<p>`) for the primary (GROBID-success) path. This narrows D-29's sentence-splitter need to the PyMuPDF/pdfplumber **fallback path only** — the GROBID path needs no separate sentence tokenizer if this parameter is set.

**Primary recommendation:** Use `grobid/grobid:0.9.1-full` (per D-24) in docker-compose with a generous healthcheck `start_period`; call `/api/processFulltextDocument` with `segmentSentences=1` via `httpx`; classify section headings with a keyword pre-filter falling back to SciBERT-checkpoint sentence-embedding cosine similarity (no GROBID `div[@type]` shortcut exists for body sections — budget for classifying every heading); use `/api/processHeaderDocument` (or the header block already present in the fulltext TEI) for D-32/PARSE-05 backfill; use the two real TEI-XML fixtures captured this session for all offline unit tests (D-36).

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| PDF → TEI-XML parsing | API/Backend (calls external GROBID service) | — | GROBID runs as its own containerized service; the backend is a thin HTTP client to it, not a parsing engine itself |
| TEI-XML → structured sections/paragraphs/sentences | API/Backend | — | Pure data transformation (`tei_parser.py`), no external call |
| Fallback parsing (PyMuPDF/pdfplumber) | API/Backend | — | In-process library calls, no separate service |
| Section-heading normalization (classifier) | API/Backend | — | In-process model inference (SciBERT/sentence-transformers), no separate service; loaded once per process, not per request |
| Persistence of parsed structure | Database/Storage (Postgres) | API/Backend (SQLAlchemy layer) | Mirrors Phase 1's `corpus.py` pattern — API layer owns the write path, Postgres owns storage |
| Manual-upload metadata backfill + re-enrichment trigger | API/Backend | Database/Storage | Reuses Phase 1's `enrich_paper`/`rows_missing_fulltext` contract; no new service |

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `grobid/grobid` (Docker image) | `0.9.1-full` (also `0.9.1-crf` — lighter, no GPU/DL fulltext models) | Primary PDF→TEI-XML parser | The canonical, actively maintained image published by the GROBID project itself (`grobidOrg/grobid` on GitHub); `lfoppiano/grobid` is a mirror using the same tags — both point to the same upstream project as of `0.8.2`+ [CITED: grobid.readthedocs.io/en/latest/Grobid-docker/] |
| `httpx` | `>=0.28,<1` (already pinned in `pyproject.toml`) | GROBID REST client | Already a project dependency (D-25); no new package needed for the HTTP call surface |
| `pymupdf` | `1.28.2` (verified on PyPI, 2026-08-22) [VERIFIED: PyPI registry, but see provenance note below] | Fallback PDF text/layout extraction | Fast, actively maintained, exposes font-size/boldness/block metadata needed for a heading heuristic |
| `pdfplumber` | `0.11.10` (verified on PyPI, 2026-08-22) [VERIFIED: PyPI registry, but see provenance note below] | Fallback PDF text/table extraction | Higher-level text/line extraction API than raw PyMuPDF; useful as a second-opinion fallback per README naming both |
| `transformers` | `4.57.6` (latest 4.x; `5.15.1` also exists but is a new major line — recommend pinning to the 4.x line for stability with existing SciBERT checkpoints) [VERIFIED: PyPI registry, but see provenance note below] | Load SciBERT / run embeddings | README explicitly names "SciBERT" and "Hugging Face Transformers" (§7) |
| `torch` | `2.13.0` latest; **install the CPU-only wheel** via `--index-url https://download.pytorch.org/whl/cpu` to avoid a multi-GB CUDA download on a machine with no GPU requirement for this phase [CITED: pytorch.org install docs] | Backing tensor runtime for `transformers` | Required by `transformers`; CPU variant keeps the install small and avoids an unnecessary CUDA toolkit pull (CLAUDE.md resource-safety guidance) |

**Package name provenance note:** All five package names above (`pymupdf`, `pdfplumber`, `transformers`, `torch`, `sentence-transformers`) came from README §7's own tech-stack table (an authoritative, owner-approved source) and were cross-verified via live `pip index versions` and `slopcheck` in this session — they are not novel name-discoveries from a web search, so they carry higher confidence than a typical `[ASSUMED]` package find. Per the provenance rule, however, package *versions and existence* are `[VERIFIED: PyPI registry]`; the choice of package *name* for the classifier task's specific library shape (raw `transformers` vs. `sentence-transformers`) is `[ASSUMED]` — see Claude's Discretion in CONTEXT.md and the Assumptions Log below.

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `sentence-transformers` | `6.0.0` (verified on PyPI) [VERIFIED: PyPI registry] | Simpler embedding/cosine-similarity wrapper around a SciBERT (or SciBERT-like) checkpoint | If the planner chooses the "lighter wrapper" path from CONTEXT.md's Claude's Discretion — reduces boilerplate vs. raw `AutoModel` + manual pooling/cosine code |
| `allenai/scibert_scivocab_uncased` (HF checkpoint, not a pip package) | current (last updated for `transformers`-compatible format) [CITED: huggingface.co/allenai/scibert_scivocab_uncased] | The concrete "SciBERT" checkpoint README names generically | Used either via raw `transformers.AutoModel` or wrapped by `sentence-transformers`; base encoder only — no built-in section/rhetorical-role classification head |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `grobid/grobid:0.9.1-full` | `grobid/grobid:0.9.1-crf` (CRF-only, ~500MB vs. ~8GB, no GPU needed) | CRF variant is far lighter and was what this research session actually used to capture the fixtures (resource-safety) — but D-24 is a locked decision for `-full` in the real service since README doesn't scope down to CRF-only, and `-full` gives materially better section/heading recognition quality that the classifier depends on |
| Raw `transformers.AutoModel` + manual mean-pooling + `torch.cosine_similarity` | `sentence-transformers` wrapper | `sentence-transformers` is less code and handles pooling/normalization for you, but the raw path gives more control if the planner wants token-level (not just heading-level) embeddings reused elsewhere in Phase 4 (NER) |
| A pretrained rhetorical-role classifier | None found — CSAbstruct is a dataset, not a checkpoint; no zero-shot-ready IMRaD classifier surfaced in HF search | Confirms D-30's embedding-similarity approach is the only buildable-without-training-data option, not merely the cheapest one |

**Installation:**
```bash
# Core
pip install pymupdf pdfplumber "transformers>=4.57,<5"
pip install torch --index-url https://download.pytorch.org/whl/cpu
# If the "lighter wrapper" discretion path is chosen:
pip install sentence-transformers
```

**Version verification:** Verified live via `pip index versions <package>` against the real PyPI registry on 2026-08-22 (see table above for exact resolved versions). `torch` versions listed include the standard CUDA-bundled index; the CPU-only wheel is a separate build published under the same version numbers at `download.pytorch.org/whl/cpu`, not a different PyPI package name.

## Package Legitimacy Audit

All five candidate packages were run through `slopcheck scan <pkg> --pkg pypi --json` in this session (installed via `pip install slopcheck`, functional as `python -m slopcheck`).

| Package | Registry | slopcheck | Disposition |
|---------|----------|-----------|-------------|
| pymupdf | PyPI | OK | Approved |
| pdfplumber | PyPI | OK | Approved |
| transformers | PyPI | OK | Approved |
| torch | PyPI | OK | Approved |
| sentence-transformers | PyPI | OK | Approved |

**Packages removed due to slopcheck [SLOP] verdict:** none.
**Packages flagged as suspicious [SUS]:** none.

All five names originate from README §7's own tech-stack table (owner-specified, not a web-search discovery), and all five passed `slopcheck` plus a live PyPI existence/version check in this session. Per CLAUDE.md's standing pre-authorization for well-known, unambiguous packages, none of these require a `checkpoint:human-verify` gate before install. The one HF *model checkpoint* (`allenai/scibert_scivocab_uncased`) is not a pip package and isn't covered by `slopcheck`; it is a long-standing (2019), heavily-cited (SciBERT paper, EMNLP 2019), officially-maintained AllenAI repo — treated as a well-known match, no gate needed.

## Architecture Patterns

### System Architecture Diagram

```
                 ┌─────────────────────────────────────────────┐
                 │            app/parsing/service.py             │
                 │         (orchestrator, source-agnostic)       │
                 └───────────────┬────────────────────────────┘
                                 │  paper.pdf_path (any source)
                                 ▼
                 ┌─────────────────────────────┐
                 │   app/parsing/grobid_client.py │──POST /api/processFulltextDocument──▶  GROBID container
                 │   (httpx, retry=2, long timeout)│◀──────── TEI-XML (200) ─────────────┘   (:8070)
                 └───────────────┬─────────────┘
                     success │        │ failure/timeout/malformed
                              ▼        ▼
        ┌────────────────────────┐   ┌─────────────────────────────┐
        │ app/parsing/tei_parser.py │   │  app/parsing/fallback.py      │
        │ TEI → sections/paras/sents │   │  PyMuPDF/pdfplumber heuristic │
        └────────────┬───────────┘   └──────────────┬──────────────┘
                     │  raw sections (head text, or untyped div)  │
                     └───────────────────┬──────────────────────┘
                                          ▼
                     ┌───────────────────────────────────────┐
                     │  app/parsing/section_classifier.py       │
                     │  1. keyword exact/near-match short-circuit│
                     │  2. SciBERT/sentence-embedding cosine sim  │
                     │     vs. 7 canonical label exemplars        │
                     └───────────────────┬───────────────────┘
                                          ▼
                     ┌───────────────────────────────────────┐
                     │  Postgres: ParsedDocument (JSON column)  │
                     │  linked to papers.id via paper_id FK      │
                     └───────────────────┬───────────────────┘
                                          │  if source='manual_upload' and title IS NULL
                                          ▼
                     ┌───────────────────────────────────────┐
                     │  corpus.enrich_paper (title/authors/DOI)  │
                     │  → if DOI recovered: rows_missing_fulltext │
                     │    / existing Unpaywall enrichment path    │
                     └───────────────────────────────────────┘
```

### Recommended Project Structure
```
backend/app/parsing/
├── __init__.py
├── grobid_client.py        # httpx call to GROBID, retry+timeout, returns raw TEI bytes/str or None
├── tei_parser.py            # TEI-XML -> {sections: [{type, heading, paragraphs: [{text, sentences}]}]}
├── fallback.py               # PyMuPDF/pdfplumber -> same shape, lower fidelity
├── section_classifier.py     # heading text -> one of 7 canonical labels
└── service.py                 # orchestrates: try GROBID -> fallback -> classify -> persist -> backfill trigger
backend/tests/fixtures/
├── grobid_sample_fulltext.tei.xml   # captured this session (real GROBID 0.9.1-crf run, trimmed)
└── grobid_sample_header.tei.xml     # captured this session (real GROBID 0.9.1-crf run, in full)
```

### Pattern 1: GROBID HTTP client (mirrors Phase 1's `download_pdf` shape)
**What:** A plain function that POSTs PDF bytes to GROBID and returns TEI-XML text or `None` on failure — no class, no hidden state, easily mockable via `respx` (already a dev dependency).
**When to use:** Every call site in `service.py`.
**Example:**
```python
# Verified request/response shape — from a real GROBID 0.9.1-crf run this session
import httpx

def parse_fulltext(pdf_bytes: bytes, grobid_url: str, timeout: float = 90.0) -> str | None:
    transport = httpx.HTTPTransport(retries=2)
    try:
        with httpx.Client(transport=transport, timeout=timeout) as client:
            resp = client.post(
                f"{grobid_url}/api/processFulltextDocument",
                files={"input": ("paper.pdf", pdf_bytes, "application/pdf")},
                data={"segmentSentences": "1"},  # GROBID splits <s> for you — see Pitfall 3
            )
            if resp.status_code != 200:
                return None
            return resp.text
    except httpx.HTTPError:
        return None
```
*(Confirmed working exactly this way against a real container in this session — `httpx.Client(transport=...)` is required, matching Phase 1's already-learned lesson that the module-level `httpx.post()` shortcut does not accept a `transport` kwarg.)*

### Pattern 2: TEI section extraction — untyped body divs, typed back-matter divs
**What:** Iterate `<text>/<body>/<div>` elements. Each has an optional `<head>` (heading text) and one or more `<p>` (each containing `<s>` sentence tags when `segmentSentences=1` was requested). **Do not rely on a `type` attribute existing on body divs** — verified empirically that it doesn't for standard sections.
**When to use:** `tei_parser.py`'s core extraction loop.
**Example (real, captured this session):**
```xml
<!-- backend/tests/fixtures/grobid_sample_fulltext.tei.xml -->
<div xmlns="http://www.tei-c.org/ns/1.0"><head n="1">Introduction</head><p><s>Recurrent neural networks...</s><s>Numerous efforts...</s></p></div>
<div xmlns="http://www.tei-c.org/ns/1.0"><head n="2">Background</head><p>...</p></div>
```
```xml
<!-- back-matter divs DO get a `type` attribute -->
<div type="acknowledgement"><div><head>Acknowledgements</head><p>...</p></div></div>
<div type="references"><listBibl><biblStruct status="extracted" xml:id="b0">...</biblStruct></listBibl></div>
```
The classifier's input is always `<head>` text (plus, optionally, the first paragraph for disambiguation) — never a GROBID-provided section-type hint, except to recognize/skip `acknowledgement` and `references` divs outright (those two categories *are* reliably typed and should bypass the classifier entirely, mapped to a non-canonical "skip" bucket or attached as metadata, not forced into one of the 7 canonical labels).

### Pattern 3: Header metadata extraction for manual-upload backfill (D-32/PARSE-05)
**What:** `teiHeader/fileDesc/titleStmt/title[@level='a'][@type='main']` for title; `teiHeader/fileDesc/sourceDesc/biblStruct/analytic/author/persName/(forename+surname)` for each author; `teiHeader/fileDesc/sourceDesc/biblStruct/idno[@type='DOI']` for DOI (not present in the captured fixture — this paper is an arXiv preprint with only `idno[@type='arXiv']` and `idno[@type='MD5']`; DOI extraction should be coded defensively as "absent if not found," not assumed always-present).
**When to use:** `service.py`'s backfill step, or a small `header_extractor.py` helper if the planner wants it split out.
**Example (real, captured this session — `backend/tests/fixtures/grobid_sample_header.tei.xml`):**
```xml
<title level="a" type="main">Attention Is All You Need</title>
...
<author>
  <persName><forename type="first">Ashish</forename><surname>Vaswani</surname></persName>
  <email>avaswani@google.com</email>
  <affiliation key="aff0"><orgName type="institution">University of Toronto</orgName></affiliation>
</author>
...
<idno type="MD5">18E1B007A1DAB45B30CC861BA2DFDA25</idno>
<idno type="arXiv">arXiv:1706.03762v7[cs.CL]</idno>
```
Note `processHeaderDocument`'s response has an **empty `<text></text>`** element — only `<teiHeader>` is populated. This is the cheaper (~15x faster per GROBID docs) call when only metadata is needed, vs. re-parsing the full document via `processFulltextDocument` and reading its `teiHeader` (which is also populated identically — D-32's "or equivalently" phrasing is confirmed correct; either endpoint gives the same header fields).

### Anti-Patterns to Avoid
- **Assuming `div[@type]` covers the 7 canonical sections:** Confirmed false this session — only `acknowledgement`/`references`/`annex`/`availability`/`conflict`/`contribution`/`funding` get typed; classify by heading text for everything else.
- **Running the full SciBERT model inside every unit test:** D-36 already forbids this — mock embedding vectors for the classifier's unit tests; reserve real-model loading for a small, separately-run integration check.
- **Batch-processing many PDFs through GROBID during development/testing:** CLAUDE.md resource-safety guidance — this research session used exactly one representative PDF and tore the container down immediately after capturing fixtures.
- **Treating `torch`'s default PyPI install as CPU-safe:** the default `pip install torch` may pull CUDA-enabled wheels/dependencies; use the `--index-url https://download.pytorch.org/whl/cpu` form explicitly in the phase's install step.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| PDF → structured document conversion | A custom PDF layout/heading parser as the primary path | GROBID | Purpose-built, ML-trained on millions of scholarly PDFs; reinventing this is a multi-year research problem GROBID already solved |
| Sentence segmentation for the GROBID-success path | A custom regex/nltk sentence splitter for GROBID output | GROBID's own `segmentSentences=1` parameter (confirmed working — TEI comes back with `<s>` tags already inserted) | GROBID already ships this; adding a second, possibly-inconsistent splitter on top of GROBID's own is duplicate and can disagree with GROBID's paragraph/sentence boundaries |
| Cosine-similarity / embedding infrastructure | Custom vector math for the classifier's embedding comparison | `torch.nn.functional.cosine_similarity` or `sentence-transformers.util.cos_sim` | Well-tested, numerically stable, no reason to hand-roll |
| PDF-fallback text extraction | A custom PDF byte-stream parser | PyMuPDF/pdfplumber | Both already expose font-size/block/line metadata needed for a heading heuristic; a hand-rolled parser would need to reimplement PDF's content-stream model from scratch |

**Key insight:** Every "don't hand-roll" item here is because the underlying problem (PDF structure recovery, sentence boundary detection, vector similarity) has decades of tooling behind it that a phase-scoped custom implementation cannot match in either correctness or maintenance cost.

## Common Pitfalls

### Pitfall 1: Assuming GROBID types every section (the D-31 correction)
**What goes wrong:** Code written expecting `div.get("type") == "introduction"` to work for standard IMRaD sections will silently fail to classify anything, because that attribute is essentially never present for body sections.
**Why it happens:** GROBID's own docs discuss `type="annex"`/`type="availability"`/`type="conflict"`/`type="contribution"`/`type="funding"` as things the segmentation model assigns to back-matter — it's easy to over-generalize this to "GROBID types all sections."
**How to avoid:** Design `tei_parser.py`/`section_classifier.py` to always classify on `<head>` text first, and only special-case `acknowledgement`/`references` divs by their (real) `type` attribute.
**Warning signs:** A classifier that returns "unclassified" for every single body section in a test run against real GROBID output.

### Pitfall 2: `httpx` module-level shortcut and `transport` kwarg
**What goes wrong:** `httpx.post(url, transport=...)` raises a `TypeError` — only `httpx.Client(transport=...)` accepts the retry-configured transport.
**Why it happens:** This is the exact defect Phase 1's live smoke test caught for Semantic Scholar/Unpaywall/ACL Anthology (see STATE.md); the same trap applies to any new GROBID client code written against the module-level `httpx.get`/`httpx.post` shortcuts.
**How to avoid:** Always use a context-managed `httpx.Client(transport=httpx.HTTPTransport(retries=2))` for the GROBID call, exactly as Phase 1 was fixed to do.
**Warning signs:** A GROBID client that passes its mocked-`respx` unit tests but fails immediately against a real container.

### Pitfall 3: GROBID first-request latency / cold start
**What goes wrong:** The first request to a freshly-started GROBID container can take noticeably longer than subsequent ones (models loading lazily inside the JVM); a client with too-short a timeout will spuriously fall back to PyMuPDF/pdfplumber even though GROBID would have succeeded.
**Why it happens:** GROBID initializes its CRF/DL models on first use in some configurations, and processing itself (this session's single fulltext call on a ~15-page paper) took ~12 seconds even after the container's `isalive` reported ready.
**How to avoid:** Use a generous per-request timeout (60-90s, not the 5-10s used for Phase 1's lightweight metadata APIs, per D-35) and gate the docker-compose `depends_on` on GROBID's `/api/isalive` healthcheck with a `start_period` of at least 30-60s (GROBID's own docs' compose examples budget similarly for JVM startup).
**Warning signs:** Intermittent, non-reproducible fallback-path triggering that disappears when timeouts are raised.

### Pitfall 4: 503 under concurrent load
**What goes wrong:** GROBID returns `503` when all of its internal processing threads are busy (documented, not a bug) — a naive client treats this identically to a hard failure and gives up.
**Why it happens:** GROBID's engine pool is finite (visible in this session's container logs: `Number of Engines in pool active/max: 1/10`); concurrent requests beyond that pool size queue or 503.
**How to avoid:** D-34/D-35's retry-then-fallback design already covers this reasonably (retry, then fallback) — just don't special-case 503 as something other than "GROBID couldn't process this one right now."
**Warning signs:** Elevated fallback-path usage specifically during batch/parallel PDF processing.

## Code Examples

### Extracting sections/paragraphs/sentences from real TEI (verified shape)
```python
# tei_parser.py — using lxml or ElementTree with the TEI namespace
NS = {"tei": "http://www.tei-c.org/ns/1.0"}

def extract_body_sections(root) -> list[dict]:
    sections = []
    for div in root.findall(".//tei:text/tei:body/tei:div", NS):
        head = div.find("tei:head", NS)
        heading = head.text if head is not None else None
        paragraphs = []
        for p in div.findall("tei:p", NS):
            sentences = [s.text for s in p.findall("tei:s", NS) if s.text]
            paragraphs.append({"text": "".join(p.itertext()), "sentences": sentences})
        sections.append({"heading": heading, "paragraphs": paragraphs, "type": div.get("type")})
    return sections
```
*(`div.get("type")` will be `None` for essentially every body section per this session's finding — code downstream must handle that as the norm, not the exception.)*

### Header field extraction for backfill
```python
def extract_header_fields(root) -> dict:
    title_el = root.find(".//tei:teiHeader/tei:fileDesc/tei:titleStmt/tei:title", NS)
    authors = []
    for author in root.findall(".//tei:sourceDesc/tei:biblStruct/tei:analytic/tei:author/tei:persName", NS):
        forename = author.find("tei:forename[@type='first']", NS)
        surname = author.find("tei:surname", NS)
        if surname is not None:
            authors.append(f"{forename.text + ' ' if forename is not None else ''}{surname.text}")
    doi_el = root.find(".//tei:idno[@type='DOI']", NS)
    return {
        "title": title_el.text if title_el is not None else None,
        "authors": authors,
        "doi": doi_el.text if doi_el is not None else None,  # commonly absent for arXiv-only papers
    }
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|---------------|--------|
| GROBID standalone JAR install | Dockerized GROBID image (`grobid/grobid`) | Ongoing since ~2018, current stable `0.9.1` (2025-era release per readthedocs) | Simplifies docker-compose integration; no local Java/Maven build needed |
| Hand-built rhetorical-role classifiers trained per-project | No shift — this remains a training-data-dependent task industry-wide; CSAbstruct (2019) is still the standard *dataset* reference, no widely-adopted zero-shot alternative has emerged | N/A | Confirms D-30's embedding-similarity workaround is a reasonable, still-current choice for a no-training-data MVP, not an outdated approach being superseded by something better |

**Deprecated/outdated:** None identified specific to this phase's stack — GROBID, PyMuPDF, pdfplumber, and SciBERT are all actively maintained as of this research date.

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `grobid/grobid:0.9.1-full`'s body-div type-tagging behavior matches what was verified against `0.9.1-crf` in this session (i.e., body sections are untyped in both flavors) | Architecture Patterns / Pitfall 1 | If the full image's deep-learning fulltext model *does* assign more granular `type` attributes than the CRF model, the classifier could skip work it doesn't need to — low-severity if wrong (classifier still works, just does slightly more work than strictly necessary); GROBID's own docs describe `type` values as a fixed back-matter-focused list, so this is considered low-risk |
| A2 | No pretrained, ready-to-use HuggingFace classifier exists for rhetorical-role/IMRaD section classification that could replace the embedding-similarity approach | Summary / Standard Stack | If one is later found, D-30's embedding approach would be more work than necessary (not wrong, just not optimal) — worth a quick re-check before Phase 3 if time allows, since Phase 3's extraction work touches similar discourse-structure territory |
| A3 | `sentence-transformers` vs. raw `transformers.AutoModel` — which one the planner should pick is left as Claude's Discretion per CONTEXT.md; this research did not run both and benchmark | Standard Stack / Alternatives Considered | Low risk either way — both are legitimate paths to the same embedding; the planner should pick one and note it as a locked decision when planning, not leave it open through implementation |
| A4 | DOI is not always present in GROBID's header extraction, even for a paper that does have one elsewhere (e.g., published version) — code should treat a missing DOI as expected, not an error | Code Examples / Pitfall (implicit) | Low risk if handled defensively (as shown in the code example); a bug if code assumes DOI is always recoverable and errors/crashes on `None` |

**If this table is empty:** N/A — see entries above.

## Open Questions (RESOLVED)

1. **Does the `-full` GROBID image assign meaningfully more `div[@type]` values than `-crf`?**
   - What we know: GROBID's segmentation-model docs describe a fixed, back-matter-focused `type` vocabulary (annex/availability/conflict/contribution/funding/acknowledgement/references) — this appears to be a schema-level design choice, not a model-capability difference.
   - What's unclear: Whether the `-full` image's deep-learning segmentation model recognizes more of these categories in practice (e.g. spots more `funding`/`availability` statements) even if it never adds `type="introduction"` etc.
   - Recommendation: Not worth pulling the 8GB `-full` image just to re-verify this during research (resource-safety) — if the executor phase runs against a real `-full` container and observes different typing behavior than these fixtures show, that's a fast, cheap check to make once the real service is up; doesn't block planning.

2. **Should the classifier's exemplar phrases (e.g. "Threats to Validity" → Discussion) be config-driven or hardcoded?**
   - What we know: CLAUDE.md requires config centralization for "model names, gap-score weights, retrieval caps, top-N sizes" — it's ambiguous whether a fixed mapping table of heading-synonyms counts as "config" in that sense.
   - What's unclear: Whether the planner should put the keyword/exemplar-phrase list in `Settings` or as a plain constant in `section_classifier.py`.
   - Recommendation: Treat it as a plain in-module constant (like a lookup table), not a `Settings` field — it's classification logic, not a tunable operational parameter, and CLAUDE.md's config-centralization examples (model names, weights, caps) are all clearly-numeric/operational values, not linguistic mapping tables.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|--------------|-----------|---------|----------|
| Docker | GROBID service | ✓ | 29.7.2 | — |
| GROBID image (`grobid/grobid:0.9.1-full`) | PARSE-01 | Not yet pulled (only `-crf` was pulled for this research session) | — | `-crf` variant works as a lighter substitute if disk/RAM is constrained during development, at some quality cost |
| Python 3.11+ / pip | All parsing code | ✓ (3.13.7 on this machine; project targets `>=3.11`) | 3.13.7 | — |
| `pip index versions` (PyPI reachability) | Package legitimacy checks | ✓ | — | — |
| `slopcheck` | Package legitimacy audit | ✓ (installed this session) | 0.6.1 | — |

**Missing dependencies with no fallback:** none — everything needed for Phase 2 is either already available or was successfully installed/tested in this session.
**Missing dependencies with fallback:** `grobid/grobid:0.9.1-full` not yet pulled locally (only the lighter `-crf` variant was used for fixture capture) — the executor's first docker-compose `up` will pull it; no blocker, just a note that the first real GROBID startup will incur an ~8GB download.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 8.3+ (already configured, `pyproject.toml` `[tool.pytest.ini_options]`) |
| Config file | `backend/pyproject.toml` |
| Quick run command | `pytest backend/tests/ -x -k parsing` |
| Full suite command | `pytest backend/tests/` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|---------------------|--------------|
| PARSE-01 | `grobid_client.parse_fulltext` returns TEI-XML on 200, `None` on non-200/error; `tei_parser` extracts sections/paragraphs/sentences from real TEI shape | unit (mocked `respx` HTTP + real captured TEI fixture) | `pytest backend/tests/parsing/test_grobid_client.py backend/tests/parsing/test_tei_parser.py -x` | ❌ Wave 0 |
| PARSE-02 | Fallback triggers on GROBID failure/timeout/malformed response; PyMuPDF/pdfplumber heuristic produces a best-effort section structure | unit (mocked GROBID failure + a small real/synthetic PDF fixture) | `pytest backend/tests/parsing/test_fallback.py -x` | ❌ Wave 0 |
| PARSE-03 | Section classifier: keyword pre-filter short-circuits common headings; embedding-similarity path classifies inconsistent headings onto the 7 canonical labels | unit (mocked/small embedding vectors, not full SciBERT) | `pytest backend/tests/parsing/test_section_classifier.py -x` | ❌ Wave 0 |
| PARSE-04 | Parsing service never branches on `source`; reads/writes through `corpus.get_session` regardless of ingestion path | unit (parametrized over `source` values) | `pytest backend/tests/parsing/test_service.py -x` | ❌ Wave 0 |
| PARSE-05 | Header-extraction backfill writes title/authors/DOI via `enrich_paper` only for `manual_upload` rows with `title IS NULL`; DOI absence handled gracefully | unit (real header TEI fixture, mocked DB session) | `pytest backend/tests/parsing/test_backfill.py -x` | ❌ Wave 0 |
| PARSE-06 | DOI recovered via backfill triggers the existing `rows_missing_fulltext`/enrichment path, no duplicate logic | unit (mocked enrichment call, asserts it's invoked with recovered DOI) | `pytest backend/tests/parsing/test_backfill.py -x` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `pytest backend/tests/ -x -k parsing`
- **Per wave merge:** `pytest backend/tests/`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `backend/tests/parsing/` directory + `__init__.py` — does not exist yet (mirrors `backend/tests/ingestion/` structure)
- [ ] `backend/tests/parsing/test_grobid_client.py`, `test_tei_parser.py`, `test_fallback.py`, `test_section_classifier.py`, `test_service.py`, `test_backfill.py` — all new
- [x] `backend/tests/fixtures/grobid_sample_fulltext.tei.xml` — captured this session, real GROBID 0.9.1-crf output (trimmed to Introduction/Background/Model Architecture body divs + acknowledgement + 2 reference entries; valid, well-formed XML, confirmed via `xml.etree.ElementTree.parse`)
- [x] `backend/tests/fixtures/grobid_sample_header.tei.xml` — captured this session, real GROBID 0.9.1-crf `processHeaderDocument` output, in full (title/9 authors/affiliations/idno[MD5,arXiv]/abstract; empty `<text>` confirmed)
- [ ] A small representative test PDF fixture for `test_fallback.py`'s PyMuPDF/pdfplumber path — not captured this session (the real PDF used to generate the TEI fixtures was downloaded to a temp directory and deleted per resource-safety cleanup, not committed to the repo); the executor should add a small, clearly-licensed PDF (e.g. a 1-2 page arXiv paper) directly, or synthesize one with `reportlab`/similar for a fully synthetic fixture if avoiding any real-paper redistribution question is preferred

## Security Domain

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|----------------|---------|-------------------|
| V2 Authentication | No | Phase 2 has no new user-facing auth surface |
| V3 Session Management | No | N/A |
| V4 Access Control | No | Internal service-to-service call (backend → GROBID), not user-facing |
| V5 Input Validation | Yes | GROBID's own TEI-XML response should be parsed defensively (missing `<head>`, missing `type`, missing DOI are all *expected*, not exceptional — see Pitfall 1/Code Examples); PDF bytes sent to GROBID should already be validated at the Phase 1 upload boundary (D-01-14's accepted-risk note on magic-byte checking still stands — not reopened here) |
| V6 Cryptography | No | No new crypto surface this phase |

### Known Threat Patterns for this stack

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|----------------------|
| XML External Entity (XXE) injection via a malicious TEI-XML response | Tampering / Information Disclosure | GROBID is a trusted, self-hosted service (not user-supplied XML) — but if `lxml`/`ElementTree` is used to parse its response, prefer `defusedxml`-style safe parsing or ensure `resolve_entities=False` if using `lxml.etree.XMLParser`, since GROBID's own output is unlikely to but a compromised container theoretically could return crafted XML |
| Resource exhaustion via a very large/malicious PDF sent to GROBID | Denial of Service | `MAX_UPLOAD_SIZE_MB` (already enforced at Phase 1's `/upload` boundary) bounds this; GROBID's own generous-but-bounded timeout (D-35) prevents an indefinite hang on a pathological PDF |

## Sources

### Primary (HIGH confidence)
- Live GROBID 0.9.1-crf container run in this session — real request/response captured for both `/api/processFulltextDocument` and `/api/processHeaderDocument` against a real arXiv PDF; fixtures committed to `backend/tests/fixtures/`
- [GROBID Docker documentation](https://grobid.readthedocs.io/en/latest/Grobid-docker/) — image tags, ports, memory guidance
- [GROBID REST API service documentation](https://grobid.readthedocs.io/en/latest/Grobid-service/) — endpoint list, parameters, `/api/isalive`/`/api/health`/`/api/version`
- [GROBID segmentation model documentation](https://grobid.readthedocs.io/en/latest/training/segmentation/) — confirms the fixed back-matter-focused `type` attribute vocabulary
- `pip index versions <package>` — live PyPI registry queries for pymupdf, pdfplumber, transformers, torch, sentence-transformers (run 2026-08-22)
- `slopcheck scan <pkg> --pkg pypi --json` — legitimacy check for all 5 candidate packages (run 2026-08-22, slopcheck 0.6.1)

### Secondary (MEDIUM confidence)
- [allenai/scibert_scivocab_uncased model card](https://huggingface.co/allenai/scibert_scivocab_uncased) — SciBERT checkpoint details (WebSearch-surfaced, cross-referenced against the well-known SciBERT/EMNLP-2019 paper)
- [PyTorch CPU-only install instructions](https://pytorch.org/get-started/previous-versions/) — WebSearch-surfaced, consistent across multiple sources (GeeksforGeeks, PyTorch forums, official docs)

### Tertiary (LOW confidence)
- CSAbstruct/argumentative-zoning WebSearch results — confirms CSAbstruct is a dataset not a shipped classifier, but this is an absence-of-evidence finding (no pretrained classifier surfaced), not a directly citable positive claim; treated as sufficient grounds to proceed with D-30 given the exhaustive nature of the search, but flagged as A2 in the Assumptions Log

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — versions verified live against PyPI; package names sourced from README's own owner-approved tech-stack table, not web-search discovery
- Architecture (GROBID TEI shape): HIGH — verified via a real, live GROBID run captured this session, not assumed from documentation alone
- Pitfalls: HIGH for the `httpx`/GROBID-specific ones (directly observed or directly inherited from Phase 1's documented live-smoke-test findings); MEDIUM for the rhetorical-role-classifier design (no pretrained shortcut confirmed absent by search, not by exhaustive proof)

**Research date:** 2026-08-22
**Valid until:** ~30 days (GROBID/PyMuPDF/pdfplumber/transformers are stable, slow-moving; re-check package versions if planning is delayed past early Q4 2026)

# Mode B live verification plan (prepared 2026-10-14, NOT executed)

Requires explicit owner approval. One top-level request per approval.

## Question and expected evidence

> Which languages have multilingual prompt injection defenses been evaluated
> on, and what limitations do the papers report about language coverage?

Cluster 12's stored statements answer it directly: paper 2 (MIPIAD:
"validation covers English and Bangla only"), 10 (restricted number of tested
languages), 9 (major languages only; dialects unhandled), 6 (translation
naturalness), 4 (translation test suite). The previous run (2026-10-13) cited
paper 2 twice plus papers 3 and 8, and refused after two rejected drafts.

## What the run now shows that the last one could not

Each grounding check records, in the API `trace` and the server log, counts
and a reason code only (no draft text):

`grounding:structural=..;grounded=..;claims=N;cited=N;invalid_citations=N;unsupported=N;reason=CODE`

with `reason` in `ok`, `invalid_draft`, `no_claims`, `invalid_citations`,
`verifier_rejected`, `verifier_contradictory`, `verifier_malformed`. The strict
regeneration is now told which claims or citation IDs failed (internally;
never returned).

## Valid grounded answer

`outcome == "grounded_answer"`, `grounded == true`, at least one claim, every
claim cites returned citations, every citation resolves (checked read-only by
`live_check`): vector citations equal the stored Qdrant payload text
(`strip()[:1200]`) and paper id; graph citations match an entity the paper
MENTIONS in Neo4j; titles equal the corpus titles. A human then reads each
claim against its cited passage (the checker is the same LLM; its verdict is
not independent verification).

## If it refuses again

`outcome` is `grounding_failed` (or `insufficient_evidence`), with no claims
and no citations. The reason codes say whether drafts were malformed, cited
invented IDs, or were rejected claim by claim. A refusal is a safe outcome but
does not meet the Done-when; do not retry without a new approval.

## Cost (from the code)

* Groq: 2-6 logical calls (0-2 reformulations, 1-2 drafts, 1-2 checks), each
  with up to `GROQ_MAX_RETRIES` = 4 retries on 429/transport errors; calls are
  paced by `GROQ_MIN_INTERVAL_SECONDS` = 60 (last run: 4 calls, 195 s).
* Local: SPECTER2 base + adapter and the cross-encoder, loaded in the backend
  process. No NLI model, no database writes.

## Preventing a model-revision mismatch

The backend loads Hugging Face models online. Before the run, compare
upstream `main` for `allenai/specter2_base`, `allenai/specter2` and
`cross-encoder/ms-marco-MiniLM-L-6-v2` with the cached `refs/main`
(`3447645e`, `2081559`, `233902d2`) via metadata-only `model_info`; stop if
any differs. After the run, the server log must show
`Loaded SPECTER2 allenai/specter2_base@3447645e... + adapter ...@2081559...`.

## Commands

    docker exec nlp-backend-1 python -m app.interactive.live_check            # dry run
    docker exec nlp-backend-1 python -m app.interactive.live_check --confirm-live

Before and after: the 45-value read-only snapshot and
`python -m app.core.reconcile --papers`; both must be unchanged.

## Preflight checklist (audited 2026-10-14)

Approval must name all three external actions: (1) three Hugging Face
`model_info` metadata requests (no download), (2) one `POST /query`, which
makes 2-6 Groq calls, (3) the local SPECTER2 + cross-encoder load.

1. `git status` / HEAD recorded; 45-value snapshot and reconcile taken.
2. Container code equals the working tree (md5 of `/app/app`).
3. Dry run exits 4 with `api_key_configured: true`, health 200, index populated.
4. Upstream `main` equals cached `refs/main` for all three repos; else stop.
5. Run once, writing to a new, unused file (never overwrite an existing
   `live_query_result*.json`; `_2` and `_3` are taken):
   `docker exec nlp-backend-1 python -m app.interactive.live_check --confirm-live > evaluation/output/live_query_result_<N>.json`
6. Verdict: exit 0 = grounded answer with every check passing (then a human
   reads each claim against its cited passage and records agree/disagree);
   exit 3 = safe refusal (`refusal_lists_no_citations` must pass; read
   `workflow.grounding_reasons`); exit 1 = failure.
7. Secret scan of the output file (`X-API-Key`, `api_key`, `Bearer`: 0 hits);
   server log for the window holds only `mode_b grounding:` counts/codes,
   `groq usage` counts and the model-load lines.
8. Snapshot and reconcile again: all 45 values identical, 19 pipeline runs.

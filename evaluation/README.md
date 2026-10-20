# ScholarLens evaluation harness

Phase 13 validates generated research gaps with three independent signals:

1. **Time-shifted validation** compares each gap discovered from a corpus cutoff against papers published after that cutoff. Optional `relevant_future_paper_ids` labels produce precision and recall; without labels, the addressed-rate remains available as a discovery indicator.
2. **Survey cross-checking** compares gaps with open-problem and future-work statements transcribed from recent survey papers.
3. **Expert scoring** aggregates 1–5 ratings for novelty, validity, evidence quality, and actionability. Validity scores of 4–5 count toward the high-validity fraction.

The text comparisons use a deterministic normalized unigram/bigram similarity score. This makes evaluation runs reproducible and inexpensive. A match is evidence for human review, not proof that a paper solved a gap.

## Run the included test corpus

From the repository root:

```bash
PYTHONPATH=. backend/.venv/bin/python -m evaluation.cli \
  --gaps evaluation/examples/gaps.json \
  --future-papers evaluation/examples/future-papers.json \
  --surveys evaluation/examples/surveys.json \
  --expert-scores evaluation/examples/expert-scores.csv \
  --similarity-threshold 0.15 \
  --output evaluation/examples/evaluation-report.json
```

The output contains per-method matches and a `validity_index`: the equal-weighted mean of the later-paper addressed rate, survey match rate, and mean expert validity normalized to 0–1.

## Input contracts

- `gaps.json`: `gap_id`, `description`, `cutoff_year`, and optional labelled `relevant_future_paper_ids`. A missing or `null` value means the gap is *unlabelled*. Its matches still count towards the addressed-rate but are excluded from precision and recall. `[]` means *labelled, and no later paper addresses it*. Precision is `null` when no labelled gap has a prediction. Since 2026-10-18 (D-13-04) the ScholarLens adapter writes `null`. The 2026-10-13 files directly in `output/scholarlens/` predate that and carry `[]`; `output/scholarlens/run-20261018/` is the corrected re-run from a byte-identical export.
- `future-papers.json`: `paper_id`, `title`, `year`, optional `abstract`, and optional `keywords`.
- `surveys.json`: `survey_id`, `title`, `year`, and a non-empty `open_problems` list. Statements should be transcribed faithfully rather than rewritten to resemble generated gaps.
- `expert-scores.csv`: one row per rater and gap using the supplied template. Rater IDs may be pseudonymous.

## Expert rubric

Use whole-number scores from 1 (poor) to 5 (excellent):

| Dimension | 1 | 3 | 5 |
|---|---|---|---|
| Novelty | Already well studied | Some open scope remains | Clearly underexplored |
| Validity | Unsupported or false | Plausible but uncertain | Strongly supported and genuinely open |
| Evidence quality | Weak/irrelevant sources | Mixed evidence | Multiple direct, credible sources |
| Actionability | Too vague to investigate | Broad but researchable | Specific, feasible research direction |

Experts should score independently before discussing disagreements. The report includes per-gap validity standard deviation to expose disagreement rather than hiding it in the mean.

## Tests

```bash
PYTHONPATH=. backend/.venv/bin/pytest -q evaluation/tests
```

## Mode B evaluation (Ask ScholarLens)

`evaluation/mode_b.py` evaluates Mode B offline from files. It never loads a
model, calls an API or touches a store. It imports the production rules
instead of copying them: the sufficiency gate (`workflow.assess_sufficiency`),
the hash split (`calibration.split_tune_test`) and the claim check
(`claim_check.check_claims`).

### Inputs

* **Dataset** `evaluation/datasets/<name>/`: `questions.csv` (the only file
  the retrieval run sees), `labels.csv`, `claims.csv`, and `dataset.json`
  (authorship and independence). See `datasets/mode_b_v1/DATASET.md` for the
  label definitions. **`mode_b_v1` is provisional and agent-authored.** For
  independent labels, follow `output/mode_b_calibration/independent/PROTOCOL.md`.
* **Passage catalogue** `output/mode_b_calibration/independent/passages.csv`
  (one row per indexed Qdrant point) and `papers.csv`.
* **Retrieval run**, made inside the backend container: a local SPECTER2 load,
  no LLM, reads Qdrant only.
* **Live results** (optional), made by `app.interactive.live_check
  --confirm-live`: one `POST /query` each, costing 2–6 Groq calls. Plus a
  claim-level review CSV (`reviewer_type` = `human` or `agent`, `independent`
  = `yes`/`no`).

### Commands

```bash
# 1. Validate the dataset against the catalogue (fails on any unknown id or paper mismatch)
PYTHONPATH=. backend/.venv/Scripts/python.exe -m evaluation.mode_b --dataset evaluation/datasets/mode_b_v1 --validate-only

# 2. Retrieval run (backend container; offline model cache; no LLM)
docker cp evaluation/datasets/mode_b_v1/questions.csv nlp-backend-1:/tmp/mode_b_v1_questions.csv
docker exec -i -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 nlp-backend-1 python - \
  --mode embed --allow-model-load --questions /tmp/mode_b_v1_questions.csv \
  < backend/app/interactive/calibration.py > evaluation/output/mode_b_eval/<run>/retrieval-run.json

# 3. (optional, costs Groq calls) one live request per question; never overwrite an existing file
docker exec nlp-backend-1 python -m app.interactive.live_check --confirm-live --test-id LV-1 \
  --question "..." --expected-paper-ids "3" --expected-outcome grounded_answer > evaluation/output/mode_b_eval/<run>/live/LV-1.json

# 4. Evaluate, then reproduce from the manifest
PYTHONPATH=. backend/.venv/Scripts/python.exe -m evaluation.mode_b --dataset evaluation/datasets/mode_b_v1 \
  --retrieval-run evaluation/output/mode_b_eval/<run>/retrieval-run.json \
  [--live evaluation/output/mode_b_eval/<run>/live/*.json --live-review evaluation/output/mode_b_eval/<run>/live/live-review.csv] \
  --output-dir evaluation/output/mode_b_eval/<run> --run-id <run>
PYTHONPATH=. backend/.venv/Scripts/python.exe -m evaluation.mode_b --reproduce evaluation/output/mode_b_eval/<run>/manifest.json
```

The evaluation refuses a retrieval run made from a different `questions.csv`
or with a query encoder whose `embed_version` differs from the stored
vectors'. `manifest.json` records the input and code hashes, git HEAD, the
settings, the label provenance, the mode (offline, mocked, live calls, LLM
judge) and `results_sha256`. `--reproduce` recomputes every result from the
recorded inputs and reports any input, code or result mismatch.

### Metrics (all with numerator `k`, denominator `n` and a 95% Wilson interval; `null` when `n = 0`)

| Metric | Unit | Definition |
|---|---|---|
| gate score | question | Highest threshold at which Mode B's rule accepts the question's round-1 hits (`null` = never, e.g. every hit from one paper) |
| false accept (weak / unrelated) | question of that label | Share the gate accepts |
| false reject (relevant / partial) | question of that label | Share the gate rejects; each rejection costs a reformulation and, after 3 rounds, a refusal |
| answerable vs not | question | Positive = gate accepts; relevant + partial should be accepted, weak + unrelated rejected. Gives precision, recall, specificity, F1 and balanced accuracy |
| relevant vs unrelated | question | The same, over the two unambiguous classes only |
| AUROC | question pair | P(gate score of a positive > a negative), ties counted half |
| chosen threshold | calibration split | Best balanced accuracy over midpoints of observed calibration gate scores; ties go to the lower threshold. Reported once on the held-out split, never re-tuned |
| passage / paper recall@k, MRR, top-1 paper | answerable question with expected passages | Against `relevant_passage_ids` / `relevant_paper_ids`. Lower bounds: expected lists are not exhaustive |
| claim veto rate | labelled claim | Share the deterministic checks reject. On `unsupported` claims it is the catch rate; on `supported` claims it is over-refusal. **Not** an entailment accuracy: the checks can only reject |
| live outcome as expected | live request | Outcome equals the request's `--expected-outcome` |
| claim support, citation correctness and completeness | reviewed live claim | From the review CSV, split by reviewer type; agent reviews are never reported as human |
| scope policy (`scope_policy`) | question | Round-1 decision of the two-paper rule vs the scope-aware rule (`workflow.assess_question`, paper-specific minimum from the run's settings or the central default), at the current and the calibration-chosen threshold, per label and split; lists every question whose decision changes; scope against `papers_needed` |
| answer-relevance proxy (`relevance_rule`) | answerable question with subjects | Questions the `off_question` rule (`intent.addresses_question`) would refuse if their expected passages were the answer's evidence (an over-refusal proxy) |
| aspect completeness (`completeness`, with `--aspects`) | live answer | Pre-registered aspects a returned claim cites an expected passage for. A lower bound on evidence; it does not check the claim's wording |

Two optional inputs (2026-10-18):

* `--claims-supplement DIR`: a further `claims.csv` + `dataset.json`,
  validated like the main set and scored separately as `claims_supplement`.
  `datasets/mode_b_claims_s2` is provisional and agent-authored.
* `--aspects FILE`: `{test_id: [{"id", "expected_passage_ids"}]}`, written
  before the live calls.

Both are hashed into the manifest and replayed by `--reproduce`.

Retrieval relevance (the gate) and answer correctness (claims) are separate
metrics and are never combined into one score.

### Acceptance-policy comparison (`evaluation/threshold_policy.py`, 2026-10-19)

Compares, on the same labelled questions and per label and per
`question_type`, the production cosine gate (`RETRIEVAL_MIN_SCORE`, a Qdrant
cosine similarity compared with `>=`), a cosine threshold re-chosen on the
calibration split, and the candidate cross-encoder floor (the **logit** of a
question's best reranked passage; unbounded, never compared with a cosine and
not part of production). It reads saved files only (the dataset, the saved
retrieval run, the saved cross-encoder scores) and loads no model.

    PYTHONPATH=. backend/.venv/Scripts/python.exe -m evaluation.threshold_policy \
        --dataset evaluation/datasets/mode_b_v1 \
        --retrieval-run evaluation/output/mode_b_eval/run-20261018-v1/retrieval-run.json \
        --rerank-scores evaluation/output/mode_b_eval/run-20261018-v2/rerank-scores.json \
        --output evaluation/output/mode_b_eval/run-20261019-v3/threshold-policy.json

A floor is only recommended when `lock_verdict` finds every criterion met:
independent labels, no answerable question rejected and no unrelated question
accepted when the floor is fitted on one half and tested on the other (both
directions), and the two halves' floors closer together than the margin to the
lowest answerable score. With agent-authored labels the first criterion cannot
be met, so the verdict is always `defer` until independent labels exist.

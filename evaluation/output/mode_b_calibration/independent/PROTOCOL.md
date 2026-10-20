# Independent evaluation of Mode B retrieval sufficiency

Purpose: decide whether `RETRIEVAL_MIN_SCORE` should change, from questions
and labels that were **not** produced by the developer who wrote the original
14 synthetic questions. Nothing here touches production data or settings:
inputs are the files in this folder, outputs are new JSON files beside them.

## Roles (three different people)

| Role | Does | Must not |
|---|---|---|
| Question author | Writes questions into `independent-questions.csv` | Label them; see scores |
| Labellers (2) | Label each question independently in their own copy of `labeller-labels-template.csv` | See scores, the other labeller's file, or the author's intent |
| Adjudicator | Resolves disagreements into `adjudicated-labels.csv` | Change labels after seeing scores |
| Operator | Runs the embedding step (approval needed) and the scoring step | Edit any question or label |

The original developer may act as operator only. The 14 synthetic questions
(`../question-labels-template.csv`) are excluded from scoring.

## 1. Write questions (`independent-questions-template.csv`)

* At least **25 per eventual label**, so ~100+ questions; more is better.
* Spread across: what the 20 papers clearly answer (see `papers.csv`); what
  they half-answer; adjacent NLP/LLM topics they do not cover; clearly
  unrelated science. Do not copy sentences from `passages.csv`.
* `question_id` is unique and stable (`i001`...). No intended category is
  recorded here, so labellers are not primed.

## 2. Label before any score exists (`labeller-labels-template.csv`)

For each question, with `papers.csv` and `passages.csv` only:

| `label` | Meaning |
|---|---|
| `relevant` | The corpus answers the question directly |
| `partially_relevant` | The corpus answers part of it |
| `weak` | Topically adjacent; the corpus does not answer it |
| `unrelated` | Off-topic for this corpus |

* `relevant_paper_ids`: every paper that helps answer it (`;`-separated).
* `relevant_passage_ids`: the passages that carry the answer, from
  `passages.csv` (`;`-separated). Leave blank for weak/unrelated.
* `confidence`: 1 (unsure) - 3 (certain).

## 3. Agreement and adjudication (`adjudicated-labels-template.csv`)

* Report Cohen's kappa on `label` (`calibration.cohen_kappa`). Below 0.6 the
  label definitions are not being applied consistently: revise them and
  relabel before going on.
* Disagreements are resolved by the adjudicator (`agreement=adjudicated`);
  paper/passage ids are the union both labellers accept.

## 4. Run retrieval (requires owner approval: local SPECTER2 load)

```
docker exec -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 nlp-backend-1 \
  python -m app.interactive.calibration --mode embed --allow-model-load \
  --questions /path/independent-questions.csv > run.json
```

Reads Qdrant only; records every top-12 hit per question; no LLM call. The
run never sees a label.

## 5. Score (offline, no approval needed)

```
python -m app.interactive.calibration --evaluate-labels adjudicated-labels.csv --run run.json
```

* Questions are split deterministically (hash of `question_id`) into a tune
  half and a test half. The threshold is chosen on the tune half only, by
  (1 - false-accept) + (1 - false-reject); every reported number is from the
  test half. The current 0.2 is reported on the same test half.

| Metric | Definition (first retrieval round, Mode B's own sufficiency rule) |
|---|---|
| false-reject rate | share of `relevant` questions judged insufficient |
| false-accept rate | share of `unrelated` questions judged sufficient |
| accepted fraction (partial, weak) | reported, not scored: whether these should be answered is a product decision |
| retry frequency | share of all questions judged insufficient (each would trigger a reformulation) |
| paper recall@12 | share of labelled relevant papers among the 12 hits |
| passage recall@12 | share of labelled answer passages among the 12 hits |
| distinct papers in top 12 | mean number of different papers retrieved |

## 6. Decide

A threshold may be proposed only if `sample_sufficient` is true (>= 25
relevant and >= 25 unrelated), kappa >= 0.6, and the test-half false-accept
and false-reject rates are both acceptable to the owner. The proposal goes to
the owner; nothing here changes the setting.

## Limits

* Measures round-1 sufficiency only. Whether a reformulation then finds
  better evidence needs the LLM and is not measured here.
* Says nothing about answer grounding or answer relevance; those are
  separate checks (see the Phase 10 release gate).

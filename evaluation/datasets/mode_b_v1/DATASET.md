# Mode B evaluation dataset v1 (provisional, agent-authored)

**Status: PROVISIONAL. Not independent ground truth.** Every question, label
and claim here was written by the AI coding agent working on this repository
(Claude, 2026-10-18), not by independent annotators or domain experts. Use it
to exercise and regression-test the Mode B evaluation harness, and to get a
first, bias-prone estimate of retrieval and claim-check behaviour. It does not
satisfy `evaluation/output/mode_b_calibration/independent/PROTOCOL.md`, which
needs a separate question author, two labellers and an adjudicator. That
protocol remains the route to a defensible threshold.

## Files

| File | Rows | Content |
|---|---|---|
| `questions.csv` | 130 | `question_id`, `question`. The only file the retrieval run reads. It holds no labels. |
| `labels.csv` | 130 | `label`, `question_type`, `papers_needed`, `relevant_paper_ids`, `relevant_passage_ids`, `rationale` |
| `claims.csv` | 60 | Claim-level grounding cases: `label` (`supported`/`unsupported`), `phenomenon`, `cited_passage_ids`, `claim`, `note` |

Passage ids refer to the passage catalogue
`evaluation/output/mode_b_calibration/independent/passages.csv` (632 rows, one
per Qdrant point, exported read-only on 2026-10-14). The harness checks every
id against that catalogue and fails on an unknown id or a paper mismatch.

## Frozen before any score was seen

These SHA-256 values were recorded at 2026-10-18T07:18Z, after authoring and
**before** any question was embedded and before any claim was run through
the claim checker:

| File | sha256 |
|---|---|
| `questions.csv` | `ce759430b148a125cdd0ee3fa07637daaa9a7050fe0778ef4c68d5329c352063` |
| `labels.csv` | `5c14007f86a6db3f9542bed8ac1a161152ef78a34ef9a318f92732ef203acc6f` |
| `claims.csv` | `1f465372d46ac2faa0517a007797def7952aff6a01b7f19b6d11adec09acf799` |
| `passages.csv` (catalogue) | `2b9d904ea64e7308447286b56fb19fbc5c888703bf165da394d6d376190bb0e0` |

The harness records the hashes it actually read in every run manifest. If a
hash here differs from a run's, the labels changed after that run.

## Label definitions (retrieval relevance of the indexed corpus)

"Corpus" means the **indexed evidence**: the 632 passages of the 11 full-text
papers (ids 1–10, 20). Papers 11–19 are metadata only, have no indexed
passage, and cannot support a Mode B answer.

| Label | n | Meaning | Desired sufficiency-gate outcome |
|---|---|---|---|
| `relevant` | 41 | The indexed passages directly answer the question. | accept |
| `partially_relevant` | 28 | Some indexed passages answer part of it; an important part is unsupported. | accept (the answer must then be scoped to what is supported) |
| `weak` | 31 | Topically adjacent (prompt injection, multilingual NLP, LLM security) or about a metadata-only paper; the passages do not answer it. | reject |
| `unrelated` | 30 | Off-topic for this corpus. | reject |

Treating `partially_relevant` as "accept" is a product choice: some supported
evidence exists, and the grounding layer has to limit the answer to it. The
harness also reports the stricter split (`relevant` vs `unrelated` only).

`question_type` values: `factual`, `numeric`, `comparison`, `limitations`,
`future_work`, `synthesis`, `causal_explanation`, `absence_stated` (the
corpus explicitly says something was not done), `partial_single_paper`,
`overgeneralization_trap`, `misleading_overlap` (shares vocabulary with the
corpus but is not answered by it), `not_in_index` (about a metadata-only
paper), `adjacent_topic`, `off_topic`.

`papers_needed` is 2 for questions whose answer needs evidence from more than
one paper. `relevant_passage_ids` lists the passages the author found that
carry the answer. **It is not exhaustive**, so passage precision computed
against it is a lower bound. Weak and unrelated rows have no expected
evidence.

## Claim labels

`supported`: every part of the claim is stated by its cited passage(s), as
judged by the author. `unsupported`: at least one part is absent from,
contradicted by, or overgeneralised from the cited passage(s). The phenomena
were chosen to cover numbers, languages, models, datasets, absolutes,
negation, comparison direction, causal insertion, partial support,
misattribution and wrong citations. The deterministic claim checker can only
**reject**. Measuring it on this set gives its veto rate on unsupported claims
and its false-rejection rate on supported ones, not an entailment accuracy.

## Splits

Retrieval questions are split by `calibration.split_tune_test`, a SHA-256 of
`question_id`. The split is fixed by the id alone and is independent of
labels and scores. The `tune` half is the calibration split; the `test` half
is held out and is never used to choose a threshold.

## Known biases and limits

* One author wrote both the questions and the labels, after reading the
  corpus and knowing the 2026-10-13 synthetic-query result (relevant ~0.95,
  unrelated ~0.87 second-best cosine). Labels were assigned from the passage
  text, not from scores, but this knowledge cannot be removed.
* The same author designed the claim set and then changed the claim checker.
  Claim-check results on this set are development-set results and say little
  about generalisation.
* "Unrelated" questions are easy by construction (clearly other sciences).
  Real off-topic traffic may sit closer to the corpus.
* There is no second labeller, so inter-annotator agreement cannot be
  computed. The `labeller_id` and kappa machinery in `PROTOCOL.md` is unused
  here.

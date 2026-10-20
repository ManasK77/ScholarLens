# Mode B retrieval-sufficiency calibration (offline, 2026-10-13)

Status: **provisional, no threshold recommended.** The production setting
`RETRIEVAL_MIN_SCORE=0.2` is unchanged.

## What the score is (verified in code)

| Item | Finding | Where |
|---|---|---|
| Score | Qdrant cosine similarity, `Distance.COSINE` | `EvidenceIndex.ensure_collection` |
| Transform | none: `float(point.score)` is passed through | `EvidenceIndex.retrieve` |
| Direction | higher = more similar; relevant when `score >= RETRIEVAL_MIN_SCORE` | `workflow.assess_sufficiency` |
| Query encoder | the same SPECTER2 base + proximity adapter as the passages | `EvidenceIndex.retrieve` → `encoder.encode` |
| Graph rows | fixed `0.5`, not a similarity; excluded from sufficiency (D-10-05) | `workflow._graph_evidence` |
| Reranker | cross-encoder score, computed *after* sufficiency; not used by it | `workflow.rerank` |

## Stored-vector run (`stored-vectors.json`, no model loaded)

`docker exec nlp-backend-1 python -m app.interactive.calibration`

Every one of the 632 stored vectors was used as a proxy query (itself excluded),
retrieving `RETRIEVAL_TOP_N=12` through Qdrant `query_points` (read-only).

| Measure (cosine) | min | median | max |
|---|---|---|---|
| any two stored passages (398,792 pairs) | 0.716 | 0.872 | 1.000 |
| nearest neighbour | 0.893 | 0.953 | 1.000 |
| 2nd-best hit (Mode B needs 2 chunks) | 0.889 | 0.945 | 1.000 |
| 12th-best hit | 0.865 | 0.927 | 0.953 |

Fraction of proxy queries Mode B would judge sufficient (2 chunks, 2 papers):

| threshold | 0.2–0.86 | 0.88 | 0.90 | 0.92 | 0.95 |
|---|---|---|---|---|---|
| sufficient | 92.1% | 91.9% | 90.2% | 81.5% | 15.7% |

The 7.9% that fail at every threshold fail the **two-paper** rule (all 12 hits
from one paper), not the score.

## What this shows, and what it cannot

* **0.2 rejects nothing.** No pair of stored passages scores below 0.716, so the
  relevance part of the sufficiency check is inert on this corpus; sufficiency
  is decided by the chunk and paper counts alone.
* **False-reject bound (proxy):** thresholds up to ~0.88 reject no in-corpus
  proxy query that 0.2 accepts; 0.90 rejects 1.9 points, 0.92 rejects 10.6.
* **No false-accept evidence.** No stored vector is unrelated to the corpus, so
  this run cannot say which threshold rejects off-topic questions. That lower
  bound is the half a threshold is actually for.
* **Proxy caveat.** Real questions are short and phrased unlike passages; their
  cosines may sit lower than passage-to-passage cosines. Even the upper bound
  is only indicative until real queries are embedded.

## What remains (needs approval)

`docker exec nlp-backend-1 python -m app.interactive.calibration --mode embed --allow-model-load`

Loads SPECTER2 + adapter **locally from the model cache** (no LLM, no external
API) and embeds the 14 synthetic queries in `calibration.QUERIES` (4 relevant,
3 partial, 3 weak, 4 unrelated; author-labelled, not expert-validated). It
reports each category's 2nd-best cosine and whether any threshold separates
relevant from unrelated (`relevant_vs_unrelated`). A threshold would still be
provisional: 14 synthetic queries are a sanity check, not a validated set.

## Real-question run (`embedded-queries.json`, approved 2026-10-13)

`docker exec -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 nlp-backend-1 python -m app.interactive.calibration --mode embed --allow-model-load`

Offline (cache only; cached `refs/main` = the commits the stored vectors were
embedded with: base `3447645e`, adapter `2081559`). No LLM, no write; 45-value
snapshot identical before/after.

2nd-best cosine (what a 2-chunk sufficiency check turns on):

| category (synthetic, n) | min | max |
|---|---|---|
| relevant (4) | 0.9450 | 0.9614 |
| partial (3) | 0.9257 | 0.9382 |
| weak (3) | 0.9233 | 0.9327 |
| unrelated (4) | 0.8685 | 0.8886 |

Queries Mode B would call sufficient:

| threshold | 0.2 | 0.86 | 0.88 | 0.90 | 0.92 | 0.95 |
|---|---|---|---|---|---|---|
| relevant | 4/4 | 4/4 | 4/4 | 4/4 | 4/4 | 3/4 |
| partial | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 0/3 |
| weak | 3/3 | 3/3 | 3/3 | 3/3 | 3/3 | 0/3 |
| unrelated | 4/4 | 4/4 | 2/4 | 0/4 | 0/4 | 0/4 |

* Relevant and unrelated separate in this sample: any threshold in
  (0.8886, 0.9450]. Margin above the highest unrelated query: 0.011 at 0.90.
* Weak and partial overlap (0.923-0.938) and sit under every relevant query; no
  threshold separates "weak" from "partial".
* 0.90 rejects all 4 unrelated, keeps all relevant/partial/weak, and costs 1.9
  points of in-corpus proxies (92.1% -> 90.2%). 0.92 costs 10.6 points and
  still accepts all weak queries.

**Not justified as a production value.** 4 unrelated and 4 relevant
author-labelled queries, a 0.011 margin, and no labels for "weak" make 0.90 a
candidate, not a calibration. `RETRIEVAL_MIN_SCORE` stays 0.2.

## Leave-one-out on the saved run (`leave-one-out.json`, no model)

`python -m app.interactive.calibration --from-file embedded-queries.json`

Each of the 8 relevant/unrelated queries was held out and the threshold set to
the midpoint of the other 7's separating range: 8/8 held-out queries landed on
their own side, thresholds 0.913-0.919. This only says the separation does not
hinge on one query. The 14 queries were written and labelled by the same
author who analyses them, so selection bias is not removed.

## Collecting independent labels (procedure; no labels are fabricated)

`question-labels-template.csv` lists the 14 synthetic questions with blank
label columns. To make a threshold defensible:

1. Add new questions (aim for >= 25 per label) written by someone other than
   the analyst, from the corpus topic and from clearly off-topic areas.
2. Each labeller fills `labeller_id`, `label` (`answerable` / `partially` /
   `not_answerable` from this 20-paper corpus) and `relevant_paper_ids`,
   without seeing any similarity score.
3. Split the labelled set: choose the threshold on one half, report
   false-accept / false-reject rates on the other half only.
4. Run `--mode embed --allow-model-load` (approval required; local SPECTER2,
   offline flags) over the labelled questions, then `--from-file`.

## 2026-10-18 -- provisional 130-question evaluation (agent-authored labels)

`evaluation/datasets/mode_b_v1/` (41 relevant / 28 partial / 31 weak / 30
unrelated, frozen before scoring), scored by `evaluation/mode_b.py` in
`evaluation/output/mode_b_eval/run-20261018-v1/` (see its `REPORT.md`). Held
out (n = 66): at 0.2 the gate accepts 12/12 unrelated and 13/13 weak. A
threshold chosen on the calibration split only (0.9153) accepts 0/12
unrelated and 7/13 weak, and rejects 2/27 relevant and 1/14 partial; the two
relevant rejections fail at any threshold under the two-paper rule.
Relevant-vs-unrelated AUROC 0.951; partial-vs-weak 0.742. The highest
unrelated question scores 0.9131 against 0.9133 for the lowest partial, so
**`RETRIEVAL_MIN_SCORE` stays 0.2**: the labels are not independent and the
margin is ~0.005. The independent protocol above is unchanged and still
required before any threshold change.

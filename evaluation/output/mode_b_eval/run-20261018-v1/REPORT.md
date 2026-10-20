# Mode B evaluation, run `run-20261018-v1`

Offline evaluation of Ask ScholarLens (Mode B), plus six live `/query`
requests. Every number below comes from `results.json` (results digest
`4e5c308a…`, reproduced from `manifest.json`), `live/live-calls.json` or
`live/live-review.csv` in this folder.

**Labels are provisional.** The dataset (`evaluation/datasets/mode_b_v1/`) was
written and labelled by the AI coding agent, with one labeller and no
adjudication. It was frozen and hashed before any score was computed. Nothing
here is independent ground truth or a human-verified rate.

## 1. What was measured

| Part | Unit | n | Source |
|---|---|---|---|
| Retrieval gate | question, round-1 hits | 130 (64 calibration / 66 held-out) | `retrieval-run.json`, made by embedding `questions.csv` in the backend container. Offline SPECTER2 at cached commits `3447645e` / `2081559`. The query fingerprint equals all 632 stored vectors', and 8/8 re-encoded stored passages reproduce their stored vectors (cosine 1.000000). |
| Claim check | claim and its own cited passages | 60 (30 supported / 30 unsupported) | `claims.csv` |
| Live behaviour | `/query` request | 6 (14 Groq calls) | `live/` |

The production settings evaluated were `RETRIEVAL_MIN_SCORE=0.2`, 2 chunks, 2
distinct papers, top-12, 3 rounds, 1 regeneration and a 180 s deadline. The
deployment also sets `GROQ_MIN_INTERVAL_SECONDS=60`.

## 2. Retrieval sufficiency gate

The **gate score** of a question is the highest threshold at which Mode B's
own rule (`assess_sufficiency`) accepts its round-1 hits.

| Label | n | gate score min / median / max |
|---|---|---|
| relevant | 41 (2 never accepted) | 0.9186 / 0.9403 / 0.9650 |
| partially relevant | 28 | 0.9133 / 0.9393 / 0.9567 |
| weak | 31 (1 never accepted) | 0.8907 / 0.9262 / 0.9561 |
| unrelated | 30 | 0.8511 / 0.8731 / 0.9131 |

AUROC (all 130): relevant vs unrelated **0.951**; answerable vs weak+unrelated
0.856; partial vs weak **0.742**; relevant vs weak 0.746.

**At the production threshold 0.2 the gate rejects nothing on topic or off it.**
Gate outcomes on the held-out split (n = 66):

| | 0.2 (current) | 0.9153 (chosen on calibration only) |
|---|---|---|
| unrelated accepted (false accept) | **12/12** (CI 0.76–1.00) | **0/12** (CI 0.00–0.24) |
| weak accepted (false accept) | 13/13 (CI 0.77–1.00) | 7/13 (CI 0.29–0.77) |
| relevant rejected (false reject) | 2/27 (CI 0.02–0.23) | 2/27 (CI 0.02–0.23) |
| partially relevant rejected | 0/14 | 1/14 (CI 0.01–0.31) |
| balanced accuracy, answerable vs not | 0.476 | 0.823 |
| relevant vs unrelated: recall / specificity | 0.926 / 0.000 | 0.926 / 1.000 |

On the calibration split, 0.9153 gave balanced accuracy 0.806 with 0/18
unrelated accepted and 14/18 weak accepted.

What this shows:

* A threshold near 0.915 would stop clearly off-topic questions, which
  today reach generation. It cannot stop topical-but-unanswerable ("weak")
  questions, because weak and answerable questions overlap across the whole
  range.
* **The margins are too small for a production setting.** The highest
  unrelated question ("What is the best way to brew espresso at home?")
  scores 0.9131. The lowest partially relevant scores 0.9133, and two relevant
  questions score 0.9186 and 0.9192. With SPECTER2's compressed cosine scale,
  a 0.003–0.006 margin is within phrasing noise.
* **Distinct-paper rule.** Both held-out relevant false rejections (p031,
  p032) fail at *every* threshold: all 12 of their hits come from the single
  paper that answers them (paper 10). At 0.9153 on held-out, `min_papers=1`
  accepts 27/27 relevant (versus 25/27) with weak acceptance unchanged (7/13),
  raising balanced accuracy from 0.823 to 0.848.

**Decision: production settings unchanged** (`RETRIEVAL_MIN_SCORE=0.2`,
`RETRIEVAL_MIN_PAPERS=2`). The labels are agent-authored, the
threshold margin is ~0.005, and the paper-rule evidence rests on two
questions. Both changes are recommendations for the owner (§6), not
calibrated values.

Ranking (relevant and partial questions with expected passages, n = 69): the
expected paper was in the top 12 for 0.934 (mean paper recall) and ranked
first for 47/69. Passage recall@12 was 0.290 and MRR 0.240. The expected
passage lists are not exhaustive, so passage figures are lower bounds.

## 3. Claim check (deterministic vetoes)

The checks can only reject. A claim that passes them still needs the LLM
verifier, which is the same model that wrote it.

| | before this work | after |
|---|---|---|
| supported claims vetoed (false rejection) | **11/30** | **1/30** (CI 0.006–0.17) |
| unsupported claims vetoed (caught before the LLM) | 20/30 | **22/30** (CI 0.56–0.86) |

Root causes of the 11 false rejections: every percentage claim was vetoed,
because `\b85%\b` can never match before a space (6 cases). Thousands
separators broke number matching (`6,000` ≠ `6000`). The stemmer leaves
`required` / `require` unmatched, and `ChatGPT` / `Chat-GPT` never matched.
Acronyms never bridged ("FPR" vs "false positive rate").

New narrow vetoes: `polarity_mismatch` (a dropped "not", with negation scope),
`unsupported_name` (a model or dataset name absent from the cited text and its
paper's title) and `unsupported_cause` (an added "because …").

Still passing the vetoes (left to the LLM verifier): a reversed comparison, two
partial-support claims, a meaning-changing paraphrase, an antonym ("lowest"
where the passage says "maximum"), an overgeneralisation at the 0.5 overlap
boundary, a "first to" priority claim and a model swap whose name the passage
contains. The one remaining false rejection is "WMT", a venue acronym its
passage never names.

**These are development-set results.** The same author wrote the claims and
changed the checker, so they show the fixes work on the cases they target,
not how the checker generalises.

## 4. Live validation (6 requests, 14 Groq calls, 24,700 prompt + 8,043 completion tokens)

| Test | Purpose | Result | Groq calls | Time |
|---|---|---|---|---|
| LV-1 | answerable factual (paper 3) | **HTTP 504**. Draft 1 was rejected by the verifier; the second verification was queued behind 60 s pacing and was sent 12 s **after** the 504. | 4 (1 after the deadline) | 180.0 s |
| LV-2 | synthesis (papers 1, 3, 10) | refusal, `grounding_failed`; both drafts vetoed | 2 | 71 s |
| LV-2b | rerun of LV-2 with per-claim veto codes | refusal; codes `unsupported_term`+`misattributed`, `unsupported_term` (twice) | 2 | 72 s |
| LV-3 | unsupported question (paper 15 not indexed) | **correct abstention** (`grounding_failed`); invented-figure claims vetoed | 2 | 72 s |
| LV-1b | rerun of LV-1 on the fixed code | **grounded answer, 9/9 checks**: "Turkish had the highest full compliance rate." [b77a41e9, paper 3] | 2 | 96 s |
| LV-2c | rerun of LV-2 with explained regeneration feedback | refusal, same codes | 2 | 82 s |

* LV-1b, agent review (not independent): the claim is supported by its
  passage ("Turkish shows the highest full compliance (80.18%)…"), and its
  citation is correct and complete. The answer is correct but minimal: it
  omits the 80.18% figure.
* LV-2 was refused three times. The vetoes that fired are the pre-existing
  Phase 10 ones: a language, resource level or number not stated by the
  claim's own citations, and attribution of paper 1's "Prior studies have
  observed …" to paper 1 itself. Mode B never records draft text, so whether
  these were correct rejections or over-refusal **cannot be verified**.
  Retrieval also offered no paper-10 passage, and graph rows displaced 3
  vector passages from the reranked top 12.
* Loop bounds held in every run (≤ 1 round, ≤ 1 regeneration). No credential
  appears in any output or in the backend log (checked by value).

## 5. Defects found and fixed in this run

| Id | Defect | Fix | Regression tests |
|---|---|---|---|
| D-B-01 | Every percentage claim was falsely vetoed; numbers with thousands separators and trailing zeros were compared as text | Numbers compared by value | `test_claim_check_hardening.py` |
| D-B-02 | Overlap missed inflections, closed compounds and acronyms; titles inflated overlap (found in review) | Prefix stems, joined compounds, acronyms only for words written in capitals; titles vouch for names only | same |
| D-B-03 | Generation could see and cite sub-threshold passages and graph rows of papers that failed the gate | `qualifying_evidence`: the gate's own rule, graph rows by seed paper | `test_evidence_selection.py` |
| D-B-04 | A Groq call queued behind `GROQ_MIN_INTERVAL_SECONDS` was sent after the query deadline (live LV-1) | The per-query wait budget refuses the slot; immediate 504, nothing sent | `test_rate_limit.py`, `test_query_deadline.py` |
| D-B-05 | Grounding refusals were undiagnosable (counts only) | `veto_codes=` per claim in the trace (codes only, no text) | `test_claim_grounding.py`, `test_live_check.py` |
| D-B-06 | The strict regeneration got bare veto codes | Each fired code is explained; it did not change LV-2c's outcome | `test_claim_grounding.py` |
| D-B-07 | Repeated claims were shown twice | De-duplicated in `generate` | `test_evidence_selection.py` |
| D-B-08 | The embed run labelled any external question file "independent" | Neutral wording; provenance comes from the dataset card | `test_calibration.py` |
| D-13-04 | Time-shift scored unlabelled gaps as "no relevant papers"; the adapter wrote `[]`; precision with no prediction was 0.0 | `null` = unlabelled; precision and recall over labelled gaps; undefined is `None` | `test_time_shift.py`, `test_scholarlens.py` |

Mutation check (scratch copies, working tree untouched): each fix was
re-broken one at a time. **25/25 mutations were killed** (15 for D-B-01–03 and
D-B-07, 10 for D-B-04–06, D-B-08 and D-13-04), and the unmutated controls
passed.

## 6. Open items for the owner

1. **`GROQ_MIN_INTERVAL_SECONDS=60` with `QUERY_DEADLINE_SECONDS=180`**: any
   Mode B path of 4 or more model calls cannot finish. That covers a verifier
   rejection followed by regeneration, and reformulations followed by
   generation. Since D-B-04 such a path fails fast with a 504 instead of
   sending a late call. Fix it by lowering the interval (Mode B issues ≤ 6
   calls per query) or raising the deadline (≥ 5 × interval + ~40 s).
2. **Threshold**: 0.915 is the evidence-backed candidate for rejecting
   off-topic questions at round 1. Adopting it needs independent labels
   (`mode_b_calibration/independent/PROTOCOL.md`), a live check that
   reformulation rescues near-threshold relevant questions, and the owner's
   decision.
3. **`RETRIEVAL_MIN_PAPERS=2`** refuses single-paper questions whose hits all
   come from that paper (paper 10 here). `min_papers=1` is supported by n=2
   on held-out; it weakens a safeguard, so it is the owner's decision.
4. **Synthesis refusals** (LV-2): diagnosing them needs the rejected drafts.
   That means either a human-reviewed debug capture or relaxing the
   no-draft-text logging rule, which is a design decision.
5. **Graph rows** compete with passages in the cross-encoder's top 12 (per
   README §9). In LV-2 they displaced 3 passages. Reserving slots for
   passages would be a design change.

## 7. Reproduce

```
PYTHONPATH=. backend/.venv/Scripts/python.exe -m evaluation.mode_b \
  --reproduce evaluation/output/mode_b_eval/run-20261018-v1/manifest.json
```

The full command chain is in `evaluation/README.md` ("Mode B evaluation").

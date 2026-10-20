# Mode B evaluation, run-20261018-v2 (quality mission, 2026-10-18)

Offline results: `results.json` (digest `b6344ee9…`, `--reproduce` verified).
Live results: `live/`. Cross-encoder analysis: `rerank-scores.json`,
`rerank-floor-analysis.json`.

**All labels are provisional and agent-authored** (`mode_b_v1`, and the new
`mode_b_claims_s2`). The same agent changed the checks in the same session, so
these are development figures, not independent ground truth. No human has
reviewed any live claim. The reviews below are agent reviews.

Inputs: dataset `mode_b_v1` (130 questions, 60 claims, frozen); retrieval run
`run-20261018-v1/retrieval-run.json` (round-1 hits, encoder = stored vectors);
supplement `mode_b_claims_s2` (34 claims); pre-registered aspects
`live/aspects.json`.

## 1. Retrieval sufficiency

### Score semantics (verified, unchanged)

* Retrieval scores are raw Qdrant cosines: `Distance.COSINE`, higher means
  more similar, and a passage qualifies when `score >= min`.
* Graph rows carry a synthetic 0.5. They never count toward sufficiency
  (D-10-05).
* Duplicate (paper, text) passages count once.
* The cross-encoder score (`rerank_score`) is kept separate from the cosine
  and never compared with the threshold.

### Scope-aware paper minimum (new)

| Policy | t = 0.2, held-out | t = 0.9153 (chosen on calibration), held-out |
|---|---|---|
| Two-paper rule (before) | relevant 25/27, partial 14/14, weak 13/13, unrelated 12/12 | relevant 25/27, partial 13/14, weak 7/13, unrelated 0/12 |
| Scope policy (now) | relevant **26/27**, partial 14/14, weak 13/13, unrelated 12/12 | relevant **26/27**, partial 13/14, weak 7/13, unrelated 0/12 |

* **Questions whose decision changes (all 130):**
  * p031 ("… in the ChatGPT multilingual fuzzing study?"): refused before,
    now accepted at both thresholds;
  * p043 (a comparison of Sentra-Guard and MIPIAD): accepted before, now
    refused at 0.9153 only, because no qualifying passage names one of the
    two compared systems.
* No weak or unrelated question changes decision.
* **Scope against the labelled papers needed:**
  * none of the 13 questions labelled as needing two papers is read as
    single-paper;
  * 26 of the 117 one-paper questions are read as single-paper;
  * 74 are `unspecified` and keep the conservative two-paper minimum.
* p032 is still refused. It names ChatGPT but no paper or study, and a
  model a title merely mentions is not counted as a single-paper signal
  (code-review fix).

### Threshold decision: `RETRIEVAL_MIN_SCORE` stays at 0.2

* **The cosine gate cannot be defended on this data.** At 0.2 it accepts
  12/12 unrelated held-out questions. At 0.9153 it accepts 0/12, but the
  margin to the closest partial question is about 0.005, and the labels are
  agent-authored. This is the Phase 13 finding, unchanged.
* **The cross-encoder top-1 score separates far better.** Measured offline
  with the cached `ms-marco-MiniLM-L-6-v2` (commit `233902d2`; HF offline, no
  LLM):

  | AUROC | relevant vs unrelated | relevant vs weak | partial vs weak | answerable vs rest |
  |---|---|---|---|---|
  | cross-encoder top-1 | **1.000** | **0.864** | 0.704 | **0.898** |
  | cosine gate score | 0.951 | 0.746 | 0.742 | 0.856 |

* **A floor of -5.74** sits at the calibration midpoint between the highest
  unrelated score (-8.68) and the lowest relevant one (-2.79). Applied once to
  held-out, it accepts:
  * 27/27 relevant;
  * 14/14 partial;
  * 11/13 weak;
  * 0/12 unrelated.
* **It is not deployed.** The lowest relevant score over all 130 questions is
  -5.36, only 0.38 above the floor. Deploying it would also be a design
  change (a gate after rerank). It needs independent labels and the owner's
  decision.
* Weak questions (topical but unanswerable) still pass every gate measured.
  Refusing them rests on the claim checks and the relevance rule (§2).

## 2. Claim and answer checks

### Deterministic vetoes

The checks can only veto: a veto rejects a claim, and passing them proves nothing.

| Set | Supported claims vetoed (false rejection) | Unsupported claims vetoed (catch) |
|---|---|---|
| `mode_b_v1` (60, development) | 1/30 (c13, the venue "WMT") | 22/30 |
| `mode_b_claims_s2` (34, new) | **0/15** | 13/19 |

* **Still left to the LLM verifier** (both sets): a reversed numeric
  comparison, a language or model swapped inside the same passage, a
  contradiction, partial support, meaning-changing reuse of the passage's
  words, and hedging dropped. Phase 13's set adds an antonym, a priority
  claim and an overgeneralisation at the overlap boundary.
* **Real-passage false rejections fixed this run** (test file in brackets):
  * "Paper 10 reports …" read as the number 10 (`test_cross_paper`);
  * a passage sentence of the form "X, and all … do not …" read as negating
    X (`test_cross_paper`);
  * "none of" not matched by "all … not" (`test_cross_paper`);
  * a missed split at "[15].Since", 44/632 passages (`test_cross_paper`);
  * digits inside a name a title states, e.g. "Llama-3-Guard"
    (`test_claim_check_supplement`).
* **New catches:**
  * `wrong_paper`: a claim names a paper id it does not cite;
  * `unsupported_absence`: "no study has …" with no stated gap;
  * `future_as_done`: a plan reported as done, only when the passage itself
    states a plan, because Stage 3 types some result sentences
    `future_work`;
  * "all LLMs";
  * prior work in a citing paper that carries [n] markers but no
    first-person wording;
  * attribution verbs beyond "report" (observes, finds, shows, …).

### Answer relevance (new)

* An answer is refused when its claims and their cited passages mention none
  of the question's subjects: named systems, models or datasets (venue
  acronyms of four letters or fewer excluded), and languages or resource
  levels.
* **Over-refusal proxy**, using expected passages as the answer's evidence:
  2/35 answerable questions with subjects would be refused (p047 and p064,
  both partially relevant). The rule can act on 13 of the 61 weak or
  unrelated questions.
* The verifier now also reads the question.

## 3. Live validation (owner-authorised, 6 requests, 13 Groq calls)

* **Preflight:**
  * container code equal to the working tree after every rebuild;
  * HF upstream commits equal the cache for SPECTER2 base, the adapter and
    the cross-encoder;
  * dry run;
  * snapshot taken before the runs.
* **Model and settings:** `openai/gpt-oss-20b`, 60 s pacing, 180 s deadline.
* **Tokens:** 20,751 prompt and 9,234 completion. No 429s or retries logged.
* **Secret scan:** 0 hits by value in outputs and the backend log.

| Run | Question | Code | Outcome | Calls / time | Finding |
|---|---|---|---|---|---|
| LV-Q1 | ChatGPT fuzzing study: languages and Q&A points | scope + pruning | grounded, 2 claims, paper 10 | 2 / 74.5 s | Gate read it single-paper (refused before). Claim 0 correct. Claim 1 gives RQ1's 3,630 as the study total (7,892): partial |
| LV-Q2 | Which papers report weaker safety for non-English prompts (LV-2) | + `Paper N` fix | grounded, papers 3 and 1 | 2 / 61.9 s | Confirms the likely LV-2 cause ("Paper 3" read as a number). **Defect:** paper 1's "Prior studies have observed …" shown as paper 1's own finding |
| LV-Q2b | same | + attribution fix | **grounded partial**, paper 3, 1 withheld | 2 / 70.8 s | Misattributed claim withheld (`veto_codes=-,misattributed`), its citation dropped. Paper 10 not retrieved |
| LV-Q3 | Turkish benchmark accuracy for GPT-4 (not indexed) | as LV-Q2b | **wrongly grounded** | 3 / 162.3 s | **Defect:** after a vetoed draft the regeneration answered with phishing-study accuracies (paper 20): supported but off-question |
| LV-Q3b | same | + relevance rule | refusal | 2 / 72.6 s | Correct refusal. Both drafts were vetoed by `unsupported_term`, so the new rule was not exercised live; it is verified on the exact LV-Q3 claim and passage in `test_answer_relevance.py` |
| LV-Q1b | same as LV-Q1 | final verifier prompt | grounded, same claims | 2 / 96.9 s | Verifier with the question still accepts the correct answer, and still accepts the 3,630 subset |

An earlier LV-Q2 invocation failed on argument parsing (`--expected-paper-ids`
takes `;`) and sent no request: `live/LV-Q2.argerror-not-sent.log`.

* **Outcome as expected:** 5/6.
* **Agent review of 8 claims:**
  * 5 supported, 2 partial, 1 unsupported;
  * citation correct 7/8;
  * complete 5/8.
* **Pre-registered aspect coverage:**
  * LV-Q1 and LV-Q1b 2/2;
  * LV-Q2 and LV-Q2b 1/2 (paper 10 never retrieved).
* The aspect metric is passage-based. It credits LV-Q1's "7,892" aspect
  because a cited passage states it, although the claim says 3,630. Read it
  as an upper bound on wording, a lower bound on evidence.

## 4. What these results do not show

* **Independent labels.** None exist for questions, claims or aspects.
  Every number here is a development figure.
* **Live coverage.** The live retry and reformulation paths, and the
  `off_question` refusal, were not exercised. Six requests describe only
  those runs.
* **Recall.** Paper 10 (expected for LV-Q2) was not retrieved in any run;
  passage recall@12 is ≤ 0.29 (Phase 13).
* **Subset answers.** A claim that is true of its passage but answers a
  different granularity (LV-Q1's "3,630") passes every check.

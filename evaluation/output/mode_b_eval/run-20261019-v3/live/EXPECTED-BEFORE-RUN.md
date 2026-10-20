# Live check plan, written before any request was sent (2026-10-19)

Purpose: confirm on the running system that the three fixes of this mission
do what the offline tests say, with the fewest Groq calls that can show it.
Nothing here is a validation of answer quality; the labels are agent-authored.

Model `openai/gpt-oss-20b`, `GROQ_MIN_INTERVAL_SECONDS=60`, 180 s deadline.
A request needing 4 or more model calls cannot finish (known, owner decision).

## LV-R1: study results (gap 2B) and the wider candidate pool (gap 2C)

Question (same as LV-Q1 / LV-Q1b of the previous mission):
"How many languages and how many question-answer data points were covered in
the ChatGPT multilingual fuzzing study?"

Expected, fixed now:

* Outcome `grounded_answer`; expected paper 10; 2 model calls.
* Languages: 121 (passage 6152189e).
* Q&A data points: acceptable answers are 7,892 as the study's total
  (passages 41cb089b, 7ad10f8c, 9a9da019, b9d03539, b9d4f2c2) or 3,630
  **scoped to RQ1 / with the total stated**.
* Failure (the previous defect): a claim giving 3,630 as the whole study's
  count without scope. After this mission that claim must either be absent from
  the shown answer (`partial_total` in the grounding trace, `removed_claim_count`
  at least 1) or be scoped.
* Not a failure: the model chooses to give only 7,892.

## LV-R2: topical but unanswerable (gap 2A)

Question (dataset item p086, labelled `weak`, `misleading_overlap`):
"Which tokenization strategies reduce jailbreak success for Arabic prompts?"

Why it tests the rule: the corpus has Arabic prompt-injection results (papers 1
and 8) and mentions tokenization (paper 8, in a preprocessing sentence), but no
passage studies tokenization as a defence. Retrieval is sufficient and a claim
about Arabic passes every deterministic check.

Expected, fixed now:

* Correct outcomes: `grounding_failed` (refusal) or `insufficient_evidence`.
  A `grounded_answer` that presents Arabic results as tokenization strategies
  is the failure.
* The new rule is **verified live** only if the grounding trace shows
  `reason=not_answerable` (and `verified=1`). Any other refusal reason is a
  correct refusal by a different check and does not count as the rule firing.

Repeat policy: if LV-R2 is correct but fires another check, one more weak
question (p099, sandboxing defences for browsing agents) may be asked to try to
exercise the rule; no other repeat without a stated diagnosis.

Maximum: 3 requests, at most 8 model calls.

## Addendum, written after LV-R1..R3 and before LV-R4 (2026-10-19)

Results so far: LV-R1 as expected. LV-R2 refused correctly, but by
`claim_check_failed` / `low_overlap` on both drafts, so the verifier never ran.
LV-R3 (p099) ended in a **grounded answer** built from two claims about defenses
in general (rule-based filtering, paper 7; Sentra-Guard, paper 8), none about
sandboxing. The verifier named one of three claims unsupported and kept the
rest; the trace did not record whether it gave `answers_question`.

Diagnosis: the verdict was too lenient for a question that names a specific
method, and the trace could not show the flag. Two changes since: the trace now
carries `answers=1|0|-`, and the verifier is told to check what the question
names and that general-topic claims do not answer it. This is a changed system,
not an identical rerun.

LV-R4: the same p099 question, once, to see whether the changed system stops
it. Fixed now:

* Rule works on this case: `grounding_failed` with `reason=not_answerable` and
  `answers=0` in the trace. A refusal by another check is correct but does not
  count as the rule firing.
* Rule does not work on this case: `grounded_answer`, or a grounded answer
  with `answers=1`. Then the rule is reported as **not shown to work live**, and
  no further live request is made.

Totals after LV-R4: 4 requests, at most 8 model calls.

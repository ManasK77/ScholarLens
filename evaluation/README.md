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

- `gaps.json`: `gap_id`, `description`, `cutoff_year`, and optional labelled `relevant_future_paper_ids`.
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

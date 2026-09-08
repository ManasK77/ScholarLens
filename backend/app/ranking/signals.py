"""Stage 8's five signals and the weighted formula over them (GAPRANK-01).

README §15 fixes the tech: **plain Python — no ML model, a transparent weighted
formula.** So this module imports nothing but the standard library: no NumPy, no
Pandas, no `sklearn`. Every number here can be recomputed by hand from the
stored breakdown `GET /gaps` serves, which is the whole point of ranking gaps
with a formula instead of a model.

```
gap_score = w1*mention_frequency + w2*trend_slope - w3*solution_density
          + w4*citation_impact  + w5*contradiction_flag
```

The signals are **raw and per-cluster** — no cross-cluster normalization
(D-08-01). Normalizing would make a cluster's score depend on which other
clusters happened to be in the run, so the same evidence would score differently
between runs and Stage 9's "27 papers mention it, only 3 propose mitigations"
sentence would stop being derivable from the cluster's own numbers. The weights
are what put the five units on a common scale; they live in `Settings`.

`service.py` owns reading the rows these functions consume.
"""

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass

# Six decimals: below any weight's significance, above the two the dashboard
# renders, and enough to keep a re-run byte-identical (D-08-08, mirroring
# Stage 7's SLOPE_DECIMALS).
SCORE_DECIMALS = 6

# Label terms, as `label_for_statements` produced them: lowercase words of three
# or more characters. Matching on this alphabet rather than on raw substrings is
# what keeps "injection" out of "injectionless".
_TERM = re.compile(r"[a-z][a-z0-9-]{2,}")


@dataclass(frozen=True, slots=True)
class GapSignals:
    """One cluster's five signals, in README §8's order.

    Stored verbatim alongside the score so a ranking can be audited by hand —
    `GET /gaps` serves exactly these under `signals`.
    """

    mention_frequency: int
    trend_slope: float
    solution_density: float
    citation_impact: float
    contradiction_flag: float


def mention_frequency(paper_ids: Iterable[int]) -> int:
    """How many distinct papers mention the theme (D-08-02).

    Papers, not member statements: one paper that contributed four limitation
    sentences to a cluster mentioned it once. Same unit as Stage 7's D-07-01 and
    as README §8 Stage 9's evidence line, *"27 papers mention it"* — and it is
    what keeps this signal correct even if the vector store ever holds a
    statement twice.
    """
    return len(set(paper_ids))


def trend_slope_signal(slope: float | None) -> float:
    """Stage 7's stored slope, signed and unscaled (D-08-03).

    Passed through rather than re-derived or rescaled, so the number the
    dashboard plots and the number the score uses cannot fork. `None` scores
    `0.0` defensively — D-07-07 guarantees a trend row per cluster.

    A non-finite value scores `0.0` too: a `NaN` propagating into the score
    would make every ordering comparison silently false.
    """
    if slope is None:
        return 0.0
    slope = float(slope)
    return slope if math.isfinite(slope) else 0.0


def _terms(text: str | None) -> list[str]:
    return _TERM.findall(text.lower()) if text else []


def tokenize_contributions(
    contributions_by_paper: dict[int, list[str]],
) -> dict[int, list[frozenset[str]]]:
    """`paper_id -> one term set per contribution statement`.

    Done once per corpus rather than once per cluster: `solution_density` is
    called for every cluster against the *same* corpus-wide contributions, so
    tokenizing inside it would re-scan every statement C times for C clusters.
    """
    return {
        paper_id: [frozenset(_terms(statement)) for statement in statements]
        for paper_id, statements in contributions_by_paper.items()
    }


def solution_density(
    label: str | None,
    mentioning_papers: int,
    contribution_terms: dict[int, list[frozenset[str]]],
) -> float:
    """Papers proposing solutions over papers mentioning the problem (D-08-04).

    A paper proposes a solution to this theme when one of its Phase 3
    `contribution` statements uses **every** term of the cluster's label —
    Phase 6's own deterministic description of what the theme is about
    (D-06-06). Requiring every term rather than any one keeps a single shared
    word ("multilingual") from marking half a focused corpus as solvers.

    `contribution_terms` (from `tokenize_contributions`) is corpus-wide, not
    restricted to the cluster's own papers: a paper that actually solved a
    problem rarely lists it as its own limitation, so it is not in the cluster,
    and restricting the numerator would leave this signal at zero for every
    theme. The ratio is clamped to `[0, 1]` so the `- w3 * density` term stays
    bounded and the value still reads as a density.

    Returns `0.0` — never raises — for a cluster with no label (nullable by
    Phase 6 contract), a label with no usable terms, or no mentioning papers.
    """
    terms = frozenset(_terms(label))
    if not terms or mentioning_papers <= 0:
        return 0.0

    solvers = sum(
        1
        for statements in contribution_terms.values()
        if any(terms <= statement for statement in statements)
    )
    return min(solvers / mentioning_papers, 1.0)


def citation_impact(citation_counts: Iterable[int | None]) -> float:
    """Mean citation count of the papers mentioning the theme (D-08-05).

    Mean rather than sum: a sum is a second copy of `mention_frequency`, since a
    40-paper theme would out-cite a 5-paper theme on headcount alone, and `w1`
    already prices frequency.

    An unknown count (`None`) counts as `0`. Dropping unknowns instead would let
    a single Semantic-Scholar-matched paper set a whole theme's impact; unknown
    means no evidence of impact. A negative count is corrupt external metadata,
    not a penalty, so it is floored at zero.
    """
    counts = list(citation_counts)
    if not counts:
        return 0.0
    return sum(max(float(count or 0), 0.0) for count in counts) / len(counts)


def contradiction_signal(flagged: bool) -> float:
    """Phase 6's cross-paper disagreement flag as `1.0` / `0.0` (D-08-06).

    README names a *flag*, and `frontend/src/types.ts` types it `number`.
    `Cluster.contradiction_score` is deliberately not substituted: a 0.71 and a
    0.99 both mean "these papers disagree", and swapping it in would re-weight
    `w5` by the NLI model's confidence without anyone asking for that.
    """
    return 1.0 if flagged else 0.0


def gap_score(
    signals: GapSignals, weights: tuple[float, float, float, float, float]
) -> float:
    """README §8's formula, applied literally.

    `weights` is `(w1..w5)` in the formula's order — `Settings.gap_weights()`
    is the one place that tuple is built, so a weight cannot drift onto the
    wrong signal.
    """
    if len(weights) != 5:
        raise ValueError(f"gap_score needs five weights, got {len(weights)}")
    w1, w2, w3, w4, w5 = weights

    score = (
        w1 * signals.mention_frequency
        + w2 * signals.trend_slope
        - w3 * signals.solution_density
        + w4 * signals.citation_impact
        + w5 * signals.contradiction_flag
    )
    if not math.isfinite(score):
        return 0.0
    score = round(score, SCORE_DECIMALS)
    # `-0.0` would render as "-0.00" in the dashboard's score pill.
    return 0.0 if score == 0.0 else score

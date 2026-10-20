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

from app.clustering.claims import content_terms

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


# What makes a contribution a *solution* rather than a finding (D-08-11).
#
# Deliberately narrow, and deliberately not "propose", "present", "introduce",
# "framework", "method" or "benchmark": measured on the 20-paper corpus, a cue
# list including those marked a solver in 26 of 27 clusters and drove density
# to 1.00 almost everywhere, which would make the -w3 term a constant. These
# terms name a remedy specifically, and they produced a real spread (0.00 to
# 1.00, most clusters between) on the same corpus.
_SOLUTION_CUE = re.compile(
    r"defen[cs]"  # defense / defence / defend
    r"|mitigat"  # mitigate / mitigation
    r"|safeguard"
    r"|countermeasur"
    r"|harden"
    r"|protect"
    r"|prevent"
    r"|neutraliz"
    r"|sanitiz"
    r"|remediat"
    r"|robust"
    r"|resilien"
    r"|\bguard"
    r"|detector|detection"
    r"|filter"
    r"|firewall"
    r"|shield"
)


@dataclass(frozen=True, slots=True)
class PaperContributions:
    """One paper's Phase 3 contributions, prepared for `solution_density`.

    `terms` is one stemmed term set per statement, used to decide whether the
    paper works on a given theme. `proposes_solution` is fixed per paper rather
    than per statement because the question the signal asks is about the paper
    ("did anyone address this?"), and Phase 3's extraction is sentence-level
    and noisy enough that requiring the remedy and the topic in one sentence is
    what made the old rule unsatisfiable.
    """

    terms: tuple[frozenset[str], ...]
    proposes_solution: bool


def index_contributions(
    contributions_by_paper: dict[int, list[str]],
) -> dict[int, PaperContributions]:
    """`paper_id -> PaperContributions`, prepared once for the whole run.

    Done once per corpus rather than once per cluster: `solution_density` is
    called for every cluster against the *same* corpus-wide contributions, so
    tokenizing inside it would re-scan every statement C times for C clusters.

    Terms are stemmed through `app.clustering.claims.content_terms`, the same
    function Stage 6's topic gate uses, so "injections" and "injection" agree
    and the two stages cannot drift apart on what a word is.
    """
    prepared: dict[int, PaperContributions] = {}
    for paper_id, statements in contributions_by_paper.items():
        prepared[paper_id] = PaperContributions(
            terms=tuple(frozenset(content_terms(text)) for text in statements),
            proposes_solution=any(
                _SOLUTION_CUE.search((text or "").lower()) for text in statements
            ),
        )
    return prepared


def solution_density(
    label: str | None,
    mentioning_paper_ids: Iterable[int],
    contributions: dict[int, PaperContributions],
    *,
    min_label_overlap: int = 2,
) -> float:
    """Papers proposing a solution over papers mentioning the problem.

    A paper counts as a solver when **both** hold:

    * it **works on this theme** — it is one of the cluster's own papers, or
      one of its contributions shares at least `min_label_overlap` stemmed
      terms with the cluster's label; and
    * it **proposes a remedy** — one of its contributions uses a solution cue
      (`_SOLUTION_CUE`), not merely a word from the label.

    **Why this replaced the all-label-terms rule (D-08-11).** The old rule asked
    for a contribution containing *every* term of the label. Measured on the
    20-paper LLM-security corpus it scored `0.00` for all 27 clusters: the top
    gap's label is "prompt attacks injection" and not one of the corpus's 92
    contribution sentences contains all three words. Worse, it was blind to the
    thing it exists to find — Sentra-Guard is a prompt injection *defense* whose
    contributions say "neutralized more than 98.7% of adversarial attempts" and
    share no word with that label at all. A signal that is always zero is not a
    signal; the `- w3 * density` term was decorative.

    Splitting the two questions is what fixes it. Lexical overlap is a poor
    test of "did you solve it" but an adequate test of "are you working on
    this", and cluster membership answers the second question outright for the
    papers the pipeline already grouped. The remedy cue answers the first.

    `contributions` stays corpus-wide rather than restricted to the cluster
    (D-08-04): a paper that actually solved a problem rarely lists it as its own
    limitation, so it is not a member, and restricting the numerator would put
    this signal back at zero for every theme.

    The ratio is clamped to `[0, 1]` so the `- w3 * density` term stays bounded
    and the value still reads as a density. Returns `0.0` — never raises — for
    a cluster with no mentioning papers.
    """
    members = set(mentioning_paper_ids)
    if not members:
        return 0.0

    label_terms = frozenset(content_terms(label))
    solvers = 0
    for paper_id, contribution in contributions.items():
        if not contribution.proposes_solution:
            continue
        on_theme = paper_id in members or (
            bool(label_terms)
            and any(
                len(label_terms & statement) >= min_label_overlap
                for statement in contribution.terms
            )
        )
        if on_theme:
            solvers += 1
    return min(solvers / len(members), 1.0)


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


def contradiction_signal(
    disagreeing_paper_pairs: int,
    *,
    min_paper_pairs: int = 2,
    full_strength: int = 3,
) -> float:
    """How broadly the cluster's papers disagree, in `[0, 1]` (D-08-10).

    Graded on the number of distinct *paper* pairs Stage 6 verified, not on a
    boolean and not on the NLI score:

    * below `min_paper_pairs` the cluster scores a clean `0.0` — Stage 6
      already declined to flag it, and this re-checks the same rule so a
      ranking cannot inherit a flag from an older run under looser settings;
    * at or above it the signal is `pairs / full_strength`, clamped to `1.0`.

    **Why breadth rather than the NLI probability.** D-08-06's reasoning is
    unchanged: a 0.71 and a 0.99 both mean "these two papers disagree", and
    substituting the model's confidence would re-weight `w5` by how sure a
    transformer felt. What *does* distinguish a strong disagreement from a weak
    one is how much of the literature takes part, and that is a count the
    pipeline can defend.

    **Why not a boolean.** On the 20-paper corpus a single uncorroborated pair
    contributed the full `w5` — 5.0 of an 11.01 gap score, enough to carry the
    cluster to rank 1 — on evidence that turned out to be an ethics disclaimer
    scored against a recommendation bullet. One pair should never have been
    able to buy the whole weight.
    """
    if disagreeing_paper_pairs < max(min_paper_pairs, 1):
        return 0.0
    return min(disagreeing_paper_pairs / full_strength, 1.0)


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


# A theme fewer papers than this state is one paper's view, not a pattern in
# the literature. Display/scope only: it changes no signal and no score, so a
# single-paper theme is ranked exactly as before and labelled exploratory.
CORROBORATION_MIN_PAPERS = 2


def evidence_scope(paper_count: int) -> str:
    """`corroborated`, or `single_paper` for a theme only one paper states."""
    return "corroborated" if paper_count >= CORROBORATION_MIN_PAPERS else "single_paper"

"""Single-paper themes are kept, ranked as before, and labelled exploratory.

A theme only one paper states is that paper's view, not a pattern across the
literature. On the live corpus cluster 13 ("adaptive adversarial defense") is
four statements from one paper; it stays in the ranking (README S8 ranks every
cluster) but is labelled so it cannot be read as a corroborated gap.
"""

import pytest

from app.ranking.signals import CORROBORATION_MIN_PAPERS, evidence_scope


@pytest.mark.parametrize(
    ("papers", "scope"),
    [
        (0, "single_paper"),
        (1, "single_paper"),
        (2, "corroborated"),
        (9, "corroborated"),
    ],
)
def test_one_paper_is_exploratory_two_or_more_corroborate(papers, scope):
    assert evidence_scope(papers) == scope


def test_the_threshold_is_two_papers():
    assert CORROBORATION_MIN_PAPERS == 2

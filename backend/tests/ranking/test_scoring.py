import pytest

from app.ranking.scoring import GapSignals, score_gap


def test_score_gap_exposes_each_weighted_signal():
    signals = GapSignals(
        mention_frequency=4,
        trend_slope=1.5,
        solution_density=0.25,
        citation_impact=10,
        contradiction_flag=1,
    )
    weights = {
        "mention_frequency": 1.0,
        "trend_slope": 2.0,
        "solution_density": 4.0,
        "citation_impact": 0.1,
        "contradiction_flag": 3.0,
    }

    result = score_gap(signals, weights)

    assert result.score == pytest.approx(10.0)
    assert result.breakdown["solution_density"] == pytest.approx(-1.0)

from evaluation.expert import evaluate_expert_scores
from evaluation.models import ExpertScore


def test_expert_scoring_aggregates_ratings_and_agreement():
    scores = [
        ExpertScore("gap-1", "rater-a", 5, 4, 5, 4),
        ExpertScore("gap-1", "rater-b", 4, 4, 4, 5),
        ExpertScore("gap-2", "rater-a", 3, 2, 4, 3),
    ]

    result = evaluate_expert_scores(scores, valid_threshold=4)

    assert result.total_ratings == 3
    assert result.gaps_rated == 2
    assert result.mean_novelty == 4.0
    assert result.mean_validity == 10 / 3
    assert result.high_validity_fraction == 2 / 3
    assert result.per_gap[0].rater_count == 2

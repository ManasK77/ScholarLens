"""The retrieval threshold policy, pinned.

`RETRIEVAL_MIN_SCORE` is a Qdrant **cosine** similarity compared with `>=`. It
stays 0.2: no better value is defensible on agent-authored labels
(PROJECT_STATUS.md, "3. Retrieval threshold, score semantics, calibration"). These tests keep the score
semantics from drifting, in particular the cross-encoder logit (unbounded, a
different scale) from ever being compared with the cosine threshold.
"""

import pytest

from app.core.config import Settings
from app.interactive.workflow import assess_sufficiency, qualifying_evidence

DEFAULTS = {name: Settings.model_fields[name].default for name in Settings.model_fields}
THRESHOLD = DEFAULTS["retrieval_min_score"]


def _hit(paper, score, **extra):
    return {
        "id": f"{paper}-{score}",
        "paper_id": paper,
        "text": f"passage {paper} {score}",
        "score": score,
        **extra,
    }


def test_locked_policy_defaults():
    assert THRESHOLD == 0.2
    assert DEFAULTS["retrieval_min_chunks"] == 2
    assert DEFAULTS["retrieval_min_papers"] == 2
    assert DEFAULTS["retrieval_min_papers_paper_specific"] == 1


def test_threshold_is_inclusive_on_the_cosine():
    hits = [_hit(1, THRESHOLD), _hit(2, THRESHOLD)]

    assert assess_sufficiency(hits, THRESHOLD, 2, 2)["sufficient"]


def test_a_cosine_below_the_threshold_does_not_count():
    hits = [_hit(1, 0.19), _hit(2, 0.19)]

    assert not assess_sufficiency(hits, THRESHOLD, 2, 2)["sufficient"]


def test_the_rerank_score_is_never_compared_with_the_threshold():
    hits = [
        _hit(1, 0.05, rerank_score=9.0),
        _hit(2, 0.05, rerank_score=7.5),
    ]

    assert not assess_sufficiency(hits, THRESHOLD, 2, 2)["sufficient"]


@pytest.mark.parametrize("logit", [7.8, 2.5, -3.2, -9.0])
def test_a_cross_encoder_logit_in_the_score_field_is_not_a_cosine(logit):
    hits = [_hit(1, logit), _hit(2, logit)]

    result = assess_sufficiency(hits, THRESHOLD, 2, 2)

    assert result == {"sufficient": False, "relevant": 0, "papers": 0}


def test_graph_rows_with_their_fixed_score_never_count():
    graph = [
        _hit(1, 0.5, source_type="graph"),
        _hit(2, 0.5, source_type="graph"),
    ]

    assert not assess_sufficiency(graph, THRESHOLD, 2, 2)["sufficient"]


def test_generation_sees_exactly_the_passages_the_gate_counted():
    hits = [_hit(1, 0.9), _hit(2, 0.1)]

    kept = qualifying_evidence(hits, THRESHOLD)

    assert [item["paper_id"] for item in kept] == [1]

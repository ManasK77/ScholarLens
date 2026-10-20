"""A report's "conflicting evidence" is verified, not taken from the LLM (D-09-13).

The live report for cluster 3 had `conflicting_evidence.supported = true`,
citing two real passages from two papers: one reporting a detector's >96%
detection rate, the other cross-lingual injections raising an LLM judge's
false-positive rate -- different systems, tasks and metrics. DeBERTa-MNLI
scores that pair 0.0066 / 0.0047 for contradiction. Citing two real passages
is not the same as those passages disagreeing.

Every claim now records its `basis`, so a reader can tell an LLM judgment
(with citations checked to be real) from a verified finding.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.clustering.schema import GapStatement, StatementCluster
from app.clustering.storage import replace_clusters
from app.explanation.service import generate_top_reports
from app.explanation.storage import GapReport, list_reports
from app.explanation.verification import (
    annotate_justification,
    conflict_verification,
    verify_stored_reports,
)
from app.ingestion import corpus
from app.ranking.signals import GapSignals
from app.ranking.storage import replace_rankings
from tests.explanation.test_service import SETTINGS, FakeIndex, FakeLLM, _point

A = {"id": "a", "paper_id": 1, "text": "Defense X stops every attack."}
B = {"id": "b", "paper_id": 2, "text": "Defense X stops no attack."}
A2 = {"id": "a2", "paper_id": 1, "text": "Defense X is fast."}


def _claim(supported, ids=(), statement="s"):
    return {"supported": supported, "statement": statement, "evidence_ids": list(ids)}


def _justification(conflict):
    return {
        "problem_evidence": _claim(True, ["a"]),
        "solution_insufficiency_evidence": _claim(False),
        "research_gap_evidence": _claim(True, ["b"]),
        "conflicting_evidence": conflict,
    }


class Scorer:
    """Returns P(contradiction) per ordered pair from a fixed table."""

    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, pairs):
        self.calls.append(list(pairs))
        return [self.table.get((left, right), 0.0) for left, right in pairs]


def test_a_conflict_needs_both_nli_directions_over_the_threshold():
    both = Scorer({(A["text"], B["text"]): 0.95, (B["text"], A["text"]): 0.9})
    one_way = Scorer({(A["text"], B["text"]): 0.95, (B["text"], A["text"]): 0.1})
    assert conflict_verification([A, B], both, 0.7)["verified"] is True
    result = conflict_verification([A, B], one_way, 0.7)
    assert result["verified"] is False
    assert result == {
        "method": "nli_both_directions",
        "threshold": 0.7,
        "pairs_checked": 1,
        "max_score": 0.1,
        "verified": False,
    }


def test_passages_from_one_paper_cannot_establish_a_cross_paper_conflict():
    scorer = Scorer({})
    result = conflict_verification([A, A2], scorer, 0.7)
    assert result["verified"] is False and result["pairs_checked"] == 0
    assert scorer.calls == []


def test_an_unverified_llm_conflict_claim_is_kept_but_not_presented_as_supported():
    justification = annotate_justification(
        _justification(_claim(True, ["a", "b"], "Studies disagree.")),
        [A, B],
        lambda cited: conflict_verification(cited, Scorer({}), 0.7),
    )
    conflict = justification["conflicting_evidence"]
    assert conflict["supported"] is False
    assert conflict["llm_supported"] is True
    assert conflict["basis"] == "llm_unverified"
    assert conflict["statement"] == "Studies disagree."  # the LLM's words, kept
    assert conflict["verification"]["verified"] is False
    # The other claims say what they are: LLM judgments over real passages.
    assert justification["problem_evidence"]["basis"] == "llm_cited"
    assert justification["solution_insufficiency_evidence"]["basis"] == "not_supported"


def test_a_verified_conflict_is_labelled_as_such():
    scorer = Scorer({(A["text"], B["text"]): 0.95, (B["text"], A["text"]): 0.9})
    justification = annotate_justification(
        _justification(_claim(True, ["a", "b"])),
        [A, B],
        lambda cited: conflict_verification(cited, scorer, 0.7),
    )
    conflict = justification["conflicting_evidence"]
    assert (conflict["supported"], conflict["basis"]) == (True, "nli_verified")


def test_no_conflict_claimed_needs_no_model():
    def verifier(cited):  # pragma: no cover - must not be called
        raise AssertionError("verifier called without a conflict claim")

    justification = annotate_justification(
        _justification(_claim(False)), [A, B], verifier
    )
    assert justification["conflicting_evidence"]["basis"] == "not_claimed"
    assert justification["conflicting_evidence"]["llm_supported"] is False


def _session():
    engine = create_engine("sqlite://")
    corpus.Base.metadata.create_all(engine)
    return Session(engine)


def test_stored_reports_are_verified_in_place_once():
    with _session() as session:
        session.add(corpus.Paper(id=1, title="P1", source="arxiv"))
        [cluster_id] = replace_clusters(
            session,
            [
                StatementCluster(
                    label="x",
                    members=[GapStatement("p", 1, "t", 2024, "limitation", (0.1,))],
                )
            ],
        )
        session.add(
            GapReport(
                cluster_id=cluster_id,
                gap="g",
                evidence=[A, B],
                confidence=0.6,
                suggested_directions=["d"],
                provider="groq",
                model="m",
                justification=_justification(_claim(True, ["a", "b"])),
            )
        )
        session.flush()
        calls = []

        def verifier(cited):
            calls.append(cited)
            return conflict_verification(cited, Scorer({}), 0.7)

        assert verify_stored_reports(session, verifier) == [cluster_id]
        assert verify_stored_reports(session, verifier) == []  # idempotent
        [row] = list_reports(session)
        assert row.justification["conflicting_evidence"]["basis"] == "llm_unverified"
        assert row.justification["conflicting_evidence"]["supported"] is False
        assert len(calls) == 1


def test_generated_reports_verify_conflict_claims_before_storing():
    with _session() as session:
        for paper_id in (101, 102):
            session.add(
                corpus.Paper(
                    id=paper_id, title=f"P{paper_id}", year=2023, source="arxiv"
                )
            )
        [cluster_id] = replace_clusters(
            session,
            [
                StatementCluster(
                    label="defense",
                    members=[
                        GapStatement(
                            "m1",
                            101,
                            "Defense fails on Tamil.",
                            2023,
                            "limitation",
                            (0.1,),
                        ),
                        # Second label of the same sentence: one statement.
                        GapStatement(
                            "m2",
                            101,
                            "Defense fails on Tamil.",
                            2023,
                            "future_work",
                            (0.1,),
                        ),
                        GapStatement(
                            "m3",
                            102,
                            "Defense fails on Hindi.",
                            2023,
                            "limitation",
                            (0.1,),
                        ),
                    ],
                )
            ],
        )
        replace_rankings(
            session, [(cluster_id, 1.0, GapSignals(2, 0.0, 0.0, 0.0, 0.0))]
        )
        index = FakeIndex(
            points=[
                _point("a0", 101, "Defense X stops every attack."),
                _point("b0", 102, "Defense X stops no attack."),
            ]
        )
        llm = FakeLLM(
            [
                {
                    "gap": "g",
                    "confidence": 0.5,
                    "suggested_directions": ["d"],
                    "evidence_ids": ["a0", "b0"],
                    "conflicting_evidence": _claim(
                        True, ["a0", "b0"], "They disagree."
                    ),
                }
            ]
        )

        generate_top_reports(
            session,
            SETTINGS,
            index,
            llm,
            verifier=lambda cited: conflict_verification(cited, Scorer({}), 0.7),
        )

        [row] = list_reports(session)
        assert row.justification["conflicting_evidence"]["basis"] == "llm_unverified"
        assert row.justification["conflicting_evidence"]["supported"] is False
        # The retrieval query names each sentence once, not once per label.
        assert index.calls[0]["query"].count("Defense fails on Tamil.") == 1

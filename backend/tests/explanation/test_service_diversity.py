"""Stage 9 end to end: diverse evidence in, honestly-scoped reports out.

These drive the real `generate_top_reports`, so they check that the retrieval
widening, the per-paper cap, the cluster-paper preference and the per-claim
grounding are actually wired together — not just that each piece works alone.

The scenario is the one from the corpus: a cluster whose papers are 101 and
102, and a vector store where one paper's passages occupy the whole top of the
ranking.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.explanation.selection import evidence_summary
from app.explanation.service import generate_top_reports
from app.explanation.storage import list_reports
from app.ingestion import corpus
from tests.explanation.test_service import (
    FakeIndex,
    FakeLLM,
    _point,
    _seed,
)

SETTINGS_CAPPED = type(
    "Settings",
    (),
    {
        "report_top_k": 5,
        "report_evidence_top_n": 4,
        "report_evidence_oversample": 4,
        "report_evidence_per_paper_cap": 2,
    },
)()


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    corpus.Base.metadata.create_all(engine)
    with Session(engine) as active:
        yield active


def _dominated_ranking() -> list[dict]:
    """One paper's passages first, the other's below the old cut-off.

    The real shape: paper 101 supplies the six best-scoring passages, so a
    top-4 retrieval used to return nothing but paper 101.
    """
    return [
        _point(f"a{index}", 101, f"paper 101 passage {index}") for index in range(6)
    ] + [
        _point("b0", 102, "paper 102 passage 0"),
        _point("b1", 102, "paper 102 passage 1"),
    ]


def _draft(evidence_ids, **overrides) -> dict:
    payload = {
        "gap": "Multilingual injection is underexplored.",
        "confidence": 0.8,
        "suggested_directions": ["Multilingual benchmark"],
        "evidence_ids": evidence_ids,
    }
    payload.update(overrides)
    return payload


class TestEvidenceDiversity:
    def test_one_paper_no_longer_supplies_the_whole_report(self, session):
        _seed(session)
        index = FakeIndex(points=_dominated_ranking())
        llm = FakeLLM([_draft(["a0", "a1", "b0", "b1"])])

        generate_top_reports(session, SETTINGS_CAPPED, index, llm)

        [row] = list_reports(session)
        summary = evidence_summary(row.evidence)
        assert summary["max_passages_from_one_paper"] <= 2
        assert summary["unique_paper_count"] == 2

    def test_retrieval_reaches_wider_than_the_report_needs(self, session):
        # Without the headroom the cap could only remove passages; paper 102
        # never appears in a top-4 of this ranking.
        _seed(session)
        index = FakeIndex(points=_dominated_ranking())

        generate_top_reports(session, SETTINGS_CAPPED, index, FakeLLM([_draft(["a0"])]))

        assert index.calls[0]["limit"] == 16

    def test_repeated_passages_from_one_paper_are_capped_before_the_prompt(
        self, session
    ):
        _seed(session)
        index = FakeIndex(points=_dominated_ranking())
        llm = FakeLLM([_draft(["a0"])])

        generate_top_reports(session, SETTINGS_CAPPED, index, llm)

        prompt = llm.prompts[0]
        assert "[a2]" not in prompt
        assert "[b0]" in prompt

    def test_the_prompt_states_papers_and_passages_separately(self, session):
        _seed(session)
        index = FakeIndex(points=_dominated_ranking())
        llm = FakeLLM([_draft(["a0"])])

        generate_top_reports(session, SETTINGS_CAPPED, index, llm)

        assert "4 passage(s) from 2 unique paper(s)" in llm.prompts[0]

    def test_the_api_summary_separates_papers_from_passages(self, session):
        _seed(session)
        index = FakeIndex(points=_dominated_ranking())
        llm = FakeLLM([_draft(["a0", "a1", "b0", "b1"])])

        generate_top_reports(session, SETTINGS_CAPPED, index, llm)

        [row] = list_reports(session)
        assert evidence_summary(row.evidence) == {
            "passage_count": 4,
            "unique_paper_count": 2,
            "max_passages_from_one_paper": 2,
        }


class TestConflictingEvidenceIsNotAsserted:
    def test_an_uncited_conflict_claim_is_stored_as_unsupported(self, session):
        # The exact failure: "inconsistent findings across studies", asserted
        # from a contradiction measurement with no opposing passage behind it.
        _seed(session)
        index = FakeIndex(points=_dominated_ranking())
        llm = FakeLLM(
            [
                _draft(
                    ["a0"],
                    conflicting_evidence={
                        "supported": True,
                        "statement": "Findings are inconsistent across studies.",
                        "evidence_ids": [],
                    },
                )
            ]
        )

        generate_top_reports(session, SETTINGS_CAPPED, index, llm)

        [row] = list_reports(session)
        assert row.justification["conflicting_evidence"]["supported"] is False

    def test_a_conflict_claim_citing_invented_ids_is_stored_as_unsupported(
        self, session
    ):
        _seed(session)
        index = FakeIndex(points=_dominated_ranking())
        llm = FakeLLM(
            [
                _draft(
                    ["a0"],
                    conflicting_evidence={
                        "supported": True,
                        "statement": "Two studies disagree.",
                        "evidence_ids": ["not-retrieved-1", "not-retrieved-2"],
                    },
                )
            ]
        )

        generate_top_reports(session, SETTINGS_CAPPED, index, llm)

        [row] = list_reports(session)
        claim = row.justification["conflicting_evidence"]
        assert claim["supported"] is False
        assert claim["evidence_ids"] == []

    def test_a_conflict_claim_citing_real_passages_survives(self, session):
        _seed(session)
        index = FakeIndex(points=_dominated_ranking())
        llm = FakeLLM(
            [
                _draft(
                    ["a0", "b0"],
                    conflicting_evidence={
                        "supported": True,
                        "statement": "Paper 101 and paper 102 report opposite "
                        "outcomes for the same defense.",
                        "evidence_ids": ["a0", "b0"],
                    },
                )
            ]
        )

        generate_top_reports(session, SETTINGS_CAPPED, index, llm)

        [row] = list_reports(session)
        claim = row.justification["conflicting_evidence"]
        assert claim["supported"] is True
        assert claim["evidence_ids"] == ["a0", "b0"]


class TestJustificationIsStored:
    def test_b_and_c_are_stored_as_unsupported_when_only_a_is_cited(self, session):
        _seed(session)
        index = FakeIndex(points=_dominated_ranking())
        llm = FakeLLM(
            [
                _draft(
                    ["a0"],
                    problem_evidence={
                        "supported": True,
                        "statement": "Injection attacks succeed often.",
                        "evidence_ids": ["a0"],
                    },
                    solution_insufficiency_evidence={
                        "supported": True,
                        "statement": "So defenses must be inadequate.",
                        "evidence_ids": [],
                    },
                    research_gap_evidence={
                        "supported": True,
                        "statement": "So this is an open gap.",
                        "evidence_ids": [],
                    },
                )
            ]
        )

        generate_top_reports(session, SETTINGS_CAPPED, index, llm)

        [row] = list_reports(session)
        assert row.justification["problem_evidence"]["supported"] is True
        assert (
            row.justification["solution_insufficiency_evidence"]["supported"] is False
        )
        assert row.justification["research_gap_evidence"]["supported"] is False

    def test_a_provider_that_omits_the_justification_stores_all_four_unsupported(
        self, session
    ):
        _seed(session)
        index = FakeIndex(points=_dominated_ranking())

        generate_top_reports(session, SETTINGS_CAPPED, index, FakeLLM([_draft(["a0"])]))

        [row] = list_reports(session)
        assert all(claim["supported"] is False for claim in row.justification.values())

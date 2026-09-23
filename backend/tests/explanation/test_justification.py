"""Stage 9's A/B/C justification and its grounding (D-09-05, D-09-06).

The report this fixes said: "Despite frequent prompt injection attacks, there
is a lack of effective, adaptive defenses and inconsistent findings across
studies, leaving LLMs vulnerable."

Three claims, one evidence base. The corpus supported the first — prompt
injection attacks are frequent and often succeed. It did not support the
second (no retrieved passage compared a defense against a baseline and found it
wanting) and it did not support the third at all. "Inconsistent findings across
studies" came from a pipeline measurement, `contradiction_flag=1.0`, which was
itself a false positive: there was no opposing passage anywhere in the evidence.

So the prompt now asks for the three separately, and the pipeline grounds each
one against the passages that were actually retrieved rather than trusting the
model's say-so.
"""

import pytest

from app.explanation.prompt import build_prompt
from app.explanation.schema import REPORT_SCHEMA, GroundedClaim, ReportDraft


def signals(**overrides):
    values = {
        "mention_frequency": 6,
        "trend_slope": 0.0,
        "solution_density": 0.0,
        "citation_impact": 0.67,
        "contradiction_flag": 0.0,
    }
    values.update(overrides)
    return type("Signals", (), values)()


def evidence(count: int = 2, paper_id: int = 5):
    return [
        {
            "id": f"e{index}",
            "paper_id": paper_id,
            "title": "Systematically Analyzing Prompt Injection Vulnerabilities",
            "text": f"passage {index}",
            "year": 2024,
            "source_type": "paragraph",
        }
        for index in range(count)
    ]


def draft(**overrides) -> dict:
    payload = {
        "gap": "Prompt injection defenses remain unevaluated across languages.",
        "confidence": 0.6,
        "suggested_directions": ["multilingual benchmark"],
        "evidence_ids": ["e0"],
        "problem_evidence": {
            "supported": True,
            "statement": "Injection attacks succeed often.",
            "evidence_ids": ["e0"],
        },
        "solution_insufficiency_evidence": {
            "supported": False,
            "statement": "No retrieved passage evaluates a defense.",
            "evidence_ids": [],
        },
        "research_gap_evidence": {
            "supported": False,
            "statement": "No passage names an unexplored direction.",
            "evidence_ids": [],
        },
        "conflicting_evidence": {
            "supported": False,
            "statement": "No opposing findings were retrieved.",
            "evidence_ids": [],
        },
    }
    payload.update(overrides)
    return payload


class TestGroundedClaim:
    def test_a_claim_asserted_without_citations_is_not_supported(self) -> None:
        # The exact failure: "inconsistent findings across studies" with nothing
        # behind it. A claim is supported when evidence says so, not when the
        # model says so.
        claim = GroundedClaim(supported=True, statement="Findings conflict.")

        assert claim.supported is False

    def test_a_cited_claim_stays_supported(self) -> None:
        claim = GroundedClaim(
            supported=True, statement="Findings conflict.", evidence_ids=["e0"]
        )

        assert claim.supported is True

    def test_an_unsupported_claim_may_still_explain_itself(self) -> None:
        claim = GroundedClaim(supported=False, statement="Nothing was retrieved.")

        assert claim.statement == "Nothing was retrieved."

    def test_a_missing_claim_defaults_to_unsupported(self) -> None:
        assert GroundedClaim().supported is False


class TestReportDraft:
    def test_the_three_claims_are_parsed_separately(self) -> None:
        parsed = ReportDraft.model_validate(draft())

        assert parsed.problem_evidence.supported is True
        assert parsed.solution_insufficiency_evidence.supported is False
        assert parsed.research_gap_evidence.supported is False

    def test_a_response_without_a_justification_is_conservative(self) -> None:
        # An older or lazy provider response must not read as "all three hold".
        bare = {
            "gap": "A gap.",
            "confidence": 0.5,
            "suggested_directions": ["x"],
            "evidence_ids": ["e0"],
        }

        parsed = ReportDraft.model_validate(bare)

        assert parsed.problem_evidence.supported is False
        assert parsed.solution_insufficiency_evidence.supported is False
        assert parsed.research_gap_evidence.supported is False
        assert parsed.conflicting_evidence.supported is False

    def test_b_and_c_do_not_follow_from_a(self) -> None:
        parsed = ReportDraft.model_validate(
            draft(
                solution_insufficiency_evidence={
                    "supported": True,
                    "statement": "Defenses must be inadequate because attacks "
                    "succeed.",
                    "evidence_ids": [],
                }
            )
        )

        assert parsed.solution_insufficiency_evidence.supported is False

    def test_the_justification_projects_to_a_storable_dict(self) -> None:
        stored = ReportDraft.model_validate(draft()).justification()

        assert set(stored) == {
            "problem_evidence",
            "solution_insufficiency_evidence",
            "research_gap_evidence",
            "conflicting_evidence",
        }
        assert stored["problem_evidence"]["supported"] is True
        assert stored["problem_evidence"]["evidence_ids"] == ["e0"]


class TestReportSchema:
    def test_the_provider_is_asked_for_all_four_claims(self) -> None:
        properties = REPORT_SCHEMA["properties"]

        for key in (
            "problem_evidence",
            "solution_insufficiency_evidence",
            "research_gap_evidence",
            "conflicting_evidence",
        ):
            assert key in properties
            assert key in REPORT_SCHEMA["required"]
            assert properties[key]["type"] == "object"

    def test_each_claim_asks_for_its_own_citations(self) -> None:
        claim = REPORT_SCHEMA["properties"]["research_gap_evidence"]

        assert set(claim["properties"]) == {
            "supported",
            "statement",
            "evidence_ids",
        }


class TestPrompt:
    def test_the_prompt_asks_for_the_three_kinds_of_evidence_separately(self) -> None:
        text = build_prompt("prompt attacks injection", signals(), evidence())

        assert "(A)" in text and "(B)" in text and "(C)" in text
        assert "problem_evidence" in text
        assert "solution_insufficiency_evidence" in text
        assert "research_gap_evidence" in text

    def test_the_prompt_forbids_inferring_b_or_c_from_a(self) -> None:
        text = build_prompt("prompt attacks injection", signals(), evidence())

        assert "does not by itself" in text

    def test_the_prompt_forbids_unevidenced_conflict_claims(self) -> None:
        text = build_prompt("prompt attacks injection", signals(), evidence())

        assert "inconsistent" in text.lower()
        assert "conflicting_evidence" in text

    def test_a_measured_contradiction_is_labelled_as_unconfirmed(self) -> None:
        # The measurement is a heuristic, and it was wrong for the gap that
        # motivated this. The model is told to treat the passages as the
        # authority, not the number.
        text = build_prompt(
            "prompt attacks injection", signals(contradiction_flag=1.0), evidence()
        )

        assert "heuristic" in text.lower()

    def test_the_prompt_reports_papers_and_passages_separately(self) -> None:
        text = build_prompt("t", signals(), evidence(count=7, paper_id=5))

        assert "7 passage" in text
        assert "1 unique paper" in text or "1 paper" in text

    @staticmethod
    def _evidence_note(text: str) -> str:
        """The EVIDENCE BASE line alone.

        Rule 7 mentions domination in every prompt, as an instruction; this is
        the line that says whether *this* evidence base is dominated.
        """
        return next(
            line for line in text.splitlines() if line.startswith("EVIDENCE BASE:")
        )

    def test_the_prompt_flags_evidence_dominated_by_one_paper(self) -> None:
        text = build_prompt("t", signals(), evidence(count=7, paper_id=5))

        assert "dominated" in self._evidence_note(text)

    def test_a_diverse_evidence_base_is_not_flagged_as_dominated(self) -> None:
        spread = evidence(count=1, paper_id=1) + evidence(count=1, paper_id=2)
        spread[1]["id"] = "e-other"

        text = build_prompt("t", signals(), spread)

        assert "dominated" not in self._evidence_note(text)
        assert "2 unique paper" in self._evidence_note(text)

    @pytest.mark.parametrize(
        "phrase",
        [
            "few unique papers",
            "lower",
        ],
    )
    def test_the_prompt_tells_the_model_when_to_lower_confidence(
        self, phrase: str
    ) -> None:
        text = build_prompt("t", signals(), evidence())

        assert phrase in text.lower()

    def test_the_prompt_is_deterministic(self) -> None:
        first = build_prompt("t", signals(), evidence())
        second = build_prompt("t", signals(), evidence())

        assert first == second

"""Stage 9's structured-output contract and evidence projection.

The LLM is the only untrusted producer in the pipeline, so everything it
returns is validated here before it can reach a database row or the dashboard.
`evidence_from_point` is the other half: it projects a Qdrant payload onto the
`Evidence` shape `frontend/src/types.ts` declares, and nothing else.
"""

import json

import pytest
from pydantic import ValidationError

from app.explanation.schema import (
    MAX_EVIDENCE_CHARS,
    REPORT_SCHEMA,
    GapReportRecord,
    ReportDraft,
    evidence_from_point,
)


def _point(**overrides) -> dict:
    point = {
        "id": "point-1",
        "score": 0.81,
        "paper_id": 101,
        "text": "Evaluation covers English prompts only.",
        "source_type": "limitation",
        "fact_id": 7,
        "year": 2023,
        "venue": "ACL",
        "section_type": "limitations",
        "graph_node_id": "paper:101",
    }
    point.update(overrides)
    return point


class TestReportDraft:
    def test_a_complete_draft_validates(self):
        draft = ReportDraft.model_validate(
            {
                "gap": "Multilingual prompt injection is underexplored.",
                "confidence": 0.8,
                "suggested_directions": ["Multilingual benchmark"],
                "evidence_ids": ["point-1"],
            }
        )

        assert draft.confidence == 0.8
        assert draft.evidence_ids == ["point-1"]

    @pytest.mark.parametrize("confidence", [-0.1, 1.1])
    def test_confidence_outside_zero_to_one_is_rejected(self, confidence):
        """`App.tsx` renders `Math.round(confidence * 100)` as a percentage."""
        with pytest.raises(ValidationError):
            ReportDraft.model_validate(
                {
                    "gap": "gap",
                    "confidence": confidence,
                    "suggested_directions": ["a"],
                    "evidence_ids": ["point-1"],
                }
            )

    def test_a_draft_citing_nothing_is_rejected(self):
        """An uncited report is an opinion, not retrieval-grounded evidence."""
        with pytest.raises(ValidationError):
            ReportDraft.model_validate(
                {
                    "gap": "gap",
                    "confidence": 0.5,
                    "suggested_directions": ["a"],
                    "evidence_ids": [],
                }
            )

    def test_a_blank_gap_statement_is_rejected(self):
        with pytest.raises(ValidationError):
            ReportDraft.model_validate(
                {
                    "gap": "   ",
                    "confidence": 0.5,
                    "suggested_directions": ["a"],
                    "evidence_ids": ["point-1"],
                }
            )

    def test_a_draft_suggesting_no_direction_is_rejected(self):
        with pytest.raises(ValidationError):
            ReportDraft.model_validate(
                {
                    "gap": "gap",
                    "confidence": 0.5,
                    "suggested_directions": [],
                    "evidence_ids": ["point-1"],
                }
            )

    def test_extra_keys_from_a_chatty_model_are_ignored(self):
        draft = ReportDraft.model_validate(
            {
                "gap": "gap",
                "confidence": 0.5,
                "suggested_directions": ["a"],
                "evidence_ids": ["point-1"],
                "reasoning": "some chain of thought the provider added",
            }
        )

        assert not hasattr(draft, "reasoning")


class TestReportSchema:
    def test_the_provider_schema_is_json_serializable(self):
        """It is posted verbatim as Groq's `response_format.json_schema.schema`."""
        assert json.loads(json.dumps(REPORT_SCHEMA)) == REPORT_SCHEMA

    def test_the_provider_schema_names_every_required_field(self):
        assert set(REPORT_SCHEMA["required"]) == {
            "gap",
            "confidence",
            "suggested_directions",
            "evidence_ids",
            # The A/B/C justification (D-09-05), asked for explicitly so the
            # provider judges the three claims rather than bundling them.
            "problem_evidence",
            "solution_insufficiency_evidence",
            "research_gap_evidence",
            "conflicting_evidence",
        }


class TestEvidenceFromPoint:
    def test_a_point_is_projected_onto_the_declared_evidence_shape(self):
        item = evidence_from_point(_point(), "Prompt injection at scale")

        # `frontend/src/types.ts` Evidence.
        assert item == {
            "id": "point-1",
            "paper_id": 101,
            "title": "Prompt injection at scale",
            "text": "Evaluation covers English prompts only.",
            "year": 2023,
            "source_type": "limitation",
        }

    def test_the_retrieval_score_and_graph_id_are_not_carried_into_storage(self):
        """Only the six fields the dashboard reads; the rest is retrieval detail."""
        item = evidence_from_point(_point(), None)

        assert "score" not in item
        assert "graph_node_id" not in item

    def test_an_unknown_title_is_stored_as_null(self):
        """`App.tsx` falls back to `Paper {paper_id}` when the title is missing."""
        assert evidence_from_point(_point(), None)["title"] is None

    def test_a_long_paragraph_is_clipped_for_context_hygiene(self):
        item = evidence_from_point(_point(text="x" * (MAX_EVIDENCE_CHARS + 500)), None)

        assert len(item["text"]) <= MAX_EVIDENCE_CHARS

    def test_a_point_without_a_paper_id_is_rejected(self):
        with pytest.raises(TypeError, match="paper_id"):
            evidence_from_point(_point(paper_id=None), None)

    def test_a_point_without_text_is_rejected(self):
        with pytest.raises(ValueError, match="text"):
            evidence_from_point(_point(text="  "), None)


class TestGapReportRecord:
    def _record(self, **overrides) -> GapReportRecord:
        fields = {
            "cluster_id": 3,
            "gap": "Multilingual prompt injection is underexplored.",
            "evidence": [evidence_from_point(_point(), None)],
            "confidence": 0.8,
            "suggested_directions": ["Multilingual benchmark"],
            "provider": "groq",
            "model": "groq-test",
        }
        fields.update(overrides)
        return GapReportRecord(**fields)

    def test_a_complete_record_is_accepted(self):
        assert self._record().cluster_id == 3

    def test_a_record_with_no_evidence_is_rejected(self):
        """GAPEXP-01: a stored report always references real retrieved evidence."""
        with pytest.raises(ValueError, match="evidence"):
            self._record(evidence=[])

    def test_a_record_with_an_out_of_range_confidence_is_rejected(self):
        with pytest.raises(ValueError, match="confidence"):
            self._record(confidence=1.4)

    def test_a_record_with_a_blank_gap_is_rejected(self):
        with pytest.raises(ValueError, match="gap"):
            self._record(gap="")

"""The single-pass prompt: retrieved evidence in, one JSON report out.

README §15 makes the grounding claim the phase's whole point, so the prompt is
tested for the properties that make grounding checkable — every evidence item
is present and addressable by its real id, the Stage 8 numbers the report has
to quote are present, and the instruction not to go beyond the evidence is
present.
"""

from types import SimpleNamespace

from app.explanation.prompt import build_prompt, retrieval_query


def _signals(**overrides):
    row = {
        "mention_frequency": 27,
        "trend_slope": 2.5,
        "solution_density": 0.11,
        "citation_impact": 43.0,
        "contradiction_flag": 1.0,
        "score": 61.4,
    }
    row.update(overrides)
    return SimpleNamespace(**row)


EVIDENCE = [
    {
        "id": "point-a",
        "paper_id": 101,
        "title": "Prompt injection at scale",
        "text": "Our evaluation covers English prompts only.",
        "year": 2023,
        "source_type": "limitation",
    },
    {
        "id": "point-b",
        "paper_id": 102,
        "title": None,
        "text": "Future work should extend the benchmark to low-resource languages.",
        "year": 2024,
        "source_type": "future_work",
    },
]


class TestRetrievalQuery:
    def test_the_query_combines_the_label_with_member_statements(self):
        query = retrieval_query(
            "multilingual injection",
            ["English prompts only.", "No low-resource coverage."],
        )

        assert "multilingual injection" in query
        assert "English prompts only." in query
        assert "No low-resource coverage." in query

    def test_an_unlabelled_cluster_still_produces_a_query(self):
        """`Cluster.label` is nullable by Phase 6's contract (D-06-06)."""
        assert "English prompts only." in retrieval_query(
            None, ["English prompts only."]
        )

    def test_the_query_is_bounded(self):
        """SPECTER2 truncates at 512 word pieces; an unbounded query is waste."""
        query = retrieval_query("label", ["sentence. " * 200] * 50)

        assert len(query) <= 4000

    def test_the_query_is_deterministic(self):
        members = ["b statement.", "a statement."]

        assert retrieval_query("label", members) == retrieval_query("label", members)


class TestBuildPrompt:
    def test_every_evidence_item_appears_with_its_real_id(self):
        prompt = build_prompt("multilingual injection", _signals(), EVIDENCE)

        for item in EVIDENCE:
            assert item["id"] in prompt
            assert item["text"] in prompt

    def test_the_stage_eight_signals_are_quoted_for_the_evidence_sentence(self):
        """README §8's example report reads '27 papers mention it · only 3 …'."""
        prompt = build_prompt("multilingual injection", _signals(), EVIDENCE)

        assert "27" in prompt
        assert "2.5" in prompt
        assert "0.11" in prompt

    def test_the_prompt_forbids_claims_beyond_the_retrieved_evidence(self):
        prompt = build_prompt("multilingual injection", _signals(), EVIDENCE).lower()

        assert "only" in prompt
        assert "evidence_ids" in prompt

    def test_the_cluster_label_is_included_when_present(self):
        assert "multilingual injection" in build_prompt(
            "multilingual injection", _signals(), EVIDENCE
        )

    def test_an_unlabelled_cluster_does_not_break_the_prompt(self):
        prompt = build_prompt(None, _signals(), EVIDENCE)

        assert EVIDENCE[0]["id"] in prompt

    def test_the_prompt_is_deterministic(self):
        assert build_prompt("label", _signals(), EVIDENCE) == build_prompt(
            "label", _signals(), EVIDENCE
        )

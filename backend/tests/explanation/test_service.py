"""Stage 9 orchestration — ranked clusters in, grounded reports out.

Two properties this phase lives or dies by:

* **Single pass.** CLAUDE.md hard constraint 3 forbids a loop around Stage 8→9,
  so exactly one retrieval and one LLM call happen per cluster, and a report
  that cannot be grounded is dropped rather than regenerated.
* **Nothing unsupported is stored.** Every evidence item on a stored report was
  returned by Qdrant *and* cited by the model. An id the model invented is
  discarded; a report whose citations are all invented is not stored at all.
"""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, delete, update
from sqlalchemy.orm import Session

from app.clustering.schema import GapStatement, StatementCluster
from app.clustering.storage import Cluster, ClusterMember, replace_clusters
from app.explanation.llm import LLMError, LLMUnavailableError
from app.explanation.service import generate_top_reports
from app.explanation.storage import list_reports
from app.ingestion import corpus
from app.ranking.signals import GapSignals
from app.ranking.storage import replace_rankings

SETTINGS = SimpleNamespace(report_top_k=5, report_evidence_top_n=4)


class FakeIndex:
    """Stands in for `EvidenceIndex`, recording exactly what was asked for."""

    def __init__(self, points_by_call=None, points=None):
        self.points_by_call = points_by_call
        self.points = points if points is not None else []
        self.calls = []

    def retrieve(self, query, limit=None, filters=None):
        self.calls.append({"query": query, "limit": limit, "filters": filters})
        if self.points_by_call is not None:
            return self.points_by_call[len(self.calls) - 1]
        return self.points


class FakeLLM:
    provider = "groq"
    model = "groq-test"

    def __init__(self, responses):
        self.responses = responses
        self.prompts = []

    def generate_json(self, prompt, schema):
        self.prompts.append(prompt)
        response = self.responses[min(len(self.prompts) - 1, len(self.responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return response


def _point(point_id: str, paper_id: int, text: str, year: int = 2023) -> dict:
    return {
        "id": point_id,
        "score": 0.9,
        "paper_id": paper_id,
        "text": text,
        "source_type": "limitation",
        "fact_id": None,
        "year": year,
        "venue": "ACL",
        "section_type": "limitations",
        "graph_node_id": f"paper:{paper_id}",
    }


def _draft(evidence_ids, gap="Multilingual injection is underexplored.") -> dict:
    return {
        "gap": gap,
        "confidence": 0.8,
        "suggested_directions": ["Multilingual benchmark"],
        "evidence_ids": evidence_ids,
    }


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    corpus.Base.metadata.create_all(engine)
    with Session(engine) as active:
        yield active


def _seed(session, labels=("multilingual",), scores=None) -> list[int]:
    """Store papers, clusters and rankings the way Phases 1, 6 and 8 leave them."""
    for paper_id in (101, 102):
        session.add(
            corpus.Paper(
                id=paper_id,
                title=f"Paper {paper_id} title",
                year=2023,
                source="arxiv",
            )
        )
    ids = replace_clusters(
        session,
        [
            StatementCluster(
                label=label,
                members=[
                    GapStatement.from_point(
                        {
                            "id": f"member-{index}-{paper_id}",
                            "paper_id": paper_id,
                            "text": f"{label} statement from {paper_id}.",
                            "year": 2023,
                            "source_type": "limitation",
                            "vector": [0.1, 0.2],
                        }
                    )
                    for paper_id in (101, 102)
                ],
            )
            for index, label in enumerate(labels)
        ],
    )
    scores = scores or [10.0 - index for index in range(len(ids))]
    replace_rankings(
        session,
        [
            (
                cluster_id,
                score,
                GapSignals(
                    mention_frequency=27,
                    trend_slope=2.5,
                    solution_density=0.11,
                    citation_impact=43.0,
                    contradiction_flag=1.0,
                ),
            )
            for cluster_id, score in zip(ids, scores, strict=True)
        ],
    )
    return ids


class TestHappyPath:
    def test_it_stores_a_grounded_report_for_the_ranked_cluster(self, session):
        [cluster_id] = _seed(session)
        index = FakeIndex(points=[_point("p1", 101, "English prompts only.")])
        llm = FakeLLM([_draft(["p1"])])

        assert generate_top_reports(session, SETTINGS, index, llm) == [cluster_id]

        [row] = list_reports(session)
        assert row.cluster_id == cluster_id
        assert row.gap == "Multilingual injection is underexplored."
        assert row.confidence == 0.8
        assert row.suggested_directions == ["Multilingual benchmark"]
        assert [item["id"] for item in row.evidence] == ["p1"]

    def test_the_stored_evidence_carries_the_real_paper_title(self, session):
        _seed(session)
        index = FakeIndex(points=[_point("p1", 101, "English prompts only.")])

        generate_top_reports(session, SETTINGS, index, FakeLLM([_draft(["p1"])]))

        [row] = list_reports(session)
        assert row.evidence[0]["title"] == "Paper 101 title"
        assert row.evidence[0]["text"] == "English prompts only."

    def test_the_provider_and_model_are_recorded(self, session):
        _seed(session)
        index = FakeIndex(points=[_point("p1", 101, "English prompts only.")])

        generate_top_reports(session, SETTINGS, index, FakeLLM([_draft(["p1"])]))

        [row] = list_reports(session)
        assert (row.provider, row.model) == ("groq", "groq-test")

    def test_reports_are_generated_best_gap_first(self, session):
        ids = _seed(session, labels=("weak", "strong"), scores=[1.0, 9.0])
        index = FakeIndex(points=[_point("p1", 101, "English prompts only.")])
        llm = FakeLLM([_draft(["p1"])])

        assert generate_top_reports(session, SETTINGS, index, llm) == [ids[1], ids[0]]
        assert [row.cluster_id for row in list_reports(session)] == [ids[1], ids[0]]


class TestSinglePass:
    def test_exactly_one_retrieval_and_one_generation_per_cluster(self, session):
        """CLAUDE.md hard constraint 3: no loop around Stage 8→9."""
        _seed(session, labels=("a", "b"))
        index = FakeIndex(points=[_point("p1", 101, "English prompts only.")])
        llm = FakeLLM([_draft(["p1"])])

        generate_top_reports(session, SETTINGS, index, llm)

        assert len(index.calls) == 2
        assert len(llm.prompts) == 2

    def test_a_grounding_failure_is_not_retried(self, session):
        _seed(session)
        index = FakeIndex(points=[_point("p1", 101, "English prompts only.")])
        llm = FakeLLM([_draft(["invented"])])

        assert generate_top_reports(session, SETTINGS, index, llm) == []
        assert len(llm.prompts) == 1


class TestRetrieval:
    def test_retrieval_is_capped_by_the_configured_evidence_size(self, session):
        _seed(session)
        index = FakeIndex(points=[_point("p1", 101, "English prompts only.")])

        generate_top_reports(session, SETTINGS, index, FakeLLM([_draft(["p1"])]))

        assert index.calls[0]["limit"] == SETTINGS.report_evidence_top_n

    def test_the_query_is_built_from_the_cluster_and_its_members(self, session):
        _seed(session)
        index = FakeIndex(points=[_point("p1", 101, "English prompts only.")])

        generate_top_reports(session, SETTINGS, index, FakeLLM([_draft(["p1"])]))

        query = index.calls[0]["query"]
        assert "multilingual" in query
        assert "multilingual statement from 101." in query

    def test_a_cluster_with_no_retrieved_evidence_is_skipped_without_an_llm_call(
        self, session
    ):
        """No evidence means no grounded report — and no wasted generation."""
        _seed(session)
        index = FakeIndex(points=[])
        llm = FakeLLM([_draft(["p1"])])

        assert generate_top_reports(session, SETTINGS, index, llm) == []
        assert llm.prompts == []

    def test_a_cluster_with_no_describable_content_is_not_retrieved_for(self, session):
        """An empty query returns arbitrary neighbours, not supporting evidence.

        Reachable when a ranking outlives its cluster — SQLite does not enforce
        the `ON DELETE CASCADE`, which is why every other table in Phases 6-8
        wipes explicitly rather than trusting it.
        """
        _seed(session)
        session.execute(delete(ClusterMember))
        session.execute(update(Cluster).values(label=None))
        index = FakeIndex(points=[_point("p1", 101, "English prompts only.")])
        llm = FakeLLM([_draft(["p1"])])

        assert generate_top_reports(session, SETTINGS, index, llm) == []
        assert index.calls == []
        assert llm.prompts == []

    def test_top_k_bounds_how_many_clusters_are_explained(self, session):
        ids = _seed(session, labels=("a", "b", "c"), scores=[9.0, 5.0, 1.0])
        index = FakeIndex(points=[_point("p1", 101, "English prompts only.")])
        settings = SimpleNamespace(report_top_k=2, report_evidence_top_n=4)

        stored = generate_top_reports(
            session, settings, index, FakeLLM([_draft(["p1"])])
        )

        assert stored == ids[:2]


class TestGrounding:
    def test_an_invented_evidence_id_is_discarded(self, session):
        _seed(session)
        index = FakeIndex(
            points=[
                _point("p1", 101, "English prompts only."),
                _point("p2", 102, "No low-resource coverage."),
            ]
        )
        llm = FakeLLM([_draft(["p1", "hallucinated-id"])])

        generate_top_reports(session, SETTINGS, index, llm)

        [row] = list_reports(session)
        assert [item["id"] for item in row.evidence] == ["p1"]

    def test_a_report_citing_only_invented_ids_is_not_stored(self, session):
        _seed(session)
        index = FakeIndex(points=[_point("p1", 101, "English prompts only.")])
        llm = FakeLLM([_draft(["hallucinated-id"])])

        assert generate_top_reports(session, SETTINGS, index, llm) == []
        assert list_reports(session) == []

    def test_uncited_retrieved_evidence_is_not_stored(self, session):
        """The report references what it used, not everything Qdrant returned."""
        _seed(session)
        index = FakeIndex(
            points=[
                _point("p1", 101, "English prompts only."),
                _point("p2", 102, "No low-resource coverage."),
            ]
        )

        generate_top_reports(session, SETTINGS, index, FakeLLM([_draft(["p2"])]))

        [row] = list_reports(session)
        assert [item["id"] for item in row.evidence] == ["p2"]

    def test_cited_evidence_keeps_retrieval_order(self, session):
        _seed(session)
        index = FakeIndex(
            points=[
                _point("p1", 101, "English prompts only."),
                _point("p2", 102, "No low-resource coverage."),
            ]
        )

        generate_top_reports(session, SETTINGS, index, FakeLLM([_draft(["p2", "p1"])]))

        [row] = list_reports(session)
        assert [item["id"] for item in row.evidence] == ["p1", "p2"]


class TestFailureHandling:
    def test_an_invalid_draft_skips_its_cluster_and_the_rest_still_run(self, session):
        ids = _seed(session, labels=("a", "b"), scores=[9.0, 1.0])
        index = FakeIndex(points=[_point("p1", 101, "English prompts only.")])
        llm = FakeLLM(
            [{"gap": "no confidence field", "evidence_ids": ["p1"]}, _draft(["p1"])]
        )

        assert generate_top_reports(session, SETTINGS, index, llm) == [ids[1]]

    def test_a_provider_schema_error_skips_its_cluster(self, session):
        ids = _seed(session, labels=("a", "b"), scores=[9.0, 1.0])
        index = FakeIndex(points=[_point("p1", 101, "English prompts only.")])
        llm = FakeLLM([LLMError("returned invalid structured JSON"), _draft(["p1"])])

        assert generate_top_reports(session, SETTINGS, index, llm) == [ids[1]]

    def test_an_offline_provider_aborts_and_leaves_the_previous_run_intact(
        self, session
    ):
        """One outage should not silently wipe every stored report."""
        [cluster_id] = _seed(session)
        index = FakeIndex(points=[_point("p1", 101, "English prompts only.")])
        generate_top_reports(session, SETTINGS, index, FakeLLM([_draft(["p1"])]))

        with pytest.raises(LLMUnavailableError):
            generate_top_reports(
                session,
                SETTINGS,
                index,
                FakeLLM([LLMUnavailableError("Groq is unavailable.")]),
            )

        [row] = list_reports(session)
        assert row.cluster_id == cluster_id

    def test_no_rankings_clears_the_stored_reports(self, session):
        _seed(session)
        index = FakeIndex(points=[_point("p1", 101, "English prompts only.")])
        generate_top_reports(session, SETTINGS, index, FakeLLM([_draft(["p1"])]))
        replace_rankings(session, [])

        assert generate_top_reports(session, SETTINGS, index, FakeLLM([])) == []
        assert list_reports(session) == []

    def test_a_report_for_a_paper_missing_from_the_corpus_still_grounds(self, session):
        """Qdrant can lag the corpus (Phase 6's `ClusterMember.paper_id` note)."""
        _seed(session)
        index = FakeIndex(points=[_point("p1", 999, "English prompts only.")])

        generate_top_reports(session, SETTINGS, index, FakeLLM([_draft(["p1"])]))

        [row] = list_reports(session)
        assert row.evidence[0]["title"] is None
        assert row.evidence[0]["paper_id"] == 999


class TestTransaction:
    def test_it_flushes_without_committing(self, session):
        _seed(session)
        index = FakeIndex(points=[_point("p1", 101, "English prompts only.")])

        generate_top_reports(session, SETTINGS, index, FakeLLM([_draft(["p1"])]))
        session.rollback()

        assert list_reports(session) == []

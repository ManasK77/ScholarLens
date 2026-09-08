"""Stage 8 against the frozen `/gaps` consumer contract (GAPRANK-02).

`app/api/routes_backend.py` survived the incomplete upstream push and already
calls this phase; it is not rewritten here. It still cannot be *imported* —
`app.explanation.service`/`storage` and `langgraph` are Phase 9/10 work — so
the contract is checked by parsing its source, which needs none of those, and
then exercising the projection it performs against real rows. This is the
technique Stage 7's `tests/trends/test_api_contract.py` established.

When Phase 10 makes that module importable, these tests should be replaced by a
`TestClient` call; until then this is what stops Stage 8 drifting away from the
handler that consumes it.
"""

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app
from app.clustering.schema import GapStatement, StatementCluster
from app.clustering.storage import replace_clusters
from app.ingestion import corpus
from app.ranking.service import rebuild_rankings
from app.ranking.storage import ClusterRanking, list_rankings
from app.trends.analysis import TrendSeries
from app.trends.storage import replace_trends

ROUTES = Path(app.__file__).parent / "api" / "routes_backend.py"
SIGNAL_FIELDS = {
    "mention_frequency",
    "trend_slope",
    "solution_density",
    "citation_impact",
    "contradiction_flag",
}
PROJECTED_FIELDS = {"cluster_id", "score"} | SIGNAL_FIELDS


def _handler(name: str) -> ast.FunctionDef:
    tree = ast.parse(ROUTES.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name!r} is no longer defined in {ROUTES.name}")


class TestFrozenHandlerShape:
    def test_the_get_handler_reads_only_fields_the_ranking_row_has(self):
        read = {
            node.attr
            for node in ast.walk(_handler("gaps"))
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "row"
        }

        assert read == PROJECTED_FIELDS
        assert read <= set(ClusterRanking.__mapper__.attrs.keys())

    def test_the_get_handler_emits_the_documented_signal_breakdown(self):
        """GAPRANK-02: the breakdown is the requirement, not a nicety."""
        dicts = [
            node for node in ast.walk(_handler("gaps")) if isinstance(node, ast.Dict)
        ]
        keys = [{key.value for key in node.keys} for node in dicts]

        assert SIGNAL_FIELDS in keys
        assert {"cluster_id", "score", "signals"} in keys

    def test_the_get_handler_passes_its_limit_through(self):
        [call] = [
            node
            for node in ast.walk(_handler("gaps"))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "list_rankings"
        ]

        assert [arg.id for arg in call.args] == ["session", "limit"]

    def test_the_post_handler_calls_rebuild_with_the_session_only(self):
        [call] = [
            node
            for node in ast.walk(_handler("rebuild_gaps"))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "rebuild_rankings"
        ]

        assert [arg.id for arg in call.args] == ["session"]
        assert call.keywords == []

    def test_the_pipeline_run_handler_passes_settings_positionally(self):
        """`rebuild_rankings(session, settings)` — settings is the 2nd parameter."""
        [call] = [
            node
            for node in ast.walk(_handler("run_pipeline"))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "rebuild_rankings"
        ]

        assert [arg.id for arg in call.args] == ["session", "settings"]
        assert call.keywords == []

    def test_the_pipeline_ranks_after_clustering_and_trends(self):
        """The signals read rows Phases 6 and 7 have to have written already."""
        order = [
            node.func.attr
            for node in ast.walk(_handler("run_pipeline"))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]

        assert order.index("rebuild_clusters") < order.index("rebuild_rankings")
        assert order.index("rebuild_trends") < order.index("rebuild_rankings")


class TestProjectedPayload:
    @pytest.fixture()
    def session(self):
        engine = create_engine("sqlite:///:memory:")
        corpus.Base.metadata.create_all(engine)
        with Session(engine) as active:
            yield active

    def test_the_payload_is_json_serializable_with_the_declared_types(self, session):
        # `frontend/src/types.ts` GapRanking: `score: number` and five
        # `number` signals, `contradiction_flag` included.
        for paper_id, year in ((101, 2021), (102, 2023), (103, 2023)):
            session.add(
                corpus.Paper(
                    id=paper_id,
                    title=f"Paper {paper_id}",
                    year=year,
                    source="arxiv",
                    citation_count=paper_id,
                )
            )
        ids = replace_clusters(
            session,
            [
                StatementCluster(
                    label="multilingual",
                    members=[
                        GapStatement.from_point(
                            {
                                "id": f"point-{paper_id}",
                                "paper_id": paper_id,
                                "text": "Evaluation covers English only.",
                                "year": year,
                                "source_type": "limitation",
                                "vector": [0.1, 0.2],
                            }
                        )
                        for paper_id, year in ((101, 2021), (102, 2023), (103, 2023))
                    ],
                )
            ],
        )
        replace_trends(session, [(ids[0], TrendSeries({"2021": 1, "2023": 2}, 0.5, 0))])
        rebuild_rankings(
            session,
            SimpleNamespace(gap_weights=lambda: (1.0, 3.0, 25.0, 0.02, 5.0)),
        )

        payload = [
            {
                "cluster_id": row.cluster_id,
                "score": row.score,
                "signals": {
                    "mention_frequency": row.mention_frequency,
                    "trend_slope": row.trend_slope,
                    "solution_density": row.solution_density,
                    "citation_impact": row.citation_impact,
                    "contradiction_flag": row.contradiction_flag,
                },
            }
            for row in list_rankings(session, 100)
        ]

        assert json.loads(json.dumps(payload)) == payload
        [gap] = payload
        assert isinstance(gap["cluster_id"], int)
        assert isinstance(gap["score"], float)
        assert gap["signals"]["mention_frequency"] == 3
        assert gap["signals"]["trend_slope"] == 0.5
        # `App.tsx` reads the flag for truthiness to show a "Conflicting
        # evidence" pill, so it must be a number, not a bool.
        assert gap["signals"]["contradiction_flag"] == 0.0
        assert not isinstance(gap["signals"]["contradiction_flag"], bool)

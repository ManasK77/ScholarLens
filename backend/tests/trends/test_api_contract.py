"""Stage 7 against the frozen `/trends` consumer contract (TREND-02).

`app/api/routes_backend.py` survived the incomplete upstream push and already
calls this phase; it is not rewritten here. It still cannot be *imported* —
`app.ranking`, `app.explanation.service`/`storage` and `langgraph` are Phase
8-10 work — so the contract is checked by parsing its source, which needs none
of those, and then exercising the projection it performs against real rows.

If Phase 8 ever makes that module importable, these tests should be replaced by
a `TestClient` call; until then this is what stops Stage 7 drifting away from
the handler that consumes it.
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
from app.trends.analysis import DEFAULT_MAX_YEAR, DEFAULT_MIN_YEAR
from app.trends.service import rebuild_trends
from app.trends.storage import ClusterTrend, list_trends

ROUTES = Path(app.__file__).parent / "api" / "routes_backend.py"
PROJECTED_FIELDS = {"cluster_id", "yearly_counts", "slope"}


def _handler(name: str) -> ast.FunctionDef:
    if name == "run_pipeline":
        name = "_execute_pipeline"
    tree = ast.parse(ROUTES.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name!r} is no longer defined in {ROUTES.name}")


class TestFrozenHandlerShape:
    def test_the_get_handler_reads_only_fields_the_trend_row_has(self):
        read = {
            node.attr
            for node in ast.walk(_handler("trends"))
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "row"
        }

        assert read == PROJECTED_FIELDS
        assert read <= set(ClusterTrend.__mapper__.attrs.keys())

    def test_the_get_handler_emits_the_documented_json_keys(self):
        [payload] = [
            node for node in ast.walk(_handler("trends")) if isinstance(node, ast.Dict)
        ]

        # Plus the additive `qualifiers` (D-07-08), derived from
        # `yearly_counts`; the read test above shows no other field is read.
        assert {key.value for key in payload.keys} == PROJECTED_FIELDS | {"qualifiers"}

    def test_the_post_handler_calls_rebuild_with_the_session_only(self):
        [call] = [
            node
            for node in ast.walk(_handler("rebuild_trends"))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "rebuild_trends"
        ]

        assert [arg.id for arg in call.args] == ["session"]
        assert call.keywords == []

    def test_the_pipeline_run_handler_also_rebuilds_trends(self):
        called = {
            node.func.attr
            for node in ast.walk(_handler("run_pipeline"))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }

        assert "rebuild_trends" in called


class TestProjectedPayload:
    @pytest.fixture()
    def session(self):
        engine = create_engine("sqlite:///:memory:")
        corpus.Base.metadata.create_all(engine)
        with Session(engine) as active:
            yield active

    def test_the_payload_is_json_serializable_with_the_declared_types(self, session):
        # `frontend/src/types.ts`: `yearly_counts: Record<string, number>`,
        # `slope: number`; `TrendChart.tsx` reads `yearly_counts[String(year)]`.
        replace_clusters(
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
        rebuild_trends(
            session,
            SimpleNamespace(
                trend_min_year=DEFAULT_MIN_YEAR, trend_max_year=DEFAULT_MAX_YEAR
            ),
        )

        payload = [
            {
                "cluster_id": row.cluster_id,
                "yearly_counts": row.yearly_counts,
                "slope": row.slope,
            }
            for row in list_trends(session)
        ]

        assert json.loads(json.dumps(payload)) == payload
        [trend] = payload
        assert isinstance(trend["cluster_id"], int)
        assert trend["yearly_counts"] == {"2021": 1, "2022": 0, "2023": 2}
        assert isinstance(trend["slope"], float)
        # The dashboard totals the series with `Object.values(...).reduce(...)`.
        assert sum(trend["yearly_counts"].values()) == 3

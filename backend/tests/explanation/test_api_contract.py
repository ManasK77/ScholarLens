"""Stage 9 against the frozen `/reports` consumer contract (GAPEXP-02).

`app/api/routes_backend.py` survived the incomplete upstream push and already
calls this phase; it is not rewritten here. It still cannot be *imported* —
`langgraph` is Phase 10 work — so the contract is checked by parsing its
source, which needs none of that, and then exercising the projection it
performs against real rows. This is the technique Stages 7 and 8 established.

When Phase 10 makes that module importable, these tests should be replaced by a
`TestClient` call; until then this is what stops Stage 9 drifting away from the
handler that consumes it.
"""

import ast
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app
from app.clustering.schema import GapStatement, StatementCluster
from app.clustering.storage import replace_clusters
from app.explanation.schema import GapReportRecord
from app.explanation.storage import GapReport, list_reports, replace_reports
from app.ingestion import corpus

ROUTES = Path(app.__file__).parent / "api" / "routes_backend.py"
PROJECTED_FIELDS = {
    "cluster_id",
    "gap",
    "evidence",
    "confidence",
    "suggested_directions",
    "provider",
    "model",
    # The grounded A/B/C justification (D-09-05). Served as `{}` when the row
    # predates the column, which is why the handler reads it through `or {}`.
    "justification",
}


def _handler(name: str) -> ast.FunctionDef:
    if name == "run_pipeline":
        name = "_execute_pipeline"
    tree = ast.parse(ROUTES.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name!r} is no longer defined in {ROUTES.name}")


class TestFrozenHandlerShape:
    def test_the_get_handler_reads_only_fields_the_report_row_has(self):
        read = {
            node.attr
            for node in ast.walk(_handler("reports"))
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "row"
        }

        assert read == PROJECTED_FIELDS
        assert read <= set(GapReport.__mapper__.attrs.keys())

    def test_the_get_handler_calls_list_reports_with_the_session_only(self):
        [call] = [
            node
            for node in ast.walk(_handler("reports"))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "list_reports"
        ]

        assert [arg.id for arg in call.args] == ["session"]
        assert call.keywords == []

    def test_the_post_handler_calls_generate_with_the_session_only(self):
        [call] = [
            node
            for node in ast.walk(_handler("generate_reports"))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "generate_top_reports"
        ]

        assert [arg.id for arg in call.args] == ["session"]
        assert call.keywords == []

    def test_the_pipeline_run_handler_passes_settings_positionally(self):
        """`generate_top_reports(session, settings)` — settings is 2nd."""
        [call] = [
            node
            for node in ast.walk(_handler("run_pipeline"))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "generate_top_reports"
        ]

        assert [arg.id for arg in call.args] == ["session", "settings"]
        assert call.keywords == []

    def test_the_pipeline_explains_after_indexing_and_ranking(self):
        """Stage 9 reads Stage 8's rows and retrieves against Stage 5's index."""
        # `index_all` is called by bare name, `generate_top_reports` through
        # its module, so both call shapes have to be collected.
        order = [
            node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            for node in ast.walk(_handler("run_pipeline"))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute | ast.Name)
        ]

        assert order.index("rebuild_rankings") < order.index("generate_top_reports")
        assert order.index("index_all") < order.index("generate_top_reports")


class TestProjectedPayload:
    @pytest.fixture()
    def session(self):
        engine = create_engine("sqlite:///:memory:")
        corpus.Base.metadata.create_all(engine)
        with Session(engine) as active:
            yield active

    def test_the_payload_is_json_serializable_with_the_declared_types(self, session):
        # `frontend/src/types.ts` GapReport: `confidence: number`,
        # `suggested_directions: string[]`, `evidence: Evidence[]`.
        [cluster_id] = replace_clusters(
            session,
            [
                StatementCluster(
                    label="multilingual",
                    members=[
                        GapStatement.from_point(
                            {
                                "id": "point-101",
                                "paper_id": 101,
                                "text": "Evaluation covers English only.",
                                "year": 2023,
                                "source_type": "limitation",
                                "vector": [0.1, 0.2],
                            }
                        )
                    ],
                )
            ],
        )
        replace_reports(
            session,
            [
                GapReportRecord(
                    cluster_id=cluster_id,
                    gap="Multilingual prompt injection is underexplored.",
                    evidence=[
                        {
                            "id": "point-101",
                            "paper_id": 101,
                            "title": "Prompt injection at scale",
                            "text": "Evaluation covers English only.",
                            "year": 2023,
                            "source_type": "limitation",
                        }
                    ],
                    confidence=0.8,
                    suggested_directions=["Multilingual benchmark"],
                    provider="groq",
                    model="groq-test",
                )
            ],
        )

        payload = [
            {
                "cluster_id": row.cluster_id,
                "gap": row.gap,
                "evidence": row.evidence,
                "confidence": row.confidence,
                "suggested_directions": row.suggested_directions,
                "provider": row.provider,
                "model": row.model,
            }
            for row in list_reports(session)
        ]

        assert json.loads(json.dumps(payload)) == payload
        [report] = payload
        assert isinstance(report["confidence"], float)
        assert 0.0 <= report["confidence"] <= 1.0
        assert isinstance(report["suggested_directions"], list)
        # `App.tsx` keys each blockquote on `item.id` and renders `item.text`.
        assert set(report["evidence"][0]) == {
            "id",
            "paper_id",
            "title",
            "text",
            "year",
            "source_type",
        }

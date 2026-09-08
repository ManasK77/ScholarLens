"""Regression coverage for the dashboard-facing API router registration."""

from app.main import app


def test_main_registers_dashboard_and_pipeline_routes() -> None:
    paths = set(app.openapi()["paths"])

    assert {
        "/papers",
        "/graph",
        "/clusters",
        "/trends",
        "/gaps",
        "/reports",
        "/query",
        "/pipeline/run",
    } <= paths

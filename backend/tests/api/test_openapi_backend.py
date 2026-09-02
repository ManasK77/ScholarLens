from app.main import app


def test_openapi_documents_complete_backend_surface_and_api_key():
    schema = app.openapi()
    expected = {
        "/collect",
        "/upload",
        "/papers",
        "/graph",
        "/clusters",
        "/trends",
        "/gaps",
        "/reports",
        "/query",
        "/pipeline/run",
    }

    assert expected <= set(schema["paths"])
    assert schema["components"]["securitySchemes"]["APIKeyHeader"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
    }

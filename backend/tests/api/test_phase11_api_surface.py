"""Phase 11 closeout: the mounted API surface, its authentication and limits.

These tests run against the real `app.main.app` — its routers, the
`require_api_key` dependency, the request-body limit middleware and the
generic error handler — rather than a hand-assembled FastAPI instance, so a
route added without the key, a middleware dropped from `main.py`, or a
handler that leaks an exception's text fails here.

Dummy credentials only. No test reaches a model, Groq, or a network service:
every request is either rejected before its handler runs or served from an
in-memory SQLite session.
"""

import asyncio
import logging

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as SASession
from sqlalchemy.pool import StaticPool

from app.api import body_limit, routes_backend
from app.api.security import API_KEY_HEADER_NAME, require_api_key
from app.core.config import Settings
from app.explanation.llm import LLMRateLimitError
from app.ingestion import corpus
from app.interactive.workflow import InteractiveServices
from app.main import app

VALID_KEY = "phase11-dummy-key-7c41e9"

REQUIRED_ENV = {
    "NEO4J_URI": "bolt://neo4j:7687",
    "NEO4J_USER": "neo4j",
    "NEO4J_PASSWORD": "pw",
    "QDRANT_URL": "http://qdrant:6333",
    "POSTGRES_HOST": "postgres",
    "POSTGRES_PORT": "5432",
    "POSTGRES_USER": "scholarlens",
    "POSTGRES_PASSWORD": "pw",
    "POSTGRES_DB": "scholarlens",
    "MAX_UPLOAD_SIZE_MB": "1",
    "UNPAYWALL_EMAIL": "you@example.com",
    "GROBID_URL": "http://grobid:8070",
}

# The complete intended surface (README §15 Phase 11 plus the collection audit
# trail, the pipeline driver and the admin reset). A route added, removed or
# registered twice changes this set and fails `test_route_inventory_*`.
EXPECTED_ROUTES = {
    ("GET", "/health"),
    ("POST", "/collect"),
    ("POST", "/upload"),
    ("GET", "/collections/{run_id}"),
    ("GET", "/collections/{run_id}/fulltext"),
    ("GET", "/collections/{run_id}/decisions"),
    ("POST", "/parse"),
    ("POST", "/extract"),
    ("GET", "/papers"),
    ("GET", "/graph"),
    ("POST", "/graph"),
    ("POST", "/index"),
    ("GET", "/clusters"),
    ("POST", "/clusters"),
    ("GET", "/trends"),
    ("POST", "/trends"),
    ("GET", "/gaps"),
    ("POST", "/gaps"),
    ("GET", "/reports"),
    ("POST", "/reports"),
    ("POST", "/query"),
    ("POST", "/pipeline/run"),
    ("DELETE", "/admin/corpus"),
}
PUBLIC_ROUTES = {("GET", "/health")}
# FastAPI's generated documentation. Public by design (README §12 points the
# owner at /docs); it describes the schema and serves no corpus data.
DOC_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def _walk(routes) -> list:
    """Every route, including those of included routers.

    FastAPI mounts an included router as one `_IncludedRouter` entry; walking
    its `original_router` reaches routes hidden from OpenAPI too, so an
    `include_in_schema=False` route cannot escape the inventory.
    """
    found = []
    for route in routes:
        original = getattr(route, "original_router", None)
        found.extend(_walk(original.routes) if original is not None else [route])
    return found


ALL_ROUTES = _walk(app.routes)


def _api_routes() -> list[tuple[str, str]]:
    return [
        (method, route.path)
        for route in ALL_ROUTES
        if isinstance(route, APIRoute)
        for method in sorted(route.methods)
    ]


PROTECTED_ROUTES = sorted(set(_api_routes()) - PUBLIC_ROUTES)


def _concrete(path: str) -> str:
    return path.replace("{run_id}", "1")


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PDF_STORAGE_PATH", str(tmp_path))
    monkeypatch.setenv("API_KEY", VALID_KEY)
    monkeypatch.delenv("MAX_REQUEST_BODY_KB", raising=False)
    monkeypatch.delenv("MAX_UPLOAD_FILES", raising=False)


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    corpus.Base.metadata.create_all(engine)
    with SASession(engine) as session:
        yield session


@pytest.fixture()
def client(db_session):
    """The real app, real auth; only the database is swapped for SQLite."""

    def _session():
        yield db_session

    app.dependency_overrides[corpus.get_session] = _session
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


# --------------------------------------------------------------------------
# Route inventory
# --------------------------------------------------------------------------


def test_route_inventory_matches_the_documented_surface() -> None:
    routes = _api_routes()

    assert len(routes) == len(set(routes)), "a route is registered twice"
    assert set(routes) == EXPECTED_ROUTES


def test_only_health_and_generated_docs_are_mounted_outside_the_api_routers() -> None:
    other_paths = {
        route.path for route in ALL_ROUTES if not isinstance(route, APIRoute)
    }

    assert other_paths <= DOC_PATHS


def test_openapi_documents_exactly_the_inventoried_operations(
    client: TestClient,
) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    documented = {
        (method.upper(), path) for path, ops in paths.items() for method in ops
    }

    assert documented == EXPECTED_ROUTES


# --------------------------------------------------------------------------
# Every protected operation, every credential failure mode
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), PROTECTED_ROUTES)
@pytest.mark.parametrize(
    "credentials",
    [
        pytest.param({}, id="missing"),
        pytest.param({"headers": {API_KEY_HEADER_NAME: ""}}, id="empty"),
        pytest.param({"headers": {API_KEY_HEADER_NAME: "wrong-key"}}, id="wrong"),
        pytest.param({"headers": {API_KEY_HEADER_NAME: f" {VALID_KEY}"}}, id="padded"),
        pytest.param({"headers": {API_KEY_HEADER_NAME: VALID_KEY[:-1]}}, id="prefix"),
        pytest.param(
            {"headers": {"Authorization": f"Bearer {VALID_KEY}"}}, id="bearer"
        ),
        pytest.param({"headers": {"X-API-Token": VALID_KEY}}, id="other-header"),
        pytest.param({"params": {"api_key": VALID_KEY}}, id="query-api_key"),
        pytest.param({"params": {"X-API-Key": VALID_KEY}}, id="query-header-name"),
        pytest.param({"cookies": {"X-API-Key": VALID_KEY}}, id="cookie"),
    ],
)
def test_every_protected_operation_rejects_bad_or_misplaced_credentials(
    client: TestClient, method: str, path: str, credentials: dict
) -> None:
    cookies = credentials.pop("cookies", None)
    if cookies:
        client.cookies.update(cookies)

    response = client.request(method, _concrete(path), **credentials)

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid or missing API key"}
    assert VALID_KEY not in response.text


@pytest.mark.parametrize(("method", "path"), PROTECTED_ROUTES)
def test_every_protected_operation_fails_closed_without_a_configured_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    monkeypatch.setenv("API_KEY", "   ")

    response = client.request(
        method, _concrete(path), headers={API_KEY_HEADER_NAME: "   "}
    )

    assert response.status_code == 500
    assert "not configured" in response.json()["detail"]


def test_the_env_example_placeholder_key_is_treated_as_unconfigured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Copying `.env.example` unchanged must not publish a known working key."""
    placeholder = "change_me_generate_a_random_value"
    monkeypatch.setenv("API_KEY", placeholder)

    response = client.get("/papers", headers={API_KEY_HEADER_NAME: placeholder})

    assert response.status_code == 500
    assert placeholder not in response.text


def test_a_conflicting_duplicate_key_header_is_rejected(client: TestClient) -> None:
    response = client.get(
        "/papers",
        headers=[(API_KEY_HEADER_NAME, VALID_KEY), (API_KEY_HEADER_NAME, "other")],
    )

    assert response.status_code == 401


def test_the_valid_key_is_accepted_under_any_header_casing(client: TestClient) -> None:
    for name in ("X-API-Key", "x-api-key", "X-API-KEY"):
        response = client.get("/papers", headers={name: VALID_KEY})
        assert response.status_code == 200, name


# --------------------------------------------------------------------------
# Public endpoints
# --------------------------------------------------------------------------


def test_health_is_public_and_exposes_only_service_booleans(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import routes_health

    monkeypatch.setattr(routes_health, "check_neo4j", lambda settings: True)
    monkeypatch.setattr(routes_health, "check_qdrant", lambda settings: False)
    monkeypatch.setattr(routes_health, "check_postgres", lambda settings: True)

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "services": {"neo4j": True, "qdrant": False, "postgres": True},
    }


def test_generated_docs_are_public_and_declare_the_key_on_every_operation(
    client: TestClient,
) -> None:
    schema = client.get("/openapi.json").json()

    for method, path in PROTECTED_ROUTES:
        assert schema["paths"][path][method.lower()].get("security"), (method, path)
    assert not schema["paths"]["/health"]["get"].get("security")
    assert VALID_KEY not in client.get("/openapi.json").text


# --------------------------------------------------------------------------
# CORS: the dashboard is same-origin (Vite proxy); no cross-origin grant
# --------------------------------------------------------------------------


def test_no_cross_origin_access_is_granted(client: TestClient) -> None:
    origin = {"Origin": "https://evil.example"}

    simple = client.get("/papers", headers={**origin, API_KEY_HEADER_NAME: VALID_KEY})
    preflight = client.options(
        "/query",
        headers={
            **origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-api-key, content-type",
        },
    )

    assert "access-control-allow-origin" not in simple.headers
    assert "access-control-allow-origin" not in preflight.headers
    assert preflight.status_code != 200


# --------------------------------------------------------------------------
# Request-body limits run before the body is read
# --------------------------------------------------------------------------


def test_an_oversized_json_body_is_refused_before_authentication(
    client: TestClient,
) -> None:
    response = client.post(
        "/query",
        content=b"{" + b" " * (300 * 1024) + b"}",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}


def test_an_oversized_streamed_body_without_content_length_is_refused(
    client: TestClient,
) -> None:
    def chunks():
        for _ in range(400):
            yield b" " * 1024

    response = client.post(
        "/collect",
        content=chunks(),
        headers={"Content-Type": "application/json", API_KEY_HEADER_NAME: VALID_KEY},
    )

    assert response.status_code == 413


def test_the_body_limit_is_configurable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_REQUEST_BODY_KB", "1")

    response = client.post(
        "/query",
        json={"question": "x" * 1500},
        headers={API_KEY_HEADER_NAME: VALID_KEY},
    )

    assert response.status_code == 413


def _run_middleware(path: str, headers: list[tuple[bytes, bytes]], root_path: str = ""):
    """Drive the middleware directly and record whether the body was read."""
    reads: list[int] = []
    sent: list[dict] = []
    reached: list[bool] = []

    async def inner(scope, receive, send):
        reached.append(True)

    async def receive():
        reads.append(1)
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "root_path": root_path,
        "headers": headers,
    }
    asyncio.run(body_limit.RequestBodyLimitMiddleware(inner)(scope, receive, send))
    return reads, sent, reached


def test_an_upload_without_the_key_is_refused_before_its_body_is_read() -> None:
    """Multipart is parsed before FastAPI dependencies run; an anonymous caller
    must not be able to make the server spool an upload to disk first."""
    reads, sent, reached = _run_middleware(
        "/upload",
        [
            (b"content-type", b"multipart/form-data; boundary=x"),
            (b"content-length", b"500000"),
        ],
    )

    assert reads == []
    assert reached == []
    assert sent[0]["status"] == 401


def test_an_upload_is_recognised_behind_a_root_path_prefix() -> None:
    _reads, sent, reached = _run_middleware(
        "/scholarlens/upload",
        [(b"content-length", b"500000")],
        root_path="/scholarlens",
    )

    assert reached == []
    assert sent[0]["status"] == 401


def test_an_upload_with_the_key_reaches_the_application() -> None:
    _reads, sent, reached = _run_middleware(
        "/upload",
        [
            (b"content-type", b"multipart/form-data; boundary=x"),
            (b"content-length", b"500000"),
            (b"x-api-key", VALID_KEY.encode()),
        ],
    )

    assert reached == [True]
    assert sent == []


@pytest.mark.parametrize("declared", [b"not-a-number", b"-1", b"\xb2", b"1e3"])
def test_a_malformed_content_length_is_rejected(declared: bytes) -> None:
    _reads, sent, reached = _run_middleware("/query", [(b"content-length", declared)])

    assert reached == []
    assert sent[0]["status"] == 400


def test_upload_rejects_more_files_than_the_configured_maximum(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_UPLOAD_FILES", "2")
    files = [
        ("files", (f"p{i}.pdf", b"%PDF-1.4 fake", "application/pdf")) for i in range(3)
    ]

    response = client.post(
        "/upload", files=files, headers={API_KEY_HEADER_NAME: VALID_KEY}
    )

    assert response.status_code == 413
    assert "at most 2" in response.json()["detail"]


# --------------------------------------------------------------------------
# Request and response contracts
# --------------------------------------------------------------------------


@pytest.mark.parametrize("topic", ["", "   ", "x" * 501])
def test_collect_rejects_blank_or_oversized_topics(
    client: TestClient, topic: str
) -> None:
    response = client.post(
        "/collect", json={"topic": topic}, headers={API_KEY_HEADER_NAME: VALID_KEY}
    )

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/papers", {"limit": 0}),
        ("/papers", {"limit": 501}),
        ("/papers", {"offset": -1}),
        ("/papers", {"year": "not-a-year"}),
        ("/gaps", {"limit": 0}),
        ("/graph", {"limit": 2001}),
        ("/collections/not-an-int", {}),
    ],
)
def test_out_of_range_or_mistyped_parameters_return_a_validation_error(
    client: TestClient, path: str, params: dict
) -> None:
    response = client.get(path, params=params, headers={API_KEY_HEADER_NAME: VALID_KEY})

    assert response.status_code == 422
    assert all(
        {"loc", "msg", "type"} <= set(item) for item in response.json()["detail"]
    )


def test_empty_collections_serialize_as_empty_lists(client: TestClient) -> None:
    headers = {API_KEY_HEADER_NAME: VALID_KEY}

    for path in ("/papers", "/trends", "/gaps", "/clusters"):
        response = client.get(path, headers=headers)
        assert response.status_code == 200, path
        assert response.json() == [], path


def test_an_unknown_collection_run_is_a_404(client: TestClient) -> None:
    response = client.get("/collections/999", headers={API_KEY_HEADER_NAME: VALID_KEY})

    assert response.status_code == 404
    assert response.json() == {"detail": "Collection run not found"}


def test_read_endpoints_publish_typed_response_schemas(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]

    for path in ("/papers", "/trends", "/gaps", "/query"):
        operation = paths[path]["get" if path != "/query" else "post"]
        schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert schema != {}, path


def test_papers_response_keeps_every_field_the_dashboard_reads(
    client: TestClient, db_session
) -> None:
    db_session.add(
        corpus.Paper(
            title="A paper",
            authors=["A. Author"],
            year=2024,
            venue="ACL",
            doi="10.1/x",
            abstract="Abstract.",
            source="arxiv",
            has_full_text=True,
            citation_count=3,
        )
    )
    db_session.commit()

    (paper,) = client.get("/papers", headers={API_KEY_HEADER_NAME: VALID_KEY}).json()

    assert set(paper) == {
        "id",
        "title",
        "authors",
        "year",
        "venue",
        "doi",
        "abstract",
        "source",
        "has_full_text",
        "citation_count",
    }


# --------------------------------------------------------------------------
# Errors: sanitized, JSON, and never carrying a secret
# --------------------------------------------------------------------------


def test_an_unhandled_error_is_a_sanitized_json_500(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    def _broken_session():
        raise RuntimeError("postgresql://scholarlens:hunter2@postgres:5432/db")

    app.dependency_overrides[corpus.get_session] = _broken_session
    with caplog.at_level(logging.DEBUG):
        response = client.get("/papers", headers={API_KEY_HEADER_NAME: VALID_KEY})

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert "hunter2" not in response.text
    assert all(VALID_KEY not in record.getMessage() for record in caplog.records)


def test_query_rate_limiting_is_a_429_with_retry_after(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        routes_backend,
        "default_services",
        lambda settings: InteractiveServices(None, None, None),
    )

    def _metered(*args, **kwargs):
        raise LLMRateLimitError("Groq rate limit reached.", retry_after=17)

    monkeypatch.setattr(routes_backend, "run_query", _metered)

    response = client.post(
        "/query",
        json={"question": "What gaps exist?"},
        headers={API_KEY_HEADER_NAME: VALID_KEY},
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "17"
    assert "rate limit" in response.json()["detail"]


def test_settings_expose_the_new_limits_with_safe_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None)

    assert settings.max_request_body_kb == 256
    assert settings.max_upload_files == 20


def test_require_api_key_is_still_the_route_dependency() -> None:
    """The middleware is a pre-check for large bodies, not the auth boundary."""
    for route in ALL_ROUTES:
        if (
            isinstance(route, APIRoute)
            and (
                next(iter(route.methods)),
                route.path,
            )
            not in PUBLIC_ROUTES
        ):
            calls = {dep.call for dep in route.dependant.dependencies}
            assert require_api_key in calls, route.path


# --------------------------------------------------------------------------
# A rejected request has no side effects
# --------------------------------------------------------------------------


def _derived_marker(db_session) -> None:
    from app.core.pipeline import DerivedState

    db_session.add(DerivedState(name="s6-s8", input_fingerprint="f", run_id="r"))
    db_session.commit()


def _marker_kept(db_session) -> bool:
    from app.core.pipeline import DerivedState

    db_session.expire_all()
    return db_session.get(DerivedState, "s6-s8") is not None


@pytest.mark.parametrize(
    ("path", "request_kwargs"),
    [
        ("/collect", {"json": {"topic": "   "}}),
        ("/collect", {"json": {"not_topic": "x"}}),
        ("/upload", {"data": {"unrelated": "field"}}),
    ],
)
def test_an_invalid_write_request_does_not_invalidate_cached_derived_results(
    client: TestClient, db_session, path: str, request_kwargs: dict
) -> None:
    """A 422 must not clear S6-S9 markers: that would make the next pipeline
    run rebuild clusters and regenerate reports (Groq calls) for nothing."""
    _derived_marker(db_session)

    response = client.post(
        path, headers={API_KEY_HEADER_NAME: VALID_KEY}, **request_kwargs
    )

    assert response.status_code == 422
    assert _marker_kept(db_session)


def test_an_accepted_upload_still_invalidates_cached_derived_results(
    client: TestClient, db_session
) -> None:
    _derived_marker(db_session)

    response = client.post(
        "/upload",
        files={"files": ("paper.pdf", b"%PDF-1.4 fake content", "application/pdf")},
        headers={API_KEY_HEADER_NAME: VALID_KEY},
    )

    assert response.status_code == 200
    assert not _marker_kept(db_session)

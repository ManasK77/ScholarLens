"""Throttling repeated failed API-key attempts (Phase 11 hardening addendum).

Runs through the real `app.main.app`: the route dependency and the `/upload`
pre-check share one throttle. The clock is injected, so recovery after the
window is tested without sleeping. Fabricated keys only.
"""

import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as SASession
from sqlalchemy.pool import StaticPool

from app.api import security
from app.ingestion import corpus
from app.main import app

VALID_KEY = "throttle-dummy-key-91b0d4"
WRONG_KEY = "THROTTLE_DUMMY_WRONG_5e2c"
LIMIT = 3
WINDOW = 60

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


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(security.auth_throttle, "clock", fake)
    return fake


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PDF_STORAGE_PATH", str(tmp_path))
    monkeypatch.setenv("API_KEY", VALID_KEY)
    monkeypatch.setenv("AUTH_FAILURE_LIMIT", str(LIMIT))
    monkeypatch.setenv("AUTH_FAILURE_WINDOW_SECONDS", str(WINDOW))


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    corpus.Base.metadata.create_all(engine)
    session = SASession(engine)

    def _session():
        yield session

    app.dependency_overrides[corpus.get_session] = _session
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()
        session.close()


def _client_from(host: str):
    """A client whose requests arrive from `host` (TestClient's `client=`)."""
    return TestClient(app, raise_server_exceptions=False, client=(host, 50000))


def _fail(client: TestClient, headers=None):
    return client.get("/papers", headers=headers or {"X-API-Key": WRONG_KEY})


def test_failures_under_the_limit_keep_the_normal_401(client, clock) -> None:
    for _ in range(LIMIT):
        response = _fail(client)
        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid or missing API key"}


def test_failures_beyond_the_limit_get_a_429_with_retry_after(client, clock) -> None:
    for _ in range(LIMIT):
        _fail(client)

    response = _fail(client)

    assert response.status_code == 429
    assert response.json() == {
        "detail": "Too many failed authentication attempts. Retry later."
    }
    assert 1 <= int(response.headers["Retry-After"]) <= WINDOW


def test_throttled_requests_do_not_extend_the_lockout(client, clock) -> None:
    """Hammering while locked out, late in the window, must not push the
    lockout past one window after the failures that caused it."""
    started = clock.now
    for _ in range(LIMIT):
        _fail(client)
    clock.now = started + WINDOW - 5
    for _ in range(50):
        assert _fail(client).status_code == 429

    clock.now = started + WINDOW + 1

    assert _fail(client).status_code == 401


def test_a_valid_key_works_normally_while_not_throttled(client, clock) -> None:
    for _ in range(LIMIT - 1):
        _fail(client)

    for _ in range(10):
        assert (
            client.get("/papers", headers={"X-API-Key": VALID_KEY}).status_code == 200
        )


def test_successful_requests_never_count_toward_the_limit(client, clock) -> None:
    for _ in range(LIMIT * 5):
        assert (
            client.get("/papers", headers={"X-API-Key": VALID_KEY}).status_code == 200
        )

    assert _fail(client).status_code == 401


def test_a_throttled_source_gets_429_even_with_the_valid_key(client, clock) -> None:
    """Otherwise the throttle is an oracle: a correct guess would answer 200
    while every wrong one answers 429. The policy is documented; the lockout
    ends with the window."""
    for _ in range(LIMIT):
        _fail(client)

    assert client.get("/papers", headers={"X-API-Key": VALID_KEY}).status_code == 429
    clock.now += WINDOW + 1
    assert client.get("/papers", headers={"X-API-Key": VALID_KEY}).status_code == 200


def test_the_limit_is_a_sliding_window(client, clock) -> None:
    for _ in range(LIMIT):
        _fail(client)
        clock.now += WINDOW / LIMIT

    # The first failure has aged out; one more is allowed before the limit.
    assert _fail(client).status_code == 401
    assert _fail(client).status_code == 429


def test_retry_after_counts_down_to_the_oldest_failure_expiring(client, clock) -> None:
    for _ in range(LIMIT):
        _fail(client)
    clock.now += 45

    assert _fail(client).headers["Retry-After"] == str(WINDOW - 45)


def test_sources_are_throttled_independently(clock) -> None:
    attacker = _client_from("203.0.113.7")
    owner = _client_from("198.51.100.2")
    for _ in range(LIMIT):
        _fail(attacker)

    assert _fail(attacker).status_code == 429
    assert owner.get("/papers", headers={"X-API-Key": VALID_KEY}).status_code != 429
    assert _fail(owner).status_code == 401


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="missing"),
        pytest.param({"X-API-Key": ""}, id="empty"),
        pytest.param({"X-API-Key": f" {VALID_KEY}"}, id="padded"),
        pytest.param(
            [("X-API-Key", VALID_KEY), ("X-API-Key", WRONG_KEY)], id="duplicate"
        ),
        pytest.param({"Authorization": f"Bearer {VALID_KEY}"}, id="bearer"),
        pytest.param({"X-API-Key": "kéy".encode()}, id="non-ascii"),
    ],
)
def test_every_invalid_form_counts_toward_the_limit(client, clock, headers) -> None:
    for _ in range(LIMIT):
        assert _fail(client, headers=headers).status_code == 401

    assert _fail(client, headers=headers).status_code == 429


def test_query_string_credentials_count_and_cannot_bypass(client, clock) -> None:
    for _ in range(LIMIT):
        client.get("/papers", params={"api_key": VALID_KEY})

    assert client.get("/papers", params={"api_key": VALID_KEY}).status_code == 429


def test_the_upload_precheck_shares_the_same_throttle(client, clock) -> None:
    files = {"files": ("p.pdf", b"%PDF-1.4 x", "application/pdf")}
    for _ in range(LIMIT):
        assert client.post("/upload", files=files).status_code == 401

    assert client.post("/upload", files=files).status_code == 429
    assert _fail(client).status_code == 429


def test_unconfigured_server_errors_are_not_counted(client, clock, monkeypatch) -> None:
    monkeypatch.setenv("API_KEY", "")
    for _ in range(LIMIT * 3):
        assert _fail(client).status_code == 500

    monkeypatch.setenv("API_KEY", VALID_KEY)
    assert client.get("/papers", headers={"X-API-Key": VALID_KEY}).status_code == 200


def test_public_health_is_never_throttled(client, clock, monkeypatch) -> None:
    from app.api import routes_health

    for name in ("check_neo4j", "check_qdrant", "check_postgres"):
        monkeypatch.setattr(routes_health, name, lambda settings: True)
    for _ in range(LIMIT):
        _fail(client)

    assert client.get("/health").status_code == 200


def test_throttle_logging_and_responses_never_carry_the_credential(
    client, clock, caplog
) -> None:
    with caplog.at_level(logging.DEBUG):
        bodies = [_fail(client).text for _ in range(LIMIT + 2)]

    assert all(WRONG_KEY not in body and VALID_KEY not in body for body in bodies)
    assert WRONG_KEY not in caplog.text and VALID_KEY not in caplog.text
    throttled = [r for r in caplog.records if "throttled" in r.getMessage()]
    assert len(throttled) == 1  # logged once when engaged, not per request


def test_tracked_sources_are_bounded(clock) -> None:
    throttle = security.AuthFailureThrottle(clock=clock, max_sources=5)
    for index in range(50):
        throttle.record_failure(f"10.0.0.{index}", limit=LIMIT, window=WINDOW)

    assert throttle.tracked_sources() <= 5

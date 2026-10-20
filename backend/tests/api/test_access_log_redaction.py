"""Access and error logs carry no query string and no exception text.

The end-to-end test runs a real uvicorn server, configured with uvicorn's own
default `LOGGING_CONFIG` exactly as the Docker `CMD` does, on a loopback port
and serving the real `app.main.app`. It sends requests carrying a fabricated
secret in the query string, including one whose handler fails with that
secret in the exception text, and reads what uvicorn's loggers and the app's
logger actually emitted. Lifespan is off and the database dependency is
replaced, so nothing outside the process is touched.
"""

import logging
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest
import uvicorn
from uvicorn.config import LOGGING_CONFIG
from uvicorn.logging import AccessFormatter

from app.core.logging import AccessLogRedactionFilter, ExceptionTextRedactionFilter
from app.ingestion import corpus
from app.main import app

DUMMY = "DUMMY_LOG_SECRET_7f3a"
KEY = "access-log-dummy-key-3c9e"
UVICORN_LOGGERS = ("uvicorn", "uvicorn.access", "uvicorn.error", "app.main")

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
    "PDF_STORAGE_PATH": "/tmp/pdfs",
    "MAX_UPLOAD_SIZE_MB": "1",
    "UNPAYWALL_EMAIL": "you@example.com",
    "GROBID_URL": "http://grobid:8070",
    "API_KEY": KEY,
}


def _uvicorn_record(path: str, status: int = 200) -> logging.LogRecord:
    """A record with the exact shape uvicorn's h11/httptools protocols log."""
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:5000", "GET", path, "1.1", status),
        None,
    )


@pytest.mark.parametrize(
    "path",
    [
        f"/papers?api_key={DUMMY}",
        f"/papers?limit=5&X-API-Key={DUMMY}",
        f"/collections/3?token={DUMMY}#frag",
        f"/papers?{DUMMY}",
    ],
)
def test_the_filter_strips_the_query_string_and_keeps_the_rest(path: str) -> None:
    record = _uvicorn_record(path, 401)

    assert AccessLogRedactionFilter().filter(record) is True
    line = AccessFormatter(use_colors=False).format(record)

    assert DUMMY not in line and "?" not in line
    assert '"GET /' in line and " 401" in line and "127.0.0.1:5000" in line
    assert path.split("?")[0] in line


def test_a_preformatted_message_is_redacted_too() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        f'127.0.0.1:5000 - "GET /papers?api_key={DUMMY} HTTP/1.1" 401',
        None,
        None,
    )

    AccessLogRedactionFilter().filter(record)

    assert DUMMY not in record.getMessage()
    assert '"GET /papers HTTP/1.1" 401' in record.getMessage()


def test_exception_text_is_reduced_to_its_type() -> None:
    try:
        raise RuntimeError(f"postgresql://u:{DUMMY}@h/db")
    except RuntimeError as exc:
        record = logging.LogRecord(
            "uvicorn.error",
            logging.ERROR,
            __file__,
            1,
            "Exception in ASGI application\n",
            None,
            (type(exc), exc, exc.__traceback__),
        )

    ExceptionTextRedactionFilter().filter(record)
    line = logging.Formatter().format(record)

    assert line == "Exception in ASGI application error=RuntimeError"


def test_importing_the_app_installs_both_filters() -> None:
    access = logging.getLogger("uvicorn.access").filters
    errors = logging.getLogger("uvicorn.error").filters

    assert sum(isinstance(f, AccessLogRedactionFilter) for f in access) == 1
    assert sum(isinstance(f, ExceptionTextRedactionFilter) for f in errors) == 1


class _Capture(logging.Handler):
    def __init__(self, formatter: logging.Formatter) -> None:
        super().__init__()
        self.setFormatter(formatter)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get(url: str, headers: dict | None = None) -> int:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


@pytest.fixture()
def _isolated_logging():
    saved = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
            list(logging.getLogger(name).filters),
        )
        for name in UVICORN_LOGGERS
    }
    yield
    for name, (handlers, level, propagate, filters) in saved.items():
        logger = logging.getLogger(name)
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate
        logger.filters[:] = filters


def test_a_real_uvicorn_server_logs_no_query_string_or_exception_text(
    monkeypatch, _isolated_logging
) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)

    def _failing_session():
        raise RuntimeError(f"postgresql://scholarlens:{DUMMY}@postgres:5432/db")

    port = _free_port()
    # The Docker CMD's configuration: uvicorn's default dictConfig, applied
    # after the app module (and its filters) was imported.
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, lifespan="off", log_config=LOGGING_CONFIG
    )
    server = uvicorn.Server(config)
    access = _Capture(AccessFormatter(use_colors=False))
    errors = _Capture(logging.Formatter("%(name)s %(message)s"))
    logging.getLogger("uvicorn.access").addHandler(access)
    logging.getLogger("uvicorn.error").addHandler(errors)
    logging.getLogger("app.main").addHandler(errors)
    app.dependency_overrides[corpus.get_session] = _failing_session
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.025)
        assert server.started

        base = f"http://127.0.0.1:{port}"
        statuses = {
            "ok": _get(f"{base}/openapi.json?api_key={DUMMY}"),
            "unauthorized": _get(f"{base}/papers?api_key={DUMMY}&limit=1"),
            "not_found": _get(f"{base}/nope?token={DUMMY}"),
            "server_error": _get(f"{base}/papers?q={DUMMY}", {"X-API-Key": KEY}),
        }
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        app.dependency_overrides.clear()

    assert statuses == {
        "ok": 200,
        "unauthorized": 401,
        "not_found": 404,
        "server_error": 500,
    }
    access_log = "\n".join(access.lines)
    assert len(access.lines) == 4
    assert DUMMY not in access_log and "?" not in access_log
    assert '"GET /openapi.json HTTP/1.1" 200' in access_log
    assert '"GET /papers HTTP/1.1" 401' in access_log
    assert '"GET /nope HTTP/1.1" 404' in access_log
    assert '"GET /papers HTTP/1.1" 500' in access_log

    # The failure stays visible to an operator by path and type, nothing more.
    error_log = "\n".join(errors.lines)
    assert "app.main Unhandled error on GET /papers error=RuntimeError" in error_log
    assert "uvicorn.error Exception in ASGI application error=RuntimeError" in error_log
    assert DUMMY not in error_log and "postgresql://" not in error_log
    assert "Traceback" not in error_log

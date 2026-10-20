"""Small, count-only pipeline telemetry; no document text or credentials."""

import logging
import math
import re
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

run_id: ContextVar[str] = ContextVar("pipeline_run_id", default="-")


class RunFilter(logging.Filter):
    def filter(self, record):
        record.run_id = run_id.get()
        return True


ACCESS_LOGGER = "uvicorn.access"
# A path's query string in an already-formatted access line: everything from
# `?` up to the next space or quote.
_QUERY_IN_LINE = re.compile(r'(\s/[^\s?"]*)\?[^\s"]*')


def _strip_query(value):
    return value.split("?", 1)[0] if isinstance(value, str) and "?" in value else value


class AccessLogRedactionFilter(logging.Filter):
    """Drop the query string from every access-log record.

    uvicorn's h11 and httptools protocols both log
    `'%s - "%s %s HTTP/%s" %d'` with `(client, method, path+query, version,
    status)`. The query string is the one part a caller controls freely, and a
    misplaced credential (`?api_key=...`) would otherwise be written to disk.
    Every string argument is cut at `?`, so a reordering of uvicorn's
    arguments still cannot leak one; a pre-formatted line is redacted in place.
    Method, path, client address and status are kept.
    """

    def filter(self, record):
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(_strip_query(arg) for arg in record.args)
        elif not record.args and isinstance(record.msg, str):
            record.msg = _QUERY_IN_LINE.sub(r"\1", record.msg)
        return True


class ExceptionTextRedactionFilter(logging.Filter):
    """Log an exception's type, never its text or traceback.

    For uvicorn's `uvicorn.error` logger, which reports every exception an
    application lets escape ("Exception in ASGI application") with the full
    traceback. Exception text can carry a connection string or a host; the
    type is enough to find the failure, and the client already got a
    sanitized 500.
    """

    def filter(self, record):
        if record.exc_info and record.exc_info[0] is not None:
            record.msg = (
                f"{record.getMessage().rstrip()} error={record.exc_info[0].__name__}"
            )
            record.args = None
            record.exc_info = None
            record.exc_text = None
        return True


def _install(logger_name: str, filter_type: type) -> None:
    logger = logging.getLogger(logger_name)
    if not any(isinstance(f, filter_type) for f in logger.filters):
        logger.addFilter(filter_type())


def install_access_log_redaction() -> None:
    """Attach the redaction filters to uvicorn's loggers, once each.

    A logger filter (not a handler filter) runs before any handler, so it
    covers whatever handlers uvicorn's `dictConfig` installs; `dictConfig`
    replaces handlers but never removes a logger's existing filters.
    """
    _install(ACCESS_LOGGER, AccessLogRedactionFilter)
    _install("uvicorn.error", ExceptionTextRedactionFilter)


def configure_logging(level: str = "INFO") -> None:
    install_access_log_redaction()
    root = logging.getLogger()
    # Own one handler, leaving hosting-server and test handlers alone.
    handler = next(
        (h for h in root.handlers if getattr(h, "_scholarlens", False)), None
    )
    if handler is None:
        handler = logging.StreamHandler()
        handler._scholarlens = True
        handler.addFilter(RunFilter())
        root.addHandler(handler)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s run=%(run_id)s %(message)s"
        )
    )
    handler.setLevel(level)
    root.setLevel(level)
    for name in ("httpx", "httpcore", "neo4j", "transformers", "adapters"):
        logging.getLogger(name).setLevel(logging.WARNING)


@contextmanager
def stage_timer(logger, name):
    started = perf_counter()
    logger.info("%s started", name)
    try:
        yield
    except BaseException as exc:
        logger.error(
            "%s failed elapsed=%.3fs error=%s",
            name,
            perf_counter() - started,
            type(exc).__name__,
        )
        raise
    else:
        logger.info("%s completed elapsed=%.3fs", name, perf_counter() - started)


class ProgressLogger:
    def __init__(self, logger, name, total):
        self.logger, self.name, self.total = logger, name, total
        self.started = self.last_time = perf_counter()
        self.last_done = 0

    def update(self, done):
        now = perf_counter()
        if (
            done < self.total
            and done - self.last_done < max(1, math.ceil(self.total / 20))
            and now - self.last_time < 30
        ):
            return
        elapsed = max(now - self.started, 0.001)
        rate = done / elapsed
        self.logger.info(
            "%s progress=%d/%d rate=%.2f/s eta=%.1fs",
            self.name,
            done,
            self.total,
            rate,
            (self.total - done) / rate if rate else 0,
        )
        self.last_done, self.last_time = done, now

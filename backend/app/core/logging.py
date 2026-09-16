"""Small, count-only pipeline telemetry; no document text or credentials."""

import logging
import math
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter

run_id: ContextVar[str] = ContextVar('pipeline_run_id', default='-')


class RunFilter(logging.Filter):
    def filter(self, record):
        record.run_id = run_id.get()
        return True


def configure_logging(level: str = 'INFO') -> None:
    root = logging.getLogger()
    # Own one handler, leaving hosting-server and test handlers alone.
    handler = next((h for h in root.handlers if getattr(h, '_scholarlens', False)), None)
    if handler is None:
        handler = logging.StreamHandler()
        handler._scholarlens = True
        handler.addFilter(RunFilter())
        root.addHandler(handler)
    handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s %(name)s run=%(run_id)s %(message)s'
    ))
    handler.setLevel(level)
    root.setLevel(level)
    for name in ('httpx', 'httpcore', 'neo4j', 'transformers', 'adapters'):
        logging.getLogger(name).setLevel(logging.WARNING)


@contextmanager
def stage_timer(logger, name):
    started = perf_counter()
    logger.info('%s started', name)
    try:
        yield
    except BaseException as exc:
        logger.error('%s failed elapsed=%.3fs error=%s', name, perf_counter() - started, type(exc).__name__)
        raise
    else:
        logger.info('%s completed elapsed=%.3fs', name, perf_counter() - started)


class ProgressLogger:
    def __init__(self, logger, name, total):
        self.logger, self.name, self.total = logger, name, total
        self.started = self.last_time = perf_counter()
        self.last_done = 0

    def update(self, done):
        now = perf_counter()
        if done < self.total and done - self.last_done < max(1, math.ceil(self.total / 20)) and now - self.last_time < 30:
            return
        elapsed = max(now - self.started, 0.001)
        rate = done / elapsed
        self.logger.info('%s progress=%d/%d rate=%.2f/s eta=%.1fs', self.name, done, self.total, rate, (self.total - done) / rate if rate else 0)
        self.last_done, self.last_time = done, now

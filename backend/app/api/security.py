"""API-key authentication for the Phase 11 REST surface.

Routers that expose corpus data or trigger pipeline work depend on
`require_api_key`. Callers present the shared secret from `API_KEY` in an
`X-API-Key` header — the contract README §12 documents (`curl -H
"X-API-Key: ..."`) and `frontend/src/api.ts` already speaks.

`GET /health` is deliberately left unauthenticated: Docker Compose and any
orchestrator probe it without a credential, and it exposes only per-service
up/down booleans.

Two properties this module exists to guarantee:

* **Fail closed.** A server with no `API_KEY` configured rejects every
  authenticated request. The tempting `if configured and not match: reject`
  shape does the opposite — it silently opens the whole API the moment the
  variable goes missing.
* **Constant-time comparison.** `secrets.compare_digest` keeps the reject
  path from leaking how much of the key was correct via timing.
"""

import logging
import math
import secrets
import threading
import time
from collections import OrderedDict, deque

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

from app.core.config import Settings, get_settings

API_KEY_HEADER_NAME = "X-API-Key"

# auto_error=False so a missing header reaches this module and gets the same
# 401 and the same generic detail as a wrong one — FastAPI's built-in error
# would otherwise distinguish the two cases for an anonymous caller.
_api_key_header = APIKeyHeader(name=API_KEY_HEADER_NAME, auto_error=False)

_REJECTED_DETAIL = "Invalid or missing API key"
_THROTTLED_DETAIL = "Too many failed authentication attempts. Retry later."
logger = logging.getLogger(__name__)
_NOT_CONFIGURED_DETAIL = "API key authentication is not configured on this server"


# `.env.example` ships `API_KEY=change_me_...`. A deployment that copied the
# file unchanged would otherwise accept a key printed in the repository.
_PLACEHOLDER_PREFIX = "change_me"


def require_api_key(
    presented_key: str | None = Security(_api_key_header),
    settings: Settings = Depends(get_settings),  # noqa: B008 -- required FastAPI DI pattern
    request: Request = None,  # type: ignore[assignment] -- injected by FastAPI
) -> None:
    """Reject the request unless it carries the configured API key.

    Returns `None` on success so it can be used either as a route-level
    dependency or in an `APIRouter(dependencies=[...])` list. `presented_key`
    is what OpenAPI documents; `request` supplies every copy of the header
    (FastAPI always injects it; a direct call may omit it).
    """
    if request is not None:
        presented = request.headers.getlist(API_KEY_HEADER_NAME)
        source = request.client.host if request.client else None
    else:
        presented = [] if presented_key is None else [presented_key]
        source = None
    rejection = api_key_rejection(presented, settings, source)
    if rejection is not None:
        raise rejection


def api_key_rejection(
    presented_keys: list[str], settings: Settings, source: str | None = None
) -> HTTPException | None:
    """Why a request carrying `presented_keys` must be refused, or `None`.

    Shared by `require_api_key` and the body-limit middleware's up-front check
    on uploads, so the two can never disagree about what a valid key is.
    `presented_keys` is every `X-API-Key` header sent: more than one is
    refused outright rather than letting header order decide which counts.
    `source` is the client address the failed-attempt throttle counts by;
    `None` (a direct call with no request) is not throttled.
    """
    # Strip the *configured* value only: a `.env` line can pick up a trailing
    # CR or space and lock the owner out. The presented header is compared as
    # sent — trimming caller input would widen what counts as a match.
    configured_key = (settings.api_key or "").strip()
    if not configured_key or configured_key.startswith(_PLACEHOLDER_PREFIX):
        return HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_NOT_CONFIGURED_DETAIL,
        )

    limit, window = settings.auth_failure_limit, settings.auth_failure_window_seconds
    if source is not None:
        # Checked before the key is compared, and for a valid key too: a
        # throttled source that could still get a 200 would learn exactly
        # when it guessed right.
        retry_after = auth_throttle.retry_after(source, limit, window)
        if retry_after is not None:
            return HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=_THROTTLED_DETAIL,
                headers={"Retry-After": str(retry_after)},
            )

    if len(presented_keys) != 1 or not _matches(presented_keys[0], configured_key):
        if source is not None and auth_throttle.record_failure(source, limit, window):
            # Once per engagement, by source address only: never the key.
            logger.warning(
                "API key authentication throttled source=%s failures=%d window=%gs",
                source,
                limit,
                window,
            )
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_REJECTED_DETAIL,
        )
    return None


class AuthFailureThrottle:
    """Failed API-key attempts per source address, in a sliding window.

    A source with `limit` failures inside the last `window` seconds is refused
    (429) until its oldest failure ages out. Refused requests are not counted,
    so a lockout always ends one window after the failures that caused it,
    however hard the source keeps trying; successes are never counted.

    **Process-local.** The state lives in this process's memory: each uvicorn
    worker or container counts on its own, and a restart forgets everything.
    The source is the TCP peer address; behind a proxy (Docker's bridge, the
    Vite dev proxy) every client shares the proxy's address and so one budget.
    """

    def __init__(self, clock=time.monotonic, max_sources: int = 10_000) -> None:
        self.clock = clock
        self.max_sources = max_sources
        self._lock = threading.Lock()
        self._failures: OrderedDict[str, deque[float]] = OrderedDict()

    def _recent(self, source: str, window: float) -> deque[float] | None:
        failures = self._failures.get(source)
        if failures is None:
            return None
        cutoff = self.clock() - window
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            del self._failures[source]
            return None
        return failures

    def retry_after(self, source: str, limit: int, window: float) -> int | None:
        """Seconds until `source` may try again, or `None` if it may now."""
        with self._lock:
            failures = self._recent(source, window)
            if failures is None or len(failures) < limit:
                return None
            return max(1, math.ceil(failures[0] + window - self.clock()))

    def record_failure(self, source: str, limit: int, window: float) -> bool:
        """Count one failure; `True` when it is the one that engages the limit."""
        with self._lock:
            failures = self._recent(source, window)
            if failures is None:
                if len(self._failures) >= self.max_sources:
                    # Bounded memory: forget the least recently failing source.
                    self._failures.popitem(last=False)
                failures = self._failures[source] = deque()
            self._failures.move_to_end(source)
            failures.append(self.clock())
            return len(failures) == limit

    def tracked_sources(self) -> int:
        with self._lock:
            return len(self._failures)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()


auth_throttle = AuthFailureThrottle()


def _matches(presented_key: str, configured_key: str) -> bool:
    """Constant-time equality over the UTF-8 encodings of both keys.

    `secrets.compare_digest` raises TypeError on non-ASCII `str` operands, so
    comparing bytes keeps a caller from turning a 401 into a 500 by sending a
    non-ASCII header value.
    """
    return secrets.compare_digest(
        presented_key.encode("utf-8"), configured_key.encode("utf-8")
    )

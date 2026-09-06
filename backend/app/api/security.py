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

import secrets

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.core.config import Settings, get_settings

API_KEY_HEADER_NAME = "X-API-Key"

# auto_error=False so a missing header reaches this module and gets the same
# 401 and the same generic detail as a wrong one — FastAPI's built-in error
# would otherwise distinguish the two cases for an anonymous caller.
_api_key_header = APIKeyHeader(name=API_KEY_HEADER_NAME, auto_error=False)

_REJECTED_DETAIL = "Invalid or missing API key"
_NOT_CONFIGURED_DETAIL = "API key authentication is not configured on this server"


def require_api_key(
    presented_key: str | None = Security(_api_key_header),
    settings: Settings = Depends(get_settings),  # noqa: B008 -- required FastAPI DI pattern
) -> None:
    """Reject the request unless it carries the configured API key.

    Returns `None` on success so it can be used either as a route-level
    dependency or in an `APIRouter(dependencies=[...])` list.
    """
    # Strip the *configured* value only: a `.env` line can pick up a trailing
    # CR or space and lock the owner out. The presented header is compared as
    # sent — trimming caller input would widen what counts as a match.
    configured_key = (settings.api_key or "").strip()
    if not configured_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_NOT_CONFIGURED_DETAIL,
        )

    if presented_key is None or not _matches(presented_key, configured_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_REJECTED_DETAIL,
        )


def _matches(presented_key: str, configured_key: str) -> bool:
    """Constant-time equality over the UTF-8 encodings of both keys.

    `secrets.compare_digest` raises TypeError on non-ASCII `str` operands, so
    comparing bytes keeps a caller from turning a 401 into a 500 by sending a
    non-ASCII header value.
    """
    return secrets.compare_digest(
        presented_key.encode("utf-8"), configured_key.encode("utf-8")
    )

"""API-key authentication shared by every non-health endpoint."""

import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader

from app.core.config import get_settings

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_api_key(provided_key: str | None, configured_key: str) -> str:
    if not provided_key or not secrets.compare_digest(provided_key, configured_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
    return provided_key


def require_api_key(
    provided_key: str | None = Depends(api_key_header),
) -> str:
    return verify_api_key(provided_key, get_settings().api_key)

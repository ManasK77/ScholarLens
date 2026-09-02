import pytest
from fastapi import HTTPException

from app.api.security import verify_api_key


def test_verify_api_key_accepts_matching_key():
    assert verify_api_key("expected", configured_key="expected") == "expected"


def test_verify_api_key_rejects_missing_or_wrong_key():
    with pytest.raises(HTTPException) as exc_info:
        verify_api_key("wrong", configured_key="expected")
    assert exc_info.value.status_code == 401

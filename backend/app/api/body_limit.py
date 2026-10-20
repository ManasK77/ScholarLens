"""Bound request bodies before FastAPI reads them (Phase 11 security review).

FastAPI reads and parses a route's body — JSON into memory, multipart into
spooled temporary files — *before* it resolves the route's dependencies, so
`require_api_key` only runs once the whole body has been received. Without a
bound, an anonymous caller could make the server buffer an arbitrarily large
body just to be told 401. This middleware runs first and:

* refuses a declared `Content-Length` over the limit with 413, unread;
* counts a streamed (chunked) body and stops it at the limit with 413;
* checks the API key up front for `/upload`, the one route whose legitimate
  bodies are large, so an anonymous upload is refused before a byte of it is
  spooled. The check is `app.api.security.api_key_rejection`, the same one the
  route dependency applies; `require_api_key` remains the auth boundary for
  every route and still runs.

The limits come from settings: `MAX_REQUEST_BODY_KB` for everything except
`/upload`, and `MAX_UPLOAD_FILES * MAX_UPLOAD_SIZE_MB` (plus multipart framing)
for `/upload`.
"""

from fastapi import HTTPException
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.security import API_KEY_HEADER_NAME, api_key_rejection
from app.core.config import get_settings

UPLOAD_PATH = "/upload"
# Multipart boundaries, part headers and the form's own fields.
_MULTIPART_OVERHEAD_BYTES = 1024 * 1024
_TOO_LARGE_DETAIL = "Request body too large"


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        headers = Headers(scope=scope)
        # The route path, without any prefix a proxy mounted the app under.
        path = scope["path"].removeprefix(scope.get("root_path", ""))
        if path == UPLOAD_PATH:
            client = scope.get("client")
            rejection = api_key_rejection(
                headers.getlist(API_KEY_HEADER_NAME),
                settings,
                client[0] if client else None,
            )
            if rejection is not None:
                await _respond(
                    scope,
                    receive,
                    send,
                    rejection.status_code,
                    rejection.detail,
                    rejection.headers,
                )
                return
            limit = (
                settings.max_upload_files * settings.max_upload_size_mb * 1024 * 1024
                + _MULTIPART_OVERHEAD_BYTES
            )
        else:
            limit = settings.max_request_body_kb * 1024

        declared = headers.get("content-length")
        if declared is not None:
            # isascii(): `isdigit()` alone accepts e.g. "²", which int() rejects.
            if not (declared.isascii() and declared.isdigit()):
                await _respond(
                    scope, receive, send, 400, "Invalid Content-Length header"
                )
                return
            if int(declared) > limit:
                await _respond(scope, receive, send, 413, _TOO_LARGE_DETAIL)
                return

        received = 0

        async def bounded_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # FastAPI re-raises an HTTPException met while reading the
                    # body, so the caller gets the normal JSON 413.
                    raise HTTPException(status_code=413, detail=_TOO_LARGE_DETAIL)
            return message

        await self.app(scope, bounded_receive, send)


async def _respond(
    scope: Scope,
    receive: Receive,
    send: Send,
    status_code: int,
    detail: str,
    headers: dict[str, str] | None = None,
) -> None:
    await JSONResponse({"detail": detail}, status_code=status_code, headers=headers)(
        scope, receive, send
    )

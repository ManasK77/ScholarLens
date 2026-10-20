"""Validated PDF storage shared by every downloader (D-FULLTEXT-01).

A 200 is not proof of a PDF: an open-access "PDF" link is often a publisher
landing page, and storing that as full text hands Stage 2 an HTML document
and stops every later provider from trying the paper again. A body is
therefore written to a temporary file beside its destination and moved into
place only once it is known to be a PDF: under the size limit, not declared
as HTML/text, and carrying the ``%PDF-`` signature in its first 1 KiB (where
PDF readers accept it). A failed download never replaces or truncates a file
already stored at the destination.

`DownloadResult.status` is one of `DOWNLOAD_STATUSES`; `retryable` says
whether trying again later could plausibly succeed.
"""

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

PDF_MAGIC = b"%PDF-"
PDF_HEADER_WINDOW = 1024

DOWNLOAD_STATUSES = (
    "pdf_stored",  # validated and stored
    "http_error",  # the server answered with a non-200 status
    "rate_limited",  # 429, or a cooldown from an earlier refusal
    "timeout",
    "network_error",  # DNS, connection or TLS failure
    "not_pdf",  # the server declared HTML/text/JSON, or sent no PDF signature
    "too_large",  # over the configured size limit; nothing stored
    "file_validation_failed",  # declared a PDF but the body is not one
    "blocked_url",  # refused before any request (scheme, host or address)
    "storage_error",  # the file could not be written
)

# Declared types that are never a PDF body.
_NOT_PDF_TYPES = ("text/", "html", "xml", "json", "javascript")
# Declared types under which a PDF body is plausible.
_PDF_TYPES = (
    "application/pdf",
    "application/x-pdf",
    "application/octet-stream",
    "binary/octet-stream",
    "application/download",
    "application/force-download",
)


@dataclass(frozen=True)
class DownloadResult:
    status: str
    http_status: int | None = None
    bytes_stored: int = 0
    content_type: str | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "pdf_stored"

    @property
    def retryable(self) -> bool:
        if self.status in ("timeout", "network_error", "rate_limited"):
            return True
        return self.status == "http_error" and (self.http_status or 0) >= 500


def declared_not_pdf(content_type: str | None) -> bool:
    """Whether a Content-Type header rules a PDF out (absent: undecided)."""
    if not content_type:
        return False
    declared = content_type.split(";", 1)[0].strip().lower()
    if declared in _PDF_TYPES:
        return False
    return any(marker in declared for marker in _NOT_PDF_TYPES)


def store(
    chunks: Iterable[bytes],
    dest_path: str,
    *,
    max_bytes: int,
    content_type: str | None = None,
    http_status: int | None = 200,
) -> DownloadResult:
    """Write `chunks` to `dest_path` only if they form a PDF within `max_bytes`."""
    if declared_not_pdf(content_type):
        return DownloadResult(
            "not_pdf", http_status, content_type=content_type, detail="content_type"
        )
    path = Path(dest_path)
    partial = path.with_name(f"{path.name}.part")
    written = 0
    head = b""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(partial, "wb") as handle:
            for chunk in chunks:
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    return DownloadResult(
                        "too_large", http_status, content_type=content_type
                    )
                if len(head) < PDF_HEADER_WINDOW:
                    head += chunk[: PDF_HEADER_WINDOW - len(head)]
                handle.write(chunk)
        if PDF_MAGIC not in head:
            status = "file_validation_failed" if content_type else "not_pdf"
            return DownloadResult(
                status,
                http_status,
                content_type=content_type,
                detail="no_pdf_signature",
            )
        os.replace(partial, path)
        return DownloadResult("pdf_stored", http_status, written, content_type)
    except OSError:
        return DownloadResult("storage_error", http_status, content_type=content_type)
    finally:
        partial.unlink(missing_ok=True)

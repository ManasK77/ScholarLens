"""Small, failure-tolerant HTTP client for the self-hosted GROBID service."""

import logging
from dataclasses import dataclass
from typing import Literal

import httpx

logger = logging.getLogger(__name__)

# A gateway error or "busy" says nothing about the PDF: the service could not
# be reached or had no worker free. 500 is GROBID's answer for a document it
# failed to process, which the same bytes will get again.
_UNAVAILABLE_STATUSES = frozenset({502, 503, 504})


@dataclass(frozen=True, slots=True)
class GrobidOutcome:
    """One GROBID call: the TEI when it worked, and why not when it did not.

    `unavailable` (transport error, timeout, 502/503/504) is a property of the
    service at that moment; `rejected` (any other non-200) is a property of the
    document. Stage 2 keeps a fallback parse made while GROBID was unavailable
    provisional, and re-tries GROBID for it on a later run.
    """

    tei: str | None
    status: Literal["ok", "rejected", "unavailable"]


def _post_pdf(
    pdf_bytes: bytes,
    grobid_url: str,
    endpoint: str,
    *,
    data: dict[str, str] | None = None,
    timeout: float = 90.0,
) -> GrobidOutcome:
    try:
        transport = httpx.HTTPTransport(retries=2)
        with httpx.Client(transport=transport, timeout=timeout) as client:
            response = client.post(
                f"{grobid_url.rstrip('/')}/api/{endpoint}",
                files={"input": ("paper.pdf", pdf_bytes, "application/pdf")},
                data=data,
            )
    except httpx.HTTPError as exc:
        logger.warning("GROBID %s request failed: %s", endpoint, type(exc).__name__)
        return GrobidOutcome(None, "unavailable")
    if response.status_code != 200:
        logger.warning("GROBID %s returned HTTP %d", endpoint, response.status_code)
        status = (
            "unavailable"
            if response.status_code in _UNAVAILABLE_STATUSES
            else "rejected"
        )
        return GrobidOutcome(None, status)
    return GrobidOutcome(response.text, "ok")


def fulltext(pdf_bytes: bytes, grobid_url: str, timeout: float = 90.0) -> GrobidOutcome:
    """Sentence-segmented TEI XML for one PDF, with the call's outcome."""
    return _post_pdf(
        pdf_bytes,
        grobid_url,
        "processFulltextDocument",
        data={"segmentSentences": "1"},
        timeout=timeout,
    )


def parse_fulltext(
    pdf_bytes: bytes, grobid_url: str, timeout: float = 90.0
) -> str | None:
    """Return sentence-segmented TEI XML, or ``None`` when GROBID fails."""
    return fulltext(pdf_bytes, grobid_url, timeout).tei


def parse_header(
    pdf_bytes: bytes, grobid_url: str, timeout: float = 90.0
) -> str | None:
    """Return header-only TEI XML, or ``None`` when GROBID fails."""
    return _post_pdf(
        pdf_bytes,
        grobid_url,
        "processHeaderDocument",
        timeout=timeout,
    ).tei

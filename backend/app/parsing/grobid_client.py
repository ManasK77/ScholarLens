"""Small, failure-tolerant HTTP client for the self-hosted GROBID service."""

import httpx


def _post_pdf(
    pdf_bytes: bytes,
    grobid_url: str,
    endpoint: str,
    *,
    data: dict[str, str] | None = None,
    timeout: float = 90.0,
) -> str | None:
    try:
        transport = httpx.HTTPTransport(retries=2)
        with httpx.Client(transport=transport, timeout=timeout) as client:
            response = client.post(
                f"{grobid_url.rstrip('/')}/api/{endpoint}",
                files={"input": ("paper.pdf", pdf_bytes, "application/pdf")},
                data=data,
            )
        return response.text if response.status_code == 200 else None
    except httpx.HTTPError:
        return None


def parse_fulltext(
    pdf_bytes: bytes, grobid_url: str, timeout: float = 90.0
) -> str | None:
    """Return sentence-segmented TEI XML, or ``None`` when GROBID fails."""
    return _post_pdf(
        pdf_bytes,
        grobid_url,
        "processFulltextDocument",
        data={"segmentSentences": "1"},
        timeout=timeout,
    )


def parse_header(
    pdf_bytes: bytes, grobid_url: str, timeout: float = 90.0
) -> str | None:
    """Return header-only TEI XML, or ``None`` when GROBID fails."""
    return _post_pdf(
        pdf_bytes,
        grobid_url,
        "processHeaderDocument",
        timeout=timeout,
    )

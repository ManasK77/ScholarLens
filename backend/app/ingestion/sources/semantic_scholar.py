"""Semantic Scholar ingestion source (Plan 01-03, INGEST-04).

Queries the public Semantic Scholar Graph API (`/graph/v1/paper/search`) for
a topic string and maps the response onto the corpus-shaped dict contract.

Strictly metadata-only per README §8 and D-14: this module never downloads
a PDF. If `openAccessPdf.url` is present in the response it is stored as
`openaccess_pdf_url` for the Unpaywall enrichment step (Plan 01-05) — or any
other later full-text-recovery step — to act on.

This module does not read Settings itself; the caller (Plan 01-06's
`/collect` orchestration) passes `api_key` explicitly so this stays a
pure/testable function per RESEARCH.md's "source module returns plain
dicts" pattern.
"""

import httpx

SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
FIELDS = "title,authors,year,venue,externalIds,abstract,openAccessPdf,citationCount"


def search_topic(topic: str, api_key: str | None = None) -> list[dict]:
    """Search Semantic Scholar for `topic` and return corpus-shaped dicts.

    Each dict has keys: title, authors (list[str] of names), year, venue,
    doi, abstract, openaccess_pdf_url (URL only — never downloaded here).
    """
    headers = {"x-api-key": api_key} if api_key else {}
    params = {"query": topic, "fields": FIELDS}

    with httpx.Client(transport=httpx.HTTPTransport(retries=2)) as client:
        resp = client.get(SEARCH_URL, params=params, headers=headers, timeout=10.0)
    resp.raise_for_status()
    body = resp.json()

    results = []
    for p in body.get("data", []):
        results.append(
            {
                "title": p.get("title"),
                "authors": [a["name"] for a in p.get("authors", [])],
                "year": p.get("year"),
                "venue": p.get("venue") or None,
                "doi": (p.get("externalIds") or {}).get("DOI"),
                "abstract": p.get("abstract"),
                "openaccess_pdf_url": (p.get("openAccessPdf") or {}).get("url"),
                "citation_count": p.get("citationCount"),
            }
        )
    return results

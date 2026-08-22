"""arXiv ingestion source (INGEST-03).

Queries the arXiv API via the official `arxiv` PyPI package and maps results
onto the corpus-shaped dict contract from `app.ingestion.corpus`.

D-13 (corrected post-research): the installed `arxiv` v4.0.1 package has no
`download_pdf`/`download_source` helper on `Result` (removed in a prior major
version). PDFs are fetched exclusively via the shared `corpus.download_pdf`
httpx helper against `Result.pdf_url` — never via a package-provided
download method.
"""

import arxiv

from app.ingestion import corpus


def search_topic(topic: str, max_results: int = 50) -> list[dict]:
    """Search arXiv for `topic` and return a list of corpus-shaped dicts."""
    client = arxiv.Client()
    search = arxiv.Search(query=topic, max_results=max_results)

    candidates = []
    for r in client.results(search):
        candidates.append(
            {
                "title": r.title,
                "authors": [a.name for a in r.authors],
                "year": r.published.year,
                "venue": None,
                "doi": r.doi,
                "abstract": r.summary,
                "arxiv_id": r.get_short_id(),
                "pdf_url": r.pdf_url,
                "source": "arxiv",
            }
        )
    return candidates


def fetch_pdf(candidate: dict, dest_dir: str) -> str | None:
    """Fetch `candidate`'s PDF via the shared corpus.download_pdf helper.

    Returns the destination path on success, None on failure.
    """
    dest_path = f"{dest_dir}/{candidate['arxiv_id']}.pdf"
    ok = corpus.download_pdf(candidate["pdf_url"], dest_path)
    return dest_path if ok else None

"""ACL Anthology ingestion source (INGEST-05).

Direct `bibtexparser` parsing of the Anthology's `anthology+abstracts.bib.gz`
export (D-15, corrected post-research) — lighter than the official
`acl-anthology` package's ~30 transitive dependencies and ~120MB first-use
download for this phase's simple topic-filter job.

Anthology ID / PDF URL derivation (Pitfall 3 + Open Question #1 resolution):
primary path strips the record's `url` field's trailing slash and takes the
last path segment; when `url` is absent (old-style pre-2020 entries), falls
back to the BibTeX cite key (`entry["ID"]`, always populated by
bibtexparser) and constructs the PDF URL directly.
"""

import gzip
import re
from pathlib import Path

import bibtexparser
import httpx

from app.ingestion import corpus

EXPORT_URL = "https://aclanthology.org/anthology+abstracts.bib.gz"


def _derive_id_and_pdf_url(entry: dict) -> tuple[str, str]:
    url = entry.get("url")
    if url:
        acl_id = url.rstrip("/").split("/")[-1]
        pdf_url = url.rstrip("/") + ".pdf"
    else:
        acl_id = entry["ID"]
        pdf_url = f"https://aclanthology.org/{acl_id}.pdf"
    return acl_id, pdf_url


def search_topic(bib_path: str, topic: str) -> list[dict]:
    """Filter the local .bib file's entries by a case-insensitive topic substring match."""
    with open(bib_path, encoding="utf-8") as f:
        bib_database = bibtexparser.load(f)

    topic_lower = topic.lower()
    results = []
    for entry in bib_database.entries:
        title = entry.get("title", "")
        abstract = entry.get("abstract", "")
        if topic_lower not in title.lower() and topic_lower not in abstract.lower():
            continue

        acl_id, pdf_url = _derive_id_and_pdf_url(entry)
        author_field = re.sub(r"\s+", " ", entry.get("author", ""))
        authors = [a.strip() for a in author_field.split(" and ") if a.strip()]
        year = int(entry["year"]) if entry.get("year") else None
        venue = entry.get("booktitle") or entry.get("journal")

        results.append(
            {
                "title": title,
                "authors": authors,
                "year": year,
                "venue": venue,
                "doi": entry.get("doi"),
                "abstract": abstract,
                "acl_id": acl_id,
                "pdf_url": pdf_url,
            }
        )
    return results


def fetch_pdf(candidate: dict, dest_dir: str) -> str | None:
    """Fetch `candidate`'s PDF via the shared corpus.download_pdf helper."""
    dest_path = f"{dest_dir}/{candidate['acl_id']}.pdf"
    ok = corpus.download_pdf(candidate["pdf_url"], dest_path)
    return dest_path if ok else None


def download_anthology_export(dest_path: str, force: bool = False) -> None:
    """Fetch-and-cache the live 42MB `.bib.gz` export, gunzipped, to `dest_path`.

    A no-op (no HTTP call) when `dest_path` already exists and `force` is False.
    """
    if Path(dest_path).exists() and not force:
        return

    with httpx.Client(transport=httpx.HTTPTransport(retries=2)) as client:
        resp = client.get(EXPORT_URL, timeout=60.0)
    decompressed = gzip.decompress(resp.content)

    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
    Path(dest_path).write_bytes(decompressed)

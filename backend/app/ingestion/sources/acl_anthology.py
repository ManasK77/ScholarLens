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


def _candidate_records(bib_path: str, topic_lower: str) -> str:
    """The raw BibTeX records that could possibly match `topic_lower`.

    `bibtexparser.load` over the real ~180MB Anthology export builds every one
    of its entries in memory — roughly 3GB, tens of minutes of CPU, and paid on
    *every* `/collect` call, not just the first. Streaming the file record by
    record and handing the parser only plausible matches costs one pass over
    the text instead.

    The prefilter is deliberately a strict *superset* of the real filter below,
    so it can never change which papers `search_topic` returns:

    * braces are dropped, so LaTeX protection (`{I}ndian`) still matches the
      parsed value (`Indian`) the caller searched for;
    * whitespace is collapsed, so a topic split across a wrapped field value
      still matches, exactly as it does once bibtexparser has joined the line.

    Anything that survives is still checked against the parsed `title` and
    `abstract` fields; this only decides what is worth parsing.
    """

    def _matches(record: str) -> bool:
        return (
            topic_lower
            in " ".join(record.replace("{", "").replace("}", "").split()).lower()
        )

    chunks: list[str] = []
    current: list[str] = []
    with open(bib_path, encoding="utf-8") as handle:
        for line in handle:
            # Records begin at column 0 with `@type{key,` — the one boundary the
            # export guarantees, and the reason this needs no BibTeX grammar.
            if line.startswith("@") and current:
                record = "".join(current)
                if _matches(record):
                    chunks.append(record)
                current = []
            current.append(line)
    if current:
        record = "".join(current)
        if _matches(record):
            chunks.append(record)
    return "".join(chunks)


def search_topic(bib_path: str, topic: str) -> list[dict]:
    """Filter the local .bib file's entries by a case-insensitive topic substring match."""
    topic_lower = topic.lower()
    bib_database = bibtexparser.loads(_candidate_records(bib_path, topic_lower))

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
    resp.raise_for_status()  # fail clearly on a non-200 body rather than a confusing gzip error
    decompressed = gzip.decompress(resp.content)

    Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
    Path(dest_path).write_bytes(decompressed)

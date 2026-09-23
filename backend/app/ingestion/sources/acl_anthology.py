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
import logging
import re
from pathlib import Path

import bibtexparser
import httpx

from app.ingestion import corpus
from app.ingestion.topic import meaningful_terms

EXPORT_URL = "https://aclanthology.org/anthology+abstracts.bib.gz"

logger = logging.getLogger(__name__)

# ACL Anthology track markers that identify a record with no original
# research content: a tutorial syllabus or a system demo, not a paper with
# a method and results (D-15-01). Anthology venue strings put the track
# after a colon on the `booktitle`/`journal` field itself
# ("...Association for Computational Linguistics: Tutorial Abstracts",
# "...Conference on Empirical Methods in NLP: System Demonstrations"), so
# this is metadata already present on every record -- never a PDF-text or
# abstract-keyword heuristic, and never something S2-S5 has to look at.
#
# Anchored so the phrase reads as a track name and not a word appearing
# mid-sentence: `(tutorial|demo)` alone would reject a workshop titled
# "Interactive Demonstrations of Multilingual Safety Failures", which is a
# research venue like any other. Requiring the marker to sit after the
# proceedings title's final colon is what Anthology venue strings actually
# do, and it is what keeps this a metadata check rather than a keyword scan.
_NON_RESEARCH_TRACK = re.compile(
    r"^(tutorial\s+abstracts?|demo(\s+track)?|system\s+demonstrations?)$",
    re.IGNORECASE,
)

# A track printed as a closing parenthetical: "...Technologies (System
# Demonstrations)".
_TRAILING_PARENTHETICAL = re.compile(r"\(([^()]*)\)$")


def is_non_research_track(venue: str | None) -> bool:
    """Whether `venue` names a non-research ACL Anthology track (D-15-01).

    Judges only the segment after the proceedings title's *last* colon --
    Anthology's own place for the track name -- normalizing whitespace so a
    line-wrapped or irregularly spaced BibTeX field still matches. A venue
    with no colon (an ordinary main-conference proceedings string) or no
    venue at all is never rejected; there is nothing to read a track marker
    off of, and absence of a track name is not a demo/tutorial signal.

    Since ACL 2023 the track sits inside a parenthesised volume label,
    "(Volume 3: System Demonstrations)", so that segment ends in ")"; NAACL
    2025 prints "...Technologies (System Demonstrations)". Both placements of
    the same markers are read; the marker list itself is unchanged.
    """
    if not venue:
        return False
    track = " ".join(venue.rsplit(":", 1)[-1].split())
    candidates = [track, track.removesuffix(")").strip()]
    trailing = _TRAILING_PARENTHETICAL.search(track)
    if trailing:
        candidates.append(trailing.group(1).strip())
    return any(_NON_RESEARCH_TRACK.match(candidate) for candidate in candidates)


def _derive_id_and_pdf_url(entry: dict) -> tuple[str, str]:
    url = entry.get("url")
    if url:
        acl_id = url.rstrip("/").split("/")[-1]
        pdf_url = url.rstrip("/") + ".pdf"
    else:
        acl_id = entry["ID"]
        pdf_url = f"https://aclanthology.org/{acl_id}.pdf"
    return acl_id, pdf_url


def _candidate_records(bib_path: str, prefilter_term: str) -> str:
    """The raw BibTeX records that could possibly match the topic's terms.

    `bibtexparser.load` over the real ~180MB Anthology export builds every one
    of its entries in memory — roughly 3GB, tens of minutes of CPU, and paid on
    *every* `/collect` call, not just the first. Streaming the file record by
    record and handing the parser only plausible matches costs one pass over
    the text instead.

    The prefilter is deliberately a strict *superset* of the real filter below,
    so it can never change which papers `search_topic` returns:

    * it requires only *one* of the topic's terms, while the real filter
      requires them all — a record holding every term necessarily holds this
      one (D-QUERY-02);
    * it searches the whole raw record, while the real filter searches only
      the parsed `title`/`abstract`, which are part of that record;
    * braces are dropped, so LaTeX protection (`{I}ndian`) still matches the
      parsed value (`Indian`) the caller searched for;
    * whitespace is collapsed, so a term split across a wrapped field value
      still matches, exactly as it does once bibtexparser has joined the line.

    Anything that survives is still checked against the parsed `title` and
    `abstract` fields; this only decides what is worth parsing.
    """

    def _matches(record: str) -> bool:
        return (
            prefilter_term
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
    """Filter the local .bib file's entries by the topic's terms (D-QUERY-02).

    An entry matches when *every* meaningful term in `topic` appears, as a
    case-insensitive substring, somewhere in its title or abstract. The old
    rule required the entire topic as one literal substring, so an ordinary
    multi-word topic ("multilingual prompt injection") matched an Anthology
    paper only where those words happened to be adjacent and in that order —
    which is why ACL contributed nothing to such a run while Semantic Scholar,
    which tokenizes the query itself, contributed ten papers.

    Each term is still a plain substring test, not a word-boundary or semantic
    one, so a single-word topic behaves exactly as it always has (`multilingual`
    still matches `non-multilingual`), and every term is still required, so the
    filter does not broaden into an OR over the topic's words.
    """
    terms = [term.lower() for term in meaningful_terms(topic)]
    if not terms:
        return []
    # Any one term is a valid superset of "all terms", so the cheap raw-text
    # pass uses the longest — the most selective single term available.
    prefilter_term = max(terms, key=len)
    bib_database = bibtexparser.loads(_candidate_records(bib_path, prefilter_term))

    results = []
    for entry in bib_database.entries:
        title = entry.get("title", "")
        abstract = entry.get("abstract", "")
        haystack = f"{title}\n{abstract}".lower()
        if not all(term in haystack for term in terms):
            continue

        acl_id, pdf_url = _derive_id_and_pdf_url(entry)
        author_field = re.sub(r"\s+", " ", entry.get("author", ""))
        authors = [a.strip() for a in author_field.split(" and ") if a.strip()]
        year = int(entry["year"]) if entry.get("year") else None
        venue = entry.get("booktitle") or entry.get("journal")

        # Rejected before a candidate dict is ever built, so a tutorial/demo
        # record never reaches `_process_capped` in routes_ingestion.py and
        # can never occupy an `acl_anthology_max_results` slot -- the cap is
        # spent on candidates that could become corpus rows, not on records
        # discarded before that point (D-15-01).
        if is_non_research_track(venue):
            logger.info(
                "ACL Anthology: skipping non-research-track record %s (venue=%r)",
                acl_id,
                venue,
            )
            continue

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

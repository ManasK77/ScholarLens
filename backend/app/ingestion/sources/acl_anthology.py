"""ACL Anthology ingestion source (INGEST-05).

Direct `bibtexparser` parsing of the Anthology's `anthology+abstracts.bib.gz`
export (D-15, corrected post-research) — lighter than the official
`acl-anthology` package's ~30 transitive dependencies and ~120MB first-use
download for this phase's simple topic-filter job.

**Freshness (D-ACL-CACHE-01, 2026-10-12).** The export is cached beside a
`.meta.json` file recording when, from where, how many records and its
SHA-256. `download_anthology_export` refreshes it when it is missing or
older than `ACL_ANTHOLOGY_MAX_AGE_DAYS`; a refresh is validated before it
replaces the cache, and a failed one keeps the last known-good file and is
reported as stale. `python -m app.ingestion.sources.acl_anthology --refresh`
does the same on demand.

Anthology ID / PDF URL derivation (Pitfall 3 + Open Question #1 resolution):
primary path strips the record's `url` field's trailing slash and takes the
last path segment; when `url` is absent (old-style pre-2020 entries), falls
back to the BibTeX cite key (`entry["ID"]`, always populated by
bibtexparser) and constructs the PDF URL directly.
"""

import codecs
import gzip
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import bibtexparser
import httpx

from app.ingestion import corpus
from app.ingestion.relevance import normalize
from app.ingestion.retrieval import error_category
from app.ingestion.topic import meaningful_terms

EXPORT_URL = "https://aclanthology.org/anthology+abstracts.bib.gz"
# A refresh is rejected below this share of the cached export's records.
_MIN_RECORD_RATIO = 0.9
# A refresh lock older than this was left by a process that died mid-refresh.
_LOCK_STALE_SECONDS = 15 * 60
_REFRESH_THREAD_LOCK = threading.Lock()
_UTF8_CONTINUATION = bytes(range(0x80, 0xC0))


class ExportInvalid(ValueError):
    """A downloaded export failed validation, or none is available at all."""


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


def _candidate_records(
    bib_path: str, prefilter_term: str, phrases: Sequence[str] = ()
) -> str:
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

    `phrases` (D-S1-GATE-01) widens the pass for the relevance gate's concept
    matcher: a record also survives when its *normalized* text -- braces
    dropped, hyphens read as spaces, as `relevance.normalize` reads it --
    contains one of them, so `{P}rompt-{I}njection` counts as "prompt
    injection".
    """

    def _matches(record: str) -> bool:
        if (
            prefilter_term
            in " ".join(record.replace("{", "").replace("}", "").split()).lower()
        ):
            return True
        if not phrases:
            return False
        normalized = normalize(record)
        return any(phrase in normalized for phrase in phrases)

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


def search_topic(
    bib_path: str,
    topic: str,
    *,
    matches: Callable[[str, str], bool] | None = None,
    prefilter_terms: Sequence[str] = (),
) -> list[dict]:
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

    **Candidates for the relevance gate (D-S1-GATE-01).** With `matches` (the
    gate's lexical facet check) an entry is also returned when *it* says every
    facet of the topic has evidence -- so "machine translation ... prompt
    injection" is found for "multilingual prompt injection" without the word
    "multilingual". Entries the legacy rule alone matches are still returned:
    they are candidates the gate will reject, and a rejection is recorded, not
    silently skipped. `prefilter_terms` are the gate's normalized phrases for
    the raw-text pass. This returns candidates; it decides nothing.
    """
    terms = [term.lower() for term in meaningful_terms(topic)]
    if not terms:
        return []
    # Any one term is a valid superset of "all terms", so the cheap raw-text
    # pass uses the longest — the most selective single term available.
    prefilter_term = max(terms, key=len)
    bib_database = bibtexparser.loads(
        _candidate_records(bib_path, prefilter_term, prefilter_terms)
    )

    results = []
    for entry in bib_database.entries:
        title = entry.get("title", "")
        abstract = entry.get("abstract", "")
        haystack = f"{title}\n{abstract}".lower()
        legacy_match = all(term in haystack for term in terms)
        if not legacy_match and not (matches is not None and matches(title, abstract)):
            continue

        acl_id, pdf_url = _derive_id_and_pdf_url(entry)
        author_field = re.sub(r"\s+", " ", entry.get("author", ""))
        authors = [a.strip() for a in author_field.split(" and ") if a.strip()]
        year = int(entry["year"]) if entry.get("year") else None
        venue = entry.get("booktitle") or entry.get("journal")

        # Rejected before a candidate dict is ever built, so a tutorial/demo
        # record never reaches `_admit_capped` in routes_ingestion.py and
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
                "source_url": entry.get("url") or f"https://aclanthology.org/{acl_id}/",
            }
        )
    return results


def fetch_pdf(candidate: dict, dest_dir: str, download=None) -> str | None:
    """Fetch `candidate`'s PDF via the shared corpus.download_pdf helper.

    `download` lets `/collect` pass a downloader that records the outcome of
    the attempt (D-FULLTEXT-01); it defaults to `corpus.download_pdf`.
    """
    dest_path = f"{dest_dir}/{candidate['acl_id']}.pdf"
    fetch = corpus.download_pdf if download is None else download
    ok = fetch(candidate["pdf_url"], dest_path)
    return dest_path if ok else None


def _meta_path(dest_path: str) -> Path:
    return Path(f"{dest_path}.meta.json")


def _read_meta(dest_path: str) -> dict:
    try:
        meta = json.loads(_meta_path(dest_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def _write_meta(dest_path: str, meta: dict) -> None:
    path = _meta_path(dest_path)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def cache_status(
    dest_path: str, max_age_days: int | None = None, *, now: datetime | None = None
) -> dict:
    """What the cached export is: when it was fetched, from where, how big.

    Read from the metadata file a refresh writes beside the export. An export
    cached before that file existed has only its modification time, and says
    so (``fetched_at_source: file_mtime``) with an unknown record count.
    """
    now = now or _utcnow()
    path = Path(dest_path)
    meta = _read_meta(dest_path)
    status: dict = {
        "file": path.name,
        "exists": path.is_file(),
        "fetched_at": None,
        "fetched_at_source": None,
        "age_days": None,
        "max_age_days": max_age_days,
        "stale": None,
        "record_count": meta.get("record_count"),
        "bytes": meta.get("bytes"),
        "sha256": meta.get("sha256"),
        "source_url": meta.get("source_url"),
        "last_refresh_attempt": meta.get("last_refresh_attempt"),
        "last_refresh_result": meta.get("last_refresh_result"),
    }
    if not status["exists"]:
        return status
    fetched_at = None
    if meta.get("fetched_at"):
        try:
            fetched_at = datetime.fromisoformat(meta["fetched_at"])
            status["fetched_at_source"] = "metadata"
        except ValueError:
            fetched_at = None
    if fetched_at is None:
        fetched_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        status["fetched_at_source"] = "file_mtime"
    status["fetched_at"] = fetched_at.isoformat()
    age = (now - fetched_at).total_seconds() / 86400
    status["age_days"] = round(age, 2)
    if max_age_days is not None:
        status["stale"] = age >= max_age_days
    return status


@contextmanager
def _refresh_lock(dest_path: str):
    """One refresh at a time, across threads and processes.

    A lock file created with O_EXCL; one older than `_LOCK_STALE_SECONDS` was
    left by a process that died mid-refresh and is taken over. Yields False
    when another refresh holds the lock.
    """
    lock = Path(f"{dest_path}.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with _REFRESH_THREAD_LOCK:
        for _ in range(2):
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    age = time.time() - lock.stat().st_mtime
                except OSError:
                    continue
                if age > _LOCK_STALE_SECONDS:
                    lock.unlink(missing_ok=True)
                    continue
                yield False
                return
            os.close(fd)
            try:
                yield True
            finally:
                lock.unlink(missing_ok=True)
            return
        yield False


def _record_count(data: bytes) -> int:
    """BibTeX records: lines that open with `@` (the export's one guarantee)."""
    return data.count(b"\n@") + (1 if data.startswith(b"@") else 0)


def refresh_export(
    dest_path: str,
    *,
    url: str = EXPORT_URL,
    min_records: int = 1,
    timeout: float = 120.0,
) -> dict:
    """Download, validate and atomically install a fresh export.

    The cached file is replaced only by a download that gunzips, opens as
    UTF-8 BibTeX, and holds at least `min_records` records and no fewer than
    `_MIN_RECORD_RATIO` of the cached file's -- so a truncated or empty
    export can never displace a good one. The new file is written beside the
    old and moved over it in one step, so a reader never sees a partial file.
    Returns ``{"result": "refreshed" | "failed" | "locked", ...}``; never
    raises for a failed download -- the caller decides whether a cache
    remains usable.
    """
    attempted_at = _utcnow().isoformat()
    meta = _read_meta(dest_path)
    with _refresh_lock(dest_path) as acquired:
        if not acquired:
            return {"result": "locked"}
        new_path = Path(f"{dest_path}.new")
        try:
            with httpx.Client(transport=httpx.HTTPTransport(retries=2)) as client:
                resp = client.get(url, timeout=timeout)
            # Fail clearly on a non-200 body rather than a confusing gzip error.
            resp.raise_for_status()
            try:
                data = gzip.decompress(resp.content)
            except (OSError, EOFError) as exc:
                raise ExportInvalid("not a gzip archive") from exc
            if not data.lstrip()[:1] == b"@":
                raise ExportInvalid("not a BibTeX export")
            # Spot-check the encoding at both ends: a slice boundary may split
            # a multi-byte character, which is not an encoding error.
            try:
                codecs.getincrementaldecoder("utf-8")().decode(data[:1_000_000])
                data[-1_000_000:].lstrip(_UTF8_CONTINUATION).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ExportInvalid("not UTF-8") from exc
            records = _record_count(data)
            floor = max(
                min_records, int(_MIN_RECORD_RATIO * (meta.get("record_count") or 0))
            )
            if records < floor:
                raise ExportInvalid(f"{records} records, {floor} required")
            Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
            new_path.write_bytes(data)
            os.replace(new_path, dest_path)
        except (httpx.HTTPError, OSError, EOFError, ValueError) as exc:
            new_path.unlink(missing_ok=True)
            category = (
                str(exc) if isinstance(exc, ExportInvalid) else error_category(exc)
            )
            logger.warning("ACL Anthology export refresh failed: %s", category)
            meta.update(
                last_refresh_attempt=attempted_at,
                last_refresh_result="failed",
                last_refresh_error=category,
            )
            if Path(dest_path).is_file():
                _write_meta(dest_path, meta)
            return {"result": "failed", "error": category, "exception": exc}
        meta = {
            "fetched_at": _utcnow().isoformat(),
            "source_url": url,
            "record_count": records,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "last_refresh_attempt": attempted_at,
            "last_refresh_result": "refreshed",
            "last_refresh_error": None,
        }
        _write_meta(dest_path, meta)
        logger.info("ACL Anthology export refreshed: %d records", records)
        return {"result": "refreshed", "record_count": records}


def download_anthology_export(
    dest_path: str,
    force: bool = False,
    *,
    max_age_days: int | None = None,
    min_records: int = 1,
    url: str = EXPORT_URL,
) -> dict:
    """Make sure a usable export is cached, refreshing it when due (D-ACL-CACHE-01).

    A refresh is attempted when the file is missing, when `force` is set, or
    when it is older than `max_age_days` (None: never by age, the behaviour
    before the freshness rule). A failed refresh keeps the last known-good
    file and is reported (``refresh: failed``, ``stale: true``); only when
    there is no usable file at all does the failure raise, failing the source
    for the run. Returns the cache status (see `cache_status`) with the
    refresh outcome: ``not_needed``, ``refreshed``, ``failed`` or ``locked``
    (another process is refreshing; the current file is used).
    """
    before = cache_status(dest_path, max_age_days)
    due = force or not before["exists"] or bool(before["stale"])
    if not due:
        return {**before, "refresh": "not_needed"}
    outcome = refresh_export(dest_path, url=url, min_records=min_records)
    after = cache_status(dest_path, max_age_days)
    if not after["exists"]:
        exc = outcome.get("exception")
        if exc is not None:
            raise exc
        raise ExportInvalid("ACL Anthology export unavailable")
    return {
        **after,
        "refresh": outcome["result"],
        "refresh_error": outcome.get("error"),
    }


def main(argv: list[str] | None = None) -> None:
    """`python -m app.ingestion.sources.acl_anthology --refresh [--force]`.

    The documented, explicit way to refresh the cached export outside a
    collection: `--refresh` refreshes when the cache is stale or missing,
    `--force` always. Prints the cache status as JSON; touches nothing else.
    """
    import argparse

    from app.core.config import get_settings

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    settings = get_settings()
    path = settings.acl_anthology_bib_path
    if args.refresh or args.force:
        status = download_anthology_export(
            path,
            force=args.force,
            max_age_days=settings.acl_anthology_max_age_days,
            min_records=settings.acl_anthology_min_records,
            url=settings.acl_anthology_export_url,
        )
    else:
        status = cache_status(path, settings.acl_anthology_max_age_days)
    print(json.dumps(status, indent=2))


if __name__ == "__main__":
    main()

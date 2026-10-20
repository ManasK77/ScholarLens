"""POST /collect and POST /upload — Phase 1's public ingestion surface (D-20, D-21, D-22).

/collect orchestrates arXiv + Semantic Scholar + ACL Anthology discovery
through dedup.py, then Unpaywall enrichment, returning the count/ids of
papers touched (new inserts + enriched rows) for the run. Each discovery source
contributes at most its configured cap of distinct papers (10 by default and at
most); manual uploads are uncapped. The caps are per source and independent,
and there is no run-wide cap, so one run stores at most 30 new papers -- an
upper bound, not a target: a source contributes fewer when fewer of its
candidates pass the relevance gate. A paper several sources return is stored
once and uses a slot only in the first source that collected it (see
`_admit_capped` for the counting rule).

The response also reports what the run actually did with what it found
(D-DUP-01): `added` rows created, a `duplicates` breakdown by category, and
`source_stats` per source (candidates returned, how many of them were processed
under that source's cap, rows created, duplicate candidates). So "30 candidates
found, 22 papers added" reads as eight recognised duplicates rather than eight
papers silently lost, and a source that stored nothing says whether it returned
nothing or returned only papers the corpus already had. `RunLedger` documents how the
categories are counted; the identity rules themselves are dedup.py's and are
unchanged.

Each source runs in isolation: a third-party outage degrades the run to the
sources that answered instead of aborting it. Semantic Scholar answers
unauthenticated callers with 429/500 routinely, and an unguarded exception
there used to roll back every arXiv row already written while the PDFs those
rows pointed at stayed on disk.

**Relevance gate (D-S1-GATE-01).** Source results are candidates, not proof
of relevance. Each source's candidate pool (larger than its cap) is judged by
`app.ingestion.relevance`; only admitted candidates are written, ranked by
`relevance.rank_key` (abstract before title-only, then title evidence, then
semantic score) rather than the source's order, so a rejected candidate never
takes a slot. Every candidate -- admitted, rejected, or capped (relevant, but
the cap was full) -- is recorded in `candidate_decisions` under a
`collection_runs` row that stores the topic (`app.ingestion.collection_audit`).

**Observability (Phase 1 closeout, 2026-10-12).** The response and the run's
`counts` also report duplicate promotions and pre-existing papers per source,
Unpaywall's outcome per row it looked up (`unpaywall`), full-text coverage of
the papers the run touched and added (`full_text`), and the paper-language
status of every admitted candidate (`paper_language`). Rejected and capped
decisions carry bounded `text_audit` evidence. None of it changes what is
admitted, capped or merged.

/upload accepts one or more PDFs via manual_upload.save_upload, and records an
`owner_upload` decision for each: the owner vouches for them, so discovery
gating does not apply.
"""

import logging
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.security import require_api_key
from app.api.write_guard import exclusive_lock, invalidate_derived
from app.core.config import get_settings
from app.ingestion import (
    collection_audit,
    corpus,
    dedup,
    fulltext,
    manual_upload,
    retrieval,
    unpaywall,  # noqa: F401 -- kept importable for callers and tests
)
from app.ingestion.relevance import build_gate, rank_key
from app.ingestion.sources import acl_anthology, arxiv, semantic_scholar

logger = logging.getLogger(__name__)

# Both endpoints write to the corpus and reach out to external services, so the
# whole router sits behind the Phase 11 API key (README §12 shows /collect
# being called with X-API-Key). /health stays open — see app.api.security.
router = APIRouter(dependencies=[Depends(require_api_key), Depends(exclusive_lock)])
# Reads of the collection audit trail take no write lock: they must stay
# available while a collection or pipeline run holds it.
audit_router = APIRouter(dependencies=[Depends(require_api_key)])


class CollectRequest(BaseModel):
    # Bounded: the topic is sent to every discovery source and stored on the
    # collection run. A blank one would query them all for nothing.
    topic: str = Field(min_length=1, max_length=500)

    @field_validator("topic")
    @classmethod
    def _strip_non_blank_topic(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("topic must contain a non-whitespace character")
        return value


def _to_paper_dict(candidate: dict, source: str, pdf_path: str | None) -> dict:
    """Map a source's raw candidate dict onto exactly the Paper column set.

    Source modules return extra keys (`pdf_url`/`openaccess_pdf_url`) that
    are not Paper columns — this is the single place those get dropped.
    """
    return {
        "title": candidate.get("title"),
        "authors": candidate.get("authors"),
        "year": candidate.get("year"),
        "venue": candidate.get("venue"),
        "doi": candidate.get("doi"),
        "abstract": candidate.get("abstract"),
        "citation_count": candidate.get("citation_count"),
        "source": source,
        "pdf_path": pdf_path,
        "has_full_text": pdf_path is not None,
        "arxiv_id": candidate.get("arxiv_id"),
        "acl_id": candidate.get("acl_id"),
        # Kept for full-text acquisition and provenance (D-FULLTEXT-01).
        "openaccess_pdf_url": candidate.get("openaccess_pdf_url"),
        "source_url": candidate.get("source_url"),
    }


_UPLOAD_CHUNK_SIZE = 1024 * 1024  # 1MB


def _read_bounded(file: UploadFile, max_bytes: int) -> bytes:
    """Read `file` in chunks, raising before buffering past `max_bytes`.

    A plain `file.file.read()` would buffer the entire upload into memory
    before manual_upload.save_upload's own size check ever runs, so an
    oversized upload could exhaust worker memory before being rejected.
    """
    chunks = []
    total = 0
    while True:
        chunk = file.file.read(_UPLOAD_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise manual_upload.UploadRejected(
                413, "Uploaded file exceeds the maximum allowed size"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _process_one(session: Session, paper_dict: dict) -> tuple[int, bool]:
    """Store one candidate, returning its corpus id and whether it was new.

    The identity rules are `dedup.find_existing`'s and are not re-stated here;
    this only reports which of the two branches ran, so the run can tell a
    newly inserted paper from a duplicate that merged onto an existing row.
    """
    existing = dedup.find_existing(session, paper_dict)
    if existing:
        updates = dedup.merge(existing, paper_dict)
        corpus.enrich_paper(session, existing.id, updates)
        return existing.id, False
    paper = corpus.insert_paper(session, paper_dict)
    return paper.id, True


@dataclass
class SourceOutcome:
    """What one discovery source produced, before the run merges it in.

    Held separately per source rather than written straight into the run's
    totals because a source that raises has its rows rolled back to its own
    savepoint — its counts must be discarded with them (see `_run`).
    """

    # Everything the source returned, before the cap and before dedup.
    candidates: int = 0
    # How many of those were actually written — fewer when the cap stopped the
    # run early. The two apart are what tell "returned nothing" from "returned
    # plenty, kept ten".
    processed: int = 0
    kept: set[int] = field(default_factory=set)
    inserted: set[int] = field(default_factory=set)
    # One entry per duplicate *encounter*: a candidate that merged onto a row
    # that already existed, whether from this run or an earlier one.
    duplicates: list[int] = field(default_factory=list)
    # The relevance gate's reason code for every rejected candidate.
    rejected: list[str] = field(default_factory=list)
    # Candidates the gate admitted but the source cap had no room for.
    capped: int = 0
    # Candidates merged onto a paper an earlier source in this run collected;
    # they use none of this source's slots.
    cross_source_duplicates: int = 0
    # Candidates the gate rejected but that were admitted as copies of a paper
    # already admitted for this topic (``duplicate_of_admitted``). Each is
    # then either processed (and counted in `duplicates`) or capped.
    duplicate_promotions: int = 0
    # Paper-language status (`evidence["language"]["status"]`) of each
    # processed admitted candidate.
    language: Counter = field(default_factory=Counter)
    # Local matches beyond the candidate pool, never judged (ACL only).
    beyond_candidate_pool: int = 0


def _admit_capped(
    session: Session,
    run: collection_audit.CollectionRun,
    gate,
    candidates: Sequence[dict],
    source: str,
    limit: int,
    fetch_pdf: Callable[[dict], str | None] | None = None,
    claimed: frozenset[int] = frozenset(),
    *,
    excerpt_chars: int,
    on_fetch: Callable[[int, str, str | None], None] | None = None,
) -> SourceOutcome:
    """Judge every candidate, then write the admitted ones until `limit` papers.

    **The counting rule (D-CAP-04, 2026-10-12, superseding D-CAP-03).** A
    source's cap counts the *new papers* it creates in this run -- nothing
    else. A candidate that merges onto any existing paper uses no slot,
    whether that paper was collected earlier in this run by another source
    (a cross-source duplicate, recorded with ``cross_source_duplicate_of``),
    earlier by this source, or by an earlier run. Under D-CAP-03 a paper
    stored by an earlier run still used a slot, so re-collecting a topic spent
    the cap re-finding the corpus (run 2: Semantic Scholar spent a slot on a
    v1 admission while a relevant candidate was capped). The cap is the
    owner's stated rule -- at most 10 new papers per source per run -- so a
    run can still add at most one quota of new papers per source, and each
    re-run at most another. A rejected candidate never reaches the cap, and a
    promoted copy (below) always merges, so it never uses a slot either.

    Admitted candidates are taken in `relevance.rank_key` order -- abstract
    before title-only, then title evidence, then score -- never the source's
    own order. The cap is checked before a candidate's PDF is fetched, so
    nothing past the cap is downloaded, and no PDF is fetched for a copy of a
    paper that already has full text. `on_fetch(paper_id, kind, pdf_path)`
    is told about every fetch (``fetched``) and every PDF reused instead
    (``reused``), once dedup has said which paper it was.

    A candidate the gate rejects is still admitted when it is a copy of a
    paper already admitted for this topic (``duplicate_of_admitted``): a
    Semantic Scholar record with no abstract is the same paper, not a new
    relevance question. A rejected candidate never modifies an existing row.

    Every candidate's decision is recorded, in the order the source returned
    them. A rejected or capped decision also records `collection_audit.
    text_audit` -- at most `excerpt_chars` characters of the abstract -- and a
    promoted or capped one keeps the gate's own verdict in `evidence["gate"]`
    with every transition in `evidence["gate_history"]`.
    """
    outcome = SourceOutcome(candidates=len(candidates))
    verdicts = gate.evaluate_many(
        [
            (candidate.get("title"), candidate.get("abstract"))
            for candidate in candidates
        ]
    )
    records: list[tuple[int, object, str | None, int | None]] = []
    admitted: list[tuple[int, dict, object]] = []
    for index, (candidate, verdict) in enumerate(
        zip(candidates, verdicts, strict=True)
    ):
        if not verdict.admitted:
            existing = dedup.find_existing(
                session, _to_paper_dict(candidate, source, None)
            )
            if existing is not None:
                if collection_audit.admitted_for_topic(session, existing.id, run.topic):
                    verdict = verdict.as_duplicate_of(existing.id)
                    outcome.duplicate_promotions += 1
                else:
                    verdict = verdict.with_evidence(existing_paper_id=existing.id)
        if verdict.admitted:
            admitted.append((index, candidate, verdict))
        else:
            outcome.rejected.append(verdict.reason_code)
            records.append((index, verdict, None, None))

    # Stable: equal keys keep the source's order.
    admitted.sort(key=lambda item: rank_key(item[2]), reverse=True)
    for index, candidate, verdict in admitted:
        existing = dedup.find_existing(session, _to_paper_dict(candidate, source, None))
        cross_source = existing is not None and existing.id in claimed
        uses_slot = existing is None
        if uses_slot and len(outcome.inserted) >= limit:
            outcome.capped += 1
            records.append((index, verdict.capped(source, limit), "capped", None))
            continue
        outcome.processed += 1
        reused = existing is not None and existing.has_full_text
        needs_pdf = fetch_pdf is not None and not reused
        pdf_path = fetch_pdf(candidate) if needs_pdf else None
        paper_id, inserted = _process_one(
            session, _to_paper_dict(candidate, source, pdf_path)
        )
        if on_fetch is not None and fetch_pdf is not None:
            on_fetch(paper_id, "reused" if reused else "fetched", pdf_path)
        outcome.kept.add(paper_id)
        outcome.language[
            (verdict.evidence.get("language") or {}).get("status", "not_recorded")
        ] += 1
        if inserted:
            outcome.inserted.add(paper_id)
        else:
            outcome.duplicates.append(paper_id)
        if cross_source:
            outcome.cross_source_duplicates += 1
            verdict = verdict.with_evidence(cross_source_duplicate_of=paper_id)
        records.append((index, verdict, None, paper_id))

    for index, verdict, override, paper_id in sorted(records, key=lambda r: r[0]):
        if not verdict.admitted:  # rejected, or capped
            verdict = verdict.with_evidence(
                text_audit=collection_audit.text_audit(candidates[index], excerpt_chars)
            )
        collection_audit.record(
            session,
            run,
            source,
            candidates[index],
            verdict,
            paper_id=paper_id,
            outcome=override,
        )
    return outcome


class RunLedger:
    """Per-run bookkeeping behind /collect's `added`/`duplicates` reporting (D-DUP-01).

    Duplicate *categories* are counted per distinct paper, never per encounter:
    one paper that arXiv, Semantic Scholar and ACL all returned is a single
    cross-source duplicate, not three. Each duplicated paper falls in exactly
    one category, in this priority:

    1. `already_in_corpus` — an earlier run had already stored it. This wins
       over the others because it is the fact the user is actually asking about
       when a search "finds" papers that do not appear as new.
    2. `cross_source` — this run inserted it, and more than one source
       returned it.
    3. `within_source` — a single source returned the same paper twice.

    A paper can be both newly inserted and duplicated (source A inserts it,
    source B re-finds it): `added` counts rows created, `duplicates` counts
    papers that were recognised rather than re-created.

    `source_stats[source]` counts *candidates* unless a key says papers:

    * ``candidates`` / ``processed`` / ``rejected`` / ``capped`` -- candidates
      returned, written or merged under the cap, turned away by the gate, and
      admitted but over the cap;
    * ``added`` -- papers this source created (rows);
    * ``duplicates`` -- processed candidates that merged onto an existing row;
    * ``cross_source_duplicates`` -- of those, ones merged onto a paper an
      earlier source collected in this run (no slot used);
    * ``already_in_corpus`` -- distinct papers this source kept that existed
      before the run started (no slot used, D-CAP-04);
    * ``cap`` / ``slots_used`` -- the source's new-paper cap and the slots
      used, which is ``added`` (set by /collect);
    * ``retrieval`` -- how the candidates were retrieved (`app.ingestion.
      retrieval`): reported total, pages, records, and whether the read was
      complete (set by /collect);
    * ``duplicate_promotions`` -- candidates the gate rejected but that were
      admitted as copies of a paper already admitted for the topic; each is
      also counted in ``duplicates`` or ``capped``, never in ``added``;
    * ``language_unverified`` -- processed candidates whose abstract could not
      establish the paper's language;
    * ``beyond_candidate_pool`` -- ACL matches never judged.
    """

    def __init__(self) -> None:
        self.touched: set[int] = set()
        self.inserted: set[int] = set()
        self.duplicated: set[int] = set()
        self.sources_by_paper: dict[int, set[str]] = {}
        self.stats: dict[str, dict[str, int]] = {}
        self.rejected: Counter[str] = Counter()
        self.capped = 0
        self.admitted = 0
        self.duplicate_promotions = 0
        self.cross_source_duplicates = 0
        self.language: Counter[str] = Counter()

    def record(self, source: str, outcome: SourceOutcome) -> None:
        already_in_corpus = outcome.kept - outcome.inserted - self.inserted
        self.touched |= outcome.kept
        self.inserted |= outcome.inserted
        self.duplicated.update(outcome.duplicates)
        for paper_id in outcome.kept:
            self.sources_by_paper.setdefault(paper_id, set()).add(source)
        self.rejected.update(outcome.rejected)
        self.capped += outcome.capped
        self.admitted += outcome.processed
        self.duplicate_promotions += outcome.duplicate_promotions
        self.cross_source_duplicates += outcome.cross_source_duplicates
        self.language.update(outcome.language)
        self.stats[source] = {
            "candidates": outcome.candidates,
            "processed": outcome.processed,
            "added": len(outcome.inserted),
            "duplicates": len(outcome.duplicates),
            "rejected": len(outcome.rejected),
            "capped": outcome.capped,
            "cross_source_duplicates": outcome.cross_source_duplicates,
            "already_in_corpus": len(already_in_corpus),
            "duplicate_promotions": outcome.duplicate_promotions,
            "language_unverified": outcome.language["unverified"],
            "beyond_candidate_pool": outcome.beyond_candidate_pool,
        }
        # One line per source per run, so a run that stored nothing from a
        # source says whether the source returned nothing or returned only
        # papers the corpus already had — the two look identical in a bare
        # `"status": "ok"`.
        logger.info(
            "Collection source=%s candidates=%d processed=%d added=%d "
            "duplicate_candidates=%d already_in_corpus=%d duplicate_promotions=%d "
            "rejected=%d capped=%d",
            source,
            outcome.candidates,
            outcome.processed,
            len(outcome.inserted),
            len(outcome.duplicates),
            len(already_in_corpus),
            outcome.duplicate_promotions,
            len(outcome.rejected),
            outcome.capped,
        )

    def enriched(self, paper_ids: Iterable[int]) -> None:
        """Full text touches rows discovery already wrote; it creates none."""
        self.touched |= set(paper_ids)

    def duplicate_summary(self) -> dict[str, int]:
        cross_source = within_source = already_in_corpus = 0
        for paper_id in self.duplicated:
            if paper_id not in self.inserted:
                already_in_corpus += 1
            elif len(self.sources_by_paper.get(paper_id, ())) > 1:
                cross_source += 1
            else:
                within_source += 1
        return {
            "total": len(self.duplicated),
            "cross_source": cross_source,
            "within_source": within_source,
            "already_in_corpus": already_in_corpus,
        }


DISCOVERY_SOURCES = ("arxiv", "semantic_scholar", "acl_anthology")
# Each source's new-paper cap setting, for the reconciliation check.
_CAP_SETTINGS = {
    "arxiv": "arxiv_max_results",
    "semantic_scholar": "semantic_scholar_max_results",
    "acl_anthology": "acl_anthology_max_results",
}

FULL_TEXT_SCOPE = (
    "Unique corpus papers, after full-text acquisition: 'touched' is every id "
    "in `ids` (including rows the full-text step completed), 'added' the rows "
    "this run created. has_full_text as stored; a PDF missing on disk is found "
    "by the pipeline."
)
UNPAYWALL_SCOPE = (
    "One outcome per corpus paper the full-text step reached Unpaywall for, "
    "from fulltext_attempts (provider 'unpaywall')."
)
PAPER_LANGUAGE_SCOPE = (
    "Admitted candidates written or merged into the corpus this run, by "
    "evidence['language']['status']."
)
UNITS = {
    "candidates": "records a source returned that the relevance gate judged",
    "decisions": "candidate_decisions rows: one per judged candidate",
    "admitted": "admitted decisions written or merged into papers",
    "added": "new papers rows created (each uses one of its source's slots)",
    "merge_events": "admitted decisions that merged onto an existing paper",
    "duplicates": "distinct papers recognised rather than re-created",
    "fulltext attempts": "one provider's try at one paper's PDF",
}


def _failure_status(exc: Exception) -> str:
    """A short, non-leaking description of why a source did not answer."""
    detail = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"failed: {type(exc).__name__}{f': {detail[:200]}' if detail else ''}"


def _full_text(session: Session, paper_ids: Iterable[int]) -> dict:
    """Full-text coverage of distinct corpus papers (never candidates)."""
    ids = list(paper_ids)
    rows = (
        session.execute(
            select(corpus.Paper.has_full_text, corpus.Paper.doi).where(
                corpus.Paper.id.in_(ids)
            )
        ).all()
        if ids
        else []
    )
    with_full_text = sum(1 for has_full_text, _ in rows if has_full_text)
    return {
        "papers": len(rows),
        "with_full_text": with_full_text,
        "metadata_only": len(rows) - with_full_text,
        "metadata_only_without_doi": sum(
            1 for has_full_text, doi in rows if not has_full_text and not doi
        ),
        "coverage": round(with_full_text / len(rows), 3) if rows else None,
    }


def _attempt_summary(session: Session, run_id: int) -> dict:
    """This run's full-text attempts, from the table (the source of truth)."""
    rows = session.execute(
        select(
            collection_audit.FulltextAttempt.provider,
            collection_audit.FulltextAttempt.stage,
            collection_audit.FulltextAttempt.outcome,
            func.count(),
        )
        .where(collection_audit.FulltextAttempt.run_id == run_id)
        .group_by(
            collection_audit.FulltextAttempt.provider,
            collection_audit.FulltextAttempt.stage,
            collection_audit.FulltextAttempt.outcome,
        )
    ).all()
    by_provider: dict[str, dict[str, int]] = {}
    by_stage: Counter[str] = Counter()
    for provider, stage, outcome, n in rows:
        by_provider.setdefault(provider, {})
        by_provider[provider][outcome] = by_provider[provider].get(outcome, 0) + n
        by_stage[stage] += n
    return {
        "attempts": sum(by_stage.values()),
        "by_stage": dict(by_stage),
        "by_provider": by_provider,
    }


def _warnings(
    retrieval: dict, statuses: dict, acl_cache: dict | None, ft: dict
) -> list:
    """Everything that makes a finished run less than exhaustive and clean."""
    warnings: list[dict] = []
    for source in DISCOVERY_SOURCES:
        status = statuses.get(source, "")
        if status != "ok":
            warnings.append(
                {
                    "source": source,
                    "code": "source_failed",
                    "message": f"{source} did not answer: {status}",
                }
            )
            continue
        report = retrieval.get(source) or {}
        if report and not report.get("complete", True):
            reason = report.get("stop_reason")
            code = (
                "retrieval_ceiling"
                if reason in ("ceiling", "page_budget")
                else "partial_retrieval"
            )
            total = report.get("total_reported")
            warnings.append(
                {
                    "source": source,
                    "code": code,
                    "message": (
                        f"{source}: read {report.get('records_fetched')} of "
                        f"{total if total is not None else 'an unknown number of'} "
                        f"reported results ({reason}"
                        f"{', ' + report['error'] if report.get('error') else ''})."
                    ),
                }
            )
    if acl_cache and acl_cache.get("stale"):
        warnings.append(
            {
                "source": "acl_anthology",
                "code": "stale_cache",
                "message": (
                    f"ACL Anthology export is {acl_cache.get('age_days')} days old "
                    f"(limit {acl_cache.get('max_age_days')}); refresh "
                    f"{acl_cache.get('refresh')}"
                    f"{': ' + acl_cache['refresh_error'] if acl_cache.get('refresh_error') else ''}."
                ),
            }
        )
    if (statuses.get("fulltext") or "").startswith("failed"):
        warnings.append(
            {
                "source": "fulltext",
                "code": "fulltext_step_failed",
                "message": f"Full-text acquisition failed: {statuses['fulltext']}",
            }
        )
    elif ft.get("budget_exhausted"):
        warnings.append(
            {
                "source": "fulltext",
                "code": "fulltext_budget_exhausted",
                "message": "Full-text acquisition ran out of time; some papers "
                "were not attempted.",
            }
        )
    return warnings


def _run_status(statuses: dict, warnings: list) -> str:
    discovery = [statuses.get(name) for name in DISCOVERY_SOURCES]
    if all(status != "ok" for status in discovery):
        return "failed"
    if any(status != "ok" for status in discovery) or statuses.get("fulltext") != "ok":
        return "partial"
    return "completed_with_warnings" if warnings else "completed"


def _reconcile(session, run_id: int, ledger: "RunLedger", settings) -> dict:
    """Checks that the run's numbers, in their different units, agree."""
    decisions = session.scalar(
        select(func.count())
        .select_from(collection_audit.CandidateDecision)
        .where(collection_audit.CandidateDecision.run_id == run_id)
    )
    # `candidates` is what each source's gate judged (ACL's pool, not every
    # local match), so it must equal the decisions recorded.
    judged = sum(stats["candidates"] for stats in ledger.stats.values())
    rejected = sum(ledger.rejected.values())
    merges = sum(stats["duplicates"] for stats in ledger.stats.values())
    checks = {
        "decisions_equal_candidates_judged": decisions == judged,
        "decisions_equal_admitted_plus_rejected_plus_capped": decisions
        == ledger.admitted + rejected + ledger.capped,
        "admitted_equal_added_plus_merge_events": ledger.admitted
        == len(ledger.inserted) + merges,
        "new_papers_within_each_source_cap": all(
            stats["added"] <= getattr(settings, _CAP_SETTINGS[source])
            for source, stats in ledger.stats.items()
        ),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "decisions": decisions,
        "candidates_judged": judged,
        "merge_events": merges,
    }


class _DiscoveryFetches:
    """Records the outcome of each PDF a source fetched while admitting.

    The downloader `wrap` returns stores its last `DownloadResult`;
    `on_fetch` writes it against the paper once dedup has said which paper
    it is. A fetch function replaced by one that never calls the downloader
    still gets a row, derived from whether it returned a path.
    """

    def __init__(self, log: fulltext.AttemptLog) -> None:
        self.log = log
        self.last: tuple | None = None

    def wrap(
        self, download: Callable[[str, str], object]
    ) -> Callable[[str, str], bool]:
        def recorded(url: str, dest_path: str) -> bool:
            result = download(url, dest_path)
            self.last = (result, url)
            return result.ok

        return recorded

    def on_fetch(self, provider: str) -> Callable[[int, str, str | None], None]:
        def record(paper_id: int, kind: str, pdf_path: str | None) -> None:
            last, self.last = self.last, None
            if kind == "reused":
                self.log.record(
                    paper_id,
                    provider,
                    "discovery",
                    "existing_full_text_reused",
                    retryable=False,
                )
            elif last is not None:
                result, url = last
                self.log.record(
                    paper_id,
                    provider,
                    "discovery",
                    result.status,
                    result=result,
                    url=url,
                    pdf_path=pdf_path if result.ok else None,
                )
            else:
                self.log.record(
                    paper_id,
                    provider,
                    "discovery",
                    "pdf_stored" if pdf_path else "download_failed",
                    pdf_path=pdf_path,
                )

        return record


def _local_retrieval(topic: str, found: int, pool: int, terms: Sequence[str]) -> dict:
    """The ACL export is searched locally: one read, every match counted."""
    return retrieval.report(
        query=f"local export: {topic}",
        params={"pool": pool, "prefilter_terms": list(terms)},
        total_reported=found,
        pages_requested=1,
        pages_succeeded=1,
        records_fetched=found,
        records_dropped={},
        candidates=min(found, pool),
        stop_reason="exhausted" if found <= pool else "ceiling",
    )


@router.post("/collect")
def collect(
    body: CollectRequest,
    session: Session = Depends(corpus.get_session),  # noqa: B008 — required FastAPI DI pattern
):
    invalidate_derived(session)
    settings = get_settings()
    ledger = RunLedger()
    statuses: dict[str, str] = {}
    reports: dict[str, dict | None] = {}
    gate = build_gate(body.topic, settings)
    arxiv_pool = max(settings.arxiv_candidate_pool, settings.arxiv_max_results)
    s2_pool = max(
        settings.semantic_scholar_request_limit, settings.semantic_scholar_max_results
    )
    acl_pool = max(
        settings.acl_anthology_candidate_pool, settings.acl_anthology_max_results
    )
    run = collection_audit.start_collection(
        session,
        body.topic,
        gate.describe(),
        {
            **gate.describe(),
            "arxiv_candidate_pool": arxiv_pool,
            "arxiv_page_size": settings.arxiv_page_size,
            "semantic_scholar_candidate_pool": s2_pool,
            "semantic_scholar_page_size": settings.semantic_scholar_page_size,
            "semantic_scholar_max_pages": settings.semantic_scholar_max_pages,
            "acl_anthology_candidate_pool": acl_pool,
            "acl_anthology_max_age_days": settings.acl_anthology_max_age_days,
            "arxiv_max_results": settings.arxiv_max_results,
            "semantic_scholar_max_results": settings.semantic_scholar_max_results,
            "acl_anthology_max_results": settings.acl_anthology_max_results,
            "relevance_audit_excerpt_chars": settings.relevance_audit_excerpt_chars,
            "fulltext_max_pdf_mb": settings.fulltext_max_pdf_mb,
            "fulltext_step_budget_seconds": settings.fulltext_step_budget_seconds,
        },
    )
    # The run exists, as `running`, whatever happens next: a run that fails
    # or dies mid-way is still on record (2026-10-12).
    session.commit()
    run_id = run.id
    excerpt_chars = settings.relevance_audit_excerpt_chars
    max_pdf_bytes = settings.fulltext_max_pdf_mb * 1024 * 1024
    log = fulltext.AttemptLog(session, run_id)
    fetches = _DiscoveryFetches(log)
    acl_cache: dict | None = None
    ft_step: dict = {}

    def _arxiv_pdf(url: str, dest_path: str):
        """Every arXiv-hosted PDF this run fetches, from discovery or enrichment.

        One downloader for both so the run's configured contact, spacing and
        retry budget reach the full-text path as well as the API path, and so
        all of it queues on arXiv's single process-wide limiter (D-PDF-01).
        """
        return arxiv.download_pdf_result(
            url,
            dest_path,
            contact=settings.arxiv_contact,
            num_retries=settings.arxiv_num_retries,
            min_interval_seconds=settings.arxiv_min_interval_seconds,
            backoff_base_seconds=settings.arxiv_backoff_base_seconds,
            backoff_max_seconds=settings.arxiv_backoff_max_seconds,
            max_bytes=max_pdf_bytes,
        )

    def _other_pdf(url: str, dest_path: str):
        return fulltext.safe_download(
            url,
            dest_path,
            timeout=settings.fulltext_timeout_seconds,
            max_bytes=max_pdf_bytes,
            max_retries=settings.fulltext_max_retries,
        )

    def _run(name: str, step) -> None:
        """Run one ingestion step, recording rather than propagating its failure.

        Each step is independent, so one source's outage must not discard what
        the others already wrote. Anything the step did before raising is rolled
        back to its own savepoint; earlier sources keep their rows.
        """
        savepoint = session.begin_nested()
        try:
            step()
        except Exception as exc:
            savepoint.rollback()
            statuses[name] = _failure_status(exc)
            logger.warning("Ingestion source %r failed: %s", name, exc, exc_info=True)
        else:
            savepoint.commit()
            statuses[name] = "ok"

    def _arxiv() -> None:
        candidates = arxiv.search_topic(
            body.topic,
            max_results=arxiv_pool,
            page_size=settings.arxiv_page_size,
            num_retries=settings.arxiv_num_retries,
            min_interval_seconds=settings.arxiv_min_interval_seconds,
            backoff_base_seconds=settings.arxiv_backoff_base_seconds,
            backoff_max_seconds=settings.arxiv_backoff_max_seconds,
            contact=settings.arxiv_contact,
            query_groups=gate.query_groups(),
        )
        reports["arxiv"] = getattr(candidates, "retrieval", None)
        ledger.record(
            "arxiv",
            _admit_capped(
                session,
                run,
                gate,
                candidates,
                "arxiv",
                settings.arxiv_max_results,
                lambda c: arxiv.fetch_pdf(
                    c, settings.pdf_storage_path, download=fetches.wrap(_arxiv_pdf)
                ),
                claimed=frozenset(ledger.touched),
                excerpt_chars=excerpt_chars,
                on_fetch=fetches.on_fetch("arxiv"),
            ),
        )

    def _semantic_scholar() -> None:
        candidates = semantic_scholar.search_topic(
            body.topic,
            settings.semantic_scholar_api_key,
            limit=s2_pool,
            request_limit=s2_pool,
            page_size=settings.semantic_scholar_page_size,
            max_pages=settings.semantic_scholar_max_pages,
            max_retries=settings.semantic_scholar_max_retries,
            min_interval_seconds=settings.semantic_scholar_min_interval_seconds,
            backoff_base_seconds=settings.semantic_scholar_backoff_base_seconds,
            backoff_max_seconds=settings.semantic_scholar_backoff_max_seconds,
        )
        reports["semantic_scholar"] = getattr(candidates, "retrieval", None)
        ledger.record(
            "semantic_scholar",
            _admit_capped(
                session,
                run,
                gate,
                candidates,
                "semantic_scholar",
                settings.semantic_scholar_max_results,
                claimed=frozenset(ledger.touched),
                excerpt_chars=excerpt_chars,
            ),
        )

    def _acl_anthology() -> None:
        nonlocal acl_cache
        cache = acl_anthology.download_anthology_export(
            settings.acl_anthology_bib_path,
            max_age_days=settings.acl_anthology_max_age_days,
            min_records=settings.acl_anthology_min_records,
            url=settings.acl_anthology_export_url,
        )
        acl_cache = cache if isinstance(cache, dict) else None
        found = acl_anthology.search_topic(
            settings.acl_anthology_bib_path,
            body.topic,
            matches=gate.facets_evidenced,
            prefilter_terms=gate.prefilter_terms(),
        )
        # The export has no ranking of its own: records with evidence for
        # every facet are judged first, so the pool is spent on candidates
        # that could be admitted.
        found.sort(
            key=lambda c: not gate.facets_evidenced(c.get("title"), c.get("abstract"))
        )
        reports["acl_anthology"] = _local_retrieval(
            body.topic, len(found), acl_pool, gate.prefilter_terms()
        )
        outcome = _admit_capped(
            session,
            run,
            gate,
            found[:acl_pool],
            "acl_anthology",
            settings.acl_anthology_max_results,
            lambda c: acl_anthology.fetch_pdf(
                c, settings.pdf_storage_path, download=fetches.wrap(_other_pdf)
            ),
            claimed=frozenset(ledger.touched),
            excerpt_chars=excerpt_chars,
            on_fetch=fetches.on_fetch("acl_anthology"),
        )
        outcome.beyond_candidate_pool = max(0, len(found) - acl_pool)
        ledger.record("acl_anthology", outcome)

    def _fulltext() -> None:
        ft_step.clear()
        ft_step.update(
            fulltext.acquire_missing_full_text(
                session,
                log,
                pdf_dir=settings.pdf_storage_path,
                unpaywall_email=settings.unpaywall_email,
                download_arxiv=_arxiv_pdf,
                download_other=_other_pdf,
                budget_seconds=settings.fulltext_step_budget_seconds,
            )
        )
        ledger.enriched(ft_step.get("stored_paper_ids", []))
        logger.info(
            "Collection step=fulltext considered=%d stored=%d",
            ft_step.get("papers_considered", 0),
            ft_step.get("pdfs_stored", 0),
        )

    try:
        _run("arxiv", _arxiv)
        _run("semantic_scholar", _semantic_scholar)
        _run("acl_anthology", _acl_anthology)
        if any(statuses[name] == "ok" for name in DISCOVERY_SOURCES):
            # Full text runs last and only over rows discovery already wrote.
            _run("fulltext", _fulltext)
        else:
            statuses["fulltext"] = "skipped: no discovery source answered"
    except Exception as exc:  # a defect, not a source outage: still on record
        session.rollback()
        _finish(session, run_id, "failed", {"error": type(exc).__name__}, statuses)
        raise

    if all(statuses[name] != "ok" for name in DISCOVERY_SOURCES):
        session.rollback()
        warnings = _warnings(reports, statuses, acl_cache, {})
        _finish(
            session,
            run_id,
            "failed",
            {"status": "failed", "sources": statuses, "warnings": warnings},
            statuses,
        )
        raise HTTPException(
            status_code=502,
            detail="No discovery source could be reached. Check network access.",
        )

    def _conclude() -> dict:
        """Statistics, warnings, status, persistence: the run's end."""
        for source, stats in ledger.stats.items():
            # The cap counts new papers only (D-CAP-04): `slots_used` is `added`.
            stats["cap"] = getattr(settings, _CAP_SETTINGS[source])
            stats["slots_used"] = stats["added"]
            stats["retrieval"] = reports.get(source)
        attempts = _attempt_summary(session, run_id)
        unpaywall_outcomes = attempts["by_provider"].get("unpaywall", {})
        fulltext_report = {
            "completed": statuses.get("fulltext") == "ok",
            "papers_considered": ft_step.get("papers_considered", 0),
            "pdfs_stored": ft_step.get("pdfs_stored", 0),
            "budget_exhausted": bool(ft_step.get("budget_exhausted")),
            **attempts,
        }
        warnings = _warnings(reports, statuses, acl_cache, fulltext_report)
        status = _run_status(statuses, warnings)
        duplicates = ledger.duplicate_summary()
        full_text = {
            "scope": FULL_TEXT_SCOPE,
            "touched": _full_text(session, ledger.touched),
            "added": _full_text(session, ledger.inserted),
        }
        unpaywall_report = {
            "scope": UNPAYWALL_SCOPE,
            "completed": statuses.get("fulltext") == "ok",
            "looked_up": sum(unpaywall_outcomes.values()),
            "outcomes": unpaywall_outcomes,
        }
        reconciliation = _reconcile(session, run_id, ledger, settings)
        result = {
            "count": len(ledger.touched),
            "ids": list(ledger.touched),
            "added": len(ledger.inserted),
            "duplicates": duplicates,
            "sources": statuses,
            "source_stats": ledger.stats,
            # Additive (D-S1-GATE-01): what the relevance gate kept out, and where
            # every decision is recorded.
            "collection_run_id": run_id,
            "rejected": {
                "total": sum(ledger.rejected.values()),
                "by_reason": dict(ledger.rejected),
            },
            "capped": ledger.capped,
            # Additive (Phase 1 closeout and S1 hardening, 2026-10-12).
            "status": status,
            "warnings": warnings,
            "full_text": full_text,
            "fulltext": fulltext_report,
            "unpaywall": unpaywall_report,
            "acl_cache": acl_cache,
            "paper_language": {
                "scope": PAPER_LANGUAGE_SCOPE,
                "admitted": dict(ledger.language),
            },
            "reconciliation": reconciliation,
            "units": UNITS,
        }
        run = session.get(collection_audit.CollectionRun, run_id)
        run.source_status = dict(statuses)
        run.counts = {
            "admitted": ledger.admitted,
            "rejected": sum(ledger.rejected.values()),
            "capped": ledger.capped,
            "added": len(ledger.inserted),
            "duplicate_promotions": ledger.duplicate_promotions,
            "cross_source_duplicates": ledger.cross_source_duplicates,
            "paper_language": dict(ledger.language),
            "full_text": full_text,
            "unpaywall": unpaywall_report,
        }
        session.commit()
        _finish(
            session,
            run_id,
            status,
            {key: value for key, value in result.items() if key != "ids"},
            statuses,
        )
        logger.info(
            "Collection complete run=%d status=%s topic=%r papers=%d added=%d "
            "duplicates=%s reconciled=%s",
            run_id,
            status,
            body.topic,
            len(ledger.touched),
            len(ledger.inserted),
            duplicates,
            reconciliation["ok"],
        )
        return result

    try:
        return _conclude()
    except Exception as exc:  # a defect after the sources ran: still on record
        session.rollback()
        _finish(session, run_id, "failed", {"error": type(exc).__name__}, statuses)
        raise


def _finish(
    session: Session, run_id: int, status: str, summary: dict, statuses
) -> None:
    """Close the run: its final status, when, and its persisted summary."""
    run = session.get(collection_audit.CollectionRun, run_id)
    run.status = status
    run.finished_at = datetime.now(UTC).replace(tzinfo=None)
    run.summary = summary
    run.source_status = dict(statuses)
    session.commit()


@router.post("/upload")
def upload(
    files: list[UploadFile],
    session: Session = Depends(corpus.get_session),  # noqa: B008 — required FastAPI DI pattern
):
    settings = get_settings()
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if len(files) > settings.max_upload_files:
        raise HTTPException(
            status_code=413,
            detail=f"Upload at most {settings.max_upload_files} files per request",
        )

    invalidate_derived(session)
    created = []
    run = collection_audit.start_upload(session)
    for file in files:
        try:
            file_bytes = _read_bounded(file, max_bytes)
            result = manual_upload.save_upload(
                file_bytes=file_bytes,
                filename=file.filename,
                content_type=file.content_type,
                dest_dir=settings.pdf_storage_path,
                max_bytes=max_bytes,
                session=session,
            )
        except manual_upload.UploadRejected as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        created.append(result["id"])
        collection_audit.record_upload(session, run, result["id"], file.filename)

    run.counts = {"admitted": len(created)}
    session.commit()

    return {"created": created}


@audit_router.get("/collections/{run_id}")
def collection_run(
    run_id: int,
    session: Session = Depends(corpus.get_session),  # noqa: B008 — required FastAPI DI pattern
):
    """One collection's persisted record: status, timing and full summary."""
    run = session.get(collection_audit.CollectionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Collection run not found")
    return collection_audit.serialize_run(run)


@audit_router.get("/collections/{run_id}/fulltext")
def collection_fulltext(
    run_id: int,
    session: Session = Depends(corpus.get_session),  # noqa: B008 — required FastAPI DI pattern
):
    """Every full-text attempt the run made, per paper and provider."""
    if session.get(collection_audit.CollectionRun, run_id) is None:
        raise HTTPException(status_code=404, detail="Collection run not found")
    return {
        "run_id": run_id,
        "attempts": [
            collection_audit.serialize_attempt(row)
            for row in collection_audit.attempts_for_run(session, run_id)
        ],
    }


@audit_router.get("/collections/{run_id}/decisions")
def collection_decisions(
    run_id: int,
    session: Session = Depends(corpus.get_session),  # noqa: B008 — required FastAPI DI pattern
):
    """One collection's audit trail: the run, and every candidate's decision."""
    run = session.get(collection_audit.CollectionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Collection run not found")
    return {
        "run": collection_audit.serialize_run(run),
        "decisions": [
            collection_audit.serialize_decision(row)
            for row in collection_audit.decisions_for_run(session, run_id)
        ],
    }

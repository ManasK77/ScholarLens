"""Stage 9 orchestration — ranked clusters in, grounded Mode A reports out.

The whole phase, for each of the top-K gaps Stage 8 ranked:

1. build a retrieval query from the cluster's label and its own statements,
2. retrieve the top-N supporting passages from Qdrant — the actual text, not
   counts or statistics,
3. hand those passages plus Stage 8's signals to the LLM in **one** call,
4. keep only the citations that name a passage that was actually retrieved,
5. store the report against the evidence it cited.

Three properties this module owns:

* **Single pass.** CLAUDE.md hard constraint 3: there is no loop around Stage
  8→9. One retrieval, one generation, per cluster. A report that cannot be
  grounded is dropped, never regenerated — that decision belongs to Mode B
  (Phase 10), which is a different mode with a different contract.
* **Nothing unsupported is stored.** An id the model invented is discarded, and
  a report whose citations are *all* invented is not stored at all. This is the
  project's core credibility claim (README §15, Phase 9) reduced to code.
* **The store is replaced only once, at the end.** The full list is built
  first, so a provider outage mid-run raises before `replace_reports` deletes
  anything and the previous run's reports survive.

Nothing here branches on a paper's `source` (CLAUDE.md hard constraint 5).
"""

import logging
from time import perf_counter

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clustering.storage import list_clusters, members_for_cluster
from app.core.config import Settings, get_settings
from app.explanation.llm import LLMError, LLMUnavailableError, get_llm_client
from app.explanation.prompt import build_prompt, retrieval_query
from app.explanation.schema import (
    REPORT_SCHEMA,
    GapReportRecord,
    ReportDraft,
    evidence_from_point,
)
from app.explanation.selection import evidence_summary, select_evidence
from app.explanation.storage import replace_reports
from app.ingestion.corpus import Paper
from app.ranking.storage import list_rankings

logger = logging.getLogger(__name__)


def _paper_titles(session: Session, paper_ids: set[int]) -> dict[int, str | None]:
    """`paper_id -> title` for the papers behind a batch of retrieved points.

    One query for the whole batch rather than one per evidence item. A point
    whose paper is no longer in the corpus is simply absent from the result —
    the vector store can lag the corpus, exactly as Phase 6 documents for
    `ClusterMember.paper_id`, and an untitled quote is still real evidence.
    """
    if not paper_ids:
        return {}
    return {
        paper_id: title
        for paper_id, title in session.execute(
            select(Paper.id, Paper.title).where(Paper.id.in_(paper_ids))
        ).all()
    }


def _evidence_for(session: Session, points: list[dict]) -> list[dict]:
    """Project retrieved points onto the stored `Evidence` shape, in rank order.

    A malformed point is skipped rather than fatal: it came out of a payload
    written by an earlier phase, and one bad record must not cost the whole
    corpus its reports.
    """
    titles = _paper_titles(
        session,
        {
            point["paper_id"]
            for point in points
            if isinstance(point.get("paper_id"), int)
        },
    )
    evidence = []
    for point in points:
        try:
            evidence.append(
                evidence_from_point(point, titles.get(point.get("paper_id")))
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Stage 9: skipping unusable evidence point: %s", exc)
    return evidence


def _grounded_evidence(evidence: list[dict], cited_ids: list[str]) -> list[dict]:
    """The cited evidence, in retrieval order, with invented ids discarded.

    Retrieval order rather than citation order: the model's ordering carries no
    information the pipeline produced, while Qdrant's is by similarity.
    """
    cited = set(cited_ids)
    invented = cited - {item["id"] for item in evidence}
    if invented:
        logger.warning(
            "Stage 9: dropping %d evidence id(s) the model invented: %s",
            len(invented),
            sorted(invented),
        )
    return [item for item in evidence if item["id"] in cited]


def generate_top_reports(
    session: Session,
    settings: Settings | None = None,
    index=None,
    llm=None,
) -> list[int]:
    """Explain the top-ranked gaps and store the reports. Returns cluster ids.

    `index` and `llm` are injectable so the phase can be tested without Qdrant
    or a model; production callers pass neither. The caller owns the transaction
    (`routes_backend` commits afterwards), so this flushes and never commits.
    """
    settings = settings or get_settings()

    rankings = list_rankings(session, settings.report_top_k)
    if not rankings:
        logger.info("Stage 9: no ranked gaps stored yet")
        return replace_reports(session, [])

    if index is None:
        from app.embeddings.indexer import EvidenceIndex

        index = EvidenceIndex(settings)
    llm = llm or get_llm_client(settings)

    labels = {cluster.id: cluster.label for cluster in list_clusters(session)}

    reports: list[GapReportRecord] = []
    for ranking in rankings:
        cluster_id = ranking.cluster_id
        members = members_for_cluster(session, cluster_id)
        query = retrieval_query(labels.get(cluster_id), [m.text for m in members])
        if not query.strip():
            # A cluster with neither a label nor a statement describes nothing,
            # and an empty query does not fail — it returns whatever happens to
            # sit nearest the zero vector, which would then be presented as
            # supporting evidence. Reachable when a ranking outlives its
            # cluster: SQLite does not enforce the `ON DELETE CASCADE`.
            logger.warning(
                "Stage 9: cluster %d has no label or members; not explained",
                cluster_id,
            )
            continue

        started = perf_counter()
        # Retrieved wider than the report needs, then narrowed by
        # `select_evidence`. Without the headroom the per-paper cap could only
        # ever remove passages, never let a crowded-out paper be promoted
        # (D-09-04).
        points = index.retrieve(
            query,
            settings.report_evidence_top_n * settings.report_evidence_oversample,
        )
        retrieval_elapsed = perf_counter() - started
        evidence = select_evidence(
            _evidence_for(session, points),
            {member.paper_id for member in members},
            settings.report_evidence_top_n,
            settings.report_evidence_per_paper_cap,
        )
        if not evidence:
            # No retrieved text means nothing to ground a report in, so there is
            # nothing worth spending a generation call on.
            logger.info(
                "Stage 9: cluster %d has no retrievable evidence; not explained",
                cluster_id,
            )
            continue

        prompt = build_prompt(labels.get(cluster_id), ranking, evidence)
        started = perf_counter()
        try:
            draft = ReportDraft.model_validate(llm.generate_json(prompt, REPORT_SCHEMA))
        except LLMUnavailableError as exc:
            # A provider outage — or an exhausted quota the client already
            # retried through — is not a per-cluster problem: every remaining
            # call would fail the same way, and burning K timeouts to store
            # nothing is the resource failure README §13.6 warns about. Raising
            # here also means `replace_reports` never runs, so the previous
            # run's reports survive.
            #
            # How far the run got is logged because it is the cost of the
            # retry: the drafts already generated are discarded with the rest,
            # so re-running spends those calls again. Nothing upstream is
            # touched, so `POST /reports` alone resumes from here.
            logger.warning(
                "Stage 9: provider stopped the run after %d of %d ranked gap(s) "
                "(%s); the evidence base is untouched, retry with POST /reports",
                len(reports),
                len(rankings),
                exc,
            )
            raise
        except (LLMError, ValidationError) as exc:
            logger.warning(
                "Stage 9: cluster %d produced no usable report: %s", cluster_id, exc
            )
            continue

        summary = evidence_summary(evidence)
        logger.debug(
            "Stage 9: cluster %d retrieval=%.3fs llm=%.3fs evidence=%d "
            "papers=%d max_per_paper=%d",
            cluster_id,
            retrieval_elapsed,
            perf_counter() - started,
            len(evidence),
            summary["unique_paper_count"],
            summary["max_passages_from_one_paper"],
        )
        grounded = _grounded_evidence(evidence, draft.evidence_ids)
        if not grounded:
            logger.warning(
                "Stage 9: cluster %d cited no retrieved evidence; report discarded",
                cluster_id,
            )
            continue

        # Each of the four claims is grounded against what was actually
        # retrieved, not against what the model says it cited. A claim whose
        # citations were all invented is downgraded to unsupported, which is
        # what stops "inconsistent findings across studies" being stored with
        # nothing behind it (D-09-05).
        justification = draft.justification({item["id"] for item in evidence})
        if (
            draft.conflicting_evidence.supported
            and not justification["conflicting_evidence"]["supported"]
        ):
            logger.info(
                "Stage 9: cluster %d claimed conflicting evidence with no "
                "retrieved passage behind it; recorded as unsupported",
                cluster_id,
            )

        reports.append(
            GapReportRecord(
                cluster_id=cluster_id,
                gap=draft.gap,
                evidence=grounded,
                confidence=draft.confidence,
                suggested_directions=draft.suggested_directions,
                provider=getattr(llm, "provider", ""),
                model=getattr(llm, "model", ""),
                justification=justification,
            )
        )

    ids = replace_reports(session, reports)
    logger.info(
        "Stage 9: stored %d grounded report(s) of %d ranked gap(s)",
        len(ids),
        len(rankings),
    )
    return ids

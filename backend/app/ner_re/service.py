"""Phase 4 orchestration — joint extraction over the corpus into Neo4j.

Reads the normalized corpus and Phase 3's extracted statements, runs the joint
entity+relation model over a bounded slice of each paper's text, and writes the
result into Neo4j through `graph.write_paper_graph`.

Two properties this module is responsible for:

* **Source-agnostic.** Every paper is processed identically; nothing here
  branches on `Paper.source` (README hard constraint — Stage 2 onward).
* **Bounded work per paper.** Running a transformer over whole papers is the
  expensive failure mode README §13.6 warns about, so input is capped to a
  configurable character budget and chunk count. Phase 3's extracted statements
  are exactly the sentences that carry model/dataset/metric claims, which is why
  they are preferred over raw parsed text.
"""

import logging
import re
from dataclasses import dataclass, replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.logging import ProgressLogger
from app.core.pipeline import current_run, fingerprint, settings_fingerprint
from app.extraction.storage import ExtractedFact
from app.ingestion.corpus import Paper
from app.ner_re.extractor import SupportsExtractMany, get_joint_extractor
from app.ner_re.graph import (
    NERRE_PROVENANCE_VERSION,
    ensure_graph_schema,
    paper_graph_current,
    write_paper_graph,
)
from app.ner_re.schema import (
    ENTITY_DESCRIPTIONS,
    RELATION_ENDPOINTS,
    JointExtractionResult,
)

logger = logging.getLogger(__name__)

ABSTRACT_REF = "abstract"


@dataclass(frozen=True, slots=True)
class SourceUnit:
    """One piece of Stage 4 input and where it came from (D-S4-PROV-01).

    `ref` is `abstract`, or `statement:<section>.<paragraph>.<sentence>` -- the
    location of a Stage 3 statement in the paper's parsed document, which is
    also the location part of `extracted_facts`' evidence key. Never a
    surrogate id: those change whenever Stage 3 re-runs.
    """

    ref: str
    text: str


def source_units(session: Session, paper: Paper) -> list[SourceUnit]:
    """The paper's abstract followed by its Phase 3 statements, deduplicated."""
    candidates: list[tuple[str, str | None]] = []
    if paper.abstract:
        candidates.append((ABSTRACT_REF, paper.abstract))

    facts = session.scalars(
        select(ExtractedFact)
        .where(ExtractedFact.paper_id == paper.id)
        .order_by(
            ExtractedFact.section_index,
            ExtractedFact.paragraph_index,
            ExtractedFact.sentence_index,
            ExtractedFact.id,
        )
    )
    candidates.extend(
        (
            (
                f"statement:{fact.section_index}.{fact.paragraph_index}"
                f".{fact.sentence_index}"
            ),
            fact.text,
        )
        for fact in facts
    )

    seen: set[str] = set()
    units: list[SourceUnit] = []
    for ref, candidate in candidates:
        if not candidate:
            continue
        normalized = " ".join(candidate.split())
        if not normalized:
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        units.append(SourceUnit(ref, normalized))
    return units


def _sentences_for_paper(session: Session, paper: Paper) -> list[str]:
    """The paper's abstract followed by its Phase 3 statements, deduplicated."""
    return [unit.text for unit in source_units(session, paper)]


def _pack(
    units: list[SourceUnit], max_chars_per_chunk: int, max_chunks: int
) -> list[list[SourceUnit]]:
    """Group units into at most `max_chunks` chunks of bounded joined length."""
    chunks: list[list[SourceUnit]] = []
    current: list[SourceUnit] = []
    length = 0

    for unit in units:
        if not current:
            # A single over-long sentence still becomes its own chunk rather
            # than being dropped; the model truncates it instead.
            current, length = [unit], len(unit.text)
        elif length + 1 + len(unit.text) <= max_chars_per_chunk:
            current.append(unit)
            length += 1 + len(unit.text)
        else:
            chunks.append(current)
            if len(chunks) >= max_chunks:
                return chunks
            current, length = [unit], len(unit.text)

    if current:
        chunks.append(current)
    return chunks[:max_chunks]


def collect_paper_text(
    session: Session,
    paper: Paper,
    max_chars_per_chunk: int = 1200,
    max_chunks: int = 12,
) -> list[str]:
    """Pack the paper's sentences into at most `max_chunks` bounded chunks."""
    return [
        " ".join(unit.text for unit in chunk)
        for chunk in _pack(
            source_units(session, paper), max_chars_per_chunk, max_chunks
        )
    ]


def _mention_pattern(text: str) -> re.Pattern:
    # Word boundaries, so "BERT" is not found inside "RoBERTa".
    return re.compile(
        r"(?<!\w)" + re.escape(" ".join(text.casefold().split())) + r"(?!\w)"
    )


def attach_evidence(
    result: JointExtractionResult,
    units: list[SourceUnit],
    stats: dict[str, int] | None = None,
) -> JointExtractionResult:
    """Tie every entity and relation to the input units it was read from.

    An entity whose text occurs in none of the units the model was shown is
    dropped, with any relation that needs it: it is not something this paper
    says. Relations need only their endpoints: the evidence names the units
    holding both, or -- marked `cross_sentence` -- the units holding either.
    """
    counts = stats if stats is not None else {}
    folded = [(unit.ref, unit.text.casefold()) for unit in units]

    def refs(text: str) -> tuple[str, ...]:
        pattern = _mention_pattern(text)
        return tuple(ref for ref, body in folded if pattern.search(body))

    entities, kept = [], {}
    for entity in result.entities:
        evidence = refs(entity.text)
        if not evidence:
            counts["entities_not_in_source"] = (
                counts.get("entities_not_in_source", 0) + 1
            )
            continue
        entities.append(replace(entity, evidence=evidence))
        kept[(" ".join(entity.text.casefold().split()), entity.entity_type)] = evidence

    relations = []
    for relation in result.relations:
        head_type, tail_type = RELATION_ENDPOINTS[relation.relation_type]
        head = kept.get((" ".join(relation.head.casefold().split()), head_type))
        tail = kept.get((" ".join(relation.tail.casefold().split()), tail_type))
        if head is None or tail is None:
            counts["relations_unsupported"] = counts.get("relations_unsupported", 0) + 1
            continue
        both = tuple(ref for ref in head if ref in tail)
        either = tuple(ref for ref, _ in folded if ref in head or ref in tail)
        relations.append(
            replace(relation, evidence=both or either, cross_sentence=not both)
        )
    return JointExtractionResult(entities=entities, relations=relations)


def _input_basis(units: list[SourceUnit]) -> str:
    has_abstract = any(unit.ref == ABSTRACT_REF for unit in units)
    has_statements = any(unit.ref != ABSTRACT_REF for unit in units)
    if has_abstract and has_statements:
        return "abstract_and_statements"
    if has_abstract:
        return "abstract_only"
    return "statements_only" if has_statements else "none"


def _provenance(
    extractor, used: list[SourceUnit], total: int, settings: Settings | None
) -> dict:
    """What the Paper node records about the projection's own origin."""
    return {
        "nerre_provenance": NERRE_PROVENANCE_VERSION,
        "nerre_model": (settings or get_settings()).nerre_model,
        "nerre_model_revision": getattr(extractor, "model_revision", None),
        "nerre_input_basis": _input_basis(used),
        "nerre_units_used": len(used),
        "nerre_units_total": total,
    }


def _project(
    session: Session,
    paper: Paper,
    extractor: SupportsExtractMany,
    max_chars_per_chunk: int,
    max_chunks: int,
    stats: dict[str, int] | None,
    settings: Settings | None = None,
) -> tuple[JointExtractionResult, dict]:
    """One paper's extraction with evidence attached, and its provenance."""
    units = source_units(session, paper)
    packed = _pack(units, max_chars_per_chunk, max_chunks)
    used = [unit for chunk in packed for unit in chunk]
    if packed:
        raw = extractor.extract_many(
            [" ".join(u.text for u in chunk) for chunk in packed]
        )
        result = attach_evidence(raw, used, stats)
    else:
        result = JointExtractionResult([], [])
    _count_result(result, stats)
    return result, _provenance(extractor, used, len(units), settings)


def extract_paper(
    session: Session,
    neo4j,
    paper: Paper,
    extractor: SupportsExtractMany,
    max_chars_per_chunk: int,
    max_chunks: int,
    stats: dict[str, int] | None = None,
) -> bool:
    """Extract and persist one paper's graph. Returns False when it was skipped."""
    if not collect_paper_text(session, paper, max_chars_per_chunk, max_chunks):
        logger.debug("Paper %s has no usable text for joint extraction", paper.id)
        return False

    result, provenance = _project(
        session, paper, extractor, max_chars_per_chunk, max_chunks, stats
    )
    write_paper_graph(neo4j, paper, result, provenance=provenance)
    return True


def _count_result(result: JointExtractionResult, stats: dict[str, int] | None) -> None:
    """Accumulate O2 counts (never entity text) for the stage summary."""
    if stats is None:
        return
    stats["entities"] = stats.get("entities", 0) + len(result.entities)
    stats["relations"] = stats.get("relations", 0) + len(result.relations)


def extract_all(
    session: Session,
    neo4j,
    extractor: SupportsExtractMany | None = None,
    settings: Settings | None = None,
    max_chars_per_chunk: int | None = None,
    max_chunks: int | None = None,
) -> list[int]:
    """Run joint extraction across the whole corpus, returning processed ids.

    A checkpoint failure on one paper must not abandon the rest of the batch, so
    per-paper errors are logged and skipped.
    """
    # Resolved lazily: a caller that supplies an extractor and explicit caps
    # must not need a fully-populated environment just to construct Settings.
    if max_chars_per_chunk is None or max_chunks is None:
        settings = settings or get_settings()
        max_chars_per_chunk = max_chars_per_chunk or settings.nerre_max_chars_per_chunk
        max_chunks = max_chunks or settings.nerre_max_chunks_per_paper
    ensure_graph_schema(neo4j)

    processed: list[int] = []
    papers = list(session.scalars(select(Paper).order_by(Paper.id)))
    logger.info("s4 start papers=%d", len(papers))
    stats: dict[str, int] = {}
    skipped_no_text = failed = 0
    progress = ProgressLogger(logger, "s4", len(papers))
    for position, paper in enumerate(papers, 1):
        try:
            run = current_run()
            if run:
                chunks = collect_paper_text(
                    session, paper, max_chars_per_chunk, max_chunks
                )
                if not chunks:
                    skipped_no_text += 1
                digest = fingerprint(
                    {
                        "chunks": chunks,
                        "settings": settings_fingerprint(
                            settings or get_settings(), ("nerre_",)
                        ),
                        "caps": [max_chars_per_chunk, max_chunks],
                        "schema": [ENTITY_DESCRIPTIONS, RELATION_ENDPOINTS],
                        "version": 1,
                        "metadata": [paper.title, paper.year, paper.venue, paper.doi],
                    }
                )

                # This paper's chunks, node and digest are bound as defaults, not
                # closed over: `run.paper` holds both callables and the loop
                # variables must not follow the loop on to the next paper.
                def write(paper=paper, chunks=chunks, digest=digest):
                    nonlocal extractor
                    if chunks:
                        extractor = extractor or get_joint_extractor()
                    result, provenance = _project(
                        session,
                        paper,
                        extractor,
                        max_chars_per_chunk,
                        max_chunks,
                        stats,
                        settings,
                    )
                    write_paper_graph(
                        neo4j, paper, result, input_hash=digest, provenance=provenance
                    )

                def graph_is_current(paper_id=paper.id, digest=digest):
                    return paper_graph_current(neo4j, paper_id, digest)

                if run.paper(paper.id, "s4", digest, write, verify=graph_is_current):
                    processed.append(paper.id)
                continue
            extractor = extractor or get_joint_extractor()
            if extract_paper(
                session, neo4j, paper, extractor, max_chars_per_chunk, max_chunks, stats
            ):
                processed.append(paper.id)
            else:
                skipped_no_text += 1
        except Exception:
            if current_run():
                raise
            failed += 1
            logger.exception("Joint extraction failed for paper %s; skipping", paper.id)
        finally:
            progress.update(position)
    logger.info(
        "s4 complete processed=%d skipped_no_text=%d failed=%d entities=%d "
        "relations=%d entities_not_in_source=%d relations_unsupported=%d",
        len(processed),
        skipped_no_text,
        failed,
        stats.get("entities", 0),
        stats.get("relations", 0),
        stats.get("entities_not_in_source", 0),
        stats.get("relations_unsupported", 0),
    )
    return processed

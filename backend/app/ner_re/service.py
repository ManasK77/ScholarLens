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

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.extraction.storage import ExtractedFact
from app.ingestion.corpus import Paper
from app.ner_re.extractor import SupportsExtractMany, get_joint_extractor
from app.ner_re.graph import ensure_graph_schema, write_paper_graph

logger = logging.getLogger(__name__)


def _sentences_for_paper(session: Session, paper: Paper) -> list[str]:
    """The paper's abstract followed by its Phase 3 statements, deduplicated."""
    candidates: list[str] = []
    if paper.abstract:
        candidates.append(paper.abstract)

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
    candidates.extend(fact.text for fact in facts)

    seen: set[str] = set()
    sentences: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        normalized = " ".join(candidate.split())
        if not normalized:
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        sentences.append(normalized)
    return sentences


def collect_paper_text(
    session: Session,
    paper: Paper,
    max_chars_per_chunk: int = 1200,
    max_chunks: int = 12,
) -> list[str]:
    """Pack the paper's sentences into at most `max_chunks` bounded chunks."""
    chunks: list[str] = []
    current = ""

    for sentence in _sentences_for_paper(session, paper):
        if not current:
            # A single over-long sentence still becomes its own chunk rather
            # than being dropped; the model truncates it instead.
            current = sentence
        elif len(current) + 1 + len(sentence) <= max_chars_per_chunk:
            current = f"{current} {sentence}"
        else:
            chunks.append(current)
            if len(chunks) >= max_chunks:
                return chunks
            current = sentence

    if current:
        chunks.append(current)
    return chunks[:max_chunks]


def extract_paper(
    session: Session,
    neo4j,
    paper: Paper,
    extractor: SupportsExtractMany,
    max_chars_per_chunk: int,
    max_chunks: int,
) -> bool:
    """Extract and persist one paper's graph. Returns False when it was skipped."""
    chunks = collect_paper_text(session, paper, max_chars_per_chunk, max_chunks)
    if not chunks:
        logger.debug("Paper %s has no usable text for joint extraction", paper.id)
        return False

    result = extractor.extract_many(chunks)
    write_paper_graph(neo4j, paper, result)
    return True


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
    extractor = extractor or get_joint_extractor()

    ensure_graph_schema(neo4j)

    processed: list[int] = []
    for paper in session.scalars(select(Paper).order_by(Paper.id)):
        try:
            if extract_paper(
                session, neo4j, paper, extractor, max_chars_per_chunk, max_chunks
            ):
                processed.append(paper.id)
        except Exception:
            logger.exception("Joint extraction failed for paper %s; skipping", paper.id)
    return processed

"""Qdrant indexing and semantic retrieval for corpus evidence.

Point ids are `uuid5` of a stable `kind:paper_id:position` key, so re-running
the batch job overwrites the same point instead of duplicating it — that is
what makes `index_all` safe to repeat. "Stable" means derived from the
evidence's own identity and never from a database surrogate key: a paragraph's
position is structural (`section:paragraph`), a fact's is the evidence tuple
`uq_extracted_fact_evidence` uses (see `_fact_position`). After each paper is
written, `prune_paper` removes whatever the current records no longer cover, so
the index mirrors the corpus rather than accumulating every past run.
"""

import logging
from uuid import NAMESPACE_URL, uuid5

from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.logging import ProgressLogger
from app.core.pipeline import current_run, fingerprint, settings_fingerprint
from app.embeddings.encoder import MAX_CHARS, Specter2Encoder, get_encoder
from app.embeddings.schema import GAP_SOURCE_TYPES, PAYLOAD_FIELDS, IndexRecord
from app.extraction.storage import ExtractedFact
from app.ingestion.corpus import Paper
from app.ner_re.graph import get_paper_node_id
from app.parsing.storage import ParsedDocument

logger = logging.getLogger(__name__)

# Metadata keys a caller may filter a semantic query on (EMBED-02). Qdrant
# indexes these so filtered retrieval stays cheap as the corpus grows.
FILTERABLE_PAYLOAD_FIELDS: dict[str, models.PayloadSchemaType] = {
    "paper_id": models.PayloadSchemaType.INTEGER,
    "year": models.PayloadSchemaType.INTEGER,
    "venue": models.PayloadSchemaType.KEYWORD,
    "section_type": models.PayloadSchemaType.KEYWORD,
    "source_type": models.PayloadSchemaType.KEYWORD,
}


# The largest result set a single `retrieve()` call may ask Qdrant for, however
# large a `limit` a caller passes. Bounds the response a Phase 9/10 request can
# pull into memory.
MAX_RETRIEVAL_LIMIT = 1000


def _record_id(kind: str, paper_id: int, position: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"scholarlens:{kind}:{paper_id}:{position}"))


def _fact_position(fact: ExtractedFact) -> str:
    """A fact's position key: its evidence identity, never its surrogate id.

    `ExtractedFact.id` is a database autoincrement and `replace_facts` is
    delete-then-insert, so the same statement is handed a *different* id on
    every extraction run. Keying the point on it meant a second
    `POST /pipeline/run` wrote a fresh point for every fact and orphaned the
    previous one — `gap_points()` then returned each limitation twice, inflating
    Stage 6 member counts and Stage 8's `mention_frequency`.

    The fields below are exactly the ones `uq_extracted_fact_evidence` uses to
    define when two rows are the same fact, so this is stable by construction:
    if the evidence is unchanged, the point id is unchanged.

    The numeric fields precede the free text, so a `:` inside a statement can
    only ever shift the *last* component and never make two different facts
    collide.
    """
    return (
        f"{fact.fact_type}:{fact.section_index}:{fact.paragraph_index}"
        f":{fact.sentence_index}:{fact.text}"
    )


def _clean(value: object, max_chars: int = MAX_CHARS) -> str:
    """Collapse whitespace in text that came out of a PDF. Never raises.

    Truncated at the encoder's own character budget, so the stored payload
    stays in step with the text the vector was built from, and a pathological
    "paragraph" out of a malformed PDF cannot become an unbounded Qdrant
    payload.
    """
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:max_chars]


def build_index_records(
    paper: Paper,
    document: ParsedDocument,
    facts: list[ExtractedFact],
    graph_node_id: str,
) -> list[IndexRecord]:
    """Everything about one paper that Stage 5 embeds.

    Parsed sections are stored JSON, so a malformed row is a data problem, not
    a bug: anything that is not a section/paragraph mapping is skipped rather
    than allowed to abort the whole corpus run.
    """
    records = []
    for section_index, section in enumerate(document.sections):
        if not isinstance(section, dict):
            continue
        section_type = section.get("type")
        paragraphs = section.get("paragraphs") or []
        if not isinstance(paragraphs, list):
            continue
        for paragraph_index, paragraph in enumerate(paragraphs):
            if not isinstance(paragraph, dict):
                continue
            text = _clean(paragraph.get("text"))
            if text:
                records.append(
                    IndexRecord(
                        _record_id(
                            "paragraph", paper.id, f"{section_index}:{paragraph_index}"
                        ),
                        paper.id,
                        text,
                        "paragraph",
                        None,
                        paper.year,
                        paper.venue,
                        section_type,
                        graph_node_id,
                    )
                )
    for fact in facts:
        if fact.fact_type not in GAP_SOURCE_TYPES:
            continue
        # A blank statement would cost a forward pass and then sit in the index
        # matching every query weakly. Phase 3 should not emit one; if it does,
        # this is where it stops.
        text = _clean(fact.text)
        if not text:
            continue
        records.append(
            IndexRecord(
                _record_id("fact", paper.id, _fact_position(fact)),
                paper.id,
                text,
                fact.fact_type,
                fact.id,
                paper.year,
                paper.venue,
                fact.section_type,
                graph_node_id,
            )
        )
    return records


class EvidenceIndex:
    def __init__(
        self,
        settings: Settings | None = None,
        client: QdrantClient | None = None,
        encoder: Specter2Encoder | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client or QdrantClient(url=self.settings.qdrant_url, timeout=30.0)
        self._encoder = encoder
        self.collection = self.settings.qdrant_collection

    @property
    def encoder(self) -> Specter2Encoder:
        if self._encoder is None:
            self._encoder = get_encoder()
        return self._encoder

    def has_evidence(self) -> bool:
        if not self.client.collection_exists(self.collection):
            return False
        return bool(
            self.client.count(collection_name=self.collection, exact=False).count
        )

    def ensure_collection(self) -> None:
        if not self.client.collection_exists(self.collection):
            try:
                self.client.create_collection(
                    collection_name=self.collection,
                    vectors_config=models.VectorParams(
                        size=self.encoder.dimension, distance=models.Distance.COSINE
                    ),
                )
            except UnexpectedResponse as exc:
                # Another request can create the collection between the existence
                # check and this call. Qdrant reports that harmless race as 409.
                if exc.status_code != 409:
                    raise

    def ensure_payload_indexes(self) -> None:
        """Index the metadata keys `retrieve(filters=...)` can filter on.

        Additive and idempotent: Qdrant builds the index over points that are
        already stored, so this can run at any point in a batch job. A 409 from
        a concurrent writer is the same harmless race `ensure_collection`
        handles, and a payload index is an optimization — never let failing to
        create one abort an indexing run that otherwise succeeded.
        """
        self.ensure_collection()
        for field_name, field_schema in FILTERABLE_PAYLOAD_FIELDS.items():
            try:
                self.client.create_payload_index(
                    collection_name=self.collection,
                    field_name=field_name,
                    field_schema=field_schema,
                )
            except UnexpectedResponse as exc:
                if exc.status_code != 409:
                    logger.warning(
                        "Could not create the %r payload index: %s", field_name, exc
                    )
            except Exception:
                logger.warning(
                    "Could not create the %r payload index", field_name, exc_info=True
                )

    def check_collection_dimension(self) -> None:
        """Refuse to write into a collection shaped for a different model.

        Swapping `EMBEDDING_MODEL` without recreating the collection would
        otherwise mix incompatible vectors, and no query would ever be right
        again. Fail loudly instead — and never delete the existing data to
        "fix" it; that is the owner's call.
        """
        vectors_config = self.client.get_collection(
            self.collection
        ).config.params.vectors
        size = getattr(vectors_config, "size", None)
        if size is not None and size != self.encoder.dimension:
            raise ValueError(
                f"vector dimension mismatch: collection {self.collection!r} stores "
                f"{size}-d vectors but the encoder produces "
                f"{self.encoder.dimension}-d; recreate the collection or restore "
                "the previous EMBEDDING_MODEL"
            )

    def upsert(self, records: list[IndexRecord]) -> int:
        """Embed and store `records`, overwriting any point with the same id.

        Encoding *and* the Qdrant write are chunked at the configured batch
        size: a paper with hundreds of paragraphs must not put its whole
        vector set in one forward pass or one HTTP body. Point ids are
        deterministic, so a run interrupted part-way is completed — not
        duplicated — by re-running it.
        """
        if not records:
            return 0
        if getattr(self.settings, "embedding_sort_by_length", False):
            records = sorted(records, key=lambda record: len(record.text))
        self.ensure_collection()
        self.check_collection_dimension()
        batch_size = self.settings.embedding_batch_size
        for start in range(0, len(records), batch_size):
            batch = records[start : start + batch_size]
            vectors = self.encoder.encode([record.text for record in batch], batch_size)
            self.client.upsert(
                collection_name=self.collection,
                points=[
                    models.PointStruct(
                        id=record.record_id,
                        vector=vector,
                        payload=self.record_payload(record),
                    )
                    for record, vector in zip(batch, vectors, strict=True)
                ],
                wait=True,
            )
        return len(records)

    def embedding_version(self):
        return fingerprint(
            {"schema": 1, **settings_fingerprint(self.settings, ("embedding_",))}
        )

    def record_payload(self, record):
        return {
            **record.payload(),
            "text_sha256": fingerprint(record.text),
            "embed_version": self.embedding_version(),
        }

    def paper_payloads(self, paper_id):
        if not self.client.collection_exists(self.collection):
            return {}
        points, offset = {}, None
        while True:
            page, offset = self.client.scroll(
                collection_name=self.collection,
                scroll_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="paper_id", match=models.MatchValue(value=paper_id)
                        )
                    ]
                ),
                with_payload=True,
                with_vectors=False,
                limit=256,
                offset=offset,
            )
            points.update((str(p.id), dict(p.payload or {})) for p in page)
            if offset is None:
                return points

    def paper_current(self, paper_id, records):
        return self.paper_payloads(paper_id) == {
            r.record_id: self.record_payload(r) for r in records
        }

    def sync_paper(self, paper_id, records):
        """Reuse vectors only when text and model settings match exactly."""
        existing = self.paper_payloads(paper_id)
        changed = []
        for record in records:
            desired = self.record_payload(record)
            stored = existing.get(record.record_id, {})
            if any(
                stored.get(key) != desired[key]
                for key in ("text_sha256", "embed_version")
            ):
                changed.append(record)
            elif stored != desired:
                self.client.overwrite_payload(
                    collection_name=self.collection,
                    payload=desired,
                    points=[record.record_id],
                    wait=True,
                )
        written = self.upsert(changed)
        self.prune_paper(paper_id, [r.record_id for r in records])
        return written

    def retrieve(
        self,
        query: str,
        limit: int | None = None,
        filters: dict | None = None,
    ) -> list[dict]:
        """Semantic search, optionally narrowed by indexed metadata.

        `limit` and `filters` reach this from a user-facing question in Mode B,
        so both are validated here rather than trusted: an unknown filter key
        would silently match nothing, and an unbounded limit would pull an
        arbitrary slice of the corpus into memory.
        """
        limit = self._resolve_limit(limit)
        conditions = []
        for key, value in (filters or {}).items():
            if key not in PAYLOAD_FIELDS:
                raise ValueError(
                    f"cannot filter on {key!r}; indexed fields are "
                    f"{sorted(PAYLOAD_FIELDS)}"
                )
            conditions.append(
                models.FieldCondition(key=key, match=models.MatchValue(value=value))
            )
        if not self.has_evidence():
            return []
        vector = self.encoder.encode([query], 1)[0]
        result = self.client.query_points(
            collection_name=self.collection,
            query=vector,
            query_filter=models.Filter(must=conditions) if conditions else None,
            limit=limit,
            with_payload=True,
        )
        points = getattr(result, "points", result)
        return [
            {
                "id": str(point.id),
                "score": float(point.score),
                **dict(point.payload or {}),
            }
            for point in points
        ]

    def _resolve_limit(self, limit: int | None) -> int:
        if limit is None:
            limit = self.settings.retrieval_top_n
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise TypeError(f"limit must be an int, got {limit!r}")
        if limit <= 0:
            raise ValueError(f"limit must be positive, got {limit}")
        return min(limit, MAX_RETRIEVAL_LIMIT)

    def prune_paper(self, paper_id: int, keep_ids: list[str]) -> None:
        """Drop this paper's points that are no longer part of its evidence.

        Deterministic ids stop a re-run *creating* an orphan, but they cannot
        clear one already stored — by an earlier id scheme, or by a statement
        the corpus no longer contains after a re-parse or re-extraction. This
        does, scoped to a single paper whose records were just written
        successfully, so an unrelated failure can never delete good vectors.

        A failure propagates: without cleanup this paper is not a complete
        projection, even though its preceding upserts may already be durable.
        """
        if not self.client.collection_exists(self.collection):
            return
        self.client.delete(
            collection_name=self.collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="paper_id", match=models.MatchValue(value=paper_id)
                        )
                    ],
                    must_not=[models.HasIdCondition(has_id=list(keep_ids))]
                    if keep_ids
                    else [],
                )
            ),
            wait=True,
        )

    def gap_points(self) -> list[dict]:
        """Return all limitation/future-work points with their stored vectors.

        A read never creates the collection: doing so would load SPECTER2 just
        to size an empty collection nothing is about to be written to.
        """
        if not self.client.collection_exists(self.collection):
            return []
        points = []
        offset = None
        query_filter = models.Filter(
            should=[
                models.FieldCondition(
                    key="source_type", match=models.MatchValue(value=source_type)
                )
                for source_type in sorted(GAP_SOURCE_TYPES)
            ]
        )
        while True:
            page, offset = self.client.scroll(
                collection_name=self.collection,
                scroll_filter=query_filter,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            for point in page:
                points.append(
                    {
                        "id": str(point.id),
                        "vector": list(point.vector),
                        **dict(point.payload or {}),
                    }
                )
            if offset is None:
                return points


def _index_paper(
    session: Session,
    neo4j_session,
    index: EvidenceIndex,
    paper: Paper,
    stats: dict[str, int] | None = None,
) -> int:
    """Index one paper. Returns 0 when it is not ready for Stage 5 yet."""
    counts = stats if stats is not None else {}
    document = session.scalar(
        select(ParsedDocument).where(ParsedDocument.paper_id == paper.id)
    )
    if document is None:
        # Silent until O2: the stage summary now names why a paper was skipped.
        counts["skipped_no_document"] = counts.get("skipped_no_document", 0) + 1
        if current_run():
            current_run().paper(
                paper.id,
                "s5",
                fingerprint("no-document"),
                lambda: index.prune_paper(paper.id, []),
                verify=lambda: not index.paper_payloads(paper.id),
            )
        return 0
    # EMBED-03: without a Neo4j node there is nothing to link the vector back
    # to, so the paper waits for Phase 4 rather than being indexed unlinked.
    graph_node_id = get_paper_node_id(neo4j_session, paper.id)
    if graph_node_id is None:
        counts["skipped_no_graph_node"] = counts.get("skipped_no_graph_node", 0) + 1
        if current_run():
            raise RuntimeError(f"Paper {paper.id} has no graph projection")
        return 0
    counts["papers_ready"] = counts.get("papers_ready", 0) + 1
    facts = list(
        session.scalars(select(ExtractedFact).where(ExtractedFact.paper_id == paper.id))
    )
    records = build_index_records(paper, document, facts, graph_node_id)
    run = current_run()
    if run:
        digest = fingerprint([(r.record_id, index.record_payload(r)) for r in records])
        written = 0

        def sync():
            nonlocal written
            written = index.sync_paper(paper.id, records)

        run.paper(
            paper.id,
            "s5",
            digest,
            sync,
            verify=lambda: index.paper_current(paper.id, records),
        )
        return written
    written = index.upsert(records)
    # After the upsert, so a paper is never left with neither its old nor its
    # new vectors: this only removes what the current records do not cover.
    index.prune_paper(paper.id, [record.record_id for record in records])
    return written


def index_all(
    session: Session,
    neo4j_session,
    index: EvidenceIndex | None = None,
) -> int:
    index = index or EvidenceIndex()
    total = 0
    papers = list(session.scalars(select(Paper).order_by(Paper.id)))
    logger.info("s5 start papers=%d", len(papers))
    stats: dict[str, int] = {}
    failed = 0
    progress = ProgressLogger(logger, "s5", len(papers))
    for position, paper in enumerate(papers, 1):
        # One unparseable paper must not abandon a corpus-wide batch run
        # (README 13.6), the same contract app/ner_re/service.py holds.
        try:
            total += _index_paper(session, neo4j_session, index, paper, stats)
        except Exception:
            if current_run():
                raise
            failed += 1
            logger.exception("Indexing failed for paper %s; skipping", paper.id)
        finally:
            progress.update(position)
    logger.info(
        "s5 complete points=%d papers_ready=%d skipped_no_document=%d "
        "skipped_no_graph_node=%d failed=%d",
        total,
        stats.get("papers_ready", 0),
        stats.get("skipped_no_document", 0),
        stats.get("skipped_no_graph_node", 0),
        failed,
    )
    if total:
        # After the writes, not before: an empty corpus must not create a
        # collection, and creating one needs the encoder's dimension — which
        # would mean loading SPECTER2 just to index nothing.
        index.ensure_payload_indexes()
    return total

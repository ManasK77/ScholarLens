"""Qdrant indexing and semantic retrieval for corpus evidence."""

from uuid import NAMESPACE_URL, uuid5

from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.embeddings.encoder import Specter2Encoder, get_encoder
from app.embeddings.schema import IndexRecord
from app.extraction.storage import ExtractedFact
from app.ingestion.corpus import Paper
from app.ner_re.graph import get_paper_node_id
from app.parsing.storage import ParsedDocument


def _record_id(kind: str, paper_id: int, position: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"scholarlens:{kind}:{paper_id}:{position}"))


def build_index_records(
    paper: Paper,
    document: ParsedDocument,
    facts: list[ExtractedFact],
    graph_node_id: str,
) -> list[IndexRecord]:
    records = []
    for section_index, section in enumerate(document.sections):
        section_type = section.get("type")
        for paragraph_index, paragraph in enumerate(section.get("paragraphs", [])):
            text = " ".join(paragraph.get("text", "").split())
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
        if fact.fact_type not in {"limitation", "future_work"}:
            continue
        records.append(
            IndexRecord(
                _record_id("fact", paper.id, str(fact.id)),
                paper.id,
                fact.text,
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

    def upsert(self, records: list[IndexRecord]) -> int:
        if not records:
            return 0
        self.ensure_collection()
        vectors = self.encoder.encode(
            [record.text for record in records], self.settings.embedding_batch_size
        )
        self.client.upsert(
            collection_name=self.collection,
            points=[
                models.PointStruct(
                    id=record.record_id,
                    vector=vector,
                    payload=record.payload(),
                )
                for record, vector in zip(records, vectors, strict=True)
            ],
            wait=True,
        )
        return len(records)

    def retrieve(
        self,
        query: str,
        limit: int | None = None,
        filters: dict | None = None,
    ) -> list[dict]:
        if not self.has_evidence():
            return []
        vector = self.encoder.encode([query], 1)[0]
        conditions = []
        for key, value in (filters or {}).items():
            conditions.append(
                models.FieldCondition(key=key, match=models.MatchValue(value=value))
            )
        result = self.client.query_points(
            collection_name=self.collection,
            query=vector,
            query_filter=models.Filter(must=conditions) if conditions else None,
            limit=limit or self.settings.retrieval_top_n,
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

    def gap_points(self) -> list[dict]:
        """Return all limitation/future-work points with their stored vectors."""
        self.ensure_collection()
        points = []
        offset = None
        query_filter = models.Filter(
            should=[
                models.FieldCondition(
                    key="source_type", match=models.MatchValue(value="limitation")
                ),
                models.FieldCondition(
                    key="source_type", match=models.MatchValue(value="future_work")
                ),
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


def index_all(
    session: Session,
    neo4j_session,
    index: EvidenceIndex | None = None,
) -> int:
    index = index or EvidenceIndex()
    total = 0
    papers = session.scalars(select(Paper).order_by(Paper.id))
    for paper in papers:
        document = session.scalar(
            select(ParsedDocument).where(ParsedDocument.paper_id == paper.id)
        )
        if document is None:
            continue
        graph_node_id = get_paper_node_id(neo4j_session, paper.id)
        if graph_node_id is None:
            continue
        facts = list(
            session.scalars(
                select(ExtractedFact).where(ExtractedFact.paper_id == paper.id)
            )
        )
        total += index.upsert(
            build_index_records(paper, document, facts, graph_node_id)
        )
    return total

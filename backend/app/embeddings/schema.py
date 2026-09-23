"""Phase 5 vector-record contract (Stage 5).

`indexer.py` survived the incomplete upstream push (PROGRESS.md, 2026-09-06)
and is the authority on this module's shape. Two properties are load-bearing
and must not change:

* **`IndexRecord` is constructed positionally** with the nine fields below, in
  this order — `build_index_records` passes them that way.
* **`payload()` is handed straight to Qdrant** as a point payload, and
  `EvidenceIndex.retrieve` splats it beside its own `id` and `score` keys. So
  the payload may never contain `id` or `score`, and every key it *does* carry
  is a filterable metadata field (EMBED-02) or the Neo4j back-link (EMBED-03).

Records are the boundary between parsed corpus text (untrusted: it came out of
a PDF) and the vector store, so validation lives here rather than being
repeated at each call site.
"""

from dataclasses import dataclass

from app.core.pipeline import fingerprint

# The three kinds of text Stage 5 indexes. `paragraph` covers normalized
# section paragraphs; the other two are Phase 3 statements and are the subset
# Phase 6 clusters, which is why `gap_points()` filters on exactly them.
SOURCE_TYPES = frozenset({"paragraph", "limitation", "future_work"})
GAP_SOURCE_TYPES = frozenset({"limitation", "future_work"})

# Payload keys, in the order `payload()` builds them. Kept as an explicit tuple
# so a schema change here is visible to Phase 6/9/10 rather than implicit.
PAYLOAD_FIELDS = (
    "paper_id",
    "text",
    "source_type",
    "fact_id",
    "year",
    "venue",
    "section_type",
    "graph_node_id",
    "text_sha256",
    "embed_version",
)


def _require_text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text, got {value!r}")


@dataclass(frozen=True, slots=True)
class IndexRecord:
    """One embeddable unit of evidence.

    Positional field order is fixed by `indexer.build_index_records`:
    `record_id, paper_id, text, source_type, fact_id, year, venue,
    section_type, graph_node_id`.

    `year`, `venue` and `section_type` are optional because a manual upload can
    reach Stage 5 with metadata GROBID could not recover; `fact_id` is `None`
    for paragraph records, which have no Phase 3 row behind them.
    """

    record_id: str
    paper_id: int
    text: str
    source_type: str
    fact_id: int | None
    year: int | None
    venue: str | None
    section_type: str | None
    graph_node_id: str

    def __post_init__(self) -> None:
        _require_text(self.record_id, "IndexRecord.record_id")
        _require_text(self.text, "IndexRecord.text")
        # EMBED-03: a vector with no node to link back to is not evidence.
        _require_text(self.graph_node_id, "IndexRecord.graph_node_id")
        if self.source_type not in SOURCE_TYPES:
            raise ValueError(
                f"unknown source_type {self.source_type!r}; "
                f"expected one of {sorted(SOURCE_TYPES)}"
            )
        if not isinstance(self.paper_id, int) or isinstance(self.paper_id, bool):
            raise TypeError(
                f"IndexRecord.paper_id must be an int, got {self.paper_id!r}"
            )
        if self.paper_id < 0:
            raise ValueError(
                f"IndexRecord.paper_id must be non-negative, got {self.paper_id}"
            )

    def payload(self) -> dict:
        """The Qdrant point payload — a fresh dict per call, never shared."""
        return {
            "paper_id": self.paper_id,
            "text": self.text,
            "source_type": self.source_type,
            "fact_id": self.fact_id,
            "year": self.year,
            "venue": self.venue,
            "section_type": self.section_type,
            "graph_node_id": self.graph_node_id,
            "text_sha256": fingerprint(self.text),
            "embed_version": None,  # Filled by EvidenceIndex with model settings.
        }

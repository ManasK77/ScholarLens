from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IndexRecord:
    record_id: str
    paper_id: int
    text: str
    source_type: str
    source_id: int | None
    year: int | None
    venue: str | None
    section_type: str | None
    graph_node_id: str

    def payload(self) -> dict:
        return {
            "record_id": self.record_id,
            "paper_id": self.paper_id,
            "text": self.text,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "year": self.year,
            "venue": self.venue,
            "section_type": self.section_type,
            "graph_node_id": self.graph_node_id,
        }

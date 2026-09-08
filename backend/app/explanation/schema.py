"""Stage 9's two contracts: what the LLM may return, and what gets stored.

The LLM is the only component in the pipeline whose output nobody controls, and
Phase 9 is where it first reaches a database row and the dashboard. So both
boundaries are validated here rather than trusted at the call site:

* `ReportDraft` is the structured response the provider is asked for. Anything
  outside it — a confidence of 4, an empty citation list, a chatty `reasoning`
  key — is rejected or dropped before `service.py` sees it.
* `GapReportRecord` is what `storage.replace_reports` accepts. A record with no
  evidence cannot exist, which is the type-level form of the phase's core
  claim: a stored report always references real retrieved evidence.

`evidence_from_point` sits between them, projecting a Qdrant payload onto the
`Evidence` interface `frontend/src/types.ts` declares — six fields, no more.
The retrieval `score` and the Neo4j `graph_node_id` are deliberately not
carried through: they are retrieval detail, and the dashboard renders neither.
"""

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Per-evidence character cap. README §15 calls out context hygiene for this
# phase specifically: `report_evidence_top_n` paragraphs go into one prompt, and
# a pathological paragraph out of a bad PDF must not crowd out the rest.
MAX_EVIDENCE_CHARS = 1200

# The keys `evidence_from_point` emits, in the order `types.ts` declares them.
EVIDENCE_FIELDS = ("id", "paper_id", "title", "text", "year", "source_type")


class ReportDraft(BaseModel):
    """The single-pass structured response, exactly as README §8 sketches it.

    `confidence` is a number rather than README's illustrative "High":
    `frontend/src/types.ts` types it `number` and `App.tsx` renders
    `Math.round(confidence * 100)`, and that consumer is frozen.

    `evidence_ids` is what makes grounding checkable. Every id has to come back
    out of the evidence block the prompt supplied; `service.py` discards the
    ones that do not, and drops the report if none survive.
    """

    # A provider may add its own keys (`reasoning`, `thoughts`); ignore them
    # rather than fail a report that is otherwise complete.
    model_config = ConfigDict(extra="ignore")

    gap: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(ge=0.0, le=1.0)
    suggested_directions: list[str] = Field(min_length=1, max_length=10)
    evidence_ids: list[str] = Field(min_length=1)

    @field_validator("gap")
    @classmethod
    def _reject_blank_gap(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("gap must not be blank")
        return value.strip()

    @field_validator("suggested_directions")
    @classmethod
    def _drop_blank_directions(cls, value: list[str]) -> list[str]:
        directions = [item.strip() for item in value if item and item.strip()]
        if not directions:
            raise ValueError("suggested_directions must contain at least one direction")
        return directions


# Handed verbatim to the provider as Groq's `response_format.json_schema`.
# Derived from the model so the two cannot drift, with the pydantic-only keys
# stripped: `title` and `$defs` are documentation, not constraints, and a
# provider's schema validator is stricter about what it accepts than JSON
# Schema requires.
REPORT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "gap": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "suggested_directions": {"type": "array", "items": {"type": "string"}},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["gap", "confidence", "suggested_directions", "evidence_ids"],
}


def evidence_from_point(point: dict, title: str | None) -> dict:
    """Project one retrieved Qdrant point onto the stored `Evidence` shape.

    `title` is passed in rather than looked up here: it lives in Postgres, and
    `service.py` reads every needed title in one query instead of one per point.
    `None` is a legitimate value — the vector store can hold a point whose paper
    the corpus no longer has, and `App.tsx` falls back to `Paper {paper_id}`.
    """
    paper_id = point.get("paper_id")
    if not isinstance(paper_id, int) or isinstance(paper_id, bool):
        # `TypeError` for a wrong type, matching `IndexRecord.__post_init__`,
        # which validates the same field on the way *into* the vector store.
        raise TypeError(f"evidence point has no usable paper_id: {paper_id!r}")

    text = point.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"evidence point {point.get('id')!r} has no text")

    return {
        "id": str(point["id"]),
        "paper_id": paper_id,
        "title": title,
        "text": text.strip()[:MAX_EVIDENCE_CHARS],
        "year": point.get("year"),
        "source_type": point.get("source_type"),
    }


@dataclass(frozen=True, slots=True)
class GapReportRecord:
    """One cluster's finished report, ready for `storage.replace_reports`.

    Constructed only by `service.py`, after the grounding check. The validation
    below is what makes an ungrounded report unrepresentable rather than merely
    discouraged.
    """

    cluster_id: int
    gap: str
    evidence: list[dict] = field(default_factory=list)
    confidence: float = 0.0
    suggested_directions: list[str] = field(default_factory=list)
    provider: str = ""
    model: str = ""

    def __post_init__(self) -> None:
        if not self.gap or not self.gap.strip():
            raise ValueError("GapReportRecord.gap must not be blank")
        if not self.evidence:
            raise ValueError(
                "GapReportRecord.evidence must not be empty — a report with no "
                "retrieved evidence behind it is an opinion, not a finding"
            )
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"GapReportRecord.confidence must be in [0, 1], got {self.confidence}"
            )

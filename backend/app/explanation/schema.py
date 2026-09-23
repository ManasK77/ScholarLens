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
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Per-evidence character cap. README §15 calls out context hygiene for this
# phase specifically: `report_evidence_top_n` paragraphs go into one prompt, and
# a pathological paragraph out of a bad PDF must not crowd out the rest.
MAX_EVIDENCE_CHARS = 1200

# The keys `evidence_from_point` emits, in the order `types.ts` declares them.
EVIDENCE_FIELDS = ("id", "paper_id", "title", "text", "year", "source_type")


class GroundedClaim(BaseModel):
    """One of the three things a gap claim rests on, with its citations.

    Stage 9 asks for three separately because they are three different
    findings and the corpus can support one without the others:

    * **(A)** the problem exists;
    * **(B)** existing solutions are insufficient or incomplete;
    * **(C)** a specific research gap remains.

    The report that motivated this asserted all three -- "a lack of effective,
    adaptive defenses and inconsistent findings across studies" -- from
    evidence for (A) alone. "Prompt injection remains common" is not a reason
    to believe "adaptive defenses are a research gap"; it is a reason to
    believe prompt injection remains common.

    `supported` is not taken at face value. A claim asserted with no citation
    is downgraded here, before `service.py` sees it, so "the model said so" can
    never become "the corpus says so" (D-09-05).
    """

    model_config = ConfigDict(extra="ignore")

    supported: bool = False
    statement: str = ""
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _support_requires_citation(self) -> "GroundedClaim":
        if self.supported and not self.evidence_ids:
            object.__setattr__(self, "supported", False)
        return self

    def grounded(self, retrieved_ids: set[str]) -> "GroundedClaim":
        """The same claim with invented citations removed.

        A claim whose citations all turn out to be invented is not supported,
        whatever it says about itself. This is the per-claim form of the check
        `service._grounded_evidence` already runs over the report as a whole.
        """
        kept = [item for item in self.evidence_ids if item in retrieved_ids]
        return GroundedClaim(
            supported=self.supported and bool(kept),
            statement=self.statement,
            evidence_ids=kept,
        )


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

    # The three-part justification (D-09-05). All four default to an
    # unsupported claim: a provider that omits them, or an older stored
    # response replayed through this model, must read as "not established"
    # rather than as "all of it holds".
    problem_evidence: GroundedClaim = Field(default_factory=GroundedClaim)
    solution_insufficiency_evidence: GroundedClaim = Field(
        default_factory=GroundedClaim
    )
    research_gap_evidence: GroundedClaim = Field(default_factory=GroundedClaim)
    conflicting_evidence: GroundedClaim = Field(default_factory=GroundedClaim)

    # ClassVar, not a field: it names the four claim fields for callers
    # that iterate them, and pydantic would otherwise try to validate it.
    CLAIM_FIELDS: ClassVar[tuple[str, ...]] = (
        "problem_evidence",
        "solution_insufficiency_evidence",
        "research_gap_evidence",
        "conflicting_evidence",
    )

    def justification(self, retrieved_ids: set[str] | None = None) -> dict:
        """The four claims as a stored dict, optionally grounded first.

        Stored alongside the prose rather than folded into it, so a reader --
        and the dashboard -- can see which parts of a gap statement the corpus
        actually carried and which the model asserted on its own.
        """
        stored = {}
        for name in self.CLAIM_FIELDS:
            claim: GroundedClaim = getattr(self, name)
            if retrieved_ids is not None:
                claim = claim.grounded(retrieved_ids)
            stored[name] = claim.model_dump()
        return stored

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
_CLAIM_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "supported": {"type": "boolean"},
        "statement": {"type": "string"},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["supported", "statement", "evidence_ids"],
}

REPORT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "gap": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "suggested_directions": {"type": "array", "items": {"type": "string"}},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "problem_evidence": _CLAIM_SCHEMA,
        "solution_insufficiency_evidence": _CLAIM_SCHEMA,
        "research_gap_evidence": _CLAIM_SCHEMA,
        "conflicting_evidence": _CLAIM_SCHEMA,
    },
    "required": [
        "gap",
        "confidence",
        "suggested_directions",
        "evidence_ids",
        "problem_evidence",
        "solution_insufficiency_evidence",
        "research_gap_evidence",
        "conflicting_evidence",
    ],
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
    # The grounded A/B/C justification (D-09-05). Empty for a report stored
    # before this existed, which `GET /reports` serves as-is.
    justification: dict = field(default_factory=dict)

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

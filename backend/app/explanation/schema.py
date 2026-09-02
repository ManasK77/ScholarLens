from pydantic import BaseModel, Field


class GapReportDraft(BaseModel):
    gap: str = Field(description="Concise description of the unresolved research gap")
    evidence_ids: list[str] = Field(
        min_length=1,
        description="Only IDs from the supplied evidence list that support the gap",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    suggested_directions: list[str]


GAP_REPORT_SCHEMA = GapReportDraft.model_json_schema()

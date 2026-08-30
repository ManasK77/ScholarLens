"""Structured, evidence-preserving output contract for scientific IE."""

from dataclasses import dataclass
from enum import Enum


class FactType(str, Enum):
    CONTRIBUTION = "contribution"
    LIMITATION = "limitation"
    FUTURE_WORK = "future_work"
    DATASET = "dataset"
    MODEL = "model"
    METRIC = "metric"


@dataclass(frozen=True, slots=True)
class SemanticMatch:
    fact_type: FactType
    confidence: float


@dataclass(frozen=True, slots=True)
class ExtractedStatement:
    fact_type: FactType
    text: str
    section_type: str | None
    section_heading: str | None
    section_index: int
    paragraph_index: int
    sentence_index: int
    confidence: float
    extraction_method: str

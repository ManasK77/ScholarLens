"""Typed contracts for joint scientific entity and relation extraction."""

from dataclasses import dataclass, field

ENTITY_TYPES = ("model", "dataset", "task", "metric", "domain")
RELATION_ENDPOINTS = {
    "trained_on": ("model", "dataset"),
    "evaluated_on": ("model", "dataset"),
    "evaluated_using": ("model", "metric"),
    "performs_task": ("model", "task"),
    "fails_on": ("model", "domain"),
    "achieves": ("model", "metric"),
    "addresses": ("model", "domain"),
    "uses_metric": ("task", "metric"),
}


@dataclass(frozen=True, slots=True)
class EntityMention:
    text: str
    entity_type: str
    confidence: float | None = None


@dataclass(frozen=True, slots=True)
class RelationMention:
    head: str
    relation_type: str
    tail: str
    confidence: float | None = None


@dataclass(slots=True)
class JointExtractionResult:
    entities: list[EntityMention] = field(default_factory=list)
    relations: list[RelationMention] = field(default_factory=list)

"""Phase 4 joint entity + relation extraction contract (Stage 4).

`graph.py` writes whatever lands here straight into Neo4j, so this module is the
single place where the vocabulary is fixed. Two consumers depend on its exact
shape and must not be broken:

* `graph.py` derives `RELATION_LABELS = {name: name.upper() ...}` from
  `RELATION_ENDPOINTS` and **interpolates the label directly into Cypher**, then
  unpacks `head_type, tail_type = RELATION_ENDPOINTS[...]` to MATCH the endpoint
  nodes. Endpoints are therefore always a 2-tuple, and relation names are
  restricted to lowercase ASCII words so the interpolation can never carry
  anything but `[A-Z_]`.
* `EntityMention` / `RelationMention` are constructed positionally.

Relation endpoints come from README §8 Stage 4's own worked examples
(`BERT —trained_on→ SQuAD`, `GPT-4 —evaluated_using→ GSM8K`,
`BERT —fails_on→ Low-resource Languages`) and Stage 3's `Contribution → SOTA
accuracy`.

`task` is a first-class entity type and reaches the graph through
`Paper-[:MENTIONS]->Entity`, but none of the four fixed relations targets it.
README's "etc." leaves room for task-bearing relations later; the MVP does not
invent them.
"""

from dataclasses import dataclass, field

# Descriptions are sent to the model with the schema. They are not cosmetic:
# measured against the real checkpoint, "GSM8K" is typed `metric` without them
# and `dataset` with them, which is the difference between an `evaluated_using`
# edge existing or not. ENTITY_TYPES derives from these keys so the two can
# never drift apart.
ENTITY_DESCRIPTIONS: dict[str, str] = {
    "model": (
        "a named machine learning model, architecture, or system (BERT, GPT-4, ResNet)"
    ),
    "dataset": (
        "a named dataset, corpus, or evaluation benchmark (SQuAD, ImageNet, GSM8K)"
    ),
    "task": (
        "a named research task or problem "
        "(question answering, prompt injection detection)"
    ),
    "metric": "an evaluation measure or score (accuracy, F1, BLEU, perplexity)",
    "domain": (
        "an application area, language, or setting (low-resource languages, healthcare)"
    ),
}

ENTITY_TYPES = frozenset(ENTITY_DESCRIPTIONS)

RELATION_ENDPOINTS: dict[str, tuple[str, str]] = {
    "trained_on": ("model", "dataset"),
    "evaluated_using": ("model", "dataset"),
    "fails_on": ("model", "domain"),
    "achieves": ("model", "metric"),
}


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text, got {value!r}")


@dataclass(frozen=True, slots=True)
class EntityMention:
    """One typed entity mention. Positional: text, entity_type, confidence.

    `evidence` names the Stage 4 input units the mention occurs in (`abstract`
    or `statement:<section>.<paragraph>.<sentence>`); empty until the service
    attaches it (D-S4-PROV-01).
    """

    text: str
    entity_type: str
    confidence: float
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.text, "EntityMention.text")
        if self.entity_type not in ENTITY_TYPES:
            raise ValueError(
                f"unknown entity_type {self.entity_type!r}; "
                f"expected one of {sorted(ENTITY_TYPES)}"
            )


@dataclass(frozen=True, slots=True)
class RelationMention:
    """One typed relation. Positional: head, relation_type, tail, confidence.

    `head`/`tail` carry entity **text**, not ids — `graph.py` casefolds them to
    build the `normalized_name` it MATCHes on.

    `evidence` names the input units holding *both* endpoints; when no single
    unit does, `cross_sentence` is set and `evidence` names the units holding
    either one, so a reader can see the model linked them across sentences.
    """

    head: str
    relation_type: str
    tail: str
    confidence: float
    evidence: tuple[str, ...] = ()
    cross_sentence: bool = False

    def __post_init__(self) -> None:
        _require_text(self.head, "RelationMention.head")
        _require_text(self.tail, "RelationMention.tail")
        if self.relation_type not in RELATION_ENDPOINTS:
            raise ValueError(
                f"unknown relation_type {self.relation_type!r}; "
                f"expected one of {sorted(RELATION_ENDPOINTS)}"
            )


@dataclass(slots=True)
class JointExtractionResult:
    """Everything one paper's joint pass produced."""

    entities: list[EntityMention] = field(default_factory=list)
    relations: list[RelationMention] = field(default_factory=list)

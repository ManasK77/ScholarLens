"""Lazy GLiNER2.5 joint entity/relation extraction.

GLiNER2's JointIE decoder extracts typed entity spans and their constrained
relations in one model pass. The adapter below deliberately accepts several
minor result-shape variations so checkpoint/library upgrades remain isolated
from the rest of ScholarLens.
"""

from functools import lru_cache
from typing import Any

from app.core.config import get_settings
from app.ner_re.schema import (
    ENTITY_TYPES,
    RELATION_ENDPOINTS,
    EntityMention,
    JointExtractionResult,
    RelationMention,
)


def _value(item: Any, key: str, default=None):
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _span_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return str(_value(value, "text", _value(value, "value", ""))).strip()


def _confidence(value: Any) -> float | None:
    score = _value(value, "confidence", _value(value, "score"))
    return float(score) if score is not None else None


def normalize_joint_result(raw: Any) -> JointExtractionResult:
    """Normalize GLiNER2 dict/object output to ScholarLens's stable contract."""
    if not isinstance(raw, dict) and hasattr(raw, "to_dict"):
        try:
            raw = raw.to_dict(include_text=True)
        except TypeError:
            raw = raw.to_dict()

    entities: list[EntityMention] = []
    entity_by_id: dict[str, EntityMention] = {}
    raw_entities = _value(raw, "entities", {}) or {}
    if isinstance(raw_entities, dict):
        entity_groups = raw_entities.items()
    else:
        entity_groups = (
            (
                _value(
                    item,
                    "type",
                    _value(item, "label", _value(item, "entity_type", "unknown")),
                ),
                [item],
            )
            for item in raw_entities
        )
    for entity_type, values in entity_groups:
        for value in values:
            text = _span_text(value)
            if text and str(entity_type).lower() in ENTITY_TYPES:
                mention = EntityMention(
                    text, str(entity_type).lower(), _confidence(value)
                )
                entities.append(mention)
                entity_id = _value(value, "id")
                if entity_id is not None:
                    entity_by_id[str(entity_id)] = mention

    relations: list[RelationMention] = []
    raw_relations = (
        _value(raw, "relation_extraction", _value(raw, "relations", {})) or {}
    )
    if isinstance(raw_relations, dict):
        relation_groups = raw_relations.items()
    else:
        relation_groups = (
            (
                _value(
                    item,
                    "type",
                    _value(item, "label", _value(item, "relation_type", "unknown")),
                ),
                [item],
            )
            for item in raw_relations
        )
    for relation_type, values in relation_groups:
        relation_type = str(relation_type).lower()
        if relation_type not in RELATION_ENDPOINTS:
            continue
        for value in values:
            if isinstance(value, (tuple, list)) and len(value) >= 2:
                head, tail = value[0], value[1]
                score = None
            else:
                head = _value(value, "head", _value(value, "source"))
                tail = _value(value, "tail", _value(value, "target"))
                score = _confidence(value)
            head_mention = entity_by_id.get(str(head))
            tail_mention = entity_by_id.get(str(tail))
            head_text = head_mention.text if head_mention else _span_text(head)
            tail_text = tail_mention.text if tail_mention else _span_text(tail)
            if head_text and tail_text:
                relations.append(
                    RelationMention(head_text, relation_type, tail_text, score)
                )

    # Joint decoders may return relation endpoints without repeating them in
    # the entity list. Materialize constrained endpoints so graph writes never
    # create untyped nodes.
    known = {(entity.text.casefold(), entity.entity_type) for entity in entities}
    for relation in relations:
        head_type, tail_type = RELATION_ENDPOINTS[relation.relation_type]
        for text, entity_type in (
            (relation.head, head_type),
            (relation.tail, tail_type),
        ):
            key = (text.casefold(), entity_type)
            if key not in known:
                entities.append(EntityMention(text, entity_type))
                known.add(key)
    return JointExtractionResult(entities, relations)


class JointScientificExtractor:
    def __init__(self, model_name: str) -> None:
        from gliner2.joint_ie import JointIE, JointIEConfig

        self._config_type = JointIEConfig
        self._model = JointIE.from_pretrained(model_name)
        schema = self._model.create_schema().entities(list(ENTITY_TYPES))
        for relation, (head, tail) in RELATION_ENDPOINTS.items():
            schema = schema.relation(relation, head, tail)
        self._schema = schema

    def extract(self, text: str) -> JointExtractionResult:
        raw = self._model.extract(
            text,
            self._schema,
            config=self._config_type(optimizer="beam", beam_size=32),
        )
        return normalize_joint_result(raw)


@lru_cache(maxsize=1)
def get_joint_extractor() -> JointScientificExtractor:
    return JointScientificExtractor(get_settings().joint_ie_model)

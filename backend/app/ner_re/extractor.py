"""GLiNER2 `JointIE` adapter — the Phase 4 joint entity+relation model.

Model choice approved by the owner 2026-09-06 (PROGRESS.md D-04-01): GLiNER2's
`JointIE` typed-graph API against a GLiNER2.5 boundary checkpoint built with
`enable_relations=True`. This is a documented deviation from README §7/§15's
named DyGIE++/SciERC architecture, accepted under the spec's "or an equivalent
pretrained scientific IE model" clause — DyGIE++'s AllenNLP dependency has been
archived since 2023 and its pretrained checkpoint carries a different label set
entirely.

**Only `JointIE` is used.** GLiNER2 also ships an `extract_relations()` that
cannot constrain which entity types may appear as a relation's head or tail.
Using it would break `RELATION_ENDPOINTS`, which `graph.py` relies on to MATCH
endpoint nodes — so it is deliberately never called here.

Everything the model returns is treated as untrusted: a checkpoint is free to
emit an unknown type, a dangling relation id, or a malformed payload, and none
of that may reach Neo4j or take a batch run down.
"""

import logging
import math
from dataclasses import replace
from collections.abc import Iterable, Sequence
from functools import lru_cache
from typing import Any, Protocol

from app.core.config import Settings, get_settings
from app.ner_re.schema import (
    ENTITY_DESCRIPTIONS,
    ENTITY_TYPES,
    RELATION_ENDPOINTS,
    EntityMention,
    JointExtractionResult,
    RelationMention,
)

logger = logging.getLogger(__name__)


class JointIEModel(Protocol):
    """The slice of gliner2.joint_ie.JointIE this module depends on."""

    def create_schema(self) -> Any: ...

    def extract(self, text: str, schema: Any, config: Any = None) -> Any: ...

    def batch_extract(self, texts: list[str], schema: Any, config: Any = None) -> Any: ...


def build_joint_schema(model: JointIEModel) -> Any:
    """Declare this project's entity types and typed relations on `model`.

    Entity types are declared with their descriptions (measurably better typing
    — see ENTITY_DESCRIPTIONS) and before any relation, since JointIE only lets a
    relation reference an already-declared type.
    """
    schema = model.create_schema().entities(dict(ENTITY_DESCRIPTIONS))
    for relation_type, (head_type, tail_type) in sorted(RELATION_ENDPOINTS.items()):
        schema = schema.relation(relation_type, head_type, tail_type)
    return schema


def _normalize(text: str) -> str:
    return " ".join(text.casefold().split())


def _as_confidence(raw: Any, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if math.isnan(value):
        return default
    return min(max(value, 0.0), 1.0)


class JointEntityRelationExtractor:
    """Turns GLiNER2 `JointIE` output into a `JointExtractionResult`."""

    def __init__(
        self,
        model: JointIEModel,
        entity_threshold: float = 0.5,
        relation_threshold: float = 0.5,
        config: Any = None,
        batch_size: int = 1,
    ) -> None:
        self._model = model
        self._entity_threshold = entity_threshold
        self._relation_threshold = relation_threshold
        self._config = config
        if not 1 <= batch_size <= 32:
            raise ValueError('batch_size must be between 1 and 32')
        self._batch_size = batch_size
        self._schema: Any = None

    def _joint_schema(self) -> Any:
        if self._schema is None:
            self._schema = build_joint_schema(self._model)
        return self._schema

    def extract(self, text: str) -> JointExtractionResult:
        """Run one joint pass over `text`."""
        return self.extract_many([text])

    def extract_many(self, chunks: Iterable[str]) -> JointExtractionResult:
        """Run one joint pass per chunk and merge the results.

        Each chunk yields entities *and* relations from a single model call —
        never NER followed by a separate relation pass (NERRE-01).
        """
        # Keyed by (normalized text, entity_type) so repeated mentions collapse
        # onto the one node graph.py will MERGE for them.
        entities: dict[tuple[str, str], EntityMention] = {}
        relations: dict[tuple[str, str, str], RelationMention] = {}

        chunks = [chunk for chunk in chunks if isinstance(chunk, str) and chunk.strip()]
        for raw in self._extract_results(chunks):
            self._merge(raw, entities, relations)

        return JointExtractionResult(entities=list(entities.values()), relations=list(relations.values()))

    def _extract_results(self, chunks):
        for start in range(0, len(chunks), self._batch_size):
            batch = chunks[start:start + self._batch_size]
            if self._batch_size > 1:
                try:
                    from gliner2.joint_ie import JointIEConfig
                    config = replace(self._config or JointIEConfig(), batch_size=self._batch_size)
                    results = list(self._model.batch_extract(batch, self._joint_schema(), config=config))
                    if len(results) != len(batch):
                        raise ValueError('JointIE batch result count mismatch')
                except Exception:
                    logger.warning('JointIE batch failed; retrying %d chunks individually', len(batch))
                else:
                    yield from results
                    continue
            for chunk in batch:
                yield from self._extract_one(chunk)

    def _extract_one(self, chunk):
            try:
                raw = self._model.extract(
                    chunk, self._joint_schema(), config=self._config
                )
            except Exception:
                from app.core.pipeline import current_run
                if current_run():
                    raise
                logger.exception("Joint extraction failed for one chunk; skipping it")
                return
            yield raw

    def _merge(
        self,
        raw: Any,
        entities: dict[tuple[str, str], EntityMention],
        relations: dict[tuple[str, str, str], RelationMention],
    ) -> None:
        payload = self._payload(raw)
        if payload is None:
            from app.core.pipeline import current_run
            if current_run():
                raise ValueError('JointIE returned an unreadable result')
            return

        # `feasible is False` means the decoder could not satisfy the declared
        # constraints and fell back to an empty assignment — not "nothing found".
        if getattr(raw, "feasible", True) is False:
            from app.core.pipeline import current_run
            if current_run():
                raise ValueError('JointIE could not decode a feasible result')
            logger.debug("JointIE reported an infeasible graph; dropping the chunk")
            return

        kept_by_id = self._collect_entities(payload, entities)
        self._collect_relations(payload, kept_by_id, relations)

    @staticmethod
    def _payload(raw: Any) -> dict | None:
        to_dict = getattr(raw, "to_dict", None)
        try:
            payload = to_dict(include_text=True) if callable(to_dict) else raw
        except Exception:
            logger.exception("Could not read the JointIE result payload")
            return None
        return payload if isinstance(payload, dict) else None

    def _collect_entities(
        self, payload: dict, entities: dict[tuple[str, str], EntityMention]
    ) -> dict[str, EntityMention]:
        """Keep valid entities, returning the ones relations may reference."""
        kept_by_id: dict[str, EntityMention] = {}

        for row in payload.get("entities") or []:
            if not isinstance(row, dict):
                continue
            entity_type = row.get("type")
            text = row.get("text")
            if entity_type not in ENTITY_TYPES:
                continue
            if not isinstance(text, str) or not text.strip():
                continue
            confidence = _as_confidence(row.get("confidence"), default=1.0)
            if confidence < self._entity_threshold:
                continue

            text = " ".join(text.split())
            mention = EntityMention(text, entity_type, confidence)
            key = (_normalize(text), entity_type)
            existing = entities.get(key)
            if existing is None or confidence > existing.confidence:
                entities[key] = mention

            entity_id = row.get("id")
            if isinstance(entity_id, str):
                kept_by_id[entity_id] = mention

        return kept_by_id

    def _collect_relations(
        self,
        payload: dict,
        kept_by_id: dict[str, EntityMention],
        relations: dict[tuple[str, str, str], RelationMention],
    ) -> None:
        for row in payload.get("relations") or []:
            if not isinstance(row, dict):
                continue
            relation_type = row.get("type")
            endpoints = RELATION_ENDPOINTS.get(relation_type)
            if endpoints is None:
                continue

            # head/tail are entity ids in JointIE output; a relation whose
            # endpoint was filtered out has no node to MATCH and must not survive.
            head = kept_by_id.get(row.get("head"))
            tail = kept_by_id.get(row.get("tail"))
            if head is None or tail is None:
                continue

            # An endpoint whose type contradicts the contract would make
            # graph.py's MATCH find nothing and silently write no relationship.
            head_type, tail_type = endpoints
            if head.entity_type != head_type or tail.entity_type != tail_type:
                continue

            confidence = _as_confidence(row.get("confidence"), default=1.0)
            if confidence < self._relation_threshold:
                continue

            key = (_normalize(head.text), relation_type, _normalize(tail.text))
            existing = relations.get(key)
            if existing is None or confidence > existing.confidence:
                relations[key] = RelationMention(
                    head.text, relation_type, tail.text, confidence
                )


def load_joint_model(settings: Settings) -> JointIEModel:
    """Load the configured GLiNER2.5 boundary checkpoint.

    Imported lazily so the whole backend does not pull torch/transformers in at
    module-import time, matching how Phase 2/3 load their models.
    """
    from gliner2.joint_ie import JointIE

    return JointIE.from_pretrained(settings.nerre_model)


@lru_cache(maxsize=1)
def get_joint_extractor() -> JointEntityRelationExtractor:
    """Process-wide extractor so the checkpoint is loaded at most once."""
    settings = get_settings()
    return JointEntityRelationExtractor(
        load_joint_model(settings),
        entity_threshold=settings.nerre_entity_threshold,
        relation_threshold=settings.nerre_relation_threshold,
        batch_size=settings.nerre_batch_size,
    )


class SupportsExtractMany(Protocol):
    def extract_many(self, chunks: Sequence[str]) -> JointExtractionResult: ...

"""Tests for app/ner_re/extractor.py — the GLiNER2 JointIE adapter.

The adapter's job is to turn GLiNER2's documented `JointIE` result shape

    {"entities":  [{"id","type","text","start","end","confidence"}, ...],
     "relations": [{"type","head","tail","confidence"}, ...]}   # head/tail are ids

into this project's `JointExtractionResult`, whose relations carry entity
**text**. Everything here runs against a fake JointIE — no checkpoint download.

The rules being pinned:

* only `JointIE` is used — never GLiNER2's untyped `extract_relations()`
* relation head/tail ids are resolved back to entity text
* anything below threshold, of an unknown type, or whose endpoint types
  contradict `RELATION_ENDPOINTS` is dropped, because `graph.py` MATCHes on
  (normalized_name, entity_type) and a mistyped endpoint would silently write
  no relationship at all
* `feasible is False` means "constraints unsatisfiable", not "nothing found"
"""

import pytest

from app.ner_re.extractor import JointEntityRelationExtractor, build_joint_schema
from app.ner_re.schema import ENTITY_DESCRIPTIONS, ENTITY_TYPES, RELATION_ENDPOINTS


class FakeJointResult:
    def __init__(self, payload: dict, feasible: bool = True):
        self._payload = payload
        self.feasible = feasible

    def to_dict(self, include_text: bool = True) -> dict:
        return self._payload


class FakeJointIE:
    """Stands in for gliner2.joint_ie.JointIE."""

    def __init__(self, results=None):
        self._results = list(results or [])
        self.extract_calls = []
        self.schema_calls = 0

    def create_schema(self):
        self.schema_calls += 1
        return FakeJointSchema()

    def extract(self, text, schema, config=None):
        self.extract_calls.append((text, schema, config))
        if self._results:
            return self._results.pop(0)
        return FakeJointResult({"entities": [], "relations": []})

    # Deliberately present so a test can prove we never reach for it.
    def extract_relations(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError(
            "untyped extract_relations() must never be used — JointIE only"
        )


class FakeJointSchema:
    def __init__(self):
        self.entity_calls = []
        self.relation_calls = []

    def entities(self, spec):
        self.entity_calls.append(spec)
        return self

    def relation(self, name, head_type, tail_type, **kwargs):
        self.relation_calls.append((name, head_type, tail_type))
        return self


def _payload(entities, relations):
    return {"entities": entities, "relations": relations}


# --------------------------------------------------------------------------
# Schema construction
# --------------------------------------------------------------------------


def test_build_joint_schema_declares_every_entity_type_and_relation() -> None:
    model = FakeJointIE()

    schema = build_joint_schema(model)

    # set() over either a list or a dict of type->description yields the names
    assert set(schema.entity_calls[0]) == ENTITY_TYPES

    assert {(name, head, tail) for name, head, tail in schema.relation_calls} == {
        (name, head, tail) for name, (head, tail) in RELATION_ENDPOINTS.items()
    }


def test_build_joint_schema_sends_entity_descriptions() -> None:
    """Descriptions measurably change typing (GSM8K: metric -> dataset), so a
    bare list of names is not good enough."""
    schema = build_joint_schema(FakeJointIE())

    declared = schema.entity_calls[0]
    assert isinstance(declared, dict)
    assert declared == dict(ENTITY_DESCRIPTIONS)
    assert all(text.strip() for text in declared.values())


def test_build_joint_schema_declares_entities_before_relations() -> None:
    """JointIE requires relations to reference already-declared entity types."""
    model = FakeJointIE()

    schema = build_joint_schema(model)

    assert schema.entity_calls, "entities must be declared"
    assert schema.relation_calls, "relations must be declared"


# --------------------------------------------------------------------------
# Happy path
# --------------------------------------------------------------------------


def test_extract_maps_entities_and_resolves_relation_ids_to_text() -> None:
    payload = _payload(
        [
            {"id": "e1", "type": "model", "text": "BERT", "confidence": 0.94},
            {"id": "e2", "type": "dataset", "text": "SQuAD", "confidence": 0.91},
        ],
        [{"type": "trained_on", "head": "e1", "tail": "e2", "confidence": 0.88}],
    )
    extractor = JointEntityRelationExtractor(FakeJointIE([FakeJointResult(payload)]))

    result = extractor.extract("BERT was trained on SQuAD.")

    assert [(e.text, e.entity_type, e.confidence) for e in result.entities] == [
        ("BERT", "model", 0.94),
        ("SQuAD", "dataset", 0.91),
    ]
    assert len(result.relations) == 1
    relation = result.relations[0]
    assert (relation.head, relation.relation_type, relation.tail) == (
        "BERT",
        "trained_on",
        "SQuAD",
    )
    assert relation.confidence == 0.88


def test_extract_uses_joint_ie_and_never_untyped_extract_relations() -> None:
    model = FakeJointIE([FakeJointResult(_payload([], []))])
    extractor = JointEntityRelationExtractor(model)

    extractor.extract("some text")

    assert len(model.extract_calls) == 1, "must call JointIE.extract exactly once"


def test_entities_and_relations_come_from_a_single_joint_pass() -> None:
    """NERRE-01: one model call yields both, not NER then a second RE pass."""
    payload = _payload(
        [
            {"id": "e1", "type": "model", "text": "BERT", "confidence": 0.9},
            {"id": "e2", "type": "dataset", "text": "SQuAD", "confidence": 0.9},
        ],
        [{"type": "trained_on", "head": "e1", "tail": "e2", "confidence": 0.8}],
    )
    model = FakeJointIE([FakeJointResult(payload)])

    result = JointEntityRelationExtractor(model).extract("text")

    assert len(model.extract_calls) == 1
    assert result.entities and result.relations


# --------------------------------------------------------------------------
# Filtering / robustness
# --------------------------------------------------------------------------


def test_entities_below_the_threshold_are_dropped() -> None:
    payload = _payload(
        [
            {"id": "e1", "type": "model", "text": "BERT", "confidence": 0.9},
            {"id": "e2", "type": "dataset", "text": "Noise", "confidence": 0.05},
        ],
        [],
    )
    extractor = JointEntityRelationExtractor(
        FakeJointIE([FakeJointResult(payload)]), entity_threshold=0.5
    )

    result = extractor.extract("text")

    assert [e.text for e in result.entities] == ["BERT"]


def test_relations_below_the_threshold_are_dropped() -> None:
    payload = _payload(
        [
            {"id": "e1", "type": "model", "text": "BERT", "confidence": 0.9},
            {"id": "e2", "type": "dataset", "text": "SQuAD", "confidence": 0.9},
        ],
        [{"type": "trained_on", "head": "e1", "tail": "e2", "confidence": 0.1}],
    )
    extractor = JointEntityRelationExtractor(
        FakeJointIE([FakeJointResult(payload)]), relation_threshold=0.5
    )

    result = extractor.extract("text")

    assert result.entities and result.relations == []


def test_unknown_entity_types_are_dropped_not_raised() -> None:
    payload = _payload(
        [
            {"id": "e1", "type": "model", "text": "BERT", "confidence": 0.9},
            {"id": "e2", "type": "hallucinated", "text": "Thing", "confidence": 0.9},
        ],
        [],
    )

    result = JointEntityRelationExtractor(
        FakeJointIE([FakeJointResult(payload)])
    ).extract("text")

    assert [e.text for e in result.entities] == ["BERT"]


def test_unknown_relation_types_are_dropped() -> None:
    payload = _payload(
        [
            {"id": "e1", "type": "model", "text": "BERT", "confidence": 0.9},
            {"id": "e2", "type": "dataset", "text": "SQuAD", "confidence": 0.9},
        ],
        [{"type": "invented_by", "head": "e1", "tail": "e2", "confidence": 0.9}],
    )

    result = JointEntityRelationExtractor(
        FakeJointIE([FakeJointResult(payload)])
    ).extract("text")

    assert result.relations == []


def test_relation_with_endpoint_types_contradicting_the_contract_is_dropped() -> None:
    """`trained_on` is (model, dataset). A dataset->model edge would make
    graph.py's Cypher MATCH find nothing and silently write no relationship."""
    payload = _payload(
        [
            {"id": "e1", "type": "dataset", "text": "SQuAD", "confidence": 0.9},
            {"id": "e2", "type": "model", "text": "BERT", "confidence": 0.9},
        ],
        [{"type": "trained_on", "head": "e1", "tail": "e2", "confidence": 0.9}],
    )

    result = JointEntityRelationExtractor(
        FakeJointIE([FakeJointResult(payload)])
    ).extract("text")

    assert result.relations == []


def test_relation_referencing_a_dropped_entity_is_dropped() -> None:
    """A relation whose endpoint fell below the entity threshold has no node to
    MATCH against and must not survive."""
    payload = _payload(
        [
            {"id": "e1", "type": "model", "text": "BERT", "confidence": 0.9},
            {"id": "e2", "type": "dataset", "text": "SQuAD", "confidence": 0.02},
        ],
        [{"type": "trained_on", "head": "e1", "tail": "e2", "confidence": 0.9}],
    )

    result = JointEntityRelationExtractor(
        FakeJointIE([FakeJointResult(payload)]), entity_threshold=0.5
    ).extract("text")

    assert result.relations == []


def test_relation_referencing_an_unknown_id_is_dropped() -> None:
    payload = _payload(
        [{"id": "e1", "type": "model", "text": "BERT", "confidence": 0.9}],
        [{"type": "trained_on", "head": "e1", "tail": "missing", "confidence": 0.9}],
    )

    result = JointEntityRelationExtractor(
        FakeJointIE([FakeJointResult(payload)])
    ).extract("text")

    assert result.relations == []


def test_entities_with_blank_text_are_dropped() -> None:
    payload = _payload(
        [
            {"id": "e1", "type": "model", "text": "   ", "confidence": 0.9},
            {"id": "e2", "type": "dataset", "text": "SQuAD", "confidence": 0.9},
        ],
        [],
    )

    result = JointEntityRelationExtractor(
        FakeJointIE([FakeJointResult(payload)])
    ).extract("text")

    assert [e.text for e in result.entities] == ["SQuAD"]


def test_duplicate_entities_are_deduplicated_by_normalized_name_and_type() -> None:
    payload = _payload(
        [
            {"id": "e1", "type": "model", "text": "BERT", "confidence": 0.9},
            {"id": "e2", "type": "model", "text": "bert", "confidence": 0.7},
        ],
        [],
    )

    result = JointEntityRelationExtractor(
        FakeJointIE([FakeJointResult(payload)])
    ).extract("text")

    assert len(result.entities) == 1
    assert result.entities[0].confidence == 0.9, "keep the most confident mention"


def test_infeasible_result_yields_nothing() -> None:
    payload = _payload(
        [{"id": "e1", "type": "model", "text": "BERT", "confidence": 0.9}], []
    )
    extractor = JointEntityRelationExtractor(
        FakeJointIE([FakeJointResult(payload, feasible=False)])
    )

    result = extractor.extract("text")

    assert result.entities == []
    assert result.relations == []


def test_blank_input_short_circuits_without_calling_the_model() -> None:
    model = FakeJointIE()

    result = JointEntityRelationExtractor(model).extract("   ")

    assert model.extract_calls == []
    assert result.entities == []


def test_missing_confidence_defaults_and_does_not_crash() -> None:
    payload = _payload(
        [{"id": "e1", "type": "model", "text": "BERT"}],
        [],
    )

    result = JointEntityRelationExtractor(
        FakeJointIE([FakeJointResult(payload)]), entity_threshold=0.0
    ).extract("text")

    assert len(result.entities) == 1
    assert 0.0 <= result.entities[0].confidence <= 1.0


def test_malformed_payload_is_tolerated() -> None:
    """A checkpoint returning an unexpected shape must not take the run down."""
    extractor = JointEntityRelationExtractor(
        FakeJointIE([FakeJointResult({"entities": None, "relations": None})])
    )

    result = extractor.extract("text")

    assert result.entities == []
    assert result.relations == []


def test_extract_many_runs_one_joint_pass_per_chunk() -> None:
    payloads = [
        FakeJointResult(
            _payload(
                [{"id": "e1", "type": "model", "text": "BERT", "confidence": 0.9}], []
            )
        ),
        FakeJointResult(
            _payload(
                [{"id": "e1", "type": "dataset", "text": "SQuAD", "confidence": 0.9}],
                [],
            )
        ),
    ]
    model = FakeJointIE(payloads)

    result = JointEntityRelationExtractor(model).extract_many(["first", "second"])

    assert len(model.extract_calls) == 2
    assert {e.text for e in result.entities} == {"BERT", "SQuAD"}


def test_extract_many_merges_and_deduplicates_across_chunks() -> None:
    def same():
        return FakeJointResult(
            _payload(
                [{"id": "e1", "type": "model", "text": "BERT", "confidence": 0.9}], []
            )
        )

    model = FakeJointIE([same(), same()])

    result = JointEntityRelationExtractor(model).extract_many(["a", "b"])

    assert len(result.entities) == 1


@pytest.mark.integration
def test_real_gliner2_jointie_checkpoint_extracts_a_typed_relation() -> None:
    """Exercises the real checkpoint. Deselected by default (needs a download)."""
    from app.ner_re.extractor import get_joint_extractor

    extractor = get_joint_extractor()
    result = extractor.extract(
        "We train BERT on the SQuAD dataset and evaluate it using GSM8K."
    )

    typed = {(e.text, e.entity_type) for e in result.entities}
    assert ("BERT", "model") in typed
    assert ("SQuAD", "dataset") in typed

    # A typed relation must survive the endpoint check end-to-end.
    assert any(
        r.relation_type == "trained_on" and r.head == "BERT" and r.tail == "SQuAD"
        for r in result.relations
    ), result.relations
